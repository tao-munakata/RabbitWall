import test_environment
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/rabbitwall'))
import dns_evidence as forwarder
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
            self.addCleanup(db.close)
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


if __name__ == '__main__':
    unittest.main()
