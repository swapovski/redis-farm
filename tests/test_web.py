"""Flask integration tests. Only local fixtures; no remote Redis connections."""
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from redis_farm.models import Endpoint
from redis_farm.settings import Settings
from fixtures import FakeTopologyCLI

HAS_FLASK = importlib.util.find_spec('flask') is not None


@unittest.skipUnless(HAS_FLASK, 'Install requirements.txt to run Flask integration tests.')
class FlaskTests(unittest.TestCase):
    def setUp(self):
        from redis_farm.app import create_app
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Settings(Path(self.tmp.name)),cli=FakeTopologyCLI())
        self.client=self.app.test_client()
        boot=self.client.get('/api/bootstrap')
        self.csrf=boot.get_json()['csrf']
        self.headers={'X-RF-CSRF':self.csrf}

    def tearDown(self):
        self.app.extensions['redis_farm']['shutdown']()
        self.tmp.cleanup()

    def post(self,path,data):
        return self.client.post(path,json=data,headers=self.headers)

    def test_index_has_no_account_login(self):
        result=self.client.get('/')
        self.assertEqual(result.status_code,200)
        self.assertIn(b'credentialForm',result.data)
        self.assertEqual(self.client.get('/login').status_code,404)

    def test_csrf_is_required(self):
        self.assertEqual(self.client.post('/api/session/unlock',json={'password':'p'}).status_code,403)

    def test_dns_rebinding_host_and_foreign_origin_rejected(self):
        result=self.client.get('/api/bootstrap',headers={'Host':'evil.example'})
        self.assertEqual(result.status_code,403)
        result=self.client.post('/api/session/unlock',json={'password':'p'},headers={**self.headers,'Origin':'https://evil.example'})
        self.assertEqual(result.status_code,403)

    def test_password_is_not_in_cookie_or_catalog(self):
        result=self.post('/api/session/unlock',{'password':'fixture-password'})
        self.assertEqual(result.status_code,200)
        self.assertNotIn('fixture-password',str(result.headers))
        self.assertNotIn(b'fixture-password',self.client.get('/api/catalog/export').data)
        self.assertEqual(self.post('/api/session/lock',{}).status_code,200)
        self.assertFalse(self.client.get('/api/bootstrap').get_json()['redis_unlocked'])

    def test_discovery_job_and_catalog(self):
        self.post('/api/session/unlock',{'password':'fixture-password'})
        response=self.post('/api/discover',{'host':'redis-b.example','ports':'6389'})
        self.assertEqual(response.status_code,202)
        jid=response.get_json()['job_id']
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=self.client.get('/api/jobs/'+jid).get_json()
            if result['state'] in ('done','error'):break
            time.sleep(.01)
        self.assertEqual(result['state'],'done',result)
        values=[item['service'] for item in result['result']['services']]
        save=self.post('/api/services',{'services':values})
        self.assertEqual(save.status_code,200)
        self.assertEqual(len(self.client.get('/api/services').get_json()['services']),1)

    def test_sessions_do_not_share_credentials(self):
        self.post('/api/session/unlock',{'password':'fixture-password'})
        other=self.app.test_client()
        self.assertFalse(other.get('/api/bootstrap').get_json()['redis_unlocked'])

    def test_terminal_inventory_initially_empty(self):
        self.assertEqual(self.client.get('/api/terminals').get_json(),{'terminals':[]})

    def test_expired_cookie_is_not_sufficient_for_api(self):
        sessions=self.app.extensions['redis_farm']['sessions']
        for item in sessions.items.values():item.active-=4000
        self.assertEqual(self.client.get('/api/services').status_code,401)


@unittest.skipUnless(HAS_FLASK, 'Install requirements.txt to run Flask integration tests.')
class AutoScanFlaskTests(unittest.TestCase):
    def setUp(self):
        from redis_farm.app import create_app
        from test_discovery_auto import ScanFixtureCLI
        self.tmp=tempfile.TemporaryDirectory();self.cli=ScanFixtureCLI()
        self.app=create_app(Settings(Path(self.tmp.name),scan_redis_end=6383,scan_sentinel_end=26383),cli=self.cli)
        self.client=self.app.test_client();boot=self.client.get('/api/bootstrap').get_json()
        self.headers={'X-RF-CSRF':boot['csrf']};self.post('/api/session/unlock',{'password':'fixture-password'})
    def tearDown(self):self.app.extensions['redis_farm']['shutdown']();self.tmp.cleanup()
    def post(self,path,data):return self.client.post(path,json=data,headers=self.headers)
    def scan(self,spec):
        response=self.post('/api/discover',spec);self.assertEqual(response.status_code,202,response.get_json())
        jid=response.get_json()['job_id'];deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=self.client.get('/api/jobs/'+jid).get_json()
            if job['state'] in ('done','error','cancelled'):break
            time.sleep(.01)
        self.assertEqual(job['state'],'done',job);return job
    def test_hostname_only_auto_adds_and_exposes_progress(self):
        job=self.scan({'host':'seed.example'});self.assertEqual(job['progress']['completed'],10)
        self.assertTrue(job['result']['auto_added']);self.assertEqual(len(self.client.get('/api/services').get_json()['services']),3)
    def test_manual_port_keeps_preview_without_save(self):
        job=self.scan({'host':'seed.example','ports':'6379'});self.assertFalse(job['result']['auto_added'])
        self.assertEqual(len(job['result']['services']),1);self.assertEqual(self.client.get('/api/services').get_json()['services'],[])
    def test_preview_opt_out_then_save(self):
        job=self.scan({'host':'seed.example','auto_add':False});self.assertEqual(self.client.get('/api/services').get_json()['services'],[])
        response=self.post('/api/services',{'services':[s['service'] for s in job['result']['services']]})
        self.assertEqual(response.status_code,200);self.assertEqual(len(response.get_json()['services']),3)
    def test_invalid_range_creates_no_requests(self):
        self.assertEqual(self.post('/api/discover',{'host':'seed.example','redis_end':1}).status_code,400)
        self.assertEqual(self.cli.calls,[])


if __name__=='__main__':unittest.main()
