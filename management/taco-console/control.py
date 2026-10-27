#!/usr/bin/env python3
"""Fixed-purpose local controller; never accepts addresses or nft commands from HTTP."""
import argparse
import concurrent.futures
import datetime as dt
import grp
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import tempfile
import time

SOCKET = Path("/run/taco-console/control.sock")
TOKEN = Path("/etc/taco-console/control.token")
GROUP = "taco-console-control"
LEASE = 20
PROBE_COMMENT = "taco-console-spark-probe"
PROBE_RULE = ('insert rule inet taco_filter output oifname "eth1" '
              'ip saddr 10.1.0.1 ip daddr 10.1.0.10 icmp type echo-request '
              f'counter accept comment "{PROBE_COMMENT}"\n')
DNS_COMMENT = "taco-console-local-dns"
DNS_RULE = ('insert rule inet taco_filter input iifname "eth1" '
            'ip saddr 10.1.0.0/24 ip daddr 10.1.0.1 '
            'meta l4proto { tcp, udp } th dport 53 counter accept '
            f'comment "{DNS_COMMENT}"\n')
# This chain only imposes extra drops. ON returns to the original firewall;
# it never replaces or bypasses its allow list. Empty/expired set means OFF.
GATE_RULES = '''table inet taco_console {
    set online_sources {
        type ipv4_addr
        flags interval,timeout
        timeout 20s
    }
    chain ai_egress {
        ct original ip saddr @online_sources counter return
        limit rate 5/second burst 10 packets log prefix "taco-forward-drop gate "
        counter drop
    }
    chain dns_request {
        ip saddr @online_sources counter return
        counter drop
    }
    chain dns_reply {
        ip daddr @online_sources counter return
        counter drop
    }
    chain input {
        type filter hook input priority -10; policy accept;
        iifname "eth1" ip daddr 10.1.0.1 meta l4proto { tcp, udp } th dport 53 jump dns_request
    }
    chain output {
        type filter hook output priority -10; policy accept;
        ip saddr 10.1.0.1 ip daddr 10.0.0.254 meta l4proto { tcp, udp } th dport 53 jump dns_request
        oifname "eth1" ip saddr 10.1.0.1 meta l4proto { tcp, udp } th sport 53 jump dns_reply
    }
    chain forward {
        type filter hook forward priority -10; policy accept;
        iifname "eth1" meta l4proto { tcp, udp } th dport 53 counter drop
        iifname "eth1" oifname "eth0" meta nfproto ipv6 counter drop
        iifname "eth0" oifname "eth1" meta nfproto ipv6 counter drop
        iifname "eth1" oifname "eth0" ip saddr 10.1.0.0/24 ct direction original jump ai_egress
        iifname "eth0" oifname "eth1" ct original ip saddr 10.1.0.0/24 jump ai_egress
    }
}
'''


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def nft(args, script=None):
    result = subprocess.run(["nft"] + args, input=script, text=True,
                            capture_output=True, timeout=5)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:500] or "nft command failed")
    return result.stdout


def request_control(action, path=SOCKET):
    if action not in {"status", "enable", "disable"}:
        raise ValueError("unsupported action")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(4)
        client.connect(str(path))
        client.sendall((json.dumps({"action": action}) + "\n").encode())
        chunks = bytearray()
        while b"\n" not in chunks:
            part = client.recv(4096)
            if not part:
                raise RuntimeError("controller closed connection")
            chunks.extend(part)
            if len(chunks) > 65536:
                raise RuntimeError("controller response too large")
        data = json.loads(chunks.split(b"\n", 1)[0])
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "controller error"))
        return data["status"]


def read_gate():
    document = json.loads(nft(["-j", "-a", "list", "table", "inet", "taco_console"]))
    objects = document.get("nftables", [])
    table = next(item["table"] for item in objects if "table" in item)
    enabled = next(item["set"] for item in objects
                   if item.get("set", {}).get("name") == "online_sources")
    chains = {item["chain"]["name"]: item["chain"] for item in objects if "chain" in item}
    if (enabled.get("type") != "ipv4_addr" or "timeout" not in enabled.get("flags", [])
            or "ai_egress" not in chains or chains.get("forward", {}).get("hook") != "forward"
            or chains["forward"].get("prio") != -10
            or any(chains.get(name, {}).get("hook") != name
                   or chains.get(name, {}).get("prio") != -10 for name in ("input", "output"))
            or not {"dns_request", "dns_reply"}.issubset(chains)):
        raise RuntimeError("gate structure differs; reinstall required")
    return {"handle": table["handle"], "enabled": bool(enabled.get("elem"))}


def baseline():
    data = json.loads(nft(["-j", "list", "ruleset"]))
    objects = data.get("nftables", [])
    chains = {item["chain"]["name"]: item["chain"] for item in objects
              if item.get("chain", {}).get("table") == "taco_filter"}
    if any(chains.get(name, {}).get("policy") != "drop" for name in ("input", "forward", "output")):
        raise RuntimeError("expected taco_filter input/forward/output drop policy is missing")
    if any("flowtable" in item for item in objects):
        raise RuntimeError("flowtable offload is not supported by the internet gate")
    return objects


def install_runtime():
    objects = baseline()
    exists = any(item.get("table", {}).get("name") == "taco_console" and
                 item["table"].get("family") == "inet" for item in objects)
    probe_exists = any(item.get("rule", {}).get("comment") == PROBE_COMMENT for item in objects)
    script = ("delete table inet taco_console\n" if exists else "") + GATE_RULES
    if not probe_exists:
        script += PROBE_RULE
    if not any(item.get("rule", {}).get("comment") == DNS_COMMENT for item in objects):
        script += DNS_RULE
    # Delete+create is one atomic nft transaction; no open interval in between.
    nft(["-f", "-"], script)
    return read_gate()["handle"]


def apply_enabled(enabled):
    script = "flush set inet taco_console online_sources\n"
    if enabled:
        script += f"add element inet taco_console online_sources {{ 10.1.0.0/24 timeout {LEASE}s }}\n"
    nft(["-f", "-"], script)


def probe_nodes(previous=None):
    previous = {node["id"]: node for node in (previous or [])}
    checked = utc_now()
    nodes = [{"id": "taco", "name": "TACO", "ip": "10.2.0.2 / 10.1.0.1",
              "state": "up", "method": "local controller", "checked_at": checked,
              "last_success": checked, "latency_ms": None}]
    for identity, name, address, interface in [("openwrt", "OpenWrt", "10.2.0.1", "eth0"),
                                                ("spark", "DGX Spark", "10.1.0.10", "eth1")]:
        node = {"id": identity, "name": name, "ip": address, "state": "unknown",
                "method": "ICMP", "checked_at": utc_now(),
                "last_success": previous.get(identity, {}).get("last_success"), "latency_ms": None}
        try:
            proc = subprocess.run(["ping", "-n", "-c", "1", "-W", "1", "-I", interface, address],
                                  text=True, capture_output=True, timeout=3,
                                  env=dict(os.environ, LC_ALL="C"))
            node["state"] = "up" if proc.returncode == 0 else "no_reply" if proc.returncode == 1 else "unknown"
            match = re.search(r"time[=<]([\d.]+)\s*ms", proc.stdout)
            if match:
                node["latency_ms"] = float(match[1])
            if proc.returncode == 0:
                node["last_success"] = node["checked_at"]
            elif proc.returncode != 1:
                node["detail"] = proc.stderr.strip()[:200]
        except (OSError, subprocess.TimeoutExpired) as exc:
            node["detail"] = str(exc)[:200]
        nodes.append(node)
    return nodes


class Controller:
    def __init__(self):
        self.handle = install_runtime()
        self.desired = False
        self.error = None
        self.nodes = []

    def status(self):
        actual = read_gate()
        if actual["handle"] != self.handle:
            # nftables reload recreates an empty gate: never restore ON silently.
            self.desired = False
            self.handle = actual["handle"]
            apply_enabled(False)
            actual = read_gate()
        return {"enabled": actual["enabled"], "available": True, "checked_at": utc_now(),
                "lease_seconds": LEASE, "nodes": self.nodes, "error": self.error}

    def set_enabled(self, enabled):
        self.status()
        apply_enabled(enabled)
        self.desired = enabled
        self.error = None
        current = self.status()
        if current["enabled"] != enabled:
            raise RuntimeError("kernel gate state did not match request")
        return current

    def tick(self):
        try:
            current = self.status()
            # Expiry (e.g. suspended process) requires a fresh ON action.
            if self.desired and not current["enabled"]:
                self.desired = False
            if self.desired:
                apply_enabled(True)
            self.error = None
        except (OSError, RuntimeError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired) as exc:
            self.desired = False
            self.error = str(exc)[:500]
            try:
                apply_enabled(False)
            except Exception:
                pass  # Existing kernel lease expires even if control is unavailable.


def run_controller():
    controller = Controller()
    print(json.dumps({"event": "controller-start", "enabled": False, "at": utc_now()}), flush=True)
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(1)
            try:
                raw = self.rfile.readline(257)
                if len(raw) > 256 or not raw.endswith(b"\n"):
                    raise ValueError("invalid control request")
                data = json.loads(raw)
                if not isinstance(data, dict) or set(data) != {"action"}:
                    raise ValueError("invalid control request")
                action = data["action"]
                if action == "status":
                    status = controller.status()
                elif action in {"enable", "disable"}:
                    status = controller.set_enabled(action == "enable")
                    pid, uid, gid = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    print(json.dumps({"event": action, "at": utc_now(), "pid": pid, "uid": uid, "gid": gid,
                                      "enabled": status["enabled"]}), flush=True)
                else:
                    raise ValueError("unsupported action")
                response = {"ok": True, "status": status}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)[:500]}
            try:
                self.wfile.write((json.dumps(response) + "\n").encode())
            except OSError:
                pass

    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET.exists():
        SOCKET.unlink()
    with socketserver.UnixStreamServer(str(SOCKET), Handler) as server, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        os.chmod(SOCKET, 0o660)
        server.timeout = 0.5
        last_tick, last_probe, future = 0.0, 0.0, None
        try:
            while running:
                now = time.monotonic()
                if now - last_tick >= 4:
                    controller.tick()
                    last_tick = now
                if future and future.done():
                    controller.nodes = future.result()
                    future = None
                if future is None and now - last_probe >= 10:
                    future = executor.submit(probe_nodes, controller.nodes)
                    last_probe = now
                server.handle_request()
        finally:
            controller.desired = False
            apply_enabled(False)
            SOCKET.unlink(missing_ok=True)


def install_persistent(config=Path("/etc/nftables.conf"), snippet=Path("/etc/nftables.d/taco-console.nft"), token=TOKEN):
    """Preserve the actual existing config; add only a validated include."""
    original = config.read_text()
    if "flush ruleset" not in original or not re.search(r"table\s+inet\s+taco_filter\b", original):
        raise RuntimeError("Expected generated taco_filter config with flush ruleset; integrate manually")
    if original.count(str(snippet)) > 1:
        raise RuntimeError("duplicate taco-console includes; integrate manually")
    include = f'include "{snippet}"'
    candidate = original if include in original else original.rstrip() + "\n\n# TACO console: boot defaults to OFF\n" + include + "\n"
    snippet.parent.mkdir(parents=True, exist_ok=True)
    old_snippet = snippet.read_bytes() if snippet.exists() else None
    snippet.write_text(GATE_RULES + "\n" + PROBE_RULE + DNS_RULE)
    os.chmod(snippet, 0o644)
    fd, temporary = tempfile.mkstemp(prefix=".taco-nft-check-", dir=config.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(candidate)
        nft(["-c", "-f", temporary])
        if candidate != original:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = config.with_name(config.name + ".taco-console-" + stamp + ".bak")
            backup.write_text(original)
            os.chmod(backup, 0o600)
            shutil.copystat(config, temporary)
            os.chown(temporary, config.stat().st_uid, config.stat().st_gid)
            os.replace(temporary, config)
    except Exception:
        if old_snippet is None:
            snippet.unlink(missing_ok=True)
        else:
            snippet.write_bytes(old_snippet)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    token.parent.mkdir(parents=True, exist_ok=True)
    group_id = grp.getgrnam(GROUP).gr_gid
    os.chown(token.parent, 0, group_id)
    os.chmod(token.parent, 0o750)
    if not token.exists():
        descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(descriptor, "w") as output:
            output.write(secrets.token_urlsafe(32) + "\n")
    os.chown(token, 0, group_id)
    os.chmod(token, 0o640)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["run", "install", "off"])
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run on TACO as root")
    os.umask(0o027)
    if args.mode == "install":
        install_persistent()
    elif args.mode == "off":
        try:
            request_control("disable")
        except (OSError, RuntimeError, ValueError):
            apply_enabled(False)
    else:
        run_controller()


if __name__ == "__main__":
    main()
