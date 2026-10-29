import test_environment
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/rabbitwall'))
sys.path.insert(0, str(ROOT/'tools'))
import uplink_probe as probe
import console
import deploy

LINUX_PING = '''PING 192.168.10.1 (192.168.10.1) 56(84) bytes of data.

--- 192.168.10.1 ping statistics ---
4 packets transmitted, 3 received, 25% packet loss, time 603ms
rtt min/avg/max/mdev = 0.412/0.501/0.611/0.070 ms
'''

def item(error=None, dns_error=None, tcp_error=None, time='2026-10-30T00:00:00+00:00'):
    return {'time': time,
            'gateway': {'target': '192.168.10.1', 'avg_ms': None if error else 0.5, 'jitter_ms': None, 'loss_pct': 100.0 if error else 0.0, 'error': error},
            'dns': {'target': '192.168.10.1', 'name': 'example.com', 'ms': None if dns_error else 4.0, 'error': dns_error},
            'internet': [{'target': '1.1.1.1', 'port': 443, 'ms': None if tcp_error else 9.0, 'error': tcp_error}]}

class Parsing(unittest.TestCase):
    def test_linux_ping_summary(self):
        self.assertEqual(probe.parse_ping(LINUX_PING), {'avg_ms': 0.501, 'jitter_ms': 0.07, 'loss_pct': 25.0})

    def test_total_loss_has_no_rtt(self):
        result = probe.parse_ping('4 packets transmitted, 0 received, 100% packet loss, time 3060ms\n')
        self.assertEqual(result, {'avg_ms': None, 'jitter_ms': None, 'loss_pct': 100.0})

    def test_missing_ping_is_an_error_not_a_crash(self):
        with patch.object(probe.subprocess, 'run', side_effect=FileNotFoundError('ping')):
            self.assertIn('ping', probe.probe_gateway('192.168.10.1')['error'])
        done = subprocess.CompletedProcess([], 2, '', 'ping: socket: Operation not permitted')
        with patch.object(probe.subprocess, 'run', return_value=done):
            self.assertIn('Operation not permitted', probe.probe_gateway('192.168.10.1')['error'])

class Probes(unittest.TestCase):
    def test_tcp_connect_time_and_refusal(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            port = listener.getsockname()[1]
            ok = probe.probe_tcp('127.0.0.1', port)
        self.assertIsNone(ok['error']); self.assertGreaterEqual(ok['ms'], 0)
        refused = probe.probe_tcp('127.0.0.1', port, timeout=1)
        self.assertIsNone(refused['ms']); self.assertTrue(refused['error'].startswith('TCP'))

    def test_dns_timeout_is_reported(self):
        import dns.exception
        with patch('dns.query.udp', side_effect=dns.exception.Timeout()):
            result = probe.probe_dns('192.168.10.1')
        self.assertIsNone(result['ms']); self.assertIn('Timeout', result['error'])

class Recording(unittest.TestCase):
    def test_history_and_errors_are_bounded_newest_error_first(self):
        recorder = probe.Recorder(30)
        for index in range(probe.HISTORY_LIMIT + 5):
            recorder.record(item(error='lost', time=f'2026-10-30T00:00:{index % 60:02d}+00:00'))
        document = recorder.document()
        self.assertEqual(len(document['history']), probe.HISTORY_LIMIT)
        self.assertEqual(len(document['errors']), probe.ERROR_LIMIT)
        self.assertEqual(document['errors'][0]['time'], document['history'][-1]['time'])

    def test_each_failed_target_is_logged(self):
        recorder = probe.Recorder(30)
        recorder.record(item(dns_error='DNS: SERVFAIL', tcp_error='TCP 443: refused'))
        self.assertEqual([e['kind'] for e in recorder.document()['errors']], ['internet', 'dns'])

    def test_state_file_round_trip_and_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'uplink.json'
            self.assertIsNone(probe.load_uplink(path))
            recorder = probe.Recorder(30); recorder.record(item())
            probe.atomic_json(path, recorder.document())
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(probe.load_uplink(path)['history'][0]['dns']['ms'], 4.0)
            path.write_text(json.dumps({'history': 'x'}))
            self.assertIsNone(probe.load_uplink(path))

class Integration(unittest.TestCase):
    def test_demo_console_has_an_hour_of_uplink_samples(self):
        uplink = console.demo_state()['uplink']
        self.assertEqual(len(uplink['history']), probe.HISTORY_LIMIT)
        self.assertEqual({e['kind'] for e in uplink['errors']}, {'gateway', 'dns'})

    def test_probe_service_is_deployed_unprivileged(self):
        files = deploy.deployment_files()
        self.assertIn(Path('/opt/rabbitwall/uplink_probe.py'), files)
        self.assertIn(Path('/etc/systemd/system/rabbitwall-collector.service.d/probe.conf'), files)
        self.assertIn('rabbitwall-probe', deploy.SERVICES)
        unit = (ROOT/'systemd/rabbitwall-probe.service').read_text()
        self.assertIn('User=rabbitwall-probe', unit)
        self.assertIn('CapabilityBoundingSet=\n', unit)
        self.assertNotIn('AF_NETLINK', unit)

    def test_release_allowlist_lists_new_files(self):
        listed = set((ROOT/'tools/release-files.txt').read_text().split())
        for name in ('src/rabbitwall/uplink_probe.py', 'systemd/rabbitwall-probe.service',
                     'systemd/rabbitwall-collector.service.d/probe.conf', 'tests/test_uplink.py'):
            self.assertIn(name, listed)

if __name__ == '__main__':
    unittest.main()
