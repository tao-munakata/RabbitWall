import test_environment
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/rabbitwall'))
from settings import validate, metadata
spec=importlib.util.spec_from_file_location('public_setup',ROOT/'setup.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)

class Setup(unittest.TestCase):
    def config(self):return json.loads((ROOT/'examples/rabbitwall.example.json').read_text())

    def test_defaults_match_published_example(self):
        from settings import DEFAULTS
        self.assertEqual(DEFAULTS,self.config())

    def test_reject_overlap_loopback_injection_duplicate_hosts_and_bad_names(self):
        edits=[{'ai_cidr':'192.168.10.1/24'},{'uplink_if':'eth0; reboot'}, {'ai_if':'eth0'},
               {'dns_upstream':'192.168.20.2'},{'dns_upstream':'127.0.0.1'}, {'console_name':'example.com'},
               {'admin_ip':'192.168.10.255'},{'nat':'true'}]
        for edit in edits:
            with self.subTest(edit=edit),self.assertRaises(ValueError):validate(dict(self.config(),**edit))
        c=self.config();c['nodes'][1]['ip']=c['nodes'][0]['ip']
        with self.assertRaises(ValueError):validate(c)

    def test_custom_nic_and_subnets_reach_every_rendered_asset(self):
        c=self.config();c['uplink_if']='enp1s0';c['ai_if']='enx001122334455'
        c['nodes'][0]['interface']=c['uplink_if'];c['nodes'][1]['interface']=c['ai_if']
        rendered=setup.assets(c);joined='\n'.join(rendered.values())
        for old in ('10.1.0.','10.2.0.','10.0.0.254','"eth0"','"eth1"'):
            self.assertNotIn(old,joined)
        self.assertIn('192.168.20.0/24',rendered['rabbitwall.nft'])
        self.assertIn('masquerade',rendered['nftables.conf'])
        self.assertIn('enx001122334455',rendered['90-rabbitwall-ai.conf'])
        self.assertIn('ignore-carrier=yes',rendered['90-rabbitwall-ai.conf'])
        self.assertEqual(len(metadata(c)['hosts']),4)

    def test_plan_never_applies_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(setup,'run') as run:
            path=Path(directory)/'plan';setup.plan(self.config(),path)
            self.assertTrue((path/'PLAN.txt').exists());run.assert_not_called()
            with self.assertRaises(ValueError):setup.plan(self.config(),path)

    def test_initial_install_refuses_without_explicit_apply(self):
        with patch.object(setup.os,'geteuid',return_value=0),self.assertRaisesRegex(ValueError,'--apply-network'):
            setup.install(Path('/not-used'),False)

    def test_reconfigure_requires_explicit_network_apply_before_changes(self):
        with patch.object(setup.os, 'geteuid', return_value=0), patch.object(setup, 'run') as run:
            with self.assertRaisesRegex(ValueError, '--apply-network'):
                setup.reconfigure(Path('/not-used'), False)
            run.assert_not_called()

    def test_reconfigure_validates_before_applying(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'bad.json'
            config.write_text(json.dumps(dict(self.config(), uplink_if='eth0; reboot')))
            with patch.object(setup.os, 'geteuid', return_value=0), patch.object(Path, 'exists', return_value=True), patch.object(setup, 'install') as install:
                with self.assertRaises(ValueError): setup.reconfigure(config, True)
                install.assert_not_called()

    def test_runtime_settings_reach_dns_console_gate_and_analytics(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json';path.write_text(json.dumps(self.config()))
            import os
            env=dict(os.environ,RABBITWALL_CONFIG=str(path))
            code='''import sys;sys.path.insert(0,sys.argv[1]);import settings,control,console,traffic_analytics,dnstap_observer,unbound_config
assert console.scope('192.168.20.10')=='AI'
assert console.scope('10.1.0.10') is None
assert console.flow_scope('192.168.10.10','192.168.20.10')=='MANAGEMENT'
assert '192.168.20.0/24' in control.GATE_RULES
assert '192.168.20.1' in control.DNS_RULE
assert str(dnstap_observer.AI_INTERFACE.network)=='192.168.20.0/24'
assert 'forward-addr: 192.168.10.1' in unbound_config.render(settings.SETTINGS)
assert console.demo_state()['flows'][0]['src']=='192.168.20.10'
'''
            subprocess.run([sys.executable,'-B','-c',code,str(ROOT/'src/rabbitwall')],env=env,check=True)

    def test_release_uses_exact_manifest_not_directory_discovery(self):
        spec=importlib.util.spec_from_file_location('release_test',ROOT/'tools/release.py')
        release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'allowed.py').write_text('# public')
            (root/'local.json').write_text('{"secret":"not for distribution"}')
            manifest=root/'files.txt';manifest.write_text('allowed.py\n')
            with patch.object(release,'ROOT',root),patch.object(release,'MANIFEST',manifest):
                self.assertEqual([p.name for p in release.sources()],['allowed.py'])
                manifest.write_text('../outside\n')
                with self.assertRaises(ValueError):list(release.sources())

    def test_loopback_resolver_does_not_block_ai_only_dns_install(self):
        stub='udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:resolved'
        self.assertFalse(setup.port_conflicts(stub,53,self.config()))
        wildcard='udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:dnsmasq'
        self.assertTrue(setup.port_conflicts(wildcard,53,self.config()))
        owned='udp UNCONN 0 0 192.168.20.1:53 0.0.0.0:*'
        self.assertTrue(setup.port_conflicts(owned,53,self.config()))

    def test_public_mtls_certificate_uses_configured_names_and_ip(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);config=base/'rabbitwall.json';config.write_text(json.dumps(self.config()))
            password=base/'password';password.write_text('temporary-test-password')
            code="""import sys,json,subprocess;from pathlib import Path
sys.path.insert(0,sys.argv[1]);import mtls
mtls.configure(Path(sys.argv[2]));base=Path(sys.argv[3]);password=base/'password'
def quiet(*args): subprocess.run(['openssl',*map(str,args)],check=True,capture_output=True)
mtls.openssl=quiet
mtls.init_ca(base/'pki',password)
mtls.issue(base/'pki','server','server',password)
mtls.issue(base/'pki','admin','client',password,password)
mtls.bundle(base/'pki','server',['admin'],'192.168.10.10','eth0',base/'bundle')
text=subprocess.check_output(['openssl','x509','-in',str(base/'pki/server/cert.pem'),'-noout','-text'],text=True)
assert 'DNS:rabbitwall.home.arpa' in text and 'IP Address:192.168.10.2' in text
assert 'bind 192.168.10.2' in (base/'bundle/Caddyfile').read_text()
assert not (base/'bundle/ca.key').exists()
"""
            subprocess.run([sys.executable,'-B','-c',code,str(ROOT/'tools'),str(config),str(base)],check=True)

if __name__=='__main__':unittest.main()
