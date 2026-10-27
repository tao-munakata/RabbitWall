import datetime as dt
import ipaddress
import json
from pathlib import Path
import socketserver
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import dns.flags
import dns.message
import dns.rcode
import dns.rrset

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'taco-console'))
import dns_forwarder as forwarder
from dns_observations import domain_hints, load_observations
import console


def answer(query):
    response = dns.message.make_response(query)
    response.answer = [dns.rrset.from_text('app.example.', 30, 'IN', 'CNAME', 'edge.example.'),
                       dns.rrset.from_text('edge.example.', 120, 'IN', 'A', '1.1.1.1'),
                       dns.rrset.from_text('unrelated.example.', 300, 'IN', 'A', '9.9.9.9')]
    return response


class DNS(unittest.TestCase):
    def test_cname_minimum_ttl_client_isolation_and_no_glue(self):
        evidence = forwarder.Evidence()
        evidence.observe('10.1.0.10', answer(dns.message.make_query('app.example', 'A')), now=100)
        records = list(evidence.records.values())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['expires_at'], 130)
        self.assertEqual(domain_hints(records, '10.1.0.10', '1.1.1.1', 101)[0]['domain'], 'app.example')
        self.assertFalse(domain_hints(records, '10.1.0.11', '1.1.1.1', 101))
        self.assertFalse(domain_hints(records, '10.1.0.10', '1.1.1.1', 130))
        self.assertFalse(domain_hints(records, '10.1.0.10', '1.1.1.1', 99))
        self.assertFalse(domain_hints(records, '10.1.0.10', '9.9.9.9', 101))

    def test_zero_ttl_truncated_and_negative_not_recorded(self):
        evidence = forwarder.Evidence()
        query = dns.message.make_query('app.example', 'A')
        for mode in ('zero', 'tc', 'negative'):
            response = answer(query)
            if mode == 'zero': response.answer[0].ttl = 0
            if mode == 'tc': response.flags |= dns.flags.TC
            if mode == 'negative': response.set_rcode(dns.rcode.NXDOMAIN)
            evidence.observe('10.1.0.10', response)
        self.assertFalse(evidence.records)

    def test_bound_snapshot_and_history_survive_ttl_without_relabeling(self):
        evidence = forwarder.Evidence()
        now = dt.datetime.now(dt.timezone.utc)
        evidence.observe('10.1.0.10', answer(dns.message.make_query('app.example', 'A')), now=now.timestamp()-1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'observations.json'
            evidence.publish(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            records = load_observations(path)
            db = sqlite3.connect(':memory:')
            flow = console.demo_state()['flows'][0]
            flow.pop('domain_hints', None)
            console.update_history(db, [flow], now.isoformat(), dns_records=records)
            self.assertEqual(flow['domain_hints'][0]['domain'], 'app.example')
            console.update_history(db, [flow], (now+dt.timedelta(seconds=50)).isoformat(), dns_records=[])
            self.assertEqual(flow['domain_hints'][0]['domain'], 'app.example')
            # A newly observed interval must not inherit evidence from a finished connection.
            console.update_history(db, [], (now+dt.timedelta(seconds=51)).isoformat())
            console.update_history(db, [flow], (now+dt.timedelta(seconds=52)).isoformat(), dns_records=[])
            self.assertEqual(flow['domain_hints'], [])
        with patch.object(forwarder, 'MAX_RECORDS', 2):
            for client in ['10.1.0.11', '10.1.0.12', '10.1.0.13']:
                evidence.observe(client, answer(dns.message.make_query('app.example', 'A')))
            self.assertEqual(len(evidence.records), 2)

    def test_transport_fallback_error_and_transfer_refusal(self):
        relay = forwarder.Relay()
        query = dns.message.make_query('app.example', 'A')
        truncated = dns.message.make_response(query); truncated.flags |= dns.flags.TC
        def udp(q, *args, **kwargs):
            response = dns.message.make_response(q); response.flags |= dns.flags.TC
            return response
        with patch.object(forwarder.dns.query, 'udp', side_effect=udp), patch.object(forwarder.dns.query, 'tcp', side_effect=lambda q,*a,**k: answer(q)) as tcp:
            result = dns.message.from_wire(relay.answer(query.to_wire(), '10.1.0.10'))
            self.assertEqual(result.id, query.id)
            self.assertEqual(result.rcode(), dns.rcode.NOERROR)
            self.assertEqual(tcp.call_args.kwargs['source'], '10.1.0.1')
        with patch.object(forwarder.dns.query, 'udp', side_effect=OSError):
            result = dns.message.from_wire(relay.answer(query.to_wire(), '10.1.0.10'))
            self.assertEqual(result.rcode(), dns.rcode.SERVFAIL)
        with patch.object(forwarder.dns.query, 'udp') as upstream:
            result = dns.message.from_wire(relay.answer(dns.message.make_query('example', 'AXFR').to_wire(), '10.1.0.10'))
            self.assertEqual(result.rcode(), dns.rcode.REFUSED)
            upstream.assert_not_called()

    def test_udp_size_limit_and_mismatched_answers(self):
        relay = forwarder.Relay()
        query = dns.message.make_query('app.example', 'A')
        def oversized(q, *args, **kwargs):
            response = dns.message.make_response(q)
            response.answer = [dns.rrset.from_text('app.example.', 60, 'IN', 'TXT',
                                                   *['"' + str(i) + 'x'*200 + '"' for i in range(8)])]
            return response
        with patch.object(forwarder.dns.query, 'udp', side_effect=oversized):
            wire = relay.answer(query.to_wire(), '10.1.0.10')
            self.assertLessEqual(len(wire), 512)
            self.assertTrue(dns.message.from_wire(wire).flags & dns.flags.TC)
        with patch.object(forwarder.dns.query, 'udp', return_value=answer(dns.message.make_query('wrong.example', 'A'))):
            response = dns.message.from_wire(relay.answer(query.to_wire(), '10.1.0.10'))
            self.assertEqual(response.rcode(), dns.rcode.SERVFAIL)
            self.assertFalse(relay.evidence.records)

    def test_actual_udp_and_tcp_relay(self):
        class UpUDP(socketserver.BaseRequestHandler):
            def handle(self):
                query = dns.message.from_wire(self.request[0])
                self.request[1].sendto(answer(query).to_wire(), self.client_address)
        class UpTCP(socketserver.BaseRequestHandler):
            def handle(self):
                import struct
                size = struct.unpack('!H', self.request.recv(2))[0]
                wire = b''
                while len(wire) < size: wire += self.request.recv(size-len(wire))
                result = answer(dns.message.from_wire(wire)).to_wire()
                self.request.sendall(struct.pack('!H', len(result))+result)
        servers = []
        try:
            up = socketserver.TCPServer(('127.0.0.1', 0), UpTCP); servers.append(up)
            udp = socketserver.UDPServer(up.server_address, UpUDP); servers.append(udp)
            relay = forwarder.Relay('127.0.0.1', '127.0.0.1', up.server_address[1])
            tcp = forwarder.TCPServer(('127.0.0.1', 0), forwarder.TCPHandler); servers.append(tcp)
            down = forwarder.UDPServer(tcp.server_address, forwarder.UDPHandler); servers.append(down)
            for server in (tcp, down):
                server.allowed = ipaddress.ip_network('127.0.0.0/8')
                server.relay = relay
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            query = dns.message.make_query('app.example', 'A')
            for query_fn in (forwarder.dns.query.udp, forwarder.dns.query.tcp):
                result = query_fn(query, '127.0.0.1', port=tcp.server_address[1], timeout=2)
                self.assertEqual(result.id, query.id)
                self.assertEqual(result.answer[1][0].address, '1.1.1.1')
            self.assertTrue(domain_hints(list(relay.evidence.records.values()), '127.0.0.1', '1.1.1.1'))
        finally:
            for server in servers:
                server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main()
