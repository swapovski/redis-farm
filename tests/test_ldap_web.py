"""Real Flask test-client gate tests; fake LDAP identity and fake Redis transport.

Safe to run by installer on the panel server: no sockets to LDAP or Redis,
no production catalog access, all files in a disposable local temp directory.
"""
from dataclasses import replace
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock
from redis_farm.ldap_auth import Identity, LoginDenied, LoginUnavailable
from redis_farm.server import parse_config
from redis_farm.settings import Settings

@unittest.skipUnless(importlib.util.find_spec('flask'), 'Flask not installed; real API test is not executed')
class LDAPWebTests(unittest.TestCase):
    def setUp(self):
        from redis_farm.app import create_app
        from fixtures import FakeTopologyCLI
        self.tmp=tempfile.TemporaryDirectory()
        self.settings=replace(parse_config({'bind_host':'192.0.2.10','allowed_client_cidrs':['192.0.2.0/24','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8','::1/128']}).settings,data_dir=Path(self.tmp.name))
        self.auth=Mock()
        self.auth.authenticate.return_value=Identity('fixture','uid=fixture,dc=test')
        self.cli=FakeTopologyCLI()
        self.app=create_app(self.settings,cli=self.cli,authenticator=self.auth)
        self.client=self.app.test_client();self.base='https://192.0.2.10:8765'
        self.peer={'REMOTE_ADDR':'10.1.1.1'}
    def tearDown(self):
        self.app.extensions['redis_farm']['shutdown']();self.tmp.cleanup()
    def get(self,path,client=None,**kw):
        return (client or self.client).get(path,base_url=self.base,environ_overrides=self.peer,**kw)
    def post(self,path,client=None,**kw):
        return (client or self.client).post(path,base_url=self.base,environ_overrides=self.peer,**kw)
    def token(self,client=None):
        page=self.get('/login',client=client)
        self.assertEqual(page.status_code,200)
        return re.search(r'name="csrf" value="([^"]+)"',page.get_data(as_text=True)).group(1)
    def login(self,client=None):
        token=self.token(client)
        reply=self.post('/login',client=client,data={'csrf':token,'username':'fixture','password':'ldap-secret-fixture'})
        self.assertEqual(reply.status_code,302)
        self.last_login_cookie = reply.headers.get('Set-Cookie', '')
        return self.get('/api/bootstrap',client=client).get_json()
    def test_unauthenticated_index_redirect(self):
        r=self.get('/');self.assertEqual(r.status_code,302);self.assertEqual(r.location,'/login')
    def test_all_anonymous_apis_denied(self):
        for path in ('/api/bootstrap','/api/services','/api/catalog/export','/api/terminals','/api/jobs/x'):
            with self.subTest(path=path):
                r=self.get(path);self.assertEqual(r.status_code,401);self.assertEqual(r.get_json()['code'],'login_required')
    def test_health_public_and_no_sessions(self):
        self.assertEqual(self.get('/healthz').status_code,200)
        self.assertEqual(len(self.app.extensions['redis_farm']['sessions'].items),0)
        self.assertEqual(self.cli.calls,[])
    def test_login_and_cookie(self):
        boot=self.login();self.assertTrue(boot['panel_login']);self.assertEqual(boot['panel_user'],'fixture')
        r=self.get('/api/bootstrap');cookie=self.last_login_cookie
        self.assertNotIn('Set-Cookie',r.headers)
        for part in ('__Host-redis_farm_server_session=', 'Secure', 'HttpOnly', 'SameSite=Strict','Path=/'):self.assertIn(part,cookie)
        self.assertNotIn('ldap-secret-fixture',cookie);self.assertFalse(boot['redis_unlocked'])
        self.assertEqual(self.get('/').status_code,200)
    def test_ldap_password_not_in_identity_or_redis_credentials(self):
        self.login()
        sessions=self.app.extensions['redis_farm']['sessions'].items
        item=next(iter(sessions.values()))
        self.assertIsNone(item.password);self.assertFalse(item.defaults)
        self.assertNotIn('ldap-secret-fixture',repr(item))
    def test_invalid_csrf_never_authenticates(self):
        self.token()
        for token in ('bad', '\u0430\u0431'):
            r=self.post('/login',data={'csrf':token,'username':'fixture','password':'secret'})
            self.assertEqual(r.status_code,400)
        self.auth.authenticate.assert_not_called()
    def test_cross_site_login_denied(self):
        token=self.token()
        r=self.post('/login',data={'csrf':token,'username':'fixture','password':'secret'},headers={'Origin':'https://elsewhere.test'})
        self.assertEqual(r.status_code,403);self.auth.authenticate.assert_not_called()
    def test_denied_generic_message(self):
        self.auth.authenticate.side_effect=LoginDenied()
        token=self.token();r=self.post('/login',data={'csrf':token,'username':'fixture','password':'ldap-secret-fixture'})
        self.assertEqual(r.status_code,401);self.assertNotIn(b'ldap-secret-fixture',r.data)
        self.assertEqual(self.get('/api/bootstrap').status_code,401)
    def test_directory_unavailable_is_not_login(self):
        self.auth.authenticate.side_effect=LoginUnavailable()
        token=self.token();r=self.post('/login',data={'csrf':token,'username':'fixture','password':'secret'})
        self.assertEqual(r.status_code,503);self.assertEqual(self.get('/api/services').status_code,401)
    def test_session_id_rotated_and_old_credentials_removed(self):
        old=self.app.extensions['redis_farm']['sessions'].create()
        old.password='old-redis-secret'
        self.client.set_cookie('__Host-redis_farm_server_session',old.id,domain='192.0.2.10',secure=True)
        self.login()
        self.assertNotIn(old.id,self.app.extensions['redis_farm']['sessions'].items)
        self.assertIsNone(old.password)
    def test_logout_requires_csrf(self):
        self.login();self.assertEqual(self.post('/api/panel/logout',json={}).status_code,403)
        self.assertEqual(self.get('/api/bootstrap').status_code,200)
    def test_logout_removes_identity_credentials_and_access(self):
        boot=self.login();item=next(iter(self.app.extensions['redis_farm']['sessions'].items.values()))
        item.password='redis-fixture';item.defaults['service']={'service':'default-fixture','endpoints':{}}
        r=self.post('/api/panel/logout',json={},headers={'X-RF-CSRF':boot['csrf']})
        self.assertEqual(r.status_code,200);self.assertIsNone(item.password);self.assertFalse(item.defaults)
        self.assertEqual(self.get('/api/bootstrap').status_code,401)
    def test_expired_session_cannot_bootstrap_again(self):
        self.login();item=next(iter(self.app.extensions['redis_farm']['sessions'].items.values()))
        item.active-=self.settings.session_idle+1
        self.assertEqual(self.get('/api/bootstrap').status_code,401)
    def test_default_does_not_require_admin(self):
        self.login()
        self.assertFalse(self.get('/api/bootstrap').get_json()['redis_unlocked'])
        self.assertEqual(self.get('/api/services').status_code,200)
    def test_separate_browsers(self):
        boot=self.login();other=self.app.test_client()
        self.assertEqual(self.get('/api/bootstrap',client=other).status_code,401)
        self.login(other)
        self.post('/api/panel/logout',json={},headers={'X-RF-CSRF':boot['csrf']})
        self.assertEqual(self.get('/api/services',client=other).status_code,200)
    def test_mutating_api_retains_csrf(self):
        self.login()
        self.assertEqual(self.post('/api/session/unlock',json={'password':'redis-fixture'}).status_code,403)
    def test_local_mode_does_not_load_connector(self):
        from redis_farm.app import create_app
        from fixtures import FakeTopologyCLI
        with tempfile.TemporaryDirectory() as folder:
            local=create_app(Settings(Path(folder),ldap_connector=Path('/does-not-exist')),cli=FakeTopologyCLI())
            try:
                c=local.test_client()
                self.assertEqual(c.get('/',base_url='http://127.0.0.1:8765').status_code,200)
                self.assertEqual(c.get('/api/bootstrap',base_url='http://127.0.0.1:8765').status_code,200)
                self.assertEqual(c.get('/login',base_url='http://127.0.0.1:8765').status_code,404)
            finally:local.extensions['redis_farm']['shutdown']()

if __name__=='__main__':unittest.main()
