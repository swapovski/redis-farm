"""Port-scan fixtures: no remote Redis, network scan or credential changes."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from redis_farm.cli import CliError, Reply
from redis_farm.discovery import Discovery
from redis_farm.models import FarmError
from redis_farm.runtime import Jobs
from redis_farm.settings import Settings
from redis_farm.storage import Inventory
from fixtures import arr, text


class ScanFixtureCLI:
    def __init__(self, delay=0):
        self.calls=[]; self.lock=threading.Lock();self.active=self.peak=0;self.delay=delay
        self.hosts=['seed.example','peer-b.example','peer-c.example']
        self.aliases={'10.20.30.11':'seed.example'}
        self.down=set();self.redis={};self.sentinels={};self.groups={};self.cluster={}
        self.add_group('cache_app',6379,26379)
        self.add_group('second-app',6381,26381,master='peer-b.example')
        self.redis[('seed.example',6383)]={'role':'master'}
    def add_group(self,name,port,sport,master='seed.example'):
        g={'name':name,'port':port,'sport':sport,'master':master};self.groups[(name,sport)]=g
        for h in self.hosts:
            self.redis[(h,port)]={'role':'master' if h==master else 'slave','group':g}
            self.sentinels.setdefault((h,sport),[]).append(g)
        return g
    def status(self):return {'available':True,'path':'fixture','version':'fixture','error':''}
    def close_owner(self,owner):pass
    def shutdown(self):pass
    @staticmethod
    def master_record(g):return {'name':g['name'],'ip':g['master'],'port':str(g['port']),
                                'runid':f'r-{g["master"]}-{g["port"]}'}
    def run(self,ep,command,password,*,output='raw',owner=None,cancel=None,timeout=None):
        with self.lock:
            self.calls.append((ep,tuple(command),timeout));self.active+=1;self.peak=max(self.peak,self.active)
        try:
            if self.delay and cancel is not None:cancel.wait(self.delay)
            if cancel is not None and cancel.is_set():raise CliError('Cancelled','cancelled')
            host=self.aliases.get(ep.host,ep.host);key=(host,ep.port);cmd=tuple(c.upper() for c in command)
            if key in self.down or (key not in self.redis and key not in self.sentinels):
                raise CliError('Connection refused (fixture)','redis_error',502)
            groups=self.sentinels.get(key)
            if groups:
                if ep.auth!='none':raise AssertionError('Credentials sent to Sentinel')
                if cmd[0]=='INFO':return Reply(text({'redis_version':'7.2.0','redis_mode':'sentinel','run_id':f's-{host}-{ep.port}'}),1)
                if cmd[:2]==('SENTINEL','MASTERS'):return Reply([arr(self.master_record(g)) for g in groups],1)
                g=next(g for g in groups if g['name']==command[2])
                if cmd[:2]==('SENTINEL','MASTER'):return Reply(arr(self.master_record(g)),1)
                if cmd[:2]==('SENTINEL','REPLICAS'):return Reply([arr({'ip':h,'port':str(g['port']),'runid':f'r-{h}-{g["port"]}'}) for h in self.hosts if h!=g['master']],1)
                if cmd[:2]==('SENTINEL','SENTINELS'):return Reply([arr({'ip':h,'port':str(g['sport'])}) for h in self.hosts if h!=host],1)
                raise AssertionError(command)
            if ep.auth=='none':raise CliError('NOAUTH Authentication required.','redis_error',502)
            if password!='fixture-password':raise CliError('WRONGPASS invalid credentials.','redis_error',502)
            record=self.redis[key]
            if cmd[0]=='INFO':
                d={'redis_version':'7.2.0','redis_mode':'standalone','role':record['role'],'run_id':f'r-{host}-{ep.port}'}
                g=record.get('group')
                if g:
                    if record['role']=='slave':d.update(master_host=g['master'],master_port=g['port'])
                    else:
                        hs=[h for h in self.hosts if h!=host];d['connected_slaves']=len(hs)
                        for i,h in enumerate(hs):d['slave'+str(i)]=f'ip={h},port={ep.port},state=online,offset=1,lag=0'
                if key in self.cluster:d.update(redis_mode='cluster',cluster_enabled=1)
                return Reply(text(d),1)
            if cmd==('CLUSTER','NODES'):return Reply('\n'.join(f'n{i} {h}:{p}@17000 master - 0 0 1 connected 0-10' for i,(h,p) in enumerate(self.cluster[key])),1)
            raise AssertionError(command)
        finally:
            with self.lock:self.active-=1


class AutoScanTests(unittest.TestCase):
    def setUp(self):
        self.settings=Settings(Path('/unused'),scan_redis_end=6383,scan_sentinel_end=26383)
        self.cli=ScanFixtureCLI();self.discovery=Discovery(self.cli,self.settings)
    def scan(self,**spec):return self.discovery.discover({'host':'seed.example',**spec},'fixture-password','test',threading.Event())
    def test_hostname_only_200_ports(self):
        d=Discovery(self.cli,Settings(Path('/unused')));p=d.plan({'host':'seed.example'})
        self.assertEqual(p['mode'],'node');self.assertEqual(len(p['probes']),200)
        self.assertEqual({e.port for e,h in p['probes']},set(range(6379,6479))|set(range(26379,26479)))
    def test_gaps_do_not_stop_scan(self):
        r=self.scan();self.assertEqual(len(r['services']),3)
        self.assertEqual(r['scan']['completed'],10);self.assertEqual(r['scan']['redis_found'],3)
        for s in r['services']:
            if s['service']['mode']=='sentinel':self.assertEqual(len(s['service']['endpoints']),6)
    def test_seed_redis_down_sentinel_still_finds_group(self):
        self.cli.down.add(('seed.example',6379));r=self.scan()
        g=next(s for s in r['services'] if s['service']['master_name']=='cache_app')
        self.assertEqual(len(g['service']['endpoints']),6);self.assertEqual(sum(not o['reachable'] for o in g['observations']),1)
    def test_advertised_port_outside_range(self):
        self.cli.add_group('outside',9000,26380);r=self.scan()
        g=next(s['service'] for s in r['services'] if s['service']['master_name']=='outside')
        self.assertEqual({e['port'] for e in g['endpoints'] if e['kind']=='redis'},{9000})
    def test_same_name_independent_groups_not_merged(self):
        self.cli.groups[('second-app',26381)]['name']='cache_app'
        self.assertEqual(sum(s['service']['mode']=='sentinel' for s in self.scan()['services']),2)
    def test_manual_single_list_range_and_sentinel(self):
        for spec,count in [({'ports':'6379'},1),({'port':6383},1),({'ports':'6379,6381'},2),({'ports':'6379-6381'},2),({'ports':'26379','kind':'sentinel'},1)]:
            with self.subTest(spec=spec):
                # Protected ports must explicitly declare the protocol before AUTH.
                r=self.scan(**dict({'kind':'redis'},**spec));self.assertEqual(r['scan']['mode'],'ports');self.assertEqual(len(r['services']),count)
    def test_no_range_scan_on_peer_hosts(self):
        self.scan();self.assertFalse(any(c==('INFO','SERVER') and e.host!='seed.example' for e,c,t in self.cli.calls))
        self.assertFalse(any(e.host.startswith('peer-') and e.port==6383 for e,c,t in self.cli.calls))
    def test_closed_ports_not_retried_with_password(self):
        self.scan();calls=[e for e,c,t in self.cli.calls if e.host=='seed.example' and e.port==6380]
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].auth,'none')
    def test_sentinel_has_no_credentials(self):
        self.scan();self.assertTrue(all(e.auth=='none' for e,c,t in self.cli.calls if e.port>=26000))
    def test_scan_has_no_writes_or_acl_commands(self):
        self.scan();self.assertLessEqual({c[:2] for e,c,t in self.cli.calls},{('INFO',),('INFO','SERVER'),('SENTINEL','MASTERS'),('SENTINEL','MASTER'),('SENTINEL','REPLICAS'),('SENTINEL','SENTINELS')})
    def test_range_validation_before_execution(self):
        for spec in ({'redis_end':6378},{'sentinel_end':0},{'redis_start':1,'redis_end':513},{'discovery_mode':'ports','ports':''},{'ports':'6379,6380','sentinel_port':26379}):
            with self.assertRaises(FarmError):self.scan(**spec)
        self.assertEqual(self.cli.calls,[])
    def test_overlapping_ranges_deduplicate_ports(self):
        p=self.discovery.plan({'host':'seed.example','redis_start':6379,'redis_end':6381,'sentinel_start':6380,'sentinel_end':6382})
        self.assertEqual(len(p['probes']),4)
    def test_last_port_not_lost_to_old_192_limit(self):
        self.cli.redis[('seed.example',6478)]={'role':'master'}
        r=Discovery(self.cli,Settings(Path('/unused'))).discover({'host':'seed.example'},'fixture-password','test',threading.Event())
        self.assertEqual(r['scan']['completed'],200);self.assertFalse(r['limited'])
        self.assertTrue(any(s['service']['seed']['port']==6478 for s in r['services']))
    def test_cluster_collects_peer_view(self):
        a=('seed.example',6384);b=('peer-b.example',7100);c=('peer-c.example',7200)
        self.cli.redis.update({x:{'role':'master'} for x in (a,b,c)});self.cli.cluster.update({a:[a,b],b:[a,b,c],c:[a,b,c]})
        g=next(s['service'] for s in self.scan(redis_end=6384)['services'] if s['service']['mode']=='cluster')
        self.assertEqual({(e['host'],e['port']) for e in g['endpoints']},{a,b,c})
    def test_parallelism_and_progress(self):
        cli=ScanFixtureCLI(delay=.003);progress=[]
        r=Discovery(cli,replace(self.settings,scan_workers=3)).discover({'host':'seed.example'},'fixture-password','test',threading.Event(),progress.append)
        self.assertGreater(cli.peak,1);self.assertLessEqual(cli.peak,3)
        self.assertEqual([v['completed'] for v in progress],sorted(v['completed'] for v in progress))
        self.assertEqual(progress[-1]['services_found'],len(r['services']))
    def test_cancel_before_scan(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(FarmError):self.discovery.discover({'host':'seed.example'},None,'test',cancel)
        self.assertEqual(self.cli.calls,[])
    def test_cancel_during_scan(self):
        cancel=threading.Event();cli=ScanFixtureCLI(delay=.01)
        def progress(v):
            if v['completed']>=2:cancel.set()
        with self.assertRaises(FarmError):Discovery(cli,self.settings).discover({'host':'seed.example'},'fixture-password','test',cancel,progress)
        self.assertFalse(any(c[0]=='SENTINEL' for e,c,t in cli.calls))
    def test_redis_and_sentinel_seeds_same_groups(self):
        r=self.scan(ports='6379,26379,6381,26381');self.assertEqual(len(r['services']),2)
        self.assertTrue(all(len(s['service']['endpoints'])==6 for s in r['services']))

    def test_direct_sentinel_has_complete_group(self):
        r=self.scan(ports='26379',kind='sentinel')
        self.assertEqual(len(r['services']),1)
        self.assertEqual(len(r['services'][0]['service']['endpoints']),6)
    def test_verified_IP_alias_preserves_entered_hostname(self):
        self.cli.hosts[0]='10.20.30.11'
        for g in self.cli.groups.values():
            if g['master']=='seed.example':g['master']='10.20.30.11'
        original=self.cli.master_record
        def aliased(g):
            r=original(g);r['runid']=r['runid'].replace('10.20.30.11','seed.example');return r
        self.cli.master_record=aliased
        g=next(s['service'] for s in self.scan()['services'] if s['service']['master_name']=='cache_app')
        self.assertEqual(len(g['endpoints']),6)
        self.assertTrue(any(e['host']=='seed.example' and e['port']==6379 for e in g['endpoints']))


class ScanStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Inventory(Path(self.tmp.name))
        d=Discovery(ScanFixtureCLI(),Settings(Path('/unused'),scan_redis_end=6383,scan_sentinel_end=26383))
        self.values=[s['service'] for s in d.discover({'host':'seed.example'},'fixture-password','test',threading.Event())['services']]
    def tearDown(self):self.tmp.cleanup()
    def test_repeat_scan_preserves_catalog_ids_and_custom_names(self):
        first=self.store.save_many(self.values);self.store.save({**first[0],'name':'custom','group':'custom-group'},first[0]['id'])
        second=self.store.save_many(self.values,preserve_labels=True)
        self.assertEqual({s['id'] for s in first},{s['id'] for s in second});self.assertEqual(len(self.store.list()),3)
        self.assertEqual(self.store.get(first[0]['id'])['name'],'custom')
    def test_validation_is_atomic(self):
        with self.assertRaises(FarmError):self.store.save_many(self.values+[{'mode':'invalid'}])
        self.assertEqual(self.store.list(),[])
    def test_rollback_mid_transaction(self):
        original=self.store._save_in;count=0
        def fail(db,clean,**kw):
            nonlocal count
            count+=1
            if count==2:raise OSError('fixture storage failure')
            return original(db,clean,**kw)
        with patch.object(self.store,'_save_in',side_effect=fail):
            with self.assertRaises(OSError):self.store.save_many(self.values)
        self.assertEqual(self.store.list(),[])


class ProgressJobTests(unittest.TestCase):
    def test_commit_and_progress_published_then_cancel_is_noop(self):
        jobs=Jobs();commits=[]
        try:
            def run(cancel):jobs.report('owner',cancel,{'completed':1});return {}
            job=jobs.submit('owner','scan',run,commit=lambda r:commits.append(r) or r);job.future.result(timeout=3)
            self.assertEqual(job.public()['progress']['completed'],1);self.assertEqual(len(commits),1)
            jobs.cancel_job('owner',job.id);self.assertEqual(job.state,'done')
        finally:jobs.shutdown()
    def test_cancel_prevents_commit(self):
        jobs=Jobs();release=threading.Event();started=threading.Event();commits=[]
        try:
            def run(cancel):started.set();release.wait(3);return {}
            job=jobs.submit('owner','scan',run,commit=lambda r:commits.append(r))
            started.wait(1);jobs.cancel_job('owner',job.id);release.set();job.future.result(timeout=3)
            self.assertEqual(commits,[]);self.assertEqual(job.state,'cancelled')
        finally:release.set();jobs.shutdown()

if __name__=='__main__':unittest.main()
