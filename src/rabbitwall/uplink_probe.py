#!/usr/bin/env python3
"""Unprivileged uplink quality probe: gateway ICMP, upstream DNS and public HTTPS reachability.

Every probe stays within the baseline output rules (ICMP to the gateway, DNS to
dns_upstream, TCP 443), so it works regardless of the AI gate state and needs no
firewall change. ICMP to public hosts and traceroute are not permitted by design.
"""
import argparse
from collections import deque
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time

PROBE_STATE = Path('/run/rabbitwall-probe/uplink.json')
INTERNET_TARGETS = ('1.1.1.1', '8.8.8.8')
DNS_NAME = 'example.com'
HISTORY_LIMIT = 120
ERROR_LIMIT = 50
PING_RE = re.compile(r'(\d+) packets transmitted, (\d+) (?:packets )?received')
RTT_RE = re.compile(r'(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)')


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def parse_ping(text):
    sent = PING_RE.search(text or '')
    rtt = RTT_RE.search(text or '')
    loss = None
    if sent and int(sent.group(1)):
        loss = round(100 * (1 - int(sent.group(2)) / int(sent.group(1))), 1)
    return {'avg_ms': float(rtt.group(2)) if rtt else None,
            'jitter_ms': float(rtt.group(4)) if rtt else None, 'loss_pct': loss}


def probe_gateway(address, count=4):
    try:
        proc = subprocess.run(['ping', '-4', '-n', '-q', '-c', str(count), '-i', '0.2', '-W', '1', address],
                              capture_output=True, text=True, timeout=count + 5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'target': address, 'avg_ms': None, 'jitter_ms': None, 'loss_pct': None, 'error': f'ping: {exc}'[:200]}
    result = dict(parse_ping(proc.stdout), target=address, error=None)
    if result['loss_pct'] is None:
        result['error'] = 'ping: ' + (proc.stderr.strip()[:200] or f'exit {proc.returncode}')
    elif result['loss_pct'] >= 100:
        result['error'] = 'ゲートウェイから応答がありません'
    return result


def probe_dns(server, name=DNS_NAME, timeout=2.0):
    import dns.exception, dns.message, dns.query, dns.rcode
    query = dns.message.make_query(name, 'A')
    started = time.monotonic()
    try:
        response = dns.query.udp(query, server, timeout=timeout)
    except (dns.exception.DNSException, OSError) as exc:
        return {'target': server, 'name': name, 'ms': None, 'error': f'DNS: {type(exc).__name__} {exc}'[:200]}
    elapsed = round((time.monotonic() - started) * 1000, 1)
    if response.rcode() != dns.rcode.NOERROR:
        return {'target': server, 'name': name, 'ms': elapsed, 'error': 'DNS: ' + dns.rcode.to_text(response.rcode())}
    return {'target': server, 'name': name, 'ms': elapsed, 'error': None}


def probe_tcp(address, port=443, timeout=2.0):
    started = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return {'target': address, 'port': port, 'ms': round((time.monotonic() - started) * 1000, 1), 'error': None}
    except OSError as exc:
        return {'target': address, 'port': port, 'ms': None, 'error': f'TCP {port}: {exc.strerror or exc}'[:200]}


def sample(config, targets=INTERNET_TARGETS):
    return {'time': utc_now(), 'gateway': probe_gateway(config['gateway']),
            'dns': probe_dns(config['dns_upstream']),
            'internet': [probe_tcp(address) for address in targets]}


class Recorder:
    """Bounded in-memory history; restarting the probe starts a new observation period."""

    def __init__(self, interval):
        self.interval = interval
        self.since = utc_now()
        self.history = deque(maxlen=HISTORY_LIMIT)
        self.errors = deque(maxlen=ERROR_LIMIT)

    def record(self, item):
        self.history.append(item)
        for kind, result in [('gateway', item['gateway']), ('dns', item['dns']),
                             *[('internet', result) for result in item['internet']]]:
            if result.get('error'):
                self.errors.appendleft({'time': item['time'], 'kind': kind,
                                        'target': result['target'], 'error': result['error']})

    def document(self):
        return {'updated_at': self.history[-1]['time'] if self.history else None, 'since': self.since,
                'interval': self.interval, 'history': list(self.history), 'errors': list(self.errors)}


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.uplink-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output, ensure_ascii=False)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_uplink(path=PROBE_STATE):
    try:
        if path.stat().st_size > 2_000_000:
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and isinstance(data.get('history'), list) else None
    except (OSError, ValueError):
        return None


def demo_uplink(config, interval=30, targets=INTERNET_TARGETS):
    """Deterministic one-hour sample set for the demo console; no packets are sent."""
    recorder = Recorder(interval)
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    for index in range(HISTORY_LIMIT):
        stamp = (end - dt.timedelta(seconds=interval * (HISTORY_LIMIT - 1 - index))).isoformat()
        wave = (1 + math.sin(index / 9)) / 2 * 0.7 + (index * 7 % 5) / 5 * 0.3
        gateway_lost = index in (37, 38)
        dns_failed = index == 91
        recorder.record({'time': stamp,
            'gateway': {'target': config['gateway'], 'avg_ms': None if gateway_lost else round(0.6 + wave, 2),
                        'jitter_ms': None if gateway_lost else round(0.1 + wave / 4, 2),
                        'loss_pct': 100.0 if gateway_lost else 0.0,
                        'error': 'ゲートウェイから応答がありません' if gateway_lost else None},
            'dns': {'target': config['dns_upstream'], 'name': DNS_NAME, 'ms': None if dns_failed else round(4 + wave * 9, 1),
                    'error': 'DNS: Timeout The DNS operation timed out.' if dns_failed else None},
            'internet': [{'target': address, 'port': 443, 'ms': round(9 + offset * 6 + wave * 5, 1), 'error': None}
                         for offset, address in enumerate(targets)]})
    recorder.since = recorder.history[0]['time']
    return recorder.document()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--interval', type=int, default=30)
    args = parser.parse_args()
    if args.interval < 10:
        parser.error('interval >= 10 required')
    from settings import require_config, load
    require_config()
    config = load()
    os.umask(0o027)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    recorder = Recorder(args.interval)
    try:
        while not stopping.is_set():
            recorder.record(sample(config))
            try:
                atomic_json(PROBE_STATE, recorder.document())
            except OSError as error:
                print(f'Uplink state unavailable: {error}', flush=True)
            stopping.wait(args.interval)
    finally:
        PROBE_STATE.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
