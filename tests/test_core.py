import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from redis_farm.cli import RedisCLI, CliError
from redis_farm.models import Endpoint, FarmError, sentinel_port, parse_ports, clean_service
from redis_farm.parsers import info, pairs, cluster_nodes, acl_rows
from redis_farm.settings import Settings
from redis_farm.storage import Inventory
from redis_farm.runtime import Sessions, Jobs
from redis_farm.terminal import Terminals
from redis_farm.discovery import Discovery
from redis_farm.telemetry import Telemetry
from fixtures import FakeTopologyCLI

FIXTURE = str(Path(__file__).with_name('fake_cli.py').resolve())


def service(ep=None, name='sample', mode='standalone', master_name=''):
    ep = ep or Endpoint('redis-a.example', 6389)
    return {'name':name, 'group':'test', 'mode':mode, 'master_name':master_name,
            'seed':ep.to_dict(), 'endpoints':[ep.to_dict()]}


class ModelTests(unittest.TestCase):
    def test_port_convention_is_prefix_not_addition(self):
        self.assertEqual(sentinel_port(6389), 26389)
        self.assertEqual(sentinel_port(123), 2123)
        self.assertIsNone(sentinel_port(16389))

    def test_explicit_ranges(self):
        self.assertEqual(parse_ports('6389,6390-6392,6389'), [6389,6390,6391,6392])
        for value in ('1-999', '70000', '4-1', ''):
            with self.assertRaises(FarmError):
                parse_ports(value)

    def test_invalid_ports(self):
        for port in (True, False, 0, 65536, 'x', 1.5):
            with self.assertRaises(FarmError):
                Endpoint('localhost', port)

    def test_endpoint_id_and_ipv6(self):
        a = Endpoint('[::1]', 6389)
        self.assertEqual(a.address, '[::1]:6389')
        self.assertNotEqual(a.id, Endpoint('::1',6390).id)
        self.assertNotEqual(a.id, Endpoint('::1',6389,'sentinel').id)

    def test_sentinel_never_inherits_admin_profile(self):
        self.assertEqual(Endpoint('localhost',26389,'sentinel','admin').auth, 'none')

    def test_url_and_option_are_not_hosts(self):
        for host in ('http://localhost', '--help', 'localhost;echo hello', 'localhost:6379'):
            with self.assertRaises(FarmError):
                Endpoint(host,6379)

    def test_missing_endpoint_fields(self):
        with self.assertRaises(FarmError):
            Endpoint.from_dict({'host':'localhost'})

    def test_only_metadata_is_serializable(self):
        raw = service()
        raw.update(password='never-store-this', transcript='secret', snapshot={'value':'secret'})
        raw['endpoints'][0]['password'] = 'nested-secret'
        self.assertNotIn('secret', json.dumps(clean_service(raw)))
        self.assertNotIn('password', json.dumps(clean_service(raw)))

    def test_sentinel_requires_master_name(self):
        with self.assertRaises(FarmError):
            clean_service(service(mode='sentinel'))


class ParserTests(unittest.TestCase):
    def test_info_values_can_contain_colons(self):
        self.assertEqual(info('# Server\npath:/etc/a:b\nrole:master\r\n'), {'path':'/etc/a:b','role':'master'})

    def test_pairs(self):
        self.assertEqual(pairs(['a',1,'b',2]), {'a':1,'b':2})
        self.assertEqual(pairs(['a']), {})

    def test_cluster_ipv6_and_slot_migration(self):
        rows = cluster_nodes('id1 [::1]:7000@17000,host-a master - 0 0 3 connected 0-5460 [5461->-id2]\n')
        self.assertEqual(rows[0]['host'], '::1')
        self.assertEqual(rows[0]['slots'], [[0,5460]])
        self.assertEqual(rows[0]['migration'], ['[5461->-id2]'])

    def test_acl_widget_removes_hashes_without_changing_rights(self):
        row = acl_rows(['user cache_app on #'+'a'*64+' ~* &* -@all +@write'])[0]
        self.assertNotIn('a'*64, row['rules'])
        self.assertIn('+@write', row['rules'])
        self.assertEqual(row['category'], 'application')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Inventory(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_reload_no_password(self):
        raw = service();raw['password']='my-secret'
        saved = self.store.save(raw)
        other = Inventory(Path(self.tmp.name))
        self.assertEqual(other.get(saved['id'])['name'], 'sample')
        self.assertNotIn(b'my-secret', self.store.path.read_bytes())
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_port_identity_not_host_identity(self):
        self.store.save(service(Endpoint('a.example',6389)))
        self.store.save(service(Endpoint('a.example',6390)))
        self.assertEqual(len(self.store.list()), 2)

    def test_multiple_groups_on_same_sentinel_are_not_merged(self):
        ep = Endpoint('a.example',26389,'sentinel')
        self.store.save(service(ep, mode='sentinel', master_name='group-a'))
        self.store.save(service(ep, mode='sentinel', master_name='group-b'))
        self.assertEqual(len(self.store.list()), 2)

    def test_import_export_and_delete_are_local(self):
        item = self.store.save(service())
        data = self.store.export()
        self.store.delete(item['id'])
        self.assertEqual(self.store.list(), [])
        self.store.import_data(data)
        self.assertEqual(len(self.store.list()), 1)

    def test_catalog_validation_before_any_import(self):
        data = {'format':'redis-farm-inventory','version':1,'services':[service(), {'mode':'invalid'}]}
        with self.assertRaises(FarmError):
            self.store.import_data(data)
        self.assertEqual(self.store.list(), [])


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name), cli_path=FIXTURE, dns_servers=())
        self.cli = RedisCLI(self.settings)
        self.ep = Endpoint('localhost',6389)

    def tearDown(self):
        self.cli.shutdown();self.tmp.cleanup()

    def test_binary_capabilities_are_detected_locally(self):
        self.assertTrue(self.cli.status()['available'])

    def test_password_in_child_environment_not_argv(self):
        argv = self.cli.arguments(self.ep)
        self.assertNotIn('fixture-password', argv)
        self.assertNotIn('-a', argv)
        result = self.cli.run(self.ep, ['ENV'], 'fixture-password', output='json').value
        self.assertEqual(result['auth'], 'fixture-password')
        self.assertEqual(result['user'], 'admin')
        self.assertEqual(result['history'], os.devnull)
        self.assertEqual(result['rcfile'], os.devnull)

    def test_sentinel_child_has_no_credentials(self):
        old = os.environ.get('REDISCLI_AUTH')
        os.environ['REDISCLI_AUTH'] = 'inherited-secret'
        try:
            ep = Endpoint('localhost',26389,'sentinel')
            result = self.cli.run(ep,['ENV'],'shared-secret',output='json').value
            self.assertIsNone(result['auth']);self.assertIsNone(result['user'])
            self.assertEqual(os.environ['REDISCLI_AUTH'], 'inherited-secret')
        finally:
            if old is None:os.environ.pop('REDISCLI_AUTH',None)
            else:os.environ['REDISCLI_AUTH']=old

    def test_open_redis_profile_does_not_inherit_password(self):
        result=self.cli.run(Endpoint('localhost',6379,auth='none'),['ENV'],'secret',output='json').value
        self.assertIsNone(result['auth'])

    def test_missing_password_is_not_fallback(self):
        with self.assertRaises(CliError) as cm:self.cli.run(self.ep,['INFO'],None)
        self.assertEqual(cm.exception.code,'password_required')

    def test_exit_status_and_auth_stderr_are_errors(self):
        for command in ('ERROR','AUTHERROR'):
            with self.assertRaises(CliError):self.cli.run(self.ep,[command],'p')

    def test_output_limit_kills_child(self):
        cli=RedisCLI(replace(self.settings,max_output=1024))
        with self.assertRaises(CliError) as cm:cli.run(self.ep,['BIG','20000'],'p')
        self.assertEqual(cm.exception.code,'output_limit')
        self.assertFalse(cli.active)

    def test_deadline_kills_child(self):
        cli=RedisCLI(replace(self.settings,command_timeout=.15))
        with self.assertRaises(CliError) as cm:cli.run(self.ep,['SLEEP','3'],'p')
        self.assertEqual(cm.exception.code,'timeout')
        self.assertFalse(cli.active)

    def test_cancellation_before_start(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(CliError) as cm:self.cli.run(self.ep,['INFO'],'p',cancel=cancel)
        self.assertEqual(cm.exception.code,'cancelled');self.assertFalse(self.cli.active)

    def test_console_persists_state_and_transaction(self):
        manager=Terminals(self.cli,self.settings)
        try:
            terminal=manager.open('owner',self.ep,'p',db=2)
            terminal.send('SELECT 4')
            terminal.send('DB')
            terminal.send('MULTI')
            terminal.send('ECHO "quoted value"')
            terminal.send('EXEC')
            deadline=time.monotonic()+3
            result=''
            while time.monotonic()<deadline:
                result=''.join(terminal.poll(0)['chunks'])
                if 'quoted value' in result:break
                time.sleep(.02)
            self.assertIn('\n4\n', result)
            self.assertIn('QUEUED',result)
            self.assertIn('quoted value',result)
            with self.assertRaises(FarmError):manager.get('other-owner',terminal.id)
            manager.close_owner('owner')
            self.assertIsNotNone(terminal.process.poll())
        finally:manager.shutdown()


class SessionAndJobTests(unittest.TestCase):
    def test_separate_sessions_and_lock(self):
        sessions=Sessions(Settings(Path('/unused')))
        one,two=sessions.create(),sessions.create()
        sessions.unlock(one.id,'one');sessions.unlock(two.id,'two')
        sessions.lock_password(one.id)
        self.assertIsNone(sessions.password(one.id));self.assertEqual(sessions.password(two.id),'two')
        self.assertNotIn('two',repr(two))

    def test_expiration_does_not_depend_on_polling(self):
        sessions=Sessions(Settings(Path('/unused'),session_idle=1))
        item=sessions.create();sessions.unlock(item.id,'secret');item.active-=2
        self.assertIsNone(sessions.get(item.id));self.assertIn(item.id,sessions.expired())
        sessions.remove(item.id);self.assertNotIn(item.id,sessions.items)

    def test_jobs_are_owner_scoped_and_deduplicated(self):
        jobs=Jobs();release=threading.Event()
        try:
            item=jobs.submit('one','same',lambda cancel: release.wait(1))
            duplicate=jobs.submit('one','same',lambda cancel: False)
            self.assertEqual(item.id,duplicate.id)
            with self.assertRaises(FarmError):jobs.get('two',item.id)
            jobs.clear_owner('one')
            with self.assertRaises(FarmError):jobs.get('one',item.id)
        finally:release.set();jobs.shutdown()


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.cli=FakeTopologyCLI();self.settings=Settings(Path('/unused'))
        self.discovery=Discovery(self.cli,self.settings)

    def discover(self, **spec):
        return self.discovery.discover({'host':'redis-b.example','ports':'6389','kind':'redis',**spec},'fixture-password','owner',threading.Event())

    def test_discovery_from_replica_finds_sentinel_and_all_nodes(self):
        result=self.discover()
        self.assertEqual(len(result['services']),1)
        found=result['services'][0]
        self.assertEqual(found['service']['mode'],'sentinel')
        self.assertEqual(found['service']['master_name'],'cache_app')
        self.assertEqual(len(found['service']['endpoints']),6)
        unreachable=[r for r in found['observations'] if not r['reachable']]
        self.assertEqual(len(unreachable),1)
        self.assertEqual(unreachable[0]['endpoint']['host'],'redis-c.example')

    def test_direct_sentinel_entry(self):
        result=self.discover(ports='26389',kind='sentinel')
        self.assertEqual(result['services'][0]['service']['mode'],'sentinel')
        sentinel_calls=[ep for ep,cmd,out in self.cli.calls if ep.kind=='sentinel']
        self.assertTrue(sentinel_calls);self.assertTrue(all(ep.auth=='none' for ep in sentinel_calls))

    def test_replication_without_sentinel(self):
        result=self.discover(find_sentinel=False)
        self.assertEqual(result['services'][0]['service']['mode'],'replication')
        self.assertFalse(any(cmd[0]=='SENTINEL' for ep,cmd,out in self.cli.calls))

    def test_standalone(self):
        result=self.discover(host='single.example',ports='6400',find_sentinel=False)
        self.assertEqual(result['services'][0]['service']['mode'],'standalone')

    def test_cluster_topology_is_not_sentinel(self):
        result=self.discover(host='cluster-0.example',ports='7000')
        found=result['services'][0]['service']
        self.assertEqual(found['mode'],'cluster');self.assertEqual(len(found['endpoints']),3)
        self.assertFalse(any(cmd[0]=='SENTINEL' for ep,cmd,out in self.cli.calls))

    def test_wrong_password_does_not_switch_to_open_profile(self):
        result=self.discovery.discover({'host':'redis-a.example','ports':'6389','kind':'redis'},'wrong','owner',threading.Event())
        # Explicit Redis type may be saved as locked; credentials are not changed.
        self.assertTrue(result['services'])
        self.assertTrue(all(not row['reachable'] for item in result['services'] for row in item['observations']))
        self.assertTrue(all(ep['auth']=='admin' for item in result['services'] for ep in item['service']['endpoints']))
        self.assertEqual(self.cli.calls[0][0].auth,'none')
        self.assertTrue(all(ep.auth=='admin' for ep,cmd,out in self.cli.calls[1:]))

    def test_snapshot_counts_views_acl_and_memory(self):
        found=self.discover()['services'][0]['service'];found['id']='svc'
        telemetry=Telemetry(self.cli,self.settings)
        first=telemetry.snapshot(found,'fixture-password','owner',threading.Event())
        self.assertEqual(first['summary']['redis_total'],3)
        self.assertEqual(first['summary']['redis_up'],2)
        self.assertEqual(first['summary']['sentinel_up'],3)
        self.assertEqual(first['summary']['memory_all'],268435456)
        self.assertTrue(first['summary']['sentinel_agree'])
        self.cli.master='redis-b.example'
        second=telemetry.snapshot(found,'fixture-password','owner',threading.Event())
        self.assertTrue(any(e['field']=='role' for e in second['events']))
        telemetry.clear_owner('owner');self.assertIsNone(telemetry.last('owner','svc'))

    def test_acl_detail_does_not_recommend_or_modify_anything(self):
        telemetry=Telemetry(self.cli,self.settings)
        result=telemetry.detail(Endpoint('redis-a.example',6389),'acl','fixture-password','owner',threading.Event())
        self.assertNotIn('a'*64,json.dumps(result))
        self.assertEqual({cmd[:2] for ep,cmd,out in self.cli.calls}, {('ACL','LIST'),('CONFIG','GET')})


if __name__=='__main__':unittest.main()
