import test_environment
import importlib.util
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src/rabbitwall'))
sys.path.insert(0, str(ROOT/'tools'))
from dependencies import check_dns_version
import console
import dnstap_observer
import deploy


class Hardening(unittest.TestCase):
    def test_dns_version_rejects_vulnerable_and_unverified_major(self):
        for version in ('2.3.0', '2.6.0', '3.0.0', '2.6.1rc1'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                check_dns_version(version)
        for version in ('2.6.1', '2.7.0', '2.8.0'):
            check_dns_version(version)

    def test_http_saturation_closes_socket_without_starting_more_threads(self):
        with console.ConsoleServer(('127.0.0.1', 0), console.make_handler(Path('/unused'), 8765, demo=True)) as server:
            for _ in range(server.max_workers):
                self.assertTrue(server.slots.acquire(blocking=False))
            request, peer = socket.socketpair()
            try:
                server.process_request(request, ('127.0.0.1', 1234))
                peer.settimeout(1)
                self.assertEqual(peer.recv(1), b'')
            finally:
                peer.close()

    def test_snapshot_is_not_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'state.json'
            console.atomic_json(target, {'flows': []})
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_deployment_contains_all_runtime_modules_and_units(self):
        files = deploy.deployment_files()
        self.assertIn(Path('/opt/rabbitwall/dependencies.py'), files)
        self.assertIn(Path('/opt/rabbitwall/dnstap_observer.py'), files)
        self.assertIn(Path('/etc/systemd/system/rabbitwall-collector.service.d/dns.conf'), files)
        self.assertNotIn(Path('/etc/rabbitwall.json'), files)
        self.assertNotIn(Path('/etc/systemd/system/rabbitwall-caddy.service'), files)
        self.assertFalse(any('test' in p.parts or '__pycache__' in p.parts for p in files.values()))

    def test_dependency_failure_prevents_gate_or_file_changes(self):
        with patch('dependencies.require_observer', side_effect=ValueError('outdated')), patch.object(deploy.os, 'geteuid', return_value=0), patch.object(deploy, 'close_installed') as close:
            with self.assertRaises(ValueError): deploy.deploy({})
            close.assert_not_called()
