"""Independent Redis/Sentinel auth. Fixtures only; never production connections."""
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from redis_farm.models import Endpoint, FarmError, clean_service, connection_profile, service_connection
from redis_farm.credentials import CredentialSet, DiscoveryCredentials, password_for
from redis_farm.runtime import Sessions
from redis_farm.settings import Settings
from redis_farm.cli import RedisCLI, CliError, Reply
from redis_farm.storage import Inventory
from redis_farm.discovery import Discovery
from redis_farm.telemetry import Telemetry

FAKE = str(Path(__file__).with_name('fake_cli.py').resolve())


def service(redis, sentinel=None):
    eps = [redis.to_dict()] + ([sentinel.to_dict()] if sentinel else [])
    return dict(name='fixture-service',group='fixture',seed=redis.to_dict(),endpoints=eps,
                mode='sentinel' if sentinel else 'standalone', master_name='cache' if sentinel else '')


class Connections(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.settings=Settings(Path(self.tmp.name),cli_path=FAKE)
        self.sessions=Sessions(self.settings); self.one=self.sessions.create();self.two=self.sessions.create()
        self.redis=Endpoint('192.0.2.11',6379,auth='user',username='ops')
        self.sentinel=Endpoint('192.0.2.11',26379,kind='sentinel',auth='user',username='sent-ops')
    def tearDown(self): self.tmp.cleanup()
    def test_old_sentinel_stays_open(self):
        self.assertEqual(Endpoint('192.0.2.1',26379,kind='sentinel').auth,'none')
    def test_named_username_is_required(self):
        with self.assertRaises(FarmError): replace(self.redis,username='')
    def test_none_clears_username(self):
        self.assertEqual(replace(self.redis,auth='none').username,'')
    def test_secret_namespaced_by_protocol(self):
        for ep,pw in [(self.redis,'redis-fixture'),(self.sentinel,'sent-fixture')]:
            self.sessions.set_access(self.one.id,'svc',ep,pw)
        c=self.sessions.credentials(self.one.id,'svc')
        self.assertEqual(password_for(c,self.redis),'redis-fixture')
        self.assertEqual(password_for(c,self.sentinel),'sent-fixture')
    def test_no_global_password_for_sentinel(self):
        self.sessions.unlock(self.one.id,'global-fixture')
        with self.assertRaises(FarmError):password_for(self.sessions.credentials(self.one.id,'svc'),self.sentinel)
    def test_same_name_different_services(self):
        self.sessions.set_access(self.one.id,'a',self.redis,'a-fixture')
        self.sessions.set_access(self.one.id,'b',self.redis,'b-fixture')
        self.assertNotEqual(password_for(self.sessions.credentials(self.one.id,'a'),self.redis),password_for(self.sessions.credentials(self.one.id,'b'),self.redis))
    def test_sessions_are_isolated(self):
        self.sessions.set_access(self.one.id,'svc',self.redis,'fixture')
        with self.assertRaises(FarmError):password_for(self.sessions.credentials(self.two.id,'svc'),self.redis)
    def test_username_change_does_not_reuse_secret(self):
        self.sessions.set_access(self.one.id,'svc',self.redis,'fixture')
        with self.assertRaises(FarmError):password_for(self.sessions.credentials(self.one.id,'svc'),replace(self.redis,username='different'))
    def test_instance_override_has_priority(self):
        self.sessions.set_access(self.one.id,'svc',self.redis,'common')
        self.sessions.set_access(self.one.id,'svc',self.redis,'specific',[self.redis.id])
        c=self.sessions.credentials(self.one.id,'svc')
        self.assertEqual(c.for_endpoint(self.redis),'specific')
        self.assertEqual(c.for_endpoint(replace(self.redis,host='192.0.2.12')),'common')
    def test_legacy_default_override_has_priority(self):
        e=replace(self.redis,auth='default')
        self.sessions.set_default(self.one.id,'svc','specific',[e.id])
        self.sessions.set_access(self.one.id,'svc',e,'common')
        self.assertEqual(self.sessions.credentials(self.one.id,'svc').for_endpoint(e),'specific')
    def test_logout_clears_all_secrets(self):
        self.sessions.set_access(self.one.id,'svc',self.redis,'fixture')
        self.sessions.set_access(self.one.id,'svc',self.sentinel,'sent-fixture')
        self.sessions.lock_password(self.one.id)
        self.assertEqual(self.one.access_passwords,{})
    def test_forget_sentinel_preserves_redis(self):
        self.sessions.set_access(self.one.id,'svc',self.redis,'fixture')
        self.sessions.set_access(self.one.id,'svc',self.sentinel,'sent-fixture')
        self.sessions.forget_access(self.one.id,'svc','sentinel')
        c=self.sessions.credentials(self.one.id,'svc')
        self.assertEqual(c.for_endpoint(self.redis),'fixture');self.assertIsNone(c.for_endpoint(self.sentinel))
    def test_snapshot_secrets_not_in_public_state(self):
        self.sessions.set_access(self.one.id,'svc',self.sentinel,'SENSITIVE_FIXTURE')
        public=self.sessions.credential_state(self.one.id,dict(service(self.redis,self.sentinel),id='svc'))
        self.assertNotIn('SENSITIVE_FIXTURE',json.dumps(public))
        self.assertIn(self.sentinel.id,public['unlocked_endpoints'])
    def test_export_excludes_passwords_but_keeps_username(self):
        store=Inventory(Path(self.tmp.name)); value=service(self.redis,self.sentinel)
        value['password']='MUST_NOT_PERSIST';value['sentinel_connection']={**self.sentinel.to_dict(),'password':'MUST_NOT_PERSIST'}
        store.save(value);export=json.dumps(store.export())
        self.assertNotIn('MUST_NOT_PERSIST',export);self.assertIn('sent-ops',export)
    def test_cross_protocol_peer_does_not_copy_secret_or_tls(self):
        source=replace(self.redis,tls=True,cacert='/ca/redis.pem')
        target=source.peer('192.0.2.1',26379,'sentinel')
        self.assertEqual(target.auth,'none');self.assertFalse(target.tls)
    def test_same_kind_peers_preserve_user_and_tls(self):
        source=replace(self.sentinel,tls=True,cacert='/ca/sentinel.pem')
        self.assertEqual(source.peer('192.0.2.12',26379,'sentinel').username,'sent-ops')
        self.assertTrue(source.peer('192.0.2.12',26379,'sentinel').tls)
    def test_mtls_requires_pair(self):
        with self.assertRaises(FarmError):replace(self.redis,certfile='/client.crt')
    def test_sentinel_no_global_profile(self):
        with self.assertRaises(FarmError):connection_profile({'auth':'admin'},'sentinel')
    def test_storage_different_named_users_not_merged(self):
        store=Inventory(Path(self.tmp.name));store.save(service(self.redis));store.save(service(replace(self.redis,username='another')))
        self.assertEqual(len(store.list()),2)
    def test_global_redis_no_changes(self):
        self.sessions.unlock(self.one.id,'shared-fixture')
        e=replace(self.redis,auth='admin');c=self.sessions.credentials(self.one.id,'svc')
        self.assertEqual(c.for_endpoint(e),'shared-fixture')


class CLIConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.cli=RedisCLI(Settings(Path(self.tmp.name),cli_path=FAKE))
    def tearDown(self):self.cli.shutdown();self.tmp.cleanup()
    def test_named_sentinel_argv_environment(self):
        e=Endpoint('192.0.2.1',26379,kind='sentinel',auth='user',username='s-admin')
        args=self.cli.arguments(e);env=self.cli.environment(e,'secret-fixture')
        self.assertEqual(args[args.index('--user')+1],'s-admin');self.assertNotIn('secret-fixture',args)
        self.assertEqual(env['REDISCLI_AUTH'],'secret-fixture')
    def test_password_only_sentinel_omits_user(self):
        e=Endpoint('192.0.2.1',26379,kind='sentinel',auth='default')
        self.assertNotIn('--user',self.cli.arguments(e));self.assertEqual(self.cli.environment(e,'fixture')['REDISCLI_AUTH'],'fixture')
    def test_open_sentinel_clears_inherited_env(self):
        e=Endpoint('192.0.2.1',26379,kind='sentinel');os.environ['REDISCLI_AUTH']='ambient-fixture'
        try:self.assertNotIn('REDISCLI_AUTH',self.cli.environment(e,'wrong-protocol'))
        finally:os.environ.pop('REDISCLI_AUTH')
    def test_mtls_arguments(self):
        e=Endpoint('192.0.2.1',26379,kind='sentinel',tls=True,cacert='/ca.pem',certfile='/client.crt',keyfile='/client.key',sni='sentinel.example.net')
        args=self.cli.arguments(e)
        for option,value in [('--cert','/client.crt'),('--key','/client.key'),('--sni','sentinel.example.net')]:self.assertEqual(args[args.index(option)+1],value)
    def test_error_categories_and_redaction(self):
        for text,code in [('NOAUTH Authentication required.','authentication_required'),('WRONGPASS fixture','authentication_failed'),('NOPERM command','permission_denied'),('ERR unknown command','unsupported_command'),('Could not connect: Connection refused','connection_failed')]:
            err=self.cli.reply_error(text,'fixture');self.assertEqual(err.code,code);self.assertNotIn('fixture',str(err))


class ProtectedSentinelFixture:
    def __init__(self, password='sent-fixture', auth='user'):
        self.password=password;self.auth=auth;self.calls=[]
    def run(self,ep,command,password,**kwargs):
        self.calls.append((ep,tuple(command),password))
        if ep.kind=='sentinel':
            if ep.auth=='none':raise CliError('NOAUTH Authentication required.','authentication_required',502)
            if ep.auth!=self.auth or (ep.auth=='user' and ep.username!='sent-ops') or password!=self.password:
                raise CliError('WRONGPASS','authentication_failed',502)
            if command[0]=='INFO':value='redis_version:7.2.0\nredis_mode:sentinel\nrun_id:s'+ep.host
            elif command[1]=='MASTERS':value=[['name','cache','ip','192.0.2.11','port','6379']]
            elif command[1]=='MASTER':value=['name','cache','ip','192.0.2.11','port','6379']
            elif command[1]=='REPLICAS':value=[['ip','192.0.2.12','port','6379']]
            elif command[1]=='SENTINELS':value=[['ip','192.0.2.22','port','26379']]
            else:value=[]
        else:
            if ep.auth!='user' or ep.username!='redis-ops' or password!='redis-fixture':
                raise CliError('NOAUTH','authentication_required',502)
            if command[0]=='INFO':value='redis_version:7.2.0\nredis_mode:standalone\nrole:master\nrun_id:r'+ep.host
            else:value=[]
        return Reply(value,1,dict(connect_host=ep.host,address=ep.address))


class SentinelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.settings=Settings(Path(self.tmp.name));self.cli=ProtectedSentinelFixture()
        self.redis=Endpoint('192.0.2.11',6379,auth='user',username='redis-ops')
        self.sent=Endpoint('192.0.2.21',26379,kind='sentinel',auth='user',username='sent-ops')
        self.spec=dict(host=self.sent.host,ports='26379',kind='sentinel',auth='user',username='redis-ops',
          sentinel_connection=connection_profile(self.sent.to_dict(),'sentinel'))
    def tearDown(self):self.tmp.cleanup()
    def scan(self,secret):return Discovery(self.cli,self.settings).discover(self.spec,secret,'test',threading.Event())
    def test_auto_protected_endpoint_does_not_try_any_secret(self):
        self.spec['kind']='auto'
        creds=DiscoveryCredentials(seed=self.redis,password='redis-fixture',sentinel_seed=self.sent,sentinel_password='sent-fixture')
        r=self.scan(creds)
        self.assertFalse(r['services'])
        self.assertTrue(r['errors'])
        self.assertTrue(all(pw is None for ep,cmd,pw in self.cli.calls))
    def test_named_empty_password_is_local_requirement(self):
        c=DiscoveryCredentials()
        with self.assertRaises(FarmError) as caught:password_for(c,self.sent)
        self.assertEqual(caught.exception.code,'sentinel_password_required')
    def test_protected_sentinel_discovers_peers(self):
        creds=DiscoveryCredentials(seed=self.redis,password='redis-fixture',sentinel_seed=self.sent,sentinel_password='sent-fixture')
        result=self.scan(creds); self.assertEqual(len(result['services']),1)
        eps=result['services'][0]['service']['endpoints']
        self.assertEqual(sum(e['kind']=='sentinel' for e in eps),2)
        self.assertEqual(sum(e['kind']=='redis' for e in eps),2)
        self.assertTrue(all(pw!='redis-fixture' for ep,cmd,pw in self.cli.calls if ep.kind=='sentinel'))
        self.assertTrue(all(pw!='sent-fixture' for ep,cmd,pw in self.cli.calls if ep.kind=='redis'))
    def test_missing_sentinel_secret_keeps_locked_seed(self):
        result=self.scan(DiscoveryCredentials())
        self.assertEqual(len(result['services']),1)
        self.assertFalse(result['services'][0]['observations'][0]['reachable'])
    def test_wrong_sentinel_secret_is_not_remembered(self):
        creds=DiscoveryCredentials(sentinel_seed=self.sent,sentinel_password='bad-fixture')
        result=self.scan(creds);self.assertEqual(creds.verified,set())
    def test_default_sentinel_discovery(self):
        self.cli.auth='default';self.sent=replace(self.sent,auth='default');self.spec['sentinel_connection']=connection_profile(self.sent.to_dict(),'sentinel')
        result=self.scan(DiscoveryCredentials(sentinel_seed=self.sent,sentinel_password='sent-fixture'))
        self.assertEqual(result['services'][0]['service']['mode'],'sentinel')
    def test_separate_hosts_plan_no_range_on_extra_host(self):
        self.spec.update(sentinel_endpoints=[{'host':'192.0.2.22','port':27000}])
        plan=Discovery(self.cli,self.settings).plan(self.spec)
        self.assertEqual([(e.host,e.port) for e,h in plan['probes']],[('192.0.2.21',26379),('192.0.2.22',27000)])
    def test_inherit_does_not_cross_protocol(self):
        c=DiscoveryCredentials(sentinel_seed=self.sent,sentinel_password='sent-fixture');c.inherit(self.sent,self.redis)
        self.assertIsNone(c.for_endpoint(self.redis))
    def test_sentinel_secret_does_not_reach_unrelated_port(self):
        c=DiscoveryCredentials(sentinel_seed=self.sent,sentinel_password='sent-fixture')
        self.assertIsNone(c.for_endpoint(replace(self.sent,port=26380)))
    def test_sentinel_master_filter(self):
        self.spec['sentinel_master']='other'
        r=self.scan(DiscoveryCredentials(sentinel_seed=self.sent,sentinel_password='sent-fixture'))
        self.assertEqual(r['services'],[])
    def test_telemetry_uses_separate_credentials(self):
        sessions=Sessions(self.settings);owner=sessions.create().id
        sessions.set_access(owner,'svc',self.redis,'redis-fixture');sessions.set_access(owner,'svc',self.sent,'sent-fixture')
        result=Telemetry(self.cli,self.settings).snapshot(dict(service(self.redis,self.sent),id='svc'),sessions.credentials(owner,'svc'),owner,threading.Event())
        self.assertEqual(result['summary']['sentinel_up'],2)
        self.assertTrue(all(pw!='redis-fixture' for ep,cmd,pw in self.cli.calls if ep.kind=='sentinel'))

if __name__=='__main__':unittest.main()
