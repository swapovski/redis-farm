"""Default/requirepass regression fixtures. No remote connections."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from redis_farm.cli import RedisCLI, CliError, Reply
from redis_farm.credentials import CredentialSet, DiscoveryCredentials, password_for
from redis_farm.discovery import Discovery
from redis_farm.models import Endpoint, FarmError, clean_service
from redis_farm.runtime import Sessions
from redis_farm.settings import Settings
from redis_farm.storage import Inventory
from redis_farm.telemetry import Telemetry
from redis_farm.terminal import Terminals
from test_core import service, FIXTURE
from test_discovery_auto import ScanFixtureCLI


class DefaultFixtureCLI(ScanFixtureCLI):
    def __init__(self):
        super().__init__()
        self.passwords={key:'default-'+str(key[1]) for key in self.redis}
        self.auth_calls=[]
    def run(self,ep,command,password,**kwargs):
        key=(self.aliases.get(ep.host,ep.host),ep.port)
        self.auth_calls.append((ep,tuple(command),password))
        if key in self.redis:
            if ep.auth=='none':raise CliError('NOAUTH Authentication required.','redis_error',502)
            if ep.auth!='default' or password!=self.passwords[key]:
                raise CliError('WRONGPASS invalid credentials.','redis_error',502)
            if command[:2]==['ACL','USERS']:return Reply(['default'],1)
            if command[:2]==['ACL','LIST']:return Reply(['user default on ~* &* +@all'],1)
            if command[:2]==['CONFIG','GET']:return Reply([command[2],''],1)
            if command[:2]==['CLIENT','LIST']:return Reply('',1)
            if command[0] in ('LATENCY','SLOWLOG'):return Reply([],1)
        return super().run(ep,command,'fixture-password' if key in self.redis else None,**kwargs)


class DefaultCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.tmp.name),dns_servers=())
        self.sessions=Sessions(self.settings);self.one=self.sessions.create();self.two=self.sessions.create()
        self.ep=Endpoint('seed.example',6379,auth='default')
    def tearDown(self):self.tmp.cleanup()
    def test_default_model_and_serialization(self):
        data=clean_service(service(self.ep));self.assertEqual(data['auth_mode'],'default')
        self.assertEqual(Endpoint.from_dict(data['seed']).auth,'default')
    def test_legacy_catalog_infers_admin(self):
        self.assertEqual(clean_service(service())['auth_mode'],'admin')
    def test_same_endpoint_in_two_profiles_not_merged(self):
        db=Inventory(Path(self.tmp.name));a=db.save(service(self.ep));b=db.save(service(replace(self.ep,auth='admin')))
        self.assertNotEqual(a['id'],b['id']);self.assertEqual(len(db.list()),2)
    def test_default_requires_no_admin_password(self):
        self.sessions.set_default(self.one.id,'a','a-secret')
        creds=self.sessions.credentials(self.one.id,'a')
        self.assertEqual(password_for(creds,self.ep),'a-secret');self.assertIsNone(creds.admin)
    def test_two_services_have_independent_passwords(self):
        self.sessions.set_default(self.one.id,'a','a-secret');self.sessions.set_default(self.one.id,'b','b-secret')
        self.assertEqual(password_for(self.sessions.credentials(self.one.id,'a'),self.ep),'a-secret')
        self.assertEqual(password_for(self.sessions.credentials(self.one.id,'b'),self.ep),'b-secret')
    def test_other_session_cannot_use_default(self):
        self.sessions.set_default(self.one.id,'a','a-secret')
        with self.assertRaises(FarmError):password_for(self.sessions.credentials(self.two.id,'a'),self.ep)
    def test_no_admin_fallback_for_default(self):
        self.sessions.unlock(self.one.id,'admin-only')
        with self.assertRaises(FarmError) as cm:password_for(self.sessions.credentials(self.one.id,'a'),self.ep)
        self.assertEqual(cm.exception.code,'default_password_required')
    def test_endpoint_override_and_common_service_password(self):
        self.sessions.set_default(self.one.id,'a','common');self.sessions.set_default(self.one.id,'a','specific',[self.ep.id])
        creds=self.sessions.credentials(self.one.id,'a')
        self.assertEqual(password_for(creds,self.ep),'specific')
        self.assertEqual(password_for(creds,replace(self.ep,host='peer-b.example')),'common')
    def test_forgetting_override_restores_explicit_service_password(self):
        self.sessions.set_default(self.one.id,'a','common');self.sessions.set_default(self.one.id,'a','specific',[self.ep.id])
        self.sessions.forget_default(self.one.id,'a',[self.ep.id])
        self.assertEqual(password_for(self.sessions.credentials(self.one.id,'a'),self.ep),'common')
    def test_admin_logout_preserves_default(self):
        self.sessions.unlock(self.one.id,'admin');self.sessions.set_default(self.one.id,'a','default-a')
        self.sessions.lock_password(self.one.id,all_profiles=False)
        self.assertEqual(password_for(self.sessions.credentials(self.one.id,'a'),self.ep),'default-a')
    def test_service_logout_preserves_other_service_and_admin(self):
        self.sessions.unlock(self.one.id,'admin');self.sessions.set_default(self.one.id,'a','aa');self.sessions.set_default(self.one.id,'b','bb')
        self.sessions.forget_default(self.one.id,'a')
        self.assertIsNone(self.sessions.credentials(self.one.id,'a').default)
        self.assertEqual(self.sessions.credentials(self.one.id,'b').default,'bb');self.assertEqual(self.sessions.password(self.one.id),'admin')
    def test_full_logout_removes_everything(self):
        self.sessions.set_default(self.one.id,'a','secret');self.sessions.hold_discovery(self.one.id,DiscoveryCredentials())
        self.sessions.lock_password(self.one.id)
        self.assertEqual(self.one.defaults,{});self.assertEqual(self.one.discovery_secrets,{})
    def test_metadata_export_never_contains_credentials(self):
        db=Inventory(Path(self.tmp.name));v=db.save(service(self.ep));self.sessions.set_default(self.one.id,v['id'],'unique-secret')
        state=self.sessions.credential_state(self.one.id,v)
        self.assertNotIn('unique-secret',json.dumps(state));self.assertNotIn('unique-secret',repr(self.one))
        self.assertNotIn(b'unique-secret',db.path.read_bytes());self.assertNotIn('unique-secret',json.dumps(db.export()))
        self.assertEqual(state['unlocked_endpoints'],[self.ep.id])
    def test_pending_discovery_owner_and_expiration(self):
        token=self.sessions.hold_discovery(self.one.id,DiscoveryCredentials())
        with self.assertRaises(FarmError):self.sessions.discovery_credentials(self.two.id,token)
        created,value=self.one.discovery_secrets[token];self.one.discovery_secrets[token]=(created-601,value)
        self.sessions.reap_discovery();self.assertEqual(self.one.discovery_secrets,{})
    def test_invalid_password_keeps_previous_value(self):
        self.sessions.set_default(self.one.id,'a','old')
        with self.assertRaises(FarmError):self.sessions.set_default(self.one.id,'a','')
        self.assertEqual(self.sessions.credentials(self.one.id,'a').default,'old')
    def test_expired_session_rejects_default_access(self):
        self.sessions.set_default(self.one.id,'a','secret');self.one.active-=4000
        with self.assertRaises(FarmError):self.sessions.credentials(self.one.id,'a')
    def test_sentinel_no_auth_with_any_credential(self):
        self.assertIsNone(password_for(CredentialSet('admin','default'),self.ep.peer('seed.example',26379,'sentinel')))


class DefaultDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.tmp.name),dns_servers=(),scan_redis_end=6383,scan_sentinel_end=26383)
        self.cli=DefaultFixtureCLI();self.discovery=Discovery(self.cli,self.settings);self.ep=Endpoint('seed.example',6379,auth='default')
    def tearDown(self):self.tmp.cleanup()
    def scan(self,creds=None,**kwargs):
        return self.discovery.discover({'host':'seed.example','auth':'default',**kwargs},creds or DiscoveryCredentials(),'one',threading.Event())
    def test_scan_without_password_finds_locked_groups_and_singleton(self):
        r=self.scan();self.assertEqual(len(r['services']),3)
        self.assertTrue(all(s['service']['auth_mode']=='default' for s in r['services']))
        self.assertTrue(all(pw is None for ep,c,pw in self.cli.auth_calls))
    def test_single_seed_password_is_not_sent_to_other_ports(self):
        creds=DiscoveryCredentials(seed=self.ep,password='default-6379')
        r=self.scan(creds)
        self.assertEqual(len(r['services']),3)
        self.assertFalse(any(pw is not None and ep.port!=6379 for ep,c,pw in self.cli.auth_calls))
        first=next(s for s in r['services'] if s['service']['master_name']=='cache_app')
        self.assertTrue(all(n['reachable'] for n in first['observations']))
    def test_wrong_password_keeps_endpoint_but_does_not_confirm_access(self):
        creds=DiscoveryCredentials(seed=self.ep,password='wrong')
        r=self.scan(creds,ports='6379',kind='redis',find_sentinel=False)
        self.assertEqual(len(r['services']),1);self.assertFalse(r['services'][0]['observations'][0]['reachable'])
        self.assertEqual(creds.verified,set())
    def test_manual_default_port_no_admin_required(self):
        r=self.scan(DiscoveryCredentials(seed=self.ep,password='default-6379'),ports='6379',kind='redis',find_sentinel=False)
        self.assertEqual(r['services'][0]['service']['mode'],'replication')
    def test_verified_discovery_password_binds_only_its_service(self):
        creds=DiscoveryCredentials(seed=self.ep,password='default-6379');result=self.scan(creds)
        store=Inventory(Path(self.tmp.name));saved=store.save_many([s['service'] for s in result['services']])
        sessions=Sessions(self.settings);one=sessions.create();creds.bind(sessions,one.id,saved)
        for s in saved:
            self.assertEqual(sessions.credentials(one.id,s['id']).default,'default-6379' if s['master_name']=='cache_app' else None)
    def test_two_instance_passwords_used_in_snapshot(self):
        self.cli.passwords[('peer-b.example',6379)]='different'
        r=self.scan(DiscoveryCredentials(seed=self.ep,password='default-6379'),ports='6379',kind='redis')
        s=next(p['service'] for p in r['services'] if p['service']['master_name']=='cache_app');s['id']='a'
        override=Endpoint('peer-b.example',6379,auth='default')
        creds=CredentialSet(default='default-6379',overrides={override.id:'different'})
        snap=Telemetry(self.cli,self.settings).snapshot(s,creds,'one',threading.Event())
        self.assertEqual(snap['summary']['redis_up'],3)
    def test_missing_password_does_not_call_cli_on_protected_redis(self):
        s=clean_service(service(self.ep));s['id']='a'
        snap=Telemetry(self.cli,self.settings).snapshot(s,CredentialSet(),'one',threading.Event())
        self.assertEqual(self.cli.auth_calls,[]);self.assertEqual(snap['nodes'][0]['error_code'],'default_password_required')
    def test_split_dns_locked_seed_and_advertised_ip_are_one_service(self):
        from redis_farm.resolver import Resolution
        from redis_farm.discovery import _Probe
        class Resolver:
            def resolve(self,host):return Resolution(host,'10.20.30.11' if host=='seed.example' else host,'fixture')
        self.cli.resolver=Resolver()
        ctx=_Probe(self.cli,self.settings,DiscoveryCredentials(),'one',threading.Event())
        a=Endpoint('seed.example',6379,auth='default');b=Endpoint('10.20.30.11',6379,auth='default')
        left=ctx._service(a,'standalone',{a.id:a})
        right=ctx._service(b,'sentinel',{b.id:b},'cache_app')
        merged=self.discovery._merge([left,right],ctx)
        self.assertEqual(len(merged),1)
        redis=[e for e in merged[0]['endpoints'] if e['kind']=='redis']
        self.assertEqual(len(redis),1);self.assertEqual(redis[0]['host'],'seed.example')

    def test_no_acl_server_is_supported(self):
        original=self.cli.run
        def no_acl(ep,command,pw,**kw):
            if command[0]=='ACL':raise CliError("ERR unknown command 'ACL'",'redis_error',502)
            return original(ep,command,pw,**kw)
        self.cli.run=no_acl
        s=clean_service(service(self.ep));s['id']='a'
        snap=Telemetry(self.cli,self.settings).snapshot(s,CredentialSet(default='default-6379'),'one',threading.Event())
        self.assertTrue(snap['nodes'][0]['reachable']);self.assertEqual(snap['nodes'][0]['acl']['state'],'unsupported')


class DefaultProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.tmp.name),cli_path=FIXTURE,dns_servers=())
        self.cli=RedisCLI(self.settings);self.ep=Endpoint('localhost',6379,auth='default')
    def tearDown(self):self.cli.shutdown();self.tmp.cleanup()
    def test_default_uses_password_only_environment(self):
        args=self.cli.arguments(self.ep);self.assertNotIn('--user',args);self.assertNotIn('-a',args)
        result=self.cli.run(self.ep,['ENV'],'default-only',output='json').value
        self.assertIsNone(result['user']);self.assertEqual(result['auth'],'default-only')
    def test_no_password_fails_before_process_creation(self):
        with self.assertRaises(CliError) as cm:self.cli.run(self.ep,['ENV'],None)
        self.assertEqual(cm.exception.code,'default_password_required');self.assertFalse(self.cli.active)
    def test_profile_specific_console_logout(self):
        terms=Terminals(self.cli,self.settings)
        try:
            admin=terms.open('one',replace(self.ep,auth='admin'),'admin',service_id='admin',profile='admin')
            default=terms.open('one',self.ep,'default',service_id='default',profile='default')
            terms.close_scope('one',profile='admin')
            self.assertIsNotNone(admin.process.poll());self.assertIsNone(default.process.poll())
            self.assertEqual(default.poll(0)['service_id'],'default')
        finally:terms.shutdown()


@unittest.skipUnless(importlib.util.find_spec('flask'), 'Flask unavailable in build environment')
class DefaultWebTests(unittest.TestCase):
    def setUp(self):
        from redis_farm.app import create_app
        self.tmp=tempfile.TemporaryDirectory();self.cli=DefaultFixtureCLI()
        self.app=create_app(Settings(Path(self.tmp.name),dns_servers=(),scan_redis_end=6383,scan_sentinel_end=26383),cli=self.cli)
        self.client=self.app.test_client();self.csrf=self.client.get('/api/bootstrap').get_json()['csrf']
        self.ext=self.app.extensions['redis_farm'];self.svc=self.ext['inventory'].save(service(Endpoint('seed.example',6379,auth='default')))
    def tearDown(self):self.ext['shutdown']();self.tmp.cleanup()
    def send(self,path,data,method='POST'):
        return self.client.open(path,method=method,json=data,headers={'X-RF-CSRF':self.csrf})
    def wait(self,response):
        self.assertEqual(response.status_code,202,response.data);jid=response.get_json()['job_id']
        for i in range(500):
            item=self.client.get('/api/jobs/'+jid).get_json()
            if item['state'] not in ('queued','running'):return item
            time.sleep(.01)
        self.fail('job timed out')
    def test_unlock_default_without_admin(self):
        r=self.send('/api/services/'+self.svc['id']+'/credentials',{'password':'default-6379','scope':'service'})
        self.assertEqual(r.status_code,200);self.assertFalse(self.client.get('/api/bootstrap').get_json()['redis_unlocked'])
        self.assertNotIn(b'default-6379',self.client.get('/api/services').data)
    def test_snapshot_with_default_service_password(self):
        self.send('/api/services/'+self.svc['id']+'/credentials',{'password':'default-6379'})
        result=self.wait(self.send('/api/services/'+self.svc['id']+'/snapshot',{}))
        self.assertEqual(result['state'],'done',result);self.assertEqual(result['result']['summary']['redis_up'],3)
    def test_scanning_and_auto_add_transfers_default_secret(self):
        result=self.wait(self.send('/api/discover',{'host':'seed.example','auth':'default','default_password':'default-6379','password_port':6379}))
        self.assertEqual(result['state'],'done',result)
        self.assertNotIn('default-6379',json.dumps(result))
        items=self.client.get('/api/services').get_json()['services']
        self.assertTrue(any(s['credentials']['service_password'] for s in items))
    def test_other_browser_cannot_read_default_secret(self):
        self.send('/api/services/'+self.svc['id']+'/credentials',{'password':'default-6379'})
        other=self.app.test_client();other.get('/api/bootstrap')
        self.assertFalse(other.get('/api/services').get_json()['services'][0]['credentials']['service_password'])
    def test_export_contains_only_metadata(self):
        self.send('/api/services/'+self.svc['id']+'/credentials',{'password':'unique-test-secret'})
        export=self.client.get('/api/catalog/export')
        self.assertNotIn(b'unique-test-secret',export.data);self.assertNotIn(b'credentials',export.data)
    def test_old_catalog_stays_admin(self):
        old=self.ext['inventory'].save(service())
        self.assertEqual(old['auth_mode'],'admin')
    def test_admin_logout_keeps_default(self):
        self.send('/api/session/unlock',{'password':'admin'})
        self.send('/api/services/'+self.svc['id']+'/credentials',{'password':'default-6379'})
        self.send('/api/session/lock',{'profile':'admin'})
        self.assertTrue(self.client.get('/api/services').get_json()['services'][0]['credentials']['service_password'])
    def test_endpoint_override_rejects_foreign_id(self):
        r=self.send('/api/services/'+self.svc['id']+'/credentials',{'password':'p','scope':'endpoint','endpoint_id':'foreign'})
        self.assertEqual(r.status_code,400)
    def test_discovery_password_not_applied_to_entire_range(self):
        self.wait(self.send('/api/discover',{'host':'seed.example','auth':'default','default_password':'default-6379','password_port':6379}))
        self.assertFalse(any(pw is not None and ep.port!=6379 for ep,c,pw in self.cli.auth_calls))
