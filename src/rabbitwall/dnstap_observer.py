#!/usr/bin/env python3
"""Receive bounded local Frame Streams/dnstap events; never proxy DNS traffic."""
import ipaddress
import os
from pathlib import Path
import signal
import socket
import socketserver
import struct
import threading
import time
import dns.exception
import dns.flags
import dns.message
from google.protobuf.message import DecodeError
import dnstap_pb2
from dependencies import require_observer
from dns_evidence import Evidence
from dns_observations import OBSERVATIONS
from settings import AI_INTERFACE, AI_IP
from unbound_config import DNSTAP_SOCKET

CONTENT_TYPE = b'protobuf:dnstap.Dnstap'
MAX_FRAME = 131072
MAX_CONTROL = 4096
ACCEPT, START, STOP, READY, FINISH = 1, 2, 3, 4, 5


def control_frame(kind, content=False):
    body = struct.pack('!I', kind)
    if content:
        body += struct.pack('!II', 1, len(CONTENT_TYPE)) + CONTENT_TYPE
    return struct.pack('!II', 0, len(body)) + body


def parse_control(body):
    if len(body) < 4:
        raise ValueError('Short control frame')
    kind, = struct.unpack('!I', body[:4])
    types, offset = [], 4
    while offset < len(body):
        if len(body) - offset < 8:
            raise ValueError('Short control field')
        field, length = struct.unpack('!II', body[offset:offset+8])
        offset += 8
        if length > len(body) - offset or field != 1:
            raise ValueError('Invalid control field')
        types.append(body[offset:offset+length])
        offset += length
    return kind, types


def ingest(payload, evidence, now=None):
    """Accept only Unbound client responses for this AI subnet and DNS endpoint."""
    now = time.time() if now is None else now
    envelope = dnstap_pb2.Dnstap()
    envelope.ParseFromString(payload)
    if (not envelope.IsInitialized() or envelope.type != dnstap_pb2.Dnstap.MESSAGE
            or not envelope.HasField('message')):
        return
    message = envelope.message
    if (message.type != dnstap_pb2.Message.CLIENT_RESPONSE
            or message.socket_family != dnstap_pb2.INET
            or not message.HasField('response_time_sec')
            or not message.HasField('response_message')):
        return
    client = ipaddress.ip_address(message.query_address)
    if client not in AI_INTERFACE.network:
        return
    # Older supported Unbound builds omit these optional responder fields.
    # The private Unix socket identifies the producer; validate fields when present.
    if message.HasField('response_port') and message.response_port != 53:
        return
    if message.HasField('response_address') and str(ipaddress.ip_address(message.response_address)) != AI_IP:
        return
    timestamp = message.response_time_sec + message.response_time_nsec / 1e9
    if message.response_time_nsec >= 1_000_000_000 or not now - 3600 <= timestamp <= now:
        return
    response = dns.message.from_wire(message.response_message)
    if not response.flags & dns.flags.QR or len(response.question) != 1 or response.question[0].rdclass != 1:
        return
    evidence.observe(str(client), response, now=timestamp)


class FrameSession:
    def __init__(self, connection, evidence, stopping):
        self.connection, self.evidence, self.stopping = connection, evidence, stopping
        self.connection.settimeout(1)

    def read_exact(self, size, idle=False):
        data = bytearray()
        deadline = None if idle else time.monotonic() + 10
        while len(data) < size:
            if self.stopping.is_set():
                raise EOFError
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('Frame read deadline exceeded')
            try:
                part = self.connection.recv(size - len(data))
            except socket.timeout:
                continue
            if not part:
                raise EOFError
            data.extend(part)
            if deadline is None:
                deadline = time.monotonic() + 10
        return bytes(data)

    def run(self):
        state = 'ready'
        while not self.stopping.is_set():
            length, = struct.unpack('!I', self.read_exact(4, idle=True))
            if length:
                if state != 'data' or length > MAX_FRAME:
                    raise ValueError('Unexpected or oversized data frame')
                payload = self.read_exact(length)
                try:
                    ingest(payload, self.evidence)
                except (ValueError, DecodeError, dns.exception.DNSException):
                    pass  # Invalid events must not kill the receiver or pollute the journal.
                continue
            length, = struct.unpack('!I', self.read_exact(4))
            if not 4 <= length <= MAX_CONTROL:
                raise ValueError('Invalid control frame size')
            kind, types = parse_control(self.read_exact(length))
            if state == 'ready' and kind == READY and CONTENT_TYPE in types:
                self.connection.sendall(control_frame(ACCEPT, content=True))
                state = 'start'
            elif state == 'start' and kind == START and types == [CONTENT_TYPE]:
                state = 'data'
            elif state == 'data' and kind == STOP and not types:
                self.connection.sendall(control_frame(FINISH))
                return
            else:
                raise ValueError('Invalid Frame Streams handshake')


class ObserverServer(socketserver.UnixStreamServer):
    """One local producer connection; no DNS listener or forwarding capability."""
    timeout = 1

    def __init__(self, path, evidence, stopping):
        self.evidence, self.stopping = evidence, stopping
        super().__init__(str(path), ObserverHandler)

    def handle_error(self, request, client_address):
        pass


class ObserverHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            FrameSession(self.request, self.server.evidence, self.server.stopping).run()
        except (OSError, EOFError, ValueError):
            pass


def publish_loop(evidence, stopping, path=OBSERVATIONS):
    last_error = 0
    while not stopping.is_set():
        try:
            evidence.publish(path)
        except OSError as error:
            if time.monotonic() - last_error > 30:
                print(f'DNS evidence unavailable: {error}', flush=True)
                last_error = time.monotonic()
        stopping.wait(2)


def main():
    require_observer()
    from settings import require_config
    require_config()
    os.umask(0o027)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    DNSTAP_SOCKET.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    DNSTAP_SOCKET.unlink(missing_ok=True)
    # Never republish events from a previous observer instance.
    evidence = Evidence()
    publisher = threading.Thread(target=publish_loop, args=(evidence, stopping), daemon=True)
    with ObserverServer(DNSTAP_SOCKET, evidence, stopping) as server:
        DNSTAP_SOCKET.chmod(0o600)
        publisher.start()
        try:
            while not stopping.is_set():
                server.handle_request()
        finally:
            stopping.set()
            publisher.join(timeout=3)
            DNSTAP_SOCKET.unlink(missing_ok=True)
            OBSERVATIONS.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
