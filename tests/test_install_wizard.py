"""First-install wizard tests; no network, package manager or systemd."""
import argparse
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from redis_farm.install_wizard import choose_dns, first_server_config, parse_dns, configure_local, load_local, TEXT

def args(**kw):
    return argparse.Namespace(**dict(dict(host='192.0.2.10',port=8765,dns_server=None,system_dns=False,
       allow_cidr=['192.0.2.0/24'],redis_cli='/usr/bin/redis-cli',non_interactive=False),**kw))

class WizardTests(unittest.TestCase):
    def setUp(self):
        from redis_farm.install_wizard import set_language
        set_language('en')
    def test_dns_help_explains_bind_route_and_fallback(self):
        text=TEXT['dns_help']['en'];self.assertIn('bind',text);self.assertIn('redis-cli',text)
        self.assertIn('split DNS',text);self.assertIn('VPN',text)
    def test_empty_dns_uses_system(self):
        ask=Mock(return_value='');say=Mock()
        self.assertEqual(choose_dns(args(),interactive=True,ask=ask,say=say),[]);say.assert_called_once()
    def test_multiple_servers_validated_and_deduplicated(self):
        self.assertEqual(parse_dns('192.0.2.53, 192.0.2.54 192.0.2.53'),['192.0.2.53','192.0.2.54'])
    def test_no_hostname_or_multicast_as_dns(self):
        for value in ('dns.example.net','0.0.0.0','224.0.0.1','https://192.0.2.1'):
            with self.subTest(value=value),self.assertRaises(ValueError):parse_dns(value)
    def test_explicit_dns_never_prompts(self):
        no=Mock(side_effect=AssertionError('must not ask'))
        self.assertEqual(choose_dns(args(dns_server=['192.0.2.53']),interactive=False,ask=no),['192.0.2.53'])
    def test_explicit_system_never_prompts(self):
        no=Mock(side_effect=AssertionError('must not ask'))
        self.assertEqual(choose_dns(args(system_dns=True),interactive=False,ask=no),[])
    def test_noninteractive_requires_explicit_dns_choice(self):
        with self.assertRaisesRegex(ValueError,'--dns-server'):
            choose_dns(args(),interactive=False)
    def test_server_preserves_unrelated_template_fields(self):
        result=first_server_config(args(system_dns=True),{'threads':32,'data_dir':'/var/lib/redis-farm'},interactive=False)
        self.assertEqual(result['threads'],32);self.assertEqual(result['dns_servers'],[])
        self.assertEqual(result['bind_host'],'192.0.2.10');self.assertIn('192.0.2.10/32',result['allowed_client_cidrs'])
    def test_server_all_first_run_inputs(self):
        ask=Mock(side_effect=['en','192.0.2.10','8766','192.0.2.53','192.0.2.0/24','/opt/redis-farm/bin/redis-cli','yes','ops-admin','ldap'])
        result=first_server_config(args(host=None,port=None,allow_cidr=None,redis_cli=None),{},interactive=True,ask=ask,say=lambda _:None)
        self.assertEqual(result['port'],8766);self.assertEqual(result['dns_servers'],['192.0.2.53'])
        self.assertEqual(ask.call_count,9); self.assertEqual(result['redis_admin_user'],'ops-admin')
    def test_no_wildcard_bind(self):
        with self.assertRaises(ValueError):first_server_config(args(host='0.0.0.0',system_dns=True),{},interactive=False)
    def test_port_range(self):
        for port in (0,443,99999):
            with self.subTest(port=port),self.assertRaises(ValueError):first_server_config(args(port=port,system_dns=True),{},interactive=False)
    def test_no_unrestricted_network_default(self):
        with self.assertRaisesRegex(ValueError,'--allow-cidr'):
            first_server_config(args(system_dns=True,allow_cidr=None),{},interactive=False)
    def test_local_creates_only_dns_and_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'local.json';a=args(system_dns=True,non_interactive=True);a.config=file
            configure_local(a,say=lambda _:None)
            self.assertEqual(load_local(file),{'dns_servers':[],'cli_path':'/usr/bin/redis-cli','language':'en','use_acl_user':True,'redis_admin_user':'admin'})
            self.assertEqual(file.stat().st_mode&0o777,0o600)
    def test_existing_local_configuration_no_questions_no_write(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'local.json';raw='{ "dns_servers": ["192.0.2.53"], "cli_path": "/usr/bin/redis-cli" }\n';file.write_text(raw)
            a=args();a.config=file;configure_local(a,ask=Mock(side_effect=AssertionError('must not ask')),say=lambda _:None)
            self.assertEqual(file.read_text(),raw)
    def test_local_rejects_secret_or_ldap_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'local.json';file.write_text('{"password":"not-valid"}')
            with self.assertRaises(ValueError):load_local(file)
    def test_local_never_imports_ldap(self):
        import inspect,redis_farm.install_wizard as w
        self.assertNotIn('import ldap',inspect.getsource(w))
    def test_local_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'target';target.write_text('{}');link=Path(temp)/'local.json';link.symlink_to(target)
            with self.assertRaises(ValueError):load_local(link)
    def test_interrupted_prompt_returns_error(self):
        with self.assertRaisesRegex(ValueError,'interrupted'):
            choose_dns(args(),interactive=True,ask=Mock(side_effect=EOFError),say=lambda _:None)

if __name__=='__main__':unittest.main()
