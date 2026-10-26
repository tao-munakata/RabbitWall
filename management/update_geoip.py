#!/usr/bin/env python3
"""Download DB-IP Country Lite and build an offline IPv4 lookup database."""
import argparse
import csv
import datetime as dt
import gzip
import ipaddress
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import urllib.request

DEFAULT_OUTPUT = Path("/var/lib/taco-console/geoip-country.sqlite3")


def build_database(source, output, edition):
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".geoip-", dir=output.parent)
    os.close(descriptor)
    database = None
    try:
        database = sqlite3.connect(temporary)
        database.execute("CREATE TABLE ipv4 (start INTEGER PRIMARY KEY, end INTEGER NOT NULL, country TEXT NOT NULL)")
        database.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        count, previous_end, size = 0, -1, 0
        with gzip.open(source, "rt", encoding="utf-8", newline="") as stream:
            for row in csv.reader(stream):
                size += sum(len(field) for field in row)
                if size > 128 * 1024 * 1024:
                    raise ValueError("database export is too large")
                if len(row) != 3 or not re.fullmatch(r"[A-Z]{2}", row[2]):
                    raise ValueError("invalid country CSV row")
                start, end = ipaddress.ip_address(row[0]), ipaddress.ip_address(row[1])
                if start.version != end.version or int(start) > int(end):
                    raise ValueError("invalid IP range")
                if start.version != 4:
                    continue
                if int(start) <= previous_end:
                    raise ValueError("overlapping or unordered IPv4 ranges")
                database.execute("INSERT INTO ipv4 VALUES (?,?,?)", (int(start), int(end), row[2]))
                previous_end = int(end)
                count += 1
        if not count:
            raise ValueError("no IPv4 records")
        metadata = {"source": "DB-IP Lite (CC BY 4.0)", "edition": edition,
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "records": str(count), "attribution_url": "https://db-ip.com"}
        database.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
        database.commit()
        database.close()
        database = None
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
        return count
    finally:
        if database:
            database.close()
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        parser.error("month must be YYYY-MM")
    url = f"https://download.db-ip.com/free/dbip-country-lite-{args.month}.csv.gz"
    try:
        with tempfile.TemporaryDirectory() as directory:
            compressed = Path(directory) / "country.csv.gz"
            request = urllib.request.Request(url, headers={"User-Agent": "taco-network-console/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response, compressed.open("wb") as output:
                total = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        raise ValueError("download is too large")
                    output.write(chunk)
            count = build_database(compressed, args.output, args.month)
        print(f"国別IPデータを更新しました：{args.month} / IPv4 {count:,}件")
        print(f"保存先：{args.output.resolve()}")
        print("IP Geolocation by DB-IP — https://db-ip.com — CC BY 4.0")
    except (OSError, ValueError, sqlite3.Error, csv.Error, EOFError) as exc:
        parser.exit(1, f"更新失敗（既存データは維持）：{exc}\n")


if __name__ == "__main__":
    main()
