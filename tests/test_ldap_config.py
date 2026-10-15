"""Local schema/wizard regressions. No directory or user account is contacted."""
from dataclasses import replace
from pathlib import Path
import ast
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from redis_farm.ldap_config import *
from redis_farm.ldap_setup import prepare_connector, save_connector

class LDAPConfigTests(unittest.TestCase):
    def config(self, **overrides):
        return dict(WIZARD_DEFAULTS, **overrides)

    def test_secure_default_and_group(self):
        cfg=parse_ldap_config(self.config(CA_CERT_FILE=None))
        self.assertEqual(cfg.transport, 'starttls')
        self.assertEqual(cfg.host, 'ldap.example.net')
        self.assertIn('redis_farm_users', cfg.require_group)

    def test_ldaps_inferred(self):
        self.assertEqual(parse_ldap_config(self.config(URL='ldaps://ldap.test', CA_CERT_FILE=None)).port, 636)

    def test_plain_requires_explicit_consent(self):
        with self.assertRaises(LDAPConfigurationError):
            parse_ldap_config(self.config(START_TLS=False))
        self.assertEqual(parse_ldap_config(self.config(TLS_MODE='plain', ALLOW_PLAINTEXT=True)).transport, 'plain')

    def test_certificate_verification_cannot_be_disabled(self):
        with self.assertRaises(LDAPConfigurationError):
            parse_ldap_config(self.config(VERIFY_CERTIFICATE=False))

    def test_bad_modes_urls_and_types(self):
        variants=[{'URL': 'http://ldap.test'}, {'URL': 'ldap://u:p@ldap.test'},
                  {'URL': 'ldap://ldap.test:0'}, {'URL': 'ldap://ldap.test:65536'},
                  {'URL': 'ldap://ldap.test/dc=test'}, {'URL': 'ldap://ldap.test?q=1'},
                  {'TLS_MODE':'unsafe'}, {'TLS_MODE':'ldaps'}, {'USER_LOGIN_ATTR':'uid)(*'},
                  {'START_TLS':'false'}, {'CONNECT_TIMEOUT':True}, {'RECEIVE_TIMEOUT':0},
                  {'GROUP_BASE_DN':'bad'}, {'REQUIRE_GROUP':'cn=g,dc=else'},
                  {'CA_CERT_FILE':'relative'}, {'CA_CERT_FILE':False}, {'AUTH_MODE':'service'}]
        for item in variants:
            with self.subTest(item=item), self.assertRaises(LDAPConfigurationError):
                parse_ldap_config(self.config(**item), check_ca=False)

    def test_bad_ca_fails_locally(self):
        with self.assertRaises(LDAPConfigurationError):
            parse_ldap_config(self.config(CA_CERT_FILE='/no-such-redis-farm-ca.pem'))

    def test_missing_fields_are_named_not_values(self):
        raw=self.config(BASE_DN='')
        self.assertEqual(missing_fields(raw), ['BASE_DN'])
        with self.assertRaisesRegex(LDAPConfigurationError, 'BASE_DN'):
            parse_ldap_config(raw)

    def test_service_bind_ignored(self):
        cfg=parse_ldap_config(self.config(BIND_DN='ignored', BIND_PASSWORD='do-not-use', FLASK_SECRET_KEY='also-ignored'))
        self.assertNotIn('do-not-use', repr(cfg))

class ConnectorWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'connectors/connector_settings.py'
        self.path.parent.mkdir()
    def tearDown(self): self.tmp.cleanup()
    def write(self, raw, prefix=''):
        text=prefix+'LDAP_CONFIG = '+repr(raw)+'\n'
        self.path.write_text(text)
        return text
    def test_complete_no_prompt_no_rewrite(self):
        original=self.write(dict(WIZARD_DEFAULTS), '# Keep exactly this comment\n')
        ask=Mock(side_effect=AssertionError('must not ask'))
        plan=prepare_connector(self.path, interactive=False, ask=ask, say=lambda s:None)
        self.assertFalse(plan.changed); ask.assert_not_called()
        save_connector(plan, os.getgid(), say=lambda s:None)
        self.assertEqual(self.path.read_text(),original)
        self.assertEqual(self.path.stat().st_mode&0o777,0o640)
    def test_unrelated_code_is_not_executed(self):
        marker=Path(self.tmp.name)/'must-not-exist'
        original=self.write(dict(WIZARD_DEFAULTS), f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
        plan=prepare_connector(self.path,interactive=False,say=lambda s:None)
        self.assertFalse(marker.exists()); self.assertEqual(plan.text,original)
    def test_dynamic_config_rejected(self):
        self.path.write_text('LDAP_CONFIG = dict(URL="ldap://test")\n')
        with self.assertRaises(LDAPConfigurationError): read_connector(self.path)
    def test_duplicate_assignment_rejected(self):
        self.path.write_text('LDAP_CONFIG = {}\nLDAP_CONFIG = {}\n')
        with self.assertRaises(LDAPConfigurationError): read_connector(self.path)
    def test_syntax_error_not_overwritten(self):
        self.path.write_text('LDAP_CONFIG = {')
        with self.assertRaises(LDAPConfigurationError): prepare_connector(self.path)
        self.assertEqual(self.path.read_text(),'LDAP_CONFIG = {')
    def test_missing_only_one_question(self):
        raw=dict(WIZARD_DEFAULTS);raw['URL']=''
        self.write(raw, 'SMTP_PASSWORD = "not-used"\n')
        asks=[]
        plan=prepare_connector(self.path,interactive=True,ask=lambda q:asks.append(q) or '',say=lambda s:None)
        self.assertEqual(len(asks),1);self.assertIn('URL',asks[0]);self.assertIn('SMTP_PASSWORD',plan.text)
        backup=save_connector(plan,os.getgid(),say=lambda s:None)
        self.assertTrue(backup.exists());self.assertEqual(backup.stat().st_mode&0o777,0o640)
    def test_fresh_prompts_and_defaults(self):
        asks=[]
        plan=prepare_connector(self.path,interactive=True,ask=lambda q:asks.append(q) or '',say=lambda s:None)
        self.assertEqual(len(asks),7)
        self.assertEqual(plan.raw['TLS_MODE'],'starttls')
        save_connector(plan,os.getgid(),say=lambda s:None)
        self.assertEqual(load_ldap_config(self.path,check_ca=False).host,'ldap.example.net')
    def test_fresh_plain_requires_confirmation(self):
        answers=iter(['']*5+['plain','ALLOW_PLAINTEXT'])
        plan=prepare_connector(self.path,interactive=True,ask=lambda _:next(answers),say=lambda _:None)
        self.assertEqual(plan.raw['TLS_MODE'],'plain');self.assertTrue(plan.raw['ALLOW_PLAINTEXT'])
        self.assertNotIn('CA_CERT_FILE',plan.raw)
    def test_fresh_plain_declined_is_not_written(self):
        answers=iter(['']*5+['plain','no'])
        with self.assertRaises(LDAPConfigurationError):
            prepare_connector(self.path,interactive=True,ask=lambda _:next(answers),say=lambda _:None)
        self.assertFalse(self.path.exists())
    def test_noninteractive_missing_fails_before_create(self):
        with self.assertRaises(LDAPConfigurationError):
            prepare_connector(self.path,interactive=False,ask=Mock(side_effect=AssertionError()))
        self.assertFalse(self.path.exists())
    def test_cancel_does_not_write(self):
        with self.assertRaises(LDAPConfigurationError):
            prepare_connector(self.path,interactive=True,ask=Mock(side_effect=EOFError))
        self.assertFalse(self.path.exists())
    def test_changed_source_not_overwritten(self):
        self.write(dict(WIZARD_DEFAULTS, URL=''))
        plan=prepare_connector(self.path,interactive=True,ask=lambda q:'',say=lambda s:None)
        self.path.write_text('# external edit\n')
        with self.assertRaises(LDAPConfigurationError): save_connector(plan,os.getgid())
        self.assertEqual(self.path.read_text(),'# external edit\n')
    def test_existing_complete_with_secrets_not_logged(self):
        self.write(dict(WIZARD_DEFAULTS,BIND_PASSWORD='sensitive-fixture'))
        lines=[]
        prepare_connector(self.path,interactive=False,say=lines.append)
        self.assertNotIn('sensitive-fixture',''.join(lines))
    def test_symlink_is_rejected(self):
        target=Path(self.tmp.name)/'original';target.write_text('LDAP_CONFIG = {}')
        self.path.symlink_to(target)
        with self.assertRaises(LDAPConfigurationError): read_connector(self.path)
    def test_example_can_be_used_without_questions(self):
        example=Path(__file__).resolve().parents[1]/'deploy/ubuntu/connector_settings.example.py'
        self.path.write_bytes(example.read_bytes())
        plan=prepare_connector(self.path,interactive=False,ask=Mock(side_effect=AssertionError()),say=lambda s:None)
        self.assertFalse(plan.changed)

if __name__=='__main__': unittest.main()
