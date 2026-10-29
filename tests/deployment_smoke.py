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
        for name in ('dnstap_observer.py', 'dependencies.py', 'static/world.json', 'static/THIRD_PARTY.md'):
            assert (Path('/opt/rabbitwall')/name).is_file()
        assert Path('/var/lib/rabbitwall').stat().st_mode & 0o777 == 0o750
        assert Path('/etc/systemd/system/rabbitwall-collector.service.d/dns.conf').exists()
        # Updating must preserve the local config, token and mTLS service.
        caddy = Path('/etc/systemd/system/rabbitwall-caddy.service')
        caddy.write_text('local mTLS service sentinel')
        original_config = Path('/etc/rabbitwall.json').read_bytes()
        real_run(['nft', 'add', 'element', 'inet', 'rabbitwall_console', 'online_sources',
                  '{', '192.168.20.0/24', 'timeout', '20s', '}'], check=True)
        override_nodes = json.loads(json.dumps(config['nodes']))
        override_nodes[-1].update(label='Renamed AI', allow_ssh=False, monitor=False)
        overrides = Path('/etc/rabbitwall-nodes/nodes.json')
        overrides.write_text(json.dumps({'nodes':override_nodes}))
        saved_nodes = overrides.read_bytes()
        deploy.deploy(setup.load())
        assert overrides.read_bytes() == saved_nodes
        assert 'ip daddr 192.168.20.10 tcp dport 22' not in Path('/etc/nftables.d/rabbitwall.nft').read_text()
        assert Path('/etc/rabbitwall/control.token').read_bytes() == token
        assert Path('/etc/rabbitwall.json').read_bytes() == original_config
        assert caddy.read_text() == 'local mTLS service sentinel'
        assert calls.count(['systemctl','restart','rabbitwall-caddy']) == 1
        assert calls.index(['systemctl','restart','rabbitwall-caddy']) > calls.index(['systemctl','restart','rabbitwall-web'])
        rules = real_run(['nft', '-j', 'list', 'set', 'inet', 'rabbitwall_console', 'online_sources'],
                         check=True, capture_output=True, text=True).stdout
        sets = [entry['set'] for entry in json.loads(rules)['nftables'] if 'set' in entry]
        assert not sets[0].get('elem')
        assert len(list(Path('/var/backups/rabbitwall').glob('application-*'))) == 2
        for service in deploy.SERVICES:
            assert calls.count(['systemctl', 'restart', service]) == 2
    # Correct a previously installed network, exercising real rendering, nftables,
    # backups and application deployment. NetworkManager/systemd are simulated.
    corrected = dict(config, uplink_cidr='10.2.0.2/30', gateway='10.2.0.1',
                     management_cidr='10.0.0.0/24', admin_ip='10.0.0.9',
                     ai_cidr='10.1.0.1/24', dns_upstream='10.2.0.1', nat=False,
                     nodes=[{'id':'gateway','label':'Gateway','ip':'10.2.0.1','interface':'eth0'},
                            {'id':'ai-host','label':'AI','ip':'10.1.0.10','interface':'eth1'}])
    mtls_root = Path('/etc/rabbitwall-mtls'); mtls_root.mkdir(exist_ok=True)
    (mtls_root/'Caddyfile').write_text('original-caddyfile')
    real_run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
              '-subj','/CN=rabbitwall.home.arpa','-addext','subjectAltName=DNS:rabbitwall.home.arpa',
              '-keyout',str(mtls_root/'server.key'),'-out',str(mtls_root/'server.crt')],
             check=True,capture_output=True)
    (mtls_root/'ca.crt').write_bytes((mtls_root/'server.crt').read_bytes())
    original_key = (mtls_root/'server.key').read_bytes()
    source = Path('/tmp/corrected.json'); source.write_text(json.dumps(corrected))
    nm = Path('/etc/NetworkManager/system-connections'); nm.mkdir(parents=True, exist_ok=True)
    (nm/'rabbitwall-uplink.nmconnection').write_text('original-profile')
    original_rules = Path('/etc/nftables.conf').read_text()
    def network_commands(args, **kwargs):
        if args[0] in ('nmcli', 'sysctl', 'modprobe', 'caddy'):
            calls.append(args)
            if args[:3] == ['nmcli','connection','up']:
                device = 'eth0' if str(args[-1]) == str(setup.uuid.uuid5(setup.uuid.NAMESPACE_DNS,'rabbitwall.rabbitwall-uplink.eth0')) else 'eth1'
                cidr = corrected['uplink_cidr' if device == 'eth0' else 'ai_cidr']
                real_run(['ip','-4','addr','flush','dev',device],check=True)
                real_run(['ip','addr','add',cidr,'dev',device],check=True)
            return subprocess.CompletedProcess(args,0,'old-profile:eth0\n' if '--active' in args else '', '')
        return commands(args, **kwargs)
    with patch.object(setup, 'check'), patch.object(setup.subprocess,'run',side_effect=network_commands):
        setup.reconfigure(source, True)
    assert (mtls_root/'server.key').read_bytes() == original_key
    assert 'bind 10.2.0.2' in (mtls_root/'Caddyfile').read_text()
    assert '@not_admin not remote_ip 10.0.0.9' in (mtls_root/'Caddyfile').read_text()
    assert '10.2.0.2' in Path('/etc/nftables.d/rabbitwall-web.nft').read_text()
    assert Path('/etc/nftables.conf').read_text().count('include "/etc/nftables.d/rabbitwall-web.nft"') == 1
    assert ['systemctl','restart','rabbitwall-caddy'] in calls
    assert not Path('/etc/rabbitwall-nodes/nodes.json').exists()
    assert json.loads(Path('/etc/rabbitwall.json').read_text()) == corrected
    assert '10.2.0.2' in Path('/etc/nftables.conf').read_text()
    assert '192.168.10.2' not in Path('/etc/nftables.conf').read_text()
    assert 'forward-addr: 10.2.0.1' in Path('/etc/rabbitwall-unbound.conf').read_text()
    assert Path('/etc/rabbitwall/control.token').read_bytes() == token
    assert ['nmcli','connection','modify','uuid','old-profile','connection.autoconnect','no'] in calls
    backups = [p for p in Path('/var/backups/rabbitwall').iterdir() if (p/'restore.sh').exists()]
    assert len(backups) == 1
    real_run(['sh','-n',str(backups[0]/'restore.sh')],check=True)
    # The persisted include files must also reload successfully, as at boot.
    real_run(['nft','-f','/etc/nftables.conf'],check=True)
    assert (backups[0]/'etc/rabbitwall-mtls/Caddyfile').read_text() == 'original-caddyfile'
    assert (backups[0]/'etc/nftables.conf').read_text() == original_rules
    assert (backups[0]/'etc/NetworkManager/system-connections/rabbitwall-uplink.nmconnection').read_text() == 'original-profile'
    assert 'systemctl disable --now rabbitwall-' not in (backups[0]/'restore.sh').read_text()
    rules = real_run(['nft','-j','list','set','inet','rabbitwall_console','online_sources'],check=True,capture_output=True,text=True).stdout
    assert not next(entry['set'] for entry in json.loads(rules)['nftables'] if 'set' in entry).get('elem')
    # Certificate mismatch must fail before stopping services or replacing config.
    source.write_text(json.dumps(dict(corrected, console_name='different.home.arpa')))
    stop_count = sum(1 for call in calls if call[:2] == ['systemctl','stop'])
    with patch.object(setup,'check'), patch.object(setup.subprocess,'run',side_effect=network_commands):
        try:
            setup.reconfigure(source, True)
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError('Mismatched server certificate accepted')
    assert sum(1 for call in calls if call[:2] == ['systemctl','stop']) == stop_count
    assert json.loads(Path('/etc/rabbitwall.json').read_text()) == corrected
    # A failure after stopping services must still leave the kernel gate closed.
    source.write_text(json.dumps(corrected))
    real_run(['nft','add','element','inet','rabbitwall_console','online_sources',
              '{','10.1.0.0/24','timeout','20s','}'],check=True)
    with patch.object(setup,'check',side_effect=[None,ValueError('occupied port')]), patch.object(setup.subprocess,'run',side_effect=network_commands):
        try:
            setup.reconfigure(source, True)
        except ValueError as error:
            assert str(error) == 'occupied port'
        else:
            raise AssertionError('Port conflict ignored')
    rules = real_run(['nft','-j','list','set','inet','rabbitwall_console','online_sources'],check=True,capture_output=True,text=True).stdout
    assert not next(entry['set'] for entry in json.loads(rules)['nftables'] if 'set' in entry).get('elem')
    print('PASS: complete application/DNS install and update, backups, private state, network correction, mTLS rebinding, certificate rejection, failure closes gate, backups and preserved keys (systemd/NetworkManager/Caddy validation simulated)')


if __name__ == '__main__':
    main()
