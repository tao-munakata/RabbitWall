import test_environment
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "tools"))
import mtls
import install_mtls


class Certificates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.password = cls.root / "password"
        cls.password.write_text("test-only-pki-password")
        cls.password.chmod(0o600)
        cls.pki = cls.root / "pki"

        def quiet(*args):
            subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)

        with patch.object(mtls, "openssl", side_effect=quiet):
            mtls.init_ca(cls.pki, cls.password)
            mtls.issue(cls.pki, "server", "server", cls.password)
            mtls.issue(cls.pki, "admin", "client", cls.password, cls.password)
            mtls.bundle(cls.pki, "server", ["admin"], "10.0.0.9", "eth0", cls.root / "bundle")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_purposes_san_and_private_key_protection(self):
        for name, purpose in (("server", "sslserver"), ("admin", "sslclient")):
            cert = self.pki / name / "cert.pem"
            subprocess.run(["openssl", "verify", "-CAfile", str(self.pki / "ca.crt"),
                            "-purpose", purpose, str(cert)], check=True, capture_output=True)
        wrong = subprocess.run(["openssl", "verify", "-CAfile", str(self.pki / "ca.crt"),
                                "-purpose", "sslserver", str(self.pki / "admin/cert.pem")], capture_output=True)
        self.assertNotEqual(wrong.returncode, 0)
        text = subprocess.check_output(["openssl", "x509", "-in", str(self.pki / "server/cert.pem"), "-text", "-noout"], text=True)
        self.assertIn("IP Address:10.2.0.2", text)
        self.assertEqual((self.pki / "ca.key").stat().st_mode & 0o777, 0o600)
        no_password = subprocess.run(["openssl", "pkey", "-in", str(self.pki / "ca.key"),
                                      "-passin", "pass:wrong", "-noout"], capture_output=True)
        self.assertNotEqual(no_password.returncode, 0)

    def test_import_container_and_deploy_bundle_exclude_management_secrets(self):
        subprocess.run(["openssl", "pkcs12", "-in", str(self.pki / "admin/identity.p12"),
                        "-passin", "file:" + str(self.password), "-noout"], check=True, capture_output=True)
        files = {path.name for path in (self.root / "bundle").iterdir()}
        self.assertEqual(files, {"server.crt", "server.key", "ca.crt", "allowed-clients.pem",
                                 "Caddyfile", "web.nft", "manifest.json"})
        self.assertNotIn((self.pki / "ca.key").read_bytes(), (self.root / "bundle/server.key").read_bytes())
        self.assertEqual((self.root / "bundle/allowed-clients.pem").read_text().strip(),
                         (self.pki / "admin/cert.pem").read_text().strip())

    def test_refuse_overwrite_or_wrong_identity_purpose(self):
        with self.assertRaises(FileExistsError):
            mtls.init_ca(self.pki, self.password)
        with self.assertRaises(ValueError):
            mtls.bundle(self.pki, "server", ["server"], "10.0.0.9", "eth0", self.root / "bad")


class Configuration(unittest.TestCase):
    def test_proxy_requires_ca_and_leaf_and_does_not_trust_forwarded_source(self):
        config = mtls.caddyfile("10.0.0.9")
        for required in ("admin off", "auto_https off", "protocols h1 h2", "bind 10.2.0.2",
                         "mode require_and_verify", "trust_pool file", "verifier leaf file",
                         "not remote_ip 10.0.0.9", "not header Origin https://rabbitwall.home.arpa",
                         'header_up Authorization "Bearer {$RABBITWALL_CONTROL_TOKEN}"'):
            self.assertIn(required, config)
        self.assertNotIn("trusted_proxies", config)
        self.assertNotIn("insecure_skip_verify", config)

    def test_firewall_is_single_pc_single_interface_single_destination_port(self):
        rule = install_mtls.web_rule("10.0.0.9", "eth0")
        self.assertIn('iifname "eth0" ip saddr 10.0.0.9 ip daddr 10.2.0.2 tcp dport 443', rule)
        objects = [{"chain": {"policy": "drop"}}, {"rule": {"comment": "rabbitwall-mtls", "handle": 7}},
                   {"rule": {"comment": "unrelated", "handle": 9}}]
        batch = install_mtls.runtime_batch(objects, rule)
        self.assertIn("delete rule inet rabbitwall_filter input handle 7", batch)
        self.assertNotIn("handle 9", batch)
        self.assertNotIn("flush", batch)
        config = 'flush ruleset\ntable inet rabbitwall_filter { }\n'
        candidate = install_mtls.persistent_config(config)
        self.assertEqual(install_mtls.persistent_config(candidate), candidate)
        with self.assertRaises(ValueError):
            install_mtls.runtime_batch([{"chain": {"policy": "accept"}}], rule)

    def test_reject_invalid_network_inputs(self):
        for ip, iface in (("10.1.0.10", "eth0"), ("10.0.0.254", "eth0"), ("10.0.0.9", "lo"),
                          ("10.0.0.9", 'eth0"; flush ruleset')):
            with self.assertRaises(ValueError):
                mtls.validate_network(ip, iface)
