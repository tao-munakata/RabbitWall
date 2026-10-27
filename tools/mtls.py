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
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/rabbitwall'))
from settings import load, UPLINK_INTERFACE
RUNTIME_CONFIG = load()
SERVER_IP = str(UPLINK_INTERFACE.ip)
SERVER_NAME = RUNTIME_CONFIG['console_name']


def configure(path):
    global RUNTIME_CONFIG, SERVER_IP, SERVER_NAME
    RUNTIME_CONFIG = load(path)
    SERVER_IP = str(ipaddress.IPv4Interface(RUNTIME_CONFIG['uplink_cidr']).ip)
    SERVER_NAME = RUNTIME_CONFIG['console_name']
REMOTE_DIR = "/etc/rabbitwall-mtls"


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
CN=RabbitWall CA
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
    network = ipaddress.IPv4Network(RUNTIME_CONFIG['management_cidr'])
    if address not in network or address in (network.network_address, network.broadcast_address, ipaddress.IPv4Address(SERVER_IP), ipaddress.IPv4Address(RUNTIME_CONFIG['gateway']), ipaddress.IPv4Address(RUNTIME_CONFIG['dns_upstream'])):
        raise ValueError("Management PC must be a usable address in the configured management subnet")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", uplink) or uplink == "lo":
        raise ValueError("Invalid uplink interface")
    return str(address)


def caddyfile(admin, cert_dir=REMOTE_DIR, listen=None, port=443, upstream="127.0.0.1:8765", bind=None):
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
    bind {bind or listen or SERVER_IP}
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
            header_up Authorization "Bearer {{$RABBITWALL_CONTROL_TOKEN}}"
            header_up X-RabbitWall-Auth mtls
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
    write(output / "web.nft", f'insert rule inet rabbitwall_filter input iifname "{uplink}" ip saddr {admin} '
          f'ip daddr {SERVER_IP} tcp dport 443 counter accept comment "rabbitwall-mtls"\n')
    fingerprints = {name: hashlib.sha256(ssl.PEM_cert_to_DER_cert((directory / name / "cert.pem").read_text())).hexdigest() for name in clients}
    write(output / "manifest.json", json.dumps({"admin_ipv4": admin, "uplink_if": uplink,
          "server_ip": SERVER_IP, "server_name": SERVER_NAME, "clients": fingerprints}, indent=2) + "\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Shared installation JSON")
    parser.add_argument("--dir", type=Path, default=Path("output/rabbitwall-pki"))
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
    cmd.add_argument("--admin-ip")
    cmd.add_argument("--uplink-if")
    cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.config:
            configure(args.config)
        if args.action == "init":
            init_ca(args.dir, args.ca_password_file)
        elif args.action.startswith("issue-"):
            issue(args.dir, args.name, args.action[6:], args.ca_password_file,
                  getattr(args, "p12_password_file", None), args.days)
        else:
            bundle(args.dir, args.server, args.client, args.admin_ip or RUNTIME_CONFIG['admin_ip'], args.uplink_if or RUNTIME_CONFIG['uplink_if'], args.output)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
