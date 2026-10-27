#!/usr/bin/env python3
"""Local-only real Caddy TLS tests. Uses a fake controller, never touches nftables."""
import argparse
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from http.server import ThreadingHTTPServer

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "tools"))
sys.path.insert(0, str(BASE / "src/rabbitwall"))
import mtls

# The live listener is local-only; production SAN 10.2.0.2 is verified by unit tests.
mtls.SERVER_IP = "127.0.0.1"

spec = importlib.util.spec_from_file_location("console", BASE / "src/rabbitwall/console.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


def quiet_openssl(*args):
    subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def caddy_server(binary, root, admin, upstream, token):
    port = free_port()
    config = root / "Caddyfile"
    config.write_text(mtls.caddyfile(admin, cert_dir=str(root), listen=mtls.SERVER_IP, port=port,
                                    upstream=upstream, bind="127.0.0.1"))
    environment = dict(os.environ, RABBITWALL_CONTROL_TOKEN=token, XDG_DATA_HOME=str(root / "data"),
                       XDG_CONFIG_HOME=str(root / "config"))
    validation=subprocess.run([binary,"validate","--config",str(config),"--adapter","caddyfile"],capture_output=True,text=True,env=environment)
    if validation.returncode:
        raise RuntimeError(f"Caddy validation exited {validation.returncode}: {validation.stdout} {validation.stderr}")
    with (root / "caddy.log").open("w") as log:
        process = subprocess.Popen([binary, "run", "--config", str(config), "--adapter", "caddyfile"],
                                   stdout=log, stderr=log, env=environment)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(f"Caddy exited {process.returncode}: " + (root / "caddy.log").read_text())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError("Caddy did not start")
            yield port
        finally:
            process.terminate()
            process.wait(timeout=10)


def request(root, port, client="admin", method="GET", path="/api/control", headers=None, body=""):
    context = ssl.create_default_context(cafile=str(root / "ca.crt"))
    if client:
        context.load_cert_chain(str(root / "pki" / client / "cert.pem"),
                                str(root / "pki" / client / "key.pem"))
    fields = {"Host": f"{mtls.SERVER_NAME}:{port}", "Connection": "close", **(headers or {})}
    if body:
        fields.update({"Content-Length": str(len(body.encode())), "Content-Type": "application/json"})
    raw = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n" + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as tcp:
        with context.wrap_socket(tcp, server_hostname=mtls.SERVER_NAME) as secure:
            secure.sendall(raw.encode())
            response = http.client.HTTPResponse(secure)
            response.begin()
            return response.status, response.read()


def main(binary):
    with tempfile.TemporaryDirectory(prefix="console-mtls-smoke-") as directory:
        root = Path(directory)
        pki = root / "pki"
        password = root / "password"
        password.write_text("test-only-ca-and-container-password")
        password.chmod(0o600)
        with patch.object(mtls, "openssl", side_effect=quiet_openssl):
            mtls.init_ca(pki, password)
            mtls.issue(pki, "server", "server", password)
            mtls.issue(pki, "admin", "client", password, password)
            mtls.issue(pki, "unlisted", "client", password, password)
        (root / "ca.crt").write_bytes((pki / "ca.crt").read_bytes())
        (root / "server.crt").write_bytes((pki / "server/cert.pem").read_bytes())
        (root / "server.key").write_bytes((pki / "server/key.pem").read_bytes())
        (root / "allowed-clients.pem").write_bytes((pki / "admin/cert.pem").read_bytes())
        token = "test-only-internal-proxy-token-1234567890"
        (root / "token").write_text(token)
        state = console.demo_state()
        state["demo"] = False
        (root / "state.json").write_text(json.dumps(state))
        control = {"enabled": False, "available": True, "nodes": []}

        def callback(action):
            if action != "status":
                control["enabled"] = action == "enable"
            return dict(control)

        backend_port = free_port()
        handler = console.make_handler(root / "state.json", backend_port,
                                       control_callback=callback, token_file=root / "token")
        backend = console.ConsoleServer(("127.0.0.1", backend_port), handler)
        worker = threading.Thread(target=backend.serve_forever, daemon=True)
        worker.start()
        try:
            with caddy_server(binary, root, "127.0.0.1", f"127.0.0.1:{backend_port}", token) as port:
                for identity in (None, "unlisted"):
                    try:
                        status, _ = request(root, port, identity)
                    except (ssl.SSLError, OSError, http.client.HTTPException):
                        pass
                    else:
                        assert status >= 400, (identity, status)
                status, content = request(root, port)
                assert status == 200, (status, content)
                assert json.loads(content)["auth"] == {"mode": "mtls", "can_control": True}
                assert token.encode() not in content
                status, html = request(root, port, path="/")
                assert status == 200
                assert b"control-token" not in html and b"remember-token" not in html
                assert token.encode() not in html
                for fields in ({}, {"Origin": "https://attacker.invalid"},
                               {"Origin": f"https://{mtls.SERVER_NAME}:{port}", "Sec-Fetch-Site": "cross-site"}):
                    status, _ = request(root, port, method="POST", path="/api/internet", headers=fields,
                                        body='{"enabled":true}')
                    assert status == 403, status
                    assert not control["enabled"]
                status, _ = request(root, port, headers={"Host": "attacker.invalid"})
                assert status >= 400
                status, content = request(root, port, method="POST", path="/api/internet",
                                          headers={"Origin": f"https://{mtls.SERVER_NAME}:{port}",
                                                   "Authorization": "Bearer attacker-value", "X-RabbitWall-Auth": "fake"},
                                          body='{"enabled":true}')
                assert status == 200, (status, content)
                assert control["enabled"]
                assert token.encode() not in content
                print("PASS: real mTLS, no-certificate/unlisted rejection, same-origin checks, server-side key injection")
            # Restarting after changing the leaf allowlist also closes existing TLS sessions.
            (root / "allowed-clients.pem").write_bytes((pki / "unlisted/cert.pem").read_bytes())
            with caddy_server(binary, root, "127.0.0.1", f"127.0.0.1:{backend_port}", token) as port:
                try:
                    status, _ = request(root, port, "admin")
                except (ssl.SSLError, OSError, http.client.HTTPException):
                    pass
                else:
                    assert status >= 400
                status, _ = request(root, port, "unlisted")
                assert status == 200
                print("PASS: replacing the permitted leaf certificate rejects the former certificate")
            (root / "allowed-clients.pem").write_bytes((pki / "admin/cert.pem").read_bytes())
            with caddy_server(binary, root, "127.0.0.2", f"127.0.0.1:{backend_port}", token) as port:
                status, _ = request(root, port)
                assert status == 403, status
                print("PASS: valid client certificate from a non-permitted source IP is rejected")
        finally:
            backend.shutdown()
            backend.server_close()
            worker.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caddy", required=True)
    main(parser.parse_args().caddy)
