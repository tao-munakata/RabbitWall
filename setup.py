#!/usr/bin/env python3
"""RabbitWall setup: init, plan, check, install and doctor. Stdlib only."""
import argparse
import datetime as dt
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src/rabbitwall'))
from settings import validate, load

DEPENDENCIES = ['python3', 'python3-dnspython', 'conntrack', 'nftables', 'iproute2', 'iputils-ping', 'kmod']


def run(*args, **kwargs):
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def init(path, defaults=False):
    if path.exists(): raise ValueError(f'Refusing to overwrite {path}')
    config = json.loads((ROOT/'examples/rabbitwall.example.json').read_text())
    if not defaults:
        print('Dedicated Linux gateway / 2 NICs. Addresses must match your network.')
        for key, prompt in [('label','Console label'), ('uplink_if','Uplink NIC'), ('uplink_cidr','Console uplink IPv4/prefix'),
                            ('gateway','Upstream gateway IPv4'), ('management_cidr','Management subnet'),
                            ('admin_ip','Management PC IPv4'), ('ai_if','AI-side NIC'), ('ai_cidr','AI-side IPv4/prefix'),
                            ('dns_upstream','Upstream DNS IPv4'), ('console_name','Console hostname (.home.arpa)')]:
            value = input(f'{prompt} [{config[key]}]: ').strip()
            if value: config[key] = value
        config['nat'] = input('Use NAT (no upstream static route needed)? [Y/n]: ').strip().lower() != 'n'
        ai = ipaddress.IPv4Interface(config['ai_cidr'])
        example = str(ai.network.network_address + 10) if ai.network.num_addresses > 11 else str(ai.network.network_address+2)
        target = input(f'AI host IPv4 [{example}]: ').strip() or example
        config['nodes'] = [{'id':'gateway','label':'Gateway','ip':config['gateway'],'interface':config['uplink_if']},
                           {'id':'ai-host','label':'AI Host','ip':target,'interface':config['ai_if']}]
    config = validate(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2)+'\n'); path.chmod(0o600)
    print(f'Created {path}. Next: python3 setup.py plan --config {path}')


def assets(config):
    """Render a complete dedicated-router firewall; never merge arbitrary host rules."""
    c = validate(config)
    from firewall import baseline, gate, dns_rule, probe_rules, SYSCTL
    firewall = baseline(c)
    gate_rules = gate(c) + dns_rule(c) + probe_rules(c)
    sysctl = SYSCTL
    result = {'rabbitwall.nft':gate_rules, 'nftables.conf':firewall, '90-rabbitwall-router.conf':sysctl,
              'rabbitwall-monitor.conf':'nf_conntrack\n',
              '90-rabbitwall-ai.conf':f'[device-rabbitwall-ai]\nmatch-device=interface-name:{c["ai_if"]}\nignore-carrier=yes\n'}
    for name, device, cidr, gateway in [('rabbitwall-uplink',c['uplink_if'],c['uplink_cidr'],c['gateway']),
                                         ('rabbitwall-ai',c['ai_if'],c['ai_cidr'],None)]:
        profile = f'''[connection]
id={name}
uuid={uuid.uuid5(uuid.NAMESPACE_DNS,'rabbitwall.'+name+'.'+device)}
type=ethernet
interface-name={device}
autoconnect=true
autoconnect-priority=999

[ipv4]
method=manual
address1={cidr}{','+gateway if gateway else ''}
never-default={'false' if gateway else 'true'}
ignore-auto-dns=true
'''
        if gateway: profile += f'dns={c["dns_upstream"]};\n'
        result[name+'.nmconnection'] = profile+'\n[ipv6]\nmethod=disabled\n'
    return result


def plan(config, output):
    if output.exists(): raise ValueError(f'Refusing to overwrite {output}; use a new plan directory')
    output.mkdir(parents=True, mode=0o700)
    (output/'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2)+'\n')
    for name, content in assets(config).items():
        target = output/name; target.write_text(content); target.chmod(0o600)
    ai = ipaddress.IPv4Interface(config['ai_cidr'])
    text = f'''RabbitWall installation plan
Console: {config['uplink_cidr']} / {config['uplink_if']}
Management PC: {config['admin_ip']}
AI side: {config['ai_cidr']} / {config['ai_if']}
Upstream: {config['gateway']} / DNS {config['dns_upstream']}
NAT: {config['nat']}
Console URL after mTLS: https://{config['console_name']}/
AI clients: gateway and DNS {ai.ip}; configure static addresses in {ai.network}.

Installation replaces this dedicated host's nftables ruleset and changes both NICs.
Use a local console for initial installation. IPv6 is disabled on these profiles.
Backups: /var/backups/rabbitwall/<timestamp>/ with restore.sh.
No packages, network or services have been changed by this plan command.
'''
    (output/'PLAN.txt').write_text(text)
    print(text)
    print(f'Review {output}/nftables.conf and .nmconnection files before installing.')


def port_conflicts(output, port, config):
    target = str(ipaddress.IPv4Interface(config['ai_cidr'] if port==53 else config['uplink_cidr']).ip) if port!=8765 else '127.0.0.1'
    for line in output.splitlines():
        fields=line.split()
        if len(fields)<5: continue
        address=fields[4].rsplit(':',1)[0].split('%',1)[0].strip('[]')
        if address in (target,'0.0.0.0','*','::'): return True
    return False


def check(config, require_addresses=False):
    errors = []
    if sys.platform != 'linux': errors.append('Install on Linux; init/plan/demo also work on macOS.')
    for program in ('python3','nft','conntrack','ip','ping','nmcli','systemctl','journalctl','sysctl','modprobe'):
        if not shutil.which(program): errors.append(f'Missing command: {program}')
    if shutil.which('systemctl'):
        for service in ('NetworkManager','ufw','firewalld','docker'):
            active = subprocess.run(['systemctl','is-active','--quiet',service], capture_output=True).returncode == 0
            if service == 'NetworkManager' and not active: errors.append('NetworkManager must already be running (do not replace a different network manager remotely).')
            if service != 'NetworkManager' and active: errors.append(f'Conflicting service: {service}; use a dedicated gateway.')
    for device in (config['uplink_if'],config['ai_if']):
        if sys.platform == 'linux' and not (Path('/sys/class/net')/device).exists(): errors.append(f'NIC not found: {device}')
    from dependencies import require_dns
    try: require_dns()
    except ValueError as error: errors.append(str(error))
    if shutil.which('ss'):
        for port in (53,443,8765):
            result = subprocess.run(['ss','-H','-lntup',f'sport = :{port}'],capture_output=True,text=True)
            if result.returncode: errors.append('Could not inspect listening ports with ss')
            elif port_conflicts(result.stdout,port,config): errors.append(f'Port {port} conflicts with the configured bind address; inspect sudo ss -lntup.')
    if require_addresses and shutil.which('ip'):
        output = json.loads(subprocess.check_output(['ip','-j','-4','addr'],text=True))
        owned = {a['local'] for device in output for a in device.get('addr_info',[])}
        for key in ('ai_cidr','uplink_cidr'):
            if str(ipaddress.IPv4Interface(config[key]).ip) not in owned: errors.append(f'Configured address is not active: {config[key]}')
    for error in errors: print('FAIL: '+error)
    if errors: raise ValueError('Preflight failed. Install packages: sudo apt install '+' '.join(DEPENDENCIES)+' (NetworkManager must be configured separately).')
    print('Preflight OK')


def install(plan_dir, apply):
    if os.geteuid()!=0: raise ValueError('Run install with sudo on the dedicated Linux gateway')
    if not apply: raise ValueError('Review the plan first; initial install requires --apply-network')
    config = load(plan_dir/'config.json'); check(config)
    for name in ('rabbitwall-uplink','rabbitwall-ai'):
        if (Path('/etc/NetworkManager/system-connections')/(name+'.nmconnection')).exists():
            raise ValueError('Generated profile name already exists; inspect NetworkManager before installing')
    if Path('/etc/rabbitwall.json').exists() or Path('/opt/rabbitwall/control.py').exists():
        raise ValueError('RabbitWall is already installed; use setup.py update')
    expected = assets(config)
    for name, content in expected.items():
        if (plan_dir/name).read_text()!=content: raise ValueError(f'Plan was changed: {name}; regenerate it from the JSON config')
    run('nft','-c','-f','-', input=expected['nftables.conf'].replace('include "/etc/nftables.d/rabbitwall.nft"', expected['rabbitwall.nft']), text=True)
    backup = Path('/var/backups/rabbitwall')/dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True,mode=0o700)
    for path in ['/etc/nftables.conf','/etc/rabbitwall.json','/etc/nftables.d/rabbitwall.nft','/etc/NetworkManager/system-connections',
                 '/etc/NetworkManager/conf.d/90-rabbitwall-ai.conf','/etc/sysctl.d/90-rabbitwall-router.conf','/etc/modules-load.d/rabbitwall-monitor.conf']:
        source=Path(path)
        if source.exists():
            target=backup/path.lstrip('/');target.parent.mkdir(parents=True,exist_ok=True)
            if source.is_dir():shutil.copytree(source,target)
            else:shutil.copy2(source,target)
    enabled=subprocess.run(['systemctl','is-enabled','--quiet','nftables']).returncode==0
    if enabled:(backup/'nftables-was-enabled').touch()
    (backup/'runtime.nft').write_text('flush ruleset\n'+subprocess.check_output(['nft','list','ruleset'],text=True))
    (backup/'restore.sh').write_text('''#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
systemctl disable --now rabbitwall-dns rabbitwall-web rabbitwall-control rabbitwall-collector rabbitwall-caddy 2>/dev/null || true
nft -f "$base/runtime.nft"
if [ ! -f "$base/nftables-was-enabled" ]; then systemctl disable nftables; fi
for file in /etc/nftables.conf /etc/rabbitwall.json /etc/nftables.d/rabbitwall.nft /etc/NetworkManager/conf.d/90-rabbitwall-ai.conf /etc/sysctl.d/90-rabbitwall-router.conf /etc/modules-load.d/rabbitwall-monitor.conf; do
    if [ ! -f "$base$file" ]; then rm -f "$file"; fi
done
cp -a "$base/etc/." /etc/
rm -f /etc/NetworkManager/system-connections/rabbitwall-uplink.nmconnection /etc/NetworkManager/system-connections/rabbitwall-ai.nmconnection
nmcli general reload
nmcli connection reload
echo 'Saved configuration restored. Reboot locally to reactivate the original profiles.'
''');(backup/'restore.sh').chmod(0o700)
    # Prepare files and closed firewall before activating either new connection.
    shutil.copyfile(plan_dir/'config.json','/etc/rabbitwall.json');os.chmod('/etc/rabbitwall.json',0o644)
    targets={'rabbitwall.nft':'/etc/nftables.d/rabbitwall.nft','nftables.conf':'/etc/nftables.conf','90-rabbitwall-router.conf':'/etc/sysctl.d/90-rabbitwall-router.conf',
             'rabbitwall-monitor.conf':'/etc/modules-load.d/rabbitwall-monitor.conf',
             '90-rabbitwall-ai.conf':'/etc/NetworkManager/conf.d/90-rabbitwall-ai.conf'}
    for name,target in targets.items():
        Path(target).parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(plan_dir/name,target);os.chmod(target,0o644)
    for name in ('rabbitwall-uplink','rabbitwall-ai'):
        target=Path('/etc/NetworkManager/system-connections')/(name+'.nmconnection')
        shutil.copyfile(plan_dir/target.name,target);target.chmod(0o600)
    run('modprobe','nf_conntrack');run('sysctl','-p','/etc/sysctl.d/90-rabbitwall-router.conf')
    run('nft','-f','/etc/nftables.conf');run('systemctl','enable','nftables')
    run('nmcli','general','reload');run('nmcli','connection','reload')
    run('nmcli','connection','up','rabbitwall-uplink');run('nmcli','connection','up','rabbitwall-ai')
    deploy_application(config)
    print(f'Installed, gate CLOSED. Backup and recovery: {backup}. Next: docs/INSTALL.md (mTLS).')


def deploy_application(config):
    sys.path.insert(0, str(ROOT/'tools'))
    from deploy import deploy
    deploy(config)


def doctor():
    for name in ('nftables','rabbitwall-control','rabbitwall-dns','rabbitwall-collector','rabbitwall-web','rabbitwall-caddy'):
        subprocess.run(['systemctl','is-active',name],check=False)
    for args in [('ip','-br','-4','addr'),('ss','-lntup'),('nft','list','table','inet','rabbitwall_console')]:
        subprocess.run(args,check=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    init_parser=sub.add_parser('init');init_parser.add_argument('--config',type=Path,default=Path('rabbitwall.json'));init_parser.add_argument('--defaults',action='store_true')
    for name in ('plan','check'):
        cmd=sub.add_parser(name);cmd.add_argument('--config',type=Path,default=Path('rabbitwall.json'))
        if name=='plan':cmd.add_argument('--output',type=Path,default=Path('output/install-plan'))
    cmd=sub.add_parser('install');cmd.add_argument('--plan',type=Path,default=Path('output/install-plan'));cmd.add_argument('--apply-network',action='store_true')
    cmd=sub.add_parser('demo');cmd.add_argument('--config',type=Path,default=ROOT/'examples/rabbitwall.example.json');cmd.add_argument('--port',type=int,default=8766)
    sub.add_parser('doctor')
    sub.add_parser('update')
    args=parser.parse_args()
    try:
        if args.command=='init':init(args.config,args.defaults)
        elif args.command=='plan':plan(load(args.config),args.output)
        elif args.command=='check':check(load(args.config))
        elif args.command=='install':install(args.plan,args.apply_network)
        elif args.command=='demo':
            load(args.config)
            env=dict(os.environ,RABBITWALL_CONFIG=str(args.config.resolve()))
            run(sys.executable,'-B',ROOT/'src/rabbitwall/console.py','demo','--port',args.port,
                '--state-dir',Path(tempfile.gettempdir())/'rabbitwall-demo',env=env)
        elif args.command=='update':
            if os.geteuid()!=0: raise ValueError('Run update with sudo on the gateway')
            deploy_application(load(Path('/etc/rabbitwall.json')))
            print('Updated. Gate is CLOSED; confirm the console before reopening.')
        else:doctor()
    except (OSError,ValueError,KeyError,subprocess.CalledProcessError) as error:
        parser.exit(1,str(error)+'\n')

if __name__=='__main__':main()
