"""Version 0.2 localization, branding and portable-configuration regressions."""
import argparse
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from redis_farm.i18n import Translator,dictionaries,localize_errors
from redis_farm.settings import Settings
from redis_farm.install_wizard import first_server_config,choose_language,validate_admin_user,load_local,configure_local,set_language
from redis_farm.server import parse_config
from redis_farm.cli import RedisCLI
from redis_farm.models import Endpoint
from redis_farm.commands import snippets
ROOT=Path(__file__).resolve().parents[1]

def args(**kw):
    data=dict(language=None,panel_auth='none',allow_no_auth=True,admin_user=None,use_acl_user=None,
        host='192.0.2.10',port=8765,dns_server=None,system_dns=True,allow_cidr=['192.0.2.0/24'],
        redis_cli='/usr/bin/redis-cli',non_interactive=True)
    return argparse.Namespace(**(data|kw))

class UniversalRelease(unittest.TestCase):
    def setUp(self):set_language('en')
    def test_fresh_defaults_english_and_system_dns(self):
        result=first_server_config(args(),{},interactive=False,say=lambda _:None)
        self.assertEqual(result['language'],'en');self.assertEqual(result['dns_servers'],[])
        self.assertEqual(result['public_host'],'192.0.2.10');self.assertNotIn('public_origin',result)
    def test_explicit_russian(self):
        result=first_server_config(args(language='ru'),{},interactive=False,say=lambda _:None)
        self.assertEqual(result['language'],'ru')
    def test_old_server_stays_ru_ldap_and_admin(self):
        s=parse_config({}).settings
        self.assertEqual((s.language,s.panel_auth,s.redis_admin_user),('ru','ldap','admin'))
    def test_old_local_stays_ru_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'local.json';original='{"dns_servers": [], "cli_path": "/usr/bin/redis-cli"}\n';p.write_text(original)
            a=args(panel_auth=None,allow_cidr=None);a.config=p
            configure_local(a,say=lambda _:None,ask=Mock(side_effect=AssertionError('no prompt')))
            self.assertEqual(p.read_text(),original)
    def test_noauth_requires_explicit_acceptance(self):
        with self.assertRaisesRegex(ValueError,'NO_AUTH'):
            first_server_config(args(allow_no_auth=False),{},interactive=False,say=lambda _:None)
    def test_named_user_or_default_selection(self):
        r=first_server_config(args(use_acl_user='no'),{},interactive=False,say=lambda _:None)
        self.assertFalse(r['use_acl_user']);self.assertEqual(r['redis_admin_user'],'admin')
        r=first_server_config(args(admin_user='ops',use_acl_user='yes'),{},interactive=False,say=lambda _:None)
        self.assertEqual(r['redis_admin_user'],'ops')
    def test_new_domains_are_not_baked_in(self):
        r=first_server_config(args(),{'public_origin':'https://old.example.net','trusted_proxy_ips':['192.0.2.99']},interactive=False,say=lambda _:None)
        self.assertNotIn('public_origin',r);self.assertNotIn('trusted_proxy_ips',r)
    def test_cli_profile_id_stays_admin_but_argument_is_configured_user(self):
        c=RedisCLI(Settings(Path('/unused'),cli_path=str(ROOT/'tests/fake_cli.py'),redis_admin_user='operator name'))
        try:
            a=c.arguments(Endpoint('127.0.0.1',6379,'redis','admin'))
            self.assertEqual(a[a.index('--user')+1],'operator name')
            b=c.arguments(Endpoint('127.0.0.1',6379,'redis','default'));self.assertNotIn('--user',b)
            sent=c.arguments(Endpoint('127.0.0.1',26379,'sentinel','none'));self.assertNotIn('--user',sent)
        finally:c.shutdown()
    def test_invalid_named_users(self):
        for user in ('','default','\x00bad',' leading','trailing ','x'*129):
            with self.subTest(user=repr(user)),self.assertRaises(ValueError):validate_admin_user(user)
    def test_translations_have_two_complete_languages(self):
        messages,errors=dictionaries()
        self.assertGreater(len(messages),350)
        for name,entries in [('messages',messages),('errors',errors)]:
            for key,values in entries.items():
                with self.subTest(name=name,key=key):self.assertEqual(set(values),{'en','ru'});self.assertTrue(all(isinstance(v,str) and v for v in values.values()))
    def test_template_placeholders_preserved(self):
        import re
        messages,_=dictionaries()
        for key,values in messages.items():
            with self.subTest(key=key):self.assertEqual(sorted(re.findall(r'\{\d+\}',values['en'])),sorted(re.findall(r'\{\d+\}',values['ru'])))
    def test_ru_password_dialog_error(self):
        t=Translator('ru')
        self.assertEqual(t.error('Enter the password.','invalid_password'),'\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043f\u0430\u0440\u043e\u043b\u044c.')
    def test_raw_errors_not_translated(self):
        t=Translator('ru');self.assertEqual(t.error('WRONGPASS invalid username-password pair','redis_error'),'WRONGPASS invalid username-password pair')
        self.assertEqual(localize_errors({'output':'Enter the password.','info':{'error':'test'}},t),{'output':'Enter the password.','info':{'error':'test'}})
    def test_assets_present_in_clean_distribution(self):
        for name in ('logo.png','add.png','setup.webp','favicon.png','favicon.svg','favicon.ico'):
            p=ROOT/'redis_farm/static'/name
            with self.subTest(name=name):self.assertTrue(p.is_file());self.assertGreater(p.stat().st_size,100)
    def test_primary_png_dimensions(self):
        import struct
        for name,expected in [('logo.png',(159,287)),('add.png',(195,208))]:
            b=(ROOT/'redis_farm/static'/name).read_bytes();self.assertEqual(b[:8],b'\x89PNG\r\n\x1a\n');self.assertEqual(struct.unpack('!II',b[16:24]),expected)
    def test_snapshot_password_error_localized(self):
        raw={'nodes':[{'error':'Enter the default password for this service or instance.','error_code':'default_password_required','info':{'server':'value'}}]}
        fixed=localize_errors(raw,Translator('ru'));self.assertNotEqual(fixed['nodes'][0]['error'],raw['nodes'][0]['error']);self.assertEqual(fixed['nodes'][0]['info'],raw['nodes'][0]['info'])
    def test_user_substitution_does_not_touch_arbitrary_data(self):
        t=Translator('en','ops');self.assertEqual(t('example-admin-key'),'example-admin-key')
    def test_default_language_settings_class(self):self.assertEqual(Settings(Path('/unused')).language,'en')
    def test_server_example_is_generic(self):
        data=json.loads((ROOT/'deploy/ubuntu/server.example.json').read_text());s=parse_config(data).settings
        self.assertEqual(s.language,'en');self.assertFalse(s.dns_servers)

if __name__=='__main__':unittest.main()
