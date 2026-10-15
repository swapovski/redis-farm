"""Installer HTTP gates. Real Flask test client; fake identities/CLI; no network.

All temp catalogs are created under a temporary directory. Nothing reads deployment
connectors or Redis data. The installer runs this after installing dependencies.
"""
from dataclasses import replace
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from redis_farm.settings import Settings
from redis_farm.ldap_auth import Identity

@unittest.skipUnless(importlib.util.find_spec('flask'), 'Flask is required for real HTTP gate tests')
class Access020(unittest.TestCase):
    def setUp(self):
        from redis_farm.app import create_app
        from fixtures import FakeTopologyCLI
        self.temp=tempfile.TemporaryDirectory()
        self.apps=[]
        self.base='https://127.0.0.1:8765'
        self.peer={'REMOTE_ADDR':'127.0.0.1'}
        self.settings=Settings(Path(self.temp.name),server_mode=True, panel_auth='none',
            allowed_hosts=('127.0.0.1',),allowed_client_cidrs=('127.0.0.0/8',),language='en')
        self.create_app=create_app;self.CLI=FakeTopologyCLI
    def tearDown(self):
        for app in self.apps: app.extensions['redis_farm']['shutdown']()
        self.temp.cleanup()
    def make(self,**values):
        auth=Mock();auth.authenticate.return_value=Identity('demo','uid=demo,dc=example,dc=net')
        with patch('redis_farm.ldap_config.load_ldap_config',side_effect=AssertionError('must not read real connectors')):
            app=self.create_app(replace(self.settings,**values),cli=self.CLI(),authenticator=auth)
        self.apps.append(app)
        return app,app.test_client(),auth
    def get(self,c,path,**kw):return c.get(path,base_url=self.base,environ_overrides=self.peer,**kw)
    def post(self,c,path,**kw):return c.post(path,base_url=self.base,environ_overrides=self.peer,**kw)
    def login(self,c):
        page=self.get(c,'/login');self.assertEqual(page.status_code,200)
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.get_data(as_text=True))[1]
        reply=self.post(c,'/login',data={'csrf':csrf,'username':'demo','password':'test-only'})
        self.assertEqual(reply.status_code,302)
    def test_noauth_index_and_bootstrap(self):
        app,c,auth=self.make()
        self.assertEqual(self.get(c,'/').status_code,200)
        boot=self.get(c,'/api/bootstrap').get_json()
        self.assertFalse(boot['panel_login']);self.assertEqual(boot['language'],'en')
        self.assertEqual(boot['admin_user'],'admin');auth.authenticate.assert_not_called()
    def test_noauth_login_does_not_exist(self):
        _,c,_=self.make();self.assertEqual(self.get(c,'/login').status_code,404)
    def test_noauth_requires_csrf_for_writes(self):
        _,c,_=self.make();self.get(c,'/api/bootstrap')
        r=self.post(c,'/api/session/unlock',json={'password':'unused'})
        self.assertEqual(r.status_code,403);self.assertEqual(r.get_json()['code'],'csrf')
    def test_noauth_denies_wrong_origin(self):
        _,c,_=self.make();boot=self.get(c,'/api/bootstrap').get_json()
        r=self.post(c,'/api/session/unlock',json={'password':'unused'},headers={'Origin':'https://other.example.net','X-RF-CSRF':boot['csrf']})
        self.assertEqual(r.status_code,403);self.assertEqual(r.get_json()['code'],'origin')
    def test_noauth_respects_allowed_host(self):
        _,c,_=self.make();r=c.get('/',base_url='https://other.example.net:8765',environ_overrides=self.peer)
        self.assertEqual(r.status_code,403)
    def test_ru_password_error(self):
        _,c,_=self.make(language='ru');boot=self.get(c,'/api/bootstrap').get_json()
        r=self.post(c,'/api/session/unlock',json={'password':''},headers={'X-RF-CSRF':boot['csrf']})
        self.assertEqual(r.status_code,400)
        self.assertEqual(r.get_json()['error'],'\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043f\u0430\u0440\u043e\u043b\u044c.')
    def test_custom_acl_username_in_bootstrap_and_template(self):
        _,c,_=self.make(redis_admin_user='operations-admin')
        page=self.get(c,'/').get_data(as_text=True)
        self.assertIn('operations-admin',page)
        self.assertEqual(self.get(c,'/api/bootstrap').get_json()['admin_user'],'operations-admin')
    def test_language_headers(self):
        _,c,_=self.make(language='ru');r=self.get(c,'/')
        self.assertEqual(r.headers['Content-Language'],'ru');self.assertIn('lang="ru"',r.get_data(as_text=True))
    def test_ldap_protects_index_and_all_api_paths(self):
        _,c,auth=self.make(panel_auth='ldap')
        self.assertEqual(self.get(c,'/').status_code,302)
        for path in ('/api/bootstrap','/api/services','/api/catalog/export','/api/terminals','/api/jobs/example','/api/services/example/topology'):
            with self.subTest(path=path):self.assertEqual(self.get(c,path).status_code,401)
        auth.authenticate.assert_not_called()
    def test_ldap_login_and_logout(self):
        app,c,auth=self.make(panel_auth='ldap');self.login(c)
        boot=self.get(c,'/api/bootstrap').get_json();self.assertTrue(boot['panel_login'])
        self.assertEqual(boot['panel_user'],'demo');self.assertFalse(boot['redis_unlocked'])
        self.assertEqual(self.post(c,'/api/panel/logout',json={},headers={'X-RF-CSRF':boot['csrf']}).status_code,200)
        self.assertEqual(self.get(c,'/api/services').status_code,401)
    def test_ldap_invalid_csrf_never_binds(self):
        _,c,auth=self.make(panel_auth='ldap');self.get(c,'/login')
        r=self.post(c,'/login',data={'csrf':'invalid','username':'demo','password':'test-only'})
        self.assertEqual(r.status_code,400);auth.authenticate.assert_not_called()
    def test_two_login_tabs_share_valid_csrf(self):
        _,c,_=self.make(panel_auth='ldap')
        def token():return re.search(r'name="csrf" value="([^"]+)"',self.get(c,'/login').get_data(as_text=True))[1]
        self.assertEqual(token(),token())
    def test_local_ignores_ldap_path(self):
        app,c,auth=self.make(server_mode=False,ldap_connector=Path('/missing'),allowed_hosts=(),allowed_client_cidrs=())
        self.assertEqual(c.get('/',base_url='http://127.0.0.1:8765').status_code,200)
        self.assertFalse(c.get('/api/bootstrap',base_url='http://127.0.0.1:8765').get_json()['panel_login'])
        auth.authenticate.assert_not_called()
    def test_session_secret_never_in_cookie(self):
        app,c,_=self.make();boot=self.get(c,'/api/bootstrap').get_json()
        r=self.post(c,'/api/session/unlock',json={'password':'secret-not-in-output'},headers={'X-RF-CSRF':boot['csrf']})
        self.assertEqual(r.status_code,200)
        self.assertNotIn('secret-not-in-output',str(r.headers));self.assertNotIn('secret-not-in-output',r.get_data(as_text=True))
    def test_raw_redis_data_is_not_translated(self):
        from redis_farm.i18n import Translator,localize_errors
        raw={'output':'NOAUTH Authentication required.','info':{'error':'Enter the password.'}}
        self.assertEqual(raw,localize_errors(raw,Translator('ru')))

if __name__=='__main__':unittest.main()
