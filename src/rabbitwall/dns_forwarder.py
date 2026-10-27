#!/usr/bin/env python3
"""AI-only UDP/TCP DNS relay. Kernel gate controls reception and upstream traffic."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import struct
import threading
import time
from dependencies import require_dns
require_dns()
import dns.exception
import dns.flags
import dns.message
import dns.opcode
import dns.query
import dns.rcode
import dns.rdatatype
from dns_observations import OBSERVATIONS, MAX_RECORDS
from settings import SETTINGS, AI_INTERFACE, AI_IP


class Evidence:
    def __init__(self):
        self.records = {}
        self.lock = threading.Lock()

    def observe(self, client, response, now=None):
        now = time.time() if now is None else now
        if response.rcode() != dns.rcode.NOERROR or response.flags & dns.flags.TC:
            return
        question = response.question[0]
        if question.rdtype not in (dns.rdatatype.A, dns.rdatatype.AAAA):
            return
        name, ttl = question.name, 3600
        # Only follow the queried answer chain. Ignore unrelated additional/glue addresses.
        for _ in range(16):
            alias = next((rr for rr in response.answer if rr.name == name and rr.rdtype == dns.rdatatype.CNAME), None)
            if alias is None:
                break
            ttl = min(ttl, alias.ttl)
            name = alias[0].target
        found = []
        for rr in response.answer:
            if rr.name == name and rr.rdtype == question.rdtype:
                lifetime = min(ttl, rr.ttl)
                if lifetime <= 0:
                    continue
                for entry in list(rr)[:32]:
                    found.append({'client': client, 'address': entry.address,
                                  'domain': question.name.to_text().rstrip('.').lower(),
                                  'observed_at': now, 'expires_at': now + lifetime})
        with self.lock:
            self.records = {key: value for key, value in self.records.items() if value['expires_at'] > now}
            for item in found:
                key = (client, item['address'], item['domain'])
                self.records.pop(key, None)
                self.records[key] = item
            while len(self.records) > MAX_RECORDS:
                del self.records[next(iter(self.records))]

    def publish(self, path=OBSERVATIONS):
        with self.lock:
            now = time.time()
            self.records = {key: value for key, value in self.records.items() if value['expires_at'] > now}
            payload = {'updated_at': now, 'records': list(self.records.values())}
        # /run is tmpfs: no per-query journal or eMMC writes.
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload))
        temporary.chmod(0o640)
        os.replace(temporary, path)


class Relay:
    def __init__(self, upstream=SETTINGS['dns_upstream'], source=AI_IP, port=53):
        self.upstream, self.source, self.port = upstream, source, port
        self.evidence = Evidence()

    def answer(self, wire, client, tcp=False):
        request = dns.message.from_wire(wire)
        if request.flags & dns.flags.QR:
            return None
        if (request.opcode() != dns.opcode.QUERY or len(request.question) != 1
                or request.question[0].rdtype in (dns.rdatatype.AXFR, dns.rdatatype.IXFR)
                or request.question[0].rdclass != 1 or request.had_tsig):
            response = dns.message.make_response(request)
            response.set_rcode(dns.rcode.REFUSED)
        else:
            query = copy.deepcopy(request)
            query.id = secrets.randbelow(65536)
            try:
                options = dict(timeout=2, port=self.port, source=self.source)
                if tcp:
                    response = dns.query.tcp(query, self.upstream, **options)
                else:
                    response = dns.query.udp(query, self.upstream, ignore_errors=True, ignore_unexpected=True, **options)
                    if response.flags & dns.flags.TC:
                        response = dns.query.tcp(query, self.upstream, **options)
                if not query.is_response(response):
                    raise ValueError('mismatched upstream response')
                self.evidence.observe(client, response)
                response.id = request.id
            except (OSError, dns.exception.DNSException, ValueError):
                response = dns.message.make_response(request)
                response.set_rcode(dns.rcode.SERVFAIL)
        limit = 65535 if tcp else max(512, min(request.payload if request.edns >= 0 else 512, 1232))
        try:
            return response.to_wire(max_size=limit)
        except dns.exception.TooBig:
            # Force a TCP retry instead of an oversized UDP packet or partial RRset.
            response = copy.deepcopy(response)
            response.answer, response.authority, response.additional = [], [], []
            response.flags |= dns.flags.TC
            return response.to_wire(max_size=limit)


class BoundedServer(socketserver.ThreadingMixIn):
    daemon_threads = True
    allow_reuse_address = True
    slots = threading.BoundedSemaphore(32)
    allowed = AI_INTERFACE.network

    def verify_request(self, request, address):
        return ipaddress.ip_address(address[0]) in self.allowed

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Malformed or disconnected clients must not flood the journal.
        pass


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            wire, sock = self.request
            response = self.server.relay.answer(wire, self.client_address[0])
            if response:
                sock.sendto(response, self.client_address)
        except (ValueError, OSError, dns.exception.DNSException):
            pass


class TCPHandler(socketserver.BaseRequestHandler):
    def read_exact(self, count):
        data = bytearray()
        while len(data) < count:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("DNS TCP connection deadline exceeded")
            self.request.settimeout(min(3, remaining))
            part = self.request.recv(count - len(data))
            if not part:
                raise EOFError
            data.extend(part)
        return bytes(data)

    def handle(self):
        self.deadline = time.monotonic() + 10
        self.request.settimeout(3)
        # Bounded persistent DNS TCP connections; kernel gate applies to every packet.
        for _ in range(16):
            try:
                size, = struct.unpack('!H', self.read_exact(2))
                response = self.server.relay.answer(self.read_exact(size), self.client_address[0], tcp=True)
                if response:
                    self.request.sendall(struct.pack('!H', len(response)) + response)
            except (EOFError, ValueError, OSError, dns.exception.DNSException):
                return


class UDPServer(BoundedServer, socketserver.UDPServer):
    pass


class TCPServer(BoundedServer, socketserver.TCPServer):
    request_queue_size = 32


def main():
    from settings import require_config
    require_config()
    relay = Relay()
    with UDPServer((AI_IP, 53), UDPHandler) as udp, TCPServer((AI_IP, 53), TCPHandler) as tcp:
        udp.relay = tcp.relay = relay
        for server in (udp, tcp):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        while True:
            relay.evidence.publish()
            time.sleep(2)


if __name__ == '__main__':
    main()
