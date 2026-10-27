"""Second network/NAT configuration; disposable Docker only, never a real host."""
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src/rabbitwall'))
import setup
import control
from settings import SETTINGS
from linux_gate_smoke import ip, run, client, SERVER


def main():
    if not Path('/.dockerenv').exists() or os.environ.get('RABBITWALL_DISPOSABLE_TEST')!='1' or Path('/sys/class/net/eth0').exists():
        raise SystemExit('Disposable --network none container required')
    c=SETTINGS
    assert c['uplink_cidr']=='192.168.10.2/24' and c['ai_cidr']=='192.168.20.1/24' and c['nat']
    children=[]
    try:
        for ns,iface,peer,local,remote in [('upstream','eth0','up-side','192.168.10.2/24','192.168.10.1/24'),
                                           ('ai','eth1','ai-side','192.168.20.1/24','192.168.20.10/24')]:
            ip('netns','add',ns);ip('link','add',iface,'type','veth','peer','name',peer)
            ip('link','set',peer,'netns',ns);ip('addr','add',local,'dev',iface);ip('link','set',iface,'up')
            ip('-n',ns,'link','set','lo','up');ip('-n',ns,'addr','add',remote,'dev',peer);ip('-n',ns,'link','set',peer,'up')
        ip('link','set','lo','up')
        ip('-n','upstream','addr','add','1.1.1.1/32','dev','lo')
        ip('-n','upstream','addr','add','192.168.10.10/32','dev','lo')
        ip('-n','upstream','route','add','192.168.20.0/24','via','192.168.10.2')
        ip('-n','ai','route','add','default','via','192.168.20.1')
        ip('route','add','default','via','192.168.10.1')
        run(['sysctl','-w','net.ipv4.ip_forward=1'])
        rendered=setup.assets(c)
        expanded=rendered['nftables.conf'].replace('include "/etc/nftables.d/rabbitwall.nft"',rendered['rabbitwall.nft'])
        control.nft(['-c','-f','-'],expanded);control.nft(['-f','-'],expanded)
        for namespace,ports,udp in [('upstream','443,53','53'),('ai','22','none'),(None,'22,53','53')]:
            prefix=['ip','netns','exec',namespace] if namespace else []
            child=subprocess.Popen(prefix+[sys.executable,'-u','-c',SERVER,ports,udp,'192.168.10.1' if namespace=='upstream' else '192.168.20.1'],stdout=subprocess.PIPE,text=True)
            children.append(child);assert child.stdout.readline().strip()=='ready'
        controller=control.Controller()
        client('ai','1.1.1.1',443,expected=False)
        client('ai','192.168.20.1',53,expected=False,udp=True)
        client('upstream','192.168.10.2',22,source='192.168.10.10')
        controller.set_enabled(True)
        client('ai','1.1.1.1',443)
        client('ai','192.168.20.1',53,udp=True)
        client(None,'192.168.10.1',53,source='192.168.20.1',udp=True)
        client('ai','192.168.10.1',53,expected=False,udp=True)
        client('upstream','192.168.20.10',22,source='192.168.10.10')
        controller.set_enabled(False)
        client('ai','192.168.20.1',53,expected=False)
        client(None,'192.168.10.1',53,source='192.168.20.1',expected=False,udp=True)
        client(None,'192.168.10.1',53)  # OS DNS uses the uplink address.
        print('PASS: public configuration baseline, NAT, gate, DNS isolation and management SSH')
    finally:
        for child in children:child.terminate()
        for child in children:child.wait(timeout=3)

if __name__=='__main__':main()
