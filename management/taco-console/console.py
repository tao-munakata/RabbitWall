#!/usr/bin/env python3
"""TACO connection monitor and authenticated fixed-purpose internet control."""
import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from control import TOKEN, request_control

AI = ipaddress.ip_network("10.1.0.0/24")
TACO_IPS = {"10.2.0.2", "10.1.0.1"}
STATIC = Path(__file__).resolve().parent / "static"
SERVICES = {("tcp", 443): "HTTPS", ("tcp", 80): "HTTP", ("tcp", 22): "SSH",
            ("tcp", 53): "DNS", ("udp", 53): "DNS", ("udp", 123): "NTP",
            ("tcp", 445): "SMB"}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def scope(src):
    if src in TACO_IPS:
        return "TACO"
    try:
        if ipaddress.ip_address(src) in AI:
            return "AI"
    except ValueError:
        pass
    return None


def parse_conntrack(text):
    """Keep the original tuple, never confuse it with the reply tuple."""
    result = []
    for line in text.splitlines():
        head = re.match(r"(?:ipv4\s+\d+\s+)?(\w+)\s+\d+\s+\d+\s+(.*)", line)
        if not head:
            continue
        proto, tail = head.groups()
        pairs = re.findall(r"(\w+)=([^\s]+)", tail)
        original, reply = {}, {}
        target = original
        for key, value in pairs:
            if key == "src" and "src" in original:
                target = reply
            target.setdefault(key, value)
        if not scope(original.get("src", "")) or "dst" not in original:
            continue
        def number(mapping, key):
            value = mapping.get(key)
            return int(value) if value and value.isdigit() else None
        state = tail.split()[0] if tail and "=" not in tail.split()[0] else "TRACKED"
        if "[UNREPLIED]" in tail:
            state = "UNREPLIED"
        elif proto != "tcp" and "[ASSURED]" in tail:
            state = "ASSURED"
        sport, dport = number(original, "sport"), number(original, "dport")
        identity = [proto, original["src"], original["dst"], sport, dport,
                    original.get("type"), original.get("code"), original.get("id"),
                    # conntrack id separates re-used tuples where supported.
                    re.findall(r"\bid=(\d+)", tail)[-1:] ]
        result.append({"key": hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24],
                       "scope": scope(original["src"]), "proto": proto.upper(),
                       "src": original["src"], "dst": original["dst"],
                       "sport": sport, "dport": dport, "state": state,
                       "service": SERVICES.get((proto, dport), proto.upper()),
                       "tx_bytes": number(original, "bytes"),
                       "rx_bytes": number(reply, "bytes")})
    return result


def parse_journal(text):
    result = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            message = str(entry.get("MESSAGE", ""))
            match = re.search(r"taco-(input|forward|output)-drop", message)
            fields = dict(re.findall(r"\b([A-Z]+)=([^\s]*)", message))
            if not match or not scope(fields.get("SRC", "")):
                continue
            result.append({"time": dt.datetime.fromtimestamp(
                int(entry["__REALTIME_TIMESTAMP"]) / 1e6, dt.timezone.utc).isoformat(),
                "scope": scope(fields["SRC"]), "chain": match.group(1),
                "src": fields["SRC"], "dst": fields.get("DST", ""),
                "proto": fields.get("PROTO", ""), "sport": fields.get("SPT", ""),
                "dport": fields.get("DPT", ""), "in": fields.get("IN", ""),
                "out": fields.get("OUT", "")})
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return sorted(result, key=lambda item: item["time"], reverse=True)


def command(args, empty_ok=False):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        empty_match = empty_ok and proc.returncode == 1 and not proc.stdout.strip() and not proc.stderr.strip()
        if proc.returncode and not empty_match:
            return "", f"{args[0]}: {proc.stderr.strip()[:400] or 'exit ' + str(proc.returncode)}"
        return proc.stdout, None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"{args[0]}: {exc}"


def update_history(db, flows, now, retention_hours=24, row_limit=20000):
    """Poll observations, not a claim of complete connection-event capture."""
    db.execute("CREATE TABLE IF NOT EXISTS flows (key TEXT PRIMARY KEY, first_seen TEXT, "
               "last_seen TEXT, ended_at TEXT, data TEXT)")
    keys = {flow["key"] for flow in flows}
    for key, in db.execute("SELECT key FROM flows WHERE ended_at IS NULL").fetchall():
        if key not in keys:
            db.execute("UPDATE flows SET ended_at=? WHERE key=?", (now, key))
    for flow in flows:
        # An observed gap starts a new observation interval for a reused key.
        row = db.execute("SELECT first_seen, ended_at FROM flows WHERE key=?", (flow["key"],)).fetchone()
        first = row[0] if row and row[1] is None else now
        flow["first_seen"], flow["last_seen"] = first, now
        db.execute("INSERT OR REPLACE INTO flows VALUES (?,?,?,NULL,?)",
                   (flow["key"], first, now, json.dumps(flow)))
    cutoff = (dt.datetime.fromisoformat(now) - dt.timedelta(hours=retention_hours)).isoformat(timespec="seconds")
    db.execute("DELETE FROM flows WHERE last_seen < ? AND ended_at IS NOT NULL", (cutoff,))
    db.execute("DELETE FROM flows WHERE key IN (SELECT key FROM flows WHERE ended_at IS NOT NULL "
               "ORDER BY last_seen DESC LIMIT -1 OFFSET ?)", (max(0, row_limit - len(flows)),))
    db.commit()
    rows = db.execute("SELECT data,ended_at FROM flows ORDER BY last_seen DESC LIMIT 500").fetchall()
    return [dict(json.loads(data), ended_at=ended) for data, ended in rows]


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect_once(db, state_path, interval):
    now, errors = utc_now(), []
    raw, error = command(["conntrack", "-L", "-f", "ipv4", "-o", "extended,id"])
    if error:
        errors.append(error)
        flows = []
        # Collector failure must not falsely close previous observations.
        history = []
    else:
        flows = parse_conntrack(raw)
        history = update_history(db, flows, now)
    journal, journal_error = command(["journalctl", "-k", "--since", "5 minutes ago",
                                     "-n", "1000", "--no-pager", "--quiet", "-o", "json",
                                     "--grep", "taco-(input|forward|output)-drop"], empty_ok=True)
    if journal_error:
        errors.append(journal_error)
    atomic_json(state_path, {"updated_at": now, "interval": interval, "demo": False,
                            "collector_ok": error is None, "errors": errors,
                            "flows": flows, "history": history,
                            "drops": parse_journal(journal)[:200]})


def demo_state():
    now = utc_now()
    raw = "\n".join([
        "ipv4 2 tcp 6 431999 ESTABLISHED src=10.1.0.10 dst=203.0.113.20 sport=52318 dport=443 packets=45 bytes=8120 src=203.0.113.20 dst=10.1.0.10 sport=443 dport=52318 packets=120 bytes=450000 [ASSURED] mark=0 id=10",
        "ipv4 2 udp 17 28 src=10.1.0.10 dst=10.0.0.254 sport=45218 dport=53 packets=1 bytes=68 src=10.0.0.254 dst=10.1.0.10 sport=53 dport=45218 packets=1 bytes=150 mark=0 id=11",
        "ipv4 2 tcp 6 431999 ESTABLISHED src=10.2.0.2 dst=198.51.100.40 sport=53712 dport=443 packets=40 bytes=9400 src=198.51.100.40 dst=10.2.0.2 sport=443 dport=53712 packets=350 bytes=1840000 [ASSURED] mark=0 id=12"])
    flows = parse_conntrack(raw)
    for flow in flows:
        flow.update(first_seen=now, last_seen=now)
    return {"updated_at": now, "interval": 5, "demo": True, "collector_ok": True,
            "errors": [], "flows": flows, "history": [dict(f, ended_at=None) for f in flows],
            "drops": [{"time": now, "scope": "AI", "chain": "forward", "src": "10.1.0.10",
                       "dst": "10.0.0.20", "proto": "TCP", "sport": "50123", "dport": "22",
                       "in": "eth1", "out": "eth0"}]}


def make_handler(state_path, port, demo=False, control_callback=None, token_file=TOKEN):
    callback = control_callback or request_control
    demo_control = {"enabled": False, "available": True, "checked_at": utc_now(),
                    "lease_seconds": 20, "error": None, "nodes": [
                        {"id": "openwrt", "name": "OpenWrt", "ip": "10.2.0.1", "state": "up", "latency_ms": 0.4, "checked_at": utc_now(), "last_success": utc_now()},
                        {"id": "taco", "name": "TACO", "ip": "10.2.0.2 / 10.1.0.1", "state": "up", "latency_ms": None, "checked_at": utc_now(), "last_success": utc_now()},
                        {"id": "spark", "name": "DGX Spark", "ip": "10.1.0.10", "state": "up", "latency_ms": 0.6, "checked_at": utc_now(), "last_success": utc_now()}]}

    def control_status(action="status"):
        if demo:
            if action != "status":
                demo_control["enabled"] = action == "enable"
            demo_control["checked_at"] = utc_now()
            for node in demo_control["nodes"]:
                node["checked_at"] = demo_control["checked_at"]
                node["last_success"] = demo_control["checked_at"]
            return dict(demo_control)
        return callback(action)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TacoConsole"

        def log_message(self, fmt, *args):
            pass

        def valid_request(self, mutation=False):
            # Reject DNS rebinding and cross-origin reads; no CORS is enabled.
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "") not in allowed:
                self.send_error(403, "Use the localhost SSH tunnel URL")
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_error(403)
                return False
            if mutation and self.headers.get("Origin") != "http://" + self.headers["Host"]:
                self.send_error(403, "Same-origin request required")
                return False
            return True

        def send_payload(self, status, payload, mime="application/json; charset=utf-8"):
            if isinstance(payload, dict):
                payload = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if not self.valid_request(mutation=True):
                return
            if urlsplit(self.path).path != "/api/internet":
                self.send_error(404)
                return
            if not demo:
                try:
                    expected = token_file.read_text().strip()
                except OSError:
                    self.send_payload(503, {"error": "操作キーが未導入です。インストール手順を確認してください。"})
                    return
                provided = self.headers.get("Authorization", "")
                if len(expected) < 32 or not secrets.compare_digest(provided.encode(), ("Bearer " + expected).encode()):
                    self.send_payload(403, {"error": "操作キーが正しくありません。"})
                    return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 128 or self.headers.get_content_type() != "application/json":
                    raise ValueError("invalid body")
                self.connection.settimeout(3)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or set(data) != {"enabled"} or type(data["enabled"]) is not bool:
                    raise ValueError("enabled must be boolean")
            except (ValueError, OSError):
                self.send_payload(400, {"error": "enabledにtrueまたはfalseを指定してください。"})
                return
            try:
                status = control_status("enable" if data["enabled"] else "disable")
                self.send_payload(200, {"control": status, "demo": demo})
            except Exception as exc:
                self.send_payload(503, {"error": "通信状態の変更を確認できませんでした。", "detail": str(exc)[:300]})

        def do_GET(self):
            if not self.valid_request():
                return
            path = urlsplit(self.path).path
            status = 200
            if path == "/api/control":
                try:
                    self.send_payload(200, {"control": control_status(), "demo": demo})
                except Exception as exc:
                    self.send_payload(503, {"error": "制御サービスに接続できません。", "detail": str(exc)[:300]})
                return
            elif path == "/api/state":
                try:
                    data = demo_state() if demo else json.loads(state_path.read_text())
                    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(data["updated_at"])).total_seconds()
                    data["stale"] = age > max(20, data.get("interval", 5) * 3)
                    payload, mime = json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8"
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    status, mime = 503, "application/json; charset=utf-8"
                    payload = json.dumps({"error": "収集データを読み込めません。collectorサービスを確認してください。",
                                          "detail": str(exc)[:200]}, ensure_ascii=False).encode()
            elif path in {"/", "/index.html", "/app.js", "/style.css"}:
                filename = "index.html" if path == "/" else path[1:]
                mime = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                        "css": "text/css; charset=utf-8"}[filename.split(".")[-1]]
                payload = (STATIC / filename).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_payload(status, payload, mime)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["collect", "serve", "demo"])
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/taco-console"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    if args.interval < 2 or not 1 <= args.port <= 65535:
        parser.error("interval >= 2 and valid TCP port required")
    state_path = args.state_dir / "state.json"
    if args.mode == "collect":
        os.umask(0o027)
        args.state_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(args.state_dir / "history.sqlite3"))
        os.chmod(args.state_dir / "history.sqlite3", 0o600)
        running = True
        def stop(*_):
            nonlocal running
            running = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while running:
                collect_once(db, state_path, args.interval)
                for _ in range(args.interval * 10):
                    if not running:
                        break
                    time.sleep(0.1)
        finally:
            db.close()
    else:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state_path, args.port, args.mode == "demo"))
        print(f"TACO console: http://127.0.0.1:{args.port}" + (" [DEMO]" if args.mode == "demo" else ""), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
