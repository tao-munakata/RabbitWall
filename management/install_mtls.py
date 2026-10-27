#!/usr/bin/env python3
"""Install a reviewed mTLS bundle on Radxa without reloading the Internet gate."""
import argparse
import grp
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from mtls import REMOTE_DIR, SERVER_IP, SERVER_NAME, caddyfile, validate_network

TAG = "taco-console-mtls"
INCLUDE = 'include "/etc/nftables.d/taco-console-web.nft"'


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def persistent_config(original):
    if "flush ruleset" not in original or not re.search(r"table\s+inet\s+taco_filter\b", original):
        raise ValueError("Expected the existing taco_filter firewall; refusing automatic integration")
    if original.count("/etc/nftables.d/taco-console-web.nft") > 1:
        raise ValueError("Duplicate console web includes")
    if "/etc/nftables.d/taco-console-web.nft" in original and INCLUDE not in original:
        raise ValueError("Unexpected include syntax; integrate manually")
    return original if INCLUDE in original else original.rstrip() + "\n\n# Management PC mTLS only\n" + INCLUDE + "\n"


def web_rule(admin, uplink):
    admin = validate_network(admin, uplink)
    return (f'insert rule inet taco_filter input iifname "{uplink}" ip saddr {admin} '
            f'ip daddr {SERVER_IP} tcp dport 443 counter accept comment "{TAG}"\n')


def runtime_batch(objects, rule):
    chains = [item["chain"] for item in objects if "chain" in item]
    if len(chains) != 1 or chains[0].get("policy") != "drop":
        raise ValueError("Existing input chain must have a drop policy")
    lines = []
    for item in objects:
        entry = item.get("rule", {})
        if entry.get("comment") == TAG:
            handle = entry.get("handle")
            if type(handle) is not int:
                raise ValueError("Invalid nft handle")
            lines.append(f"delete rule inet taco_filter input handle {handle}")
    return "\n".join(lines) + "\n" + rule


def install(bundle):
    if os.geteuid() != 0:
        raise ValueError("Run this installer with sudo on Radxa")
    for command in ("nft", "caddy", "openssl", "systemctl"):
        if not shutil.which(command):
            raise ValueError(f"Install {command} first")
    if not Path("/usr/bin/caddy").is_file():
        raise ValueError("Install the official Caddy package at /usr/bin/caddy")
    version = run("/usr/bin/caddy", "version", capture_output=True, text=True).stdout
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (2, 11, 7):
        raise ValueError("Caddy 2.11.7 or later is required")
    group = grp.getgrnam("caddy").gr_gid
    data = json.loads((bundle / "manifest.json").read_text())
    if data["server_ip"] != SERVER_IP or data["server_name"] != SERVER_NAME:
        raise ValueError("Unexpected server IP")
    admin = validate_network(data["admin_ipv4"], data["uplink_if"])
    token = Path("/etc/taco-console/control.token").read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Invalid existing operation key")
    config = Path("/etc/nftables.conf")
    original = config.read_text()
    candidate = persistent_config(original)
    chain = json.loads(run("nft", "-j", "list", "chain", "inet", "taco_filter", "input",
                           capture_output=True, text=True).stdout)["nftables"]
    rule = web_rule(admin, data["uplink_if"])
    batch = runtime_batch(chain, rule)
    for name in ("server.crt", "server.key", "ca.crt", "allowed-clients.pem"):
        if not (bundle / name).is_file():
            raise ValueError(f"Missing bundle file: {name}")
    run("openssl", "verify", "-CAfile", bundle / "ca.crt", "-purpose", "sslserver", bundle / "server.crt")
    run("openssl", "x509", "-in", bundle / "server.crt", "-checkend", "86400", "-noout")
    root = Path(REMOTE_DIR)
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(root, 0, group)
    root.chmod(0o750)
    Path("/etc/nftables.d").mkdir(exist_ok=True)
    targets = {root / name: (bundle / name).read_bytes() for name in
               ("server.crt", "server.key", "ca.crt", "allowed-clients.pem")}
    targets[root / "Caddyfile"] = caddyfile(admin).encode()
    targets[root / "control.env"] = ("TACO_CONTROL_TOKEN=" + token + "\n").encode()
    snippet = Path("/etc/nftables.d/taco-console-web.nft")
    old_rule = snippet.read_text() if snippet.exists() else ""
    targets[snippet] = rule.encode()
    targets[config] = candidate.encode()
    unit = Path("/etc/systemd/system/taco-console-caddy.service")
    targets[unit] = (Path(__file__).parent / "systemd/taco-console-caddy.service").read_bytes()
    saved = {path: (path.read_bytes(), path.stat()) if path.exists() else None for path in targets}
    backup = Path("/etc/taco-console/backups") / ("mtls-" + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    for path, previous in saved.items():
        if previous:
            dest = backup / str(path).lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest.write_bytes(previous[0])
            dest.chmod(0o600)
    was_active = subprocess.run(["systemctl", "is-active", "--quiet", "taco-console-caddy"]).returncode == 0
    runtime_changed = False
    try:
        for path, value in targets.items():
            path.write_bytes(value)
            if path == root / "control.env":
                path.chmod(0o600)
            elif path.parent == root:
                os.chown(path, 0, group)
                path.chmod(0o640)
            else:
                path.chmod(0o644)
        environment = dict(os.environ, TACO_CONTROL_TOKEN=token)
        run("/usr/bin/caddy", "validate", "--config", root / "Caddyfile", "--adapter", "caddyfile", env=environment)
        run("nft", "-c", "-f", config)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".nft") as temporary:
            temporary.write(batch)
            temporary.flush()
            run("nft", "-c", "-f", temporary.name)
            run("systemctl", "daemon-reload")
            run("systemctl", "restart", "taco-console-caddy")
            run("nft", "-f", temporary.name)
            runtime_changed = True
        run("systemctl", "enable", "taco-console-caddy")
    except Exception:
        for path, previous in saved.items():
            if previous:
                path.write_bytes(previous[0])
                os.chmod(path, previous[1].st_mode)
                os.chown(path, previous[1].st_uid, previous[1].st_gid)
            elif path.exists():
                path.unlink()
        if runtime_changed:
            current = json.loads(run("nft", "-j", "list", "chain", "inet", "taco_filter", "input",
                                     capture_output=True, text=True).stdout)["nftables"]
            with tempfile.NamedTemporaryFile(mode="w", suffix=".nft") as temporary:
                temporary.write(runtime_batch(current, old_rule))
                temporary.flush()
                run("nft", "-f", temporary.name)
        run("systemctl", "daemon-reload")
        recovery = subprocess.run(["systemctl", "restart" if was_active else "stop", "taco-console-caddy"], capture_output=True, text=True)
        if recovery.returncode and was_active:
            print("Caddy recovery failed; inspect systemctl status and the saved backup", file=sys.stderr)
        raise
    print(f"mTLS installed: https://{SERVER_NAME}/ ; allowed management PC: {admin}")
    print(f"Backups: {backup}. Internet gate/control service was not restarted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        install(args.bundle)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + "\n")
