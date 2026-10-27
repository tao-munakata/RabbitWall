"""Bounded, short-lived DNS evidence; this is a hint, never an HTTP host claim."""
import json
from pathlib import Path
import time

OBSERVATIONS = Path('/run/taco-console-dns/observations.json')
MAX_RECORDS = 10000


def load_observations(path=OBSERVATIONS):
    try:
        if path.stat().st_size > 8_000_000:
            return []
        data = json.loads(path.read_text())
        return data.get('records', [])[:MAX_RECORDS]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def domain_hints(records, client, address, now=None):
    now = time.time() if now is None else now
    candidates = {}
    for item in records:
        try:
            if (item['client'] == client and item['address'] == address
                    and item['observed_at'] <= now < item['expires_at']):
                name = item['domain']
                if isinstance(name, str) and len(name) <= 254:
                    candidates[name] = item
        except (KeyError, TypeError):
            continue
    return sorted(candidates.values(), key=lambda item: item['observed_at'], reverse=True)[:8]
