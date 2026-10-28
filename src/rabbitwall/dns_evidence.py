"""Bounded DNS response evidence, independent of the resolving service."""
import json
import os
import threading
import time
import dns.flags
import dns.rcode
import dns.rdatatype
from dns_observations import OBSERVATIONS, MAX_RECORDS


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

