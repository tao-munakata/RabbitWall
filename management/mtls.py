#!/usr/bin/env python3
"""Offline PKI and deployment bundle for the console's Caddy mTLS endpoint."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import subprocess

SERVER_IP = "10.2.0.2"
SERVER_NAME = "radxa-console.home.arpa"
REMOTE_DIR = "/etc/taco-console-mtls"


def openssl(*args):
    subprocess.run(["openssl", *map(str, args)], check=True)


def new_directory(path):
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    path.chmod(0o700)


def write(path, value, mode=0o600):
    path.write_text(value)
    path.chmod(mode)


def password_args(path, option="-passin"):
    return [option, "file:" + str(path.resolve())] if path else []


def valid_name(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise ValueError("Name must contain only letters, digits, '-' and '_'")
    return name


def init_ca(directory, password_file=None):
    new_directory(directory)
    config = directory / "ca.cnf"
    write(config, """[req]
distinguished_name=dn
x509_extensions=ca
prompt=no
[dn]
CN=Development Network Console CA
[ca]
basicConstraints=critical,CA:true,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always
""")
    openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
            "-aes-256-cbc", *password_args(password_file, "-pass"), "-out", directory / "ca.key")
    (directory / "ca.key").chmod(0o600)
    openssl("req", "-new", "-x509", "-sha256", "-days", "3650", "-key", directory / "ca.key",
            *password_args(password_file), "-config", config, "-out", directory / "ca.crt")


def issue(directory, name, kind, password_file=None, p12_password_file=None, days=365):
    valid_name(name)
    target = directory / name
    if not 1 <= days <= 397:
        raise ValueError("Certificate lifetime must be between 1 and 397 days")
    if not (directory / "ca.key").is_file():
        raise ValueError("Run init first; keep the CA private key on the management PC")
    new_directory(target)
    config = target / "extensions.cnf"
    extensions = "basicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
    extensions += "extendedKeyUsage=" + ("serverAuth" if kind == "server" else "clientAuth") + "\n"
    extensions += "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
    if kind == "server":
        extensions += f"subjectAltName=DNS:{SERVER_NAME},IP:{SERVER_IP}\n"
    write(config, extensions)
    openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", target / "key.pem")
    (target / "key.pem").chmod(0o600)
    openssl("req", "-new", "-sha256", "-key", target / "key.pem", "-subj", "/CN=" + name,
            "-out", target / "request.csr")
    openssl("x509", "-req", "-sha256", "-days", str(days), "-in", target / "request.csr",
            "-CA", directory / "ca.crt", "-CAkey", directory / "ca.key", *password_args(password_file),
            "-set_serial", "0x" + secrets.token_hex(16), "-extfile", config, "-out", target / "cert.pem")
    write(target / "identity.json", json.dumps({"name": name, "kind": kind, "days": days}) + "\n")
    if kind == "client":
        # 3DES/SHA-1 here is only the password-protected import container, for macOS compatibility.
        # TLS certificates themselves use SHA-256 and modern TLS defaults.
        iterations = [] if "LibreSSL" in subprocess.check_output(["openssl", "version"], text=True) else ["-iter", "100000"]
        openssl("pkcs12", "-export", "-inkey", target / "key.pem", "-in", target / "cert.pem",
                "-certfile", directory / "ca.crt", "-name", name, "-keypbe", "PBE-SHA1-3DES",
                "-certpbe", "PBE-SHA1-3DES", "-macalg", "sha1", *iterations,
                *password_args(p12_password_file, "-passout"), "-out", target / "identity.p12")
        (target / "identity.p12").chmod(0o600)


def validate_network(admin, uplink):
    address = ipaddress.IPv4Address(admin)
    if address not in ipaddress.IPv4Network("10.0.0.0/24") or address.packed[-1] in (0, 254, 255):
        raise ValueError("Management PC must be in 10.0.0.1-253")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", uplink) or uplink == "lo":
        raise ValueError("Invalid uplink interface")
    return str(address)


def caddyfile(admin, cert_dir=REMOTE_DIR, listen=SERVER_IP, port=443, upstream="127.0.0.1:8765", bind=None):
    authority = SERVER_NAME if port == 443 else f"{SERVER_NAME}:{port}"
    return f"""{{
    admin off
    persist_config off
    grace_period 5s
    auto_https off
    servers {{
        protocols h1 h2
        strict_sni_host on
    }}
}}
https://{authority} {{
    bind {bind or listen}
    tls {cert_dir}/server.crt {cert_dir}/server.key {{
        client_auth {{
            mode require_and_verify
            trust_pool file {cert_dir}/ca.crt
            verifier leaf file {cert_dir}/allowed-clients.pem
        }}
    }}
    route {{
        @not_admin not remote_ip {admin}
        respond @not_admin "Forbidden" 403
        @bad_host not host {SERVER_NAME}
        respond @bad_host "Forbidden" 403
        @cross_site header Sec-Fetch-Site cross-site
        respond @cross_site "Forbidden" 403
        @bad_origin {{
            method POST
            not header Origin https://{authority}
        }}
        respond @bad_origin "Same-origin request required" 403
        header -Server
        reverse_proxy {upstream} {{
            header_up Host {upstream}
            header_up Origin http://{upstream}
            header_up Authorization "Bearer {{$TACO_CONTROL_TOKEN}}"
            header_up X-Taco-Auth mtls
        }}
    }}
}}
"""


def bundle(directory, server, clients, admin, uplink, output):
    admin = validate_network(admin, uplink)
    valid_name(server)
    if not clients:
        raise ValueError("At least one permitted client is required")
    identities = [(server, "server")] + [(valid_name(name), "client") for name in clients]
    for name, kind in identities:
        identity = directory / name
        if json.loads((identity / "identity.json").read_text())["kind"] != kind:
            raise ValueError(f"Wrong certificate purpose: {name}")
        openssl("verify", "-CAfile", directory / "ca.crt", "-purpose",
                "sslserver" if kind == "server" else "sslclient", identity / "cert.pem")
    new_directory(output)
    shutil.copyfile(directory / "ca.crt", output / "ca.crt")
    shutil.copyfile(directory / server / "cert.pem", output / "server.crt")
    shutil.copyfile(directory / server / "key.pem", output / "server.key")
    (output / "server.key").chmod(0o600)
    write(output / "allowed-clients.pem", "\n".join((directory / name / "cert.pem").read_text() for name in clients))
    write(output / "Caddyfile", caddyfile(admin))
    write(output / "web.nft", f'insert rule inet taco_filter input iifname "{uplink}" ip saddr {admin} '
          f'ip daddr {SERVER_IP} tcp dport 443 counter accept comment "taco-console-mtls"\n')
    write(output / "openwrt.fragment", f"""config rule 'ai_admin_console'
        option name 'Management PC mTLS console'
        option src 'lan'
        option dest 'ai'
        option family 'ipv4'
        option src_ip '{admin}'
        option dest_ip '{SERVER_IP}'
        option proto 'tcp'
        option dest_port '443'
        option target 'ACCEPT'
""")
    write(output / "openwrt-apply.sh", f"""#!/bin/sh
set -eu
if [ -n "$(uci changes firewall)" ]; then
    echo 'Commit or revert existing firewall UCI changes first.' >&2
    exit 1
fi
backup="/etc/config/firewall.console-mtls.$(date +%Y%m%d-%H%M%S).bak"
cp -p /etc/config/firewall "$backup"
trap 'uci revert firewall' 0
uci batch <<'UCI'
set firewall.ai_admin_console=rule
set firewall.ai_admin_console.name='Management PC mTLS console'
set firewall.ai_admin_console.src='lan'
set firewall.ai_admin_console.dest='ai'
set firewall.ai_admin_console.family='ipv4'
set firewall.ai_admin_console.src_ip='{admin}'
set firewall.ai_admin_console.dest_ip='{SERVER_IP}'
set firewall.ai_admin_console.proto='tcp'
set firewall.ai_admin_console.dest_port='443'
set firewall.ai_admin_console.target='ACCEPT'
UCI
fw4 check
uci commit firewall
if ! /etc/init.d/firewall reload; then
    cp -p "$backup" /etc/config/firewall
    /etc/init.d/firewall reload
    echo 'Reload failed; previous configuration restored.' >&2
    exit 1
fi
echo "mTLS rule applied; backup: $backup"
""", 0o700)
    fingerprints = {name: hashlib.sha256(ssl.PEM_cert_to_DER_cert((directory / name / "cert.pem").read_text())).hexdigest() for name in clients}
    write(output / "manifest.json", json.dumps({"admin_ipv4": admin, "uplink_if": uplink,
          "server_ip": SERVER_IP, "server_name": SERVER_NAME, "clients": fingerprints}, indent=2) + "\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path("output/mtls-pki"))
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--ca-password-file", type=Path)
    for kind in ("server", "client"):
        cmd = sub.add_parser("issue-" + kind)
        cmd.add_argument("name", type=valid_name)
        cmd.add_argument("--days", type=int, default=365)
        cmd.add_argument("--ca-password-file", type=Path)
        if kind == "client":
            cmd.add_argument("--p12-password-file", type=Path)
    cmd = sub.add_parser("bundle")
    cmd.add_argument("--server", required=True)
    cmd.add_argument("--client", action="append", required=True)
    cmd.add_argument("--admin-ip", required=True)
    cmd.add_argument("--uplink-if", default="eth0")
    cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "init":
            init_ca(args.dir, args.ca_password_file)
        elif args.action.startswith("issue-"):
            issue(args.dir, args.name, args.action[6:], args.ca_password_file,
                  getattr(args, "p12_password_file", None), args.days)
        else:
            bundle(args.dir, args.server, args.client, args.admin_ip, args.uplink_if, args.output)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
