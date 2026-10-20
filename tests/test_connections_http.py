"""Real Flask test client, fake CLI; skipped explicitly when Flask is absent."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from redis_farm.models import Endpoint, connection_profile
from redis_farm.settings import Settings

@unittest.skipUnless(importlib.util.find_spec('flask'), 'Flask is required for HTTP connection-profile tests')
class ConnectionHTTP(unittest.TestCase):
    def setUp(self):
        from redis_farm.app import create_app
        from fixtures import FakeTopologyCLI
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Settings(Path(self.tmp.name),language='en'),cli=FakeTopologyCLI())
        self.c=self.app.test_client();self.base='http://127.0.0.1:8765'
        self.boot=self.c.get('/api/bootstrap',base_url=self.base).get_json()
        self.h={'X-RF-CSRF':self.boot['csrf']}
        self.ext=self.app.extensions['redis_farm']
        r=Endpoint('192.0.2.11',6379);s=Endpoint('192.0.2.21',26379,'sentinel')
        self.redis,self.sentinel=r,s
        self.service=self.ext['inventory'].save({'name':'fixture','mode':'sentinel','seed':r.to_dict(),'endpoints':[r.to_dict(),s.to_dict()]})
        self.sid=self.service['id'];self.path='/api/services/'+self.sid+'/connection'
    def tearDown(self):
        self.ext['shutdown']();self.tmp.cleanup()
    def post(self,data,**kw):return self.c.post(self.path,json=data,base_url=self.base,headers=kw.pop('headers',self.h),**kw)
    def test_config_only_keeps_ids(self):
        r=self.post({'kind':'sentinel','connection':{'auth':'user','username':'sent-ops'}})
        self.assertEqual(r.status_code,200,r.get_data(as_text=True))
        service=r.get_json()['service'];self.assertEqual({e['id'] for e in service['endpoints']},{self.redis.id,self.sentinel.id})
        self.assertEqual(service['sentinel_connection']['username'],'sent-ops')
        self.assertFalse(r.get_json()['credentials']['profiles']['sentinel']['service_password'])
    def test_no_secret_in_reply_disk_export(self):
        secret='unique_test_secret_123456'
        r=self.post({'kind':'sentinel','connection':{'auth':'default'},'password':secret})
        self.assertEqual(r.status_code,200,r.get_data(as_text=True))
        self.assertNotIn(secret,r.get_data(as_text=True));self.assertNotIn(secret,str(r.headers))
        self.assertNotIn(secret,json.dumps(self.ext['inventory'].export()))
        for f in Path(self.tmp.name).glob('*.sqlite3*'):
            self.assertNotIn(secret.encode(),f.read_bytes())
    def test_named_service_separate_from_global(self):
        r=self.post({'kind':'redis','connection':{'auth':'user','username':'operations'},'password':'specific-fixture'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.get_json()['service']['auth_mode'],'user')
        self.assertFalse(self.c.get('/api/bootstrap',base_url=self.base).get_json()['redis_unlocked'])
    def test_sentinel_global_rejected_without_writes(self):
        before=self.ext['inventory'].get(self.sid)
        self.assertEqual(self.post({'kind':'sentinel','connection':{'auth':'admin'}}).status_code,400)
        self.assertEqual(self.ext['inventory'].get(self.sid),before)
    def test_bad_kind_rejected(self):
        self.assertEqual(self.post({'kind':'other','connection':{'auth':'none'}}).status_code,400)
    def test_mtls_pair_required(self):
        self.assertEqual(self.post({'kind':'redis','connection':{'auth':'none','tls':True,'certfile':'/c.crt'}}).status_code,400)
    def test_no_csrf_rejected(self):
        self.assertEqual(self.post({'kind':'redis','connection':{'auth':'none'}},headers={}).status_code,403)
    def test_foreign_origin_rejected(self):
        self.assertEqual(self.post({'kind':'redis','connection':{'auth':'none'}},headers={**self.h,'Origin':'https://other.example.net'}).status_code,403)
    def test_unknown_endpoint_rejected(self):
        r=self.post({'kind':'redis','scope':'endpoint','endpoint_id':'nonexistent','connection':{'auth':'default'}})
        self.assertEqual(r.status_code,400)
    def test_endpoint_override_survives_service_edit(self):
        r=self.post({'kind':'redis','scope':'endpoint','endpoint_id':self.redis.id,'connection':{'auth':'user','username':'special'}})
        self.assertEqual(r.status_code,200)
        r=self.post({'kind':'redis','connection':{'auth':'user','username':'common'}})
        self.assertEqual(r.status_code,200)
        ep=next(e for e in r.get_json()['service']['endpoints'] if e['id']==self.redis.id)
        self.assertEqual(ep['username'],'special')
    def test_other_browser_does_not_share_secret(self):
        self.post({'kind':'sentinel','connection':{'auth':'default'},'password':'first-session-only'})
        other=self.app.test_client();other.get('/api/bootstrap',base_url=self.base)
        data=other.get('/api/services',base_url=self.base).get_json()
        entry=next(s for s in data['services'] if s['id']==self.sid)
        self.assertNotIn(self.sentinel.id,entry['credentials']['unlocked_endpoints'])
    def test_disconnect_forgets_only_sentinel(self):
        for k in ('redis','sentinel'):
            self.assertEqual(self.post({'kind':k,'connection':{'auth':'default'},'password':k+'-fixture'}).status_code,200)
        r=self.c.delete('/api/services/'+self.sid+'/credentials',base_url=self.base,headers=self.h,json={'kind':'sentinel','scope':'service'})
        self.assertEqual(r.status_code,200)
        state=r.get_json()['credentials'];self.assertIn(self.redis.id,state['unlocked_endpoints']);self.assertNotIn(self.sentinel.id,state['unlocked_endpoints'])

if __name__=='__main__':unittest.main()
