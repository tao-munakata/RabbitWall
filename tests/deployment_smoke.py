#!/usr/bin/env python3
"""Application install/update in an isolated container; systemd commands are simulated."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools'))
sys.path.insert(0, str(ROOT/'src/rabbitwall'))
import deploy


def main():
    if os.environ.get('RABBITWALL_DISPOSABLE_TEST') != '1' or not Path('/.dockerenv').exists():
        raise SystemExit('Run only in a disposable privileged container, with --network none.')
    real_run = subprocess.run
    config = json.loads((ROOT/'examples/rabbitwall.example.json').read_text())
    os.environ['RABBITWALL_CONFIG'] = '/etc/rabbitwall.json'
    Path('/etc/rabbitwall.json').write_text(json.dumps(config))
    spec = importlib.util.spec_from_file_location('setup_cli', ROOT/'setup.py')
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    for device, address in (('eth0', config['uplink_cidr']), ('eth1', config['ai_cidr'])):
        real_run(['ip', 'link', 'add', device, 'type', 'dummy'], check=True)
        real_run(['ip', 'addr', 'add', address, 'dev', device], check=True)
        real_run(['ip', 'link', 'set', device, 'up'], check=True)
    assets = setup.assets(config)
    Path('/etc/nftables.d').mkdir(exist_ok=True)
    Path('/etc/nftables.d/rabbitwall.nft').write_text(assets['rabbitwall.nft'])
    Path('/etc/nftables.conf').write_text(assets['nftables.conf'])
    real_run(['nft', '-f', '/etc/nftables.conf'], check=True)
    calls = []

    def commands(args, **kwargs):
        if args[0] == 'systemctl':
            calls.append(args)
            if args[1] == 'show':
                unit = Path('/etc/systemd/system')/(args[-1]+'.service')
                return subprocess.CompletedProcess(args, 0, 'loaded\n' if unit.exists() else 'not-found\n', '')
            return subprocess.CompletedProcess(args, 0)
        return real_run(args, **kwargs)

    with patch.object(deploy.subprocess, 'run', side_effect=commands):
        deploy.deploy(config)
        token = Path('/etc/rabbitwall/control.token').read_bytes()
        for name in ('dns_forwarder.py', 'dependencies.py', 'static/world.json', 'static/THIRD_PARTY.md'):
            assert (Path('/opt/rabbitwall')/name).is_file()
        assert Path('/var/lib/rabbitwall').stat().st_mode & 0o777 == 0o750
        assert Path('/etc/systemd/system/rabbitwall-collector.service.d/dns.conf').exists()
        # Updating must preserve the local config, token and mTLS service.
        caddy = Path('/etc/systemd/system/rabbitwall-caddy.service')
        caddy.write_text('local mTLS service sentinel')
        original_config = Path('/etc/rabbitwall.json').read_bytes()
        real_run(['nft', 'add', 'element', 'inet', 'rabbitwall_console', 'online_sources',
                  '{', '192.168.20.0/24', 'timeout', '20s', '}'], check=True)
        deploy.deploy(config)
        assert Path('/etc/rabbitwall/control.token').read_bytes() == token
        assert Path('/etc/rabbitwall.json').read_bytes() == original_config
        assert caddy.read_text() == 'local mTLS service sentinel'
        rules = real_run(['nft', '-j', 'list', 'set', 'inet', 'rabbitwall_console', 'online_sources'],
                         check=True, capture_output=True, text=True).stdout
        sets = [entry['set'] for entry in json.loads(rules)['nftables'] if 'set' in entry]
        assert not sets[0].get('elem')
        assert len(list(Path('/var/backups/rabbitwall').glob('application-*'))) == 2
        for service in deploy.SERVICES:
            assert calls.count(['systemctl', 'restart', service]) == 2
    print('PASS: complete application/DNS install and update, backups, private state, preserved mTLS/config/token, CLOSED gate (systemd simulated)')


if __name__ == '__main__':
    main()
