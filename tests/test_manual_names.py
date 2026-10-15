"""Manual FQDN labels and physical identity; all resolver answers are fixtures."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from redis_farm.models import Endpoint, FarmError, clean_service
from redis_farm.storage import Inventory
from redis_farm.display import HostLabels
from redis_farm.credentials import DiscoveryCredentials
from redis_farm.runtime import Sessions
from redis_farm.settings import Settings

IP='10.20.30.12'; NAME='redis-shared-02.dev.example.net'

def service(host,port=6379,auth='admin',**extra):
    ep=Endpoint(host,port,auth=auth).to_dict()
    return {'name':ep['address'],'group':'cluster','mode':'standalone','seed':ep,'endpoints':[ep],**extra}

class ManualNamesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.resolver=Mock()
        self.resolver.resolve.side_effect=lambda host:SimpleNamespace(connect_host=IP if host in (NAME,'another-alias.test') else host)
        self.resolver.display_ip.side_effect=lambda host:IP if host==NAME else host
        self.resolver.reverse.side_effect=AssertionError('PTR must not be used')
        self.store=Inventory(Path(self.tmp.name),resolver=self.resolver)
    def tearDown(self):self.tmp.cleanup()
    def test_manual_name_reuses_existing_service_and_endpoint_ids(self):
        old=self.store.save(service(IP,name='custom-name',group='custom-group'))
        saved=self.store.save_many([service(NAME,host_aliases={IP:NAME},group='')],preserve_labels=True)[0]
        self.assertEqual(saved['id'],old['id']);self.assertEqual(saved['endpoints'],old['endpoints'])
        self.assertEqual(saved['name'],'custom-name');self.assertEqual(saved['group'],'custom-group')
        self.assertEqual(len(self.store.list()),1);self.assertEqual(saved['host_aliases'],{IP:NAME})
    def test_existing_tls_parameters_are_not_replaced_by_alias_add(self):
        old=service(IP);old['endpoints'][0].update(tls=True,cacert='/trusted/ca.pem',sni='redis.test');old['seed']=old['endpoints'][0]
        first=self.store.save(old)
        saved=self.store.save_many([service(NAME,host_aliases={IP:NAME})],preserve_labels=True)[0]
        self.assertEqual(saved['endpoints'],first['endpoints']);self.assertEqual(saved['seed'],first['seed'])
    def test_different_port_stays_independent(self):
        self.store.save(service(IP))
        self.store.save_many([service(NAME,6380,host_aliases={IP:NAME})])
        self.assertEqual(len(self.store.list()),2)
    def test_different_auth_stays_independent(self):
        self.store.save(service(IP))
        self.store.save_many([service(NAME,auth='default',host_aliases={IP:NAME})])
        self.assertEqual(len(self.store.list()),2)
    def test_sentinal_groups_not_merged_by_a_shared_sentinel(self):
        a=service(IP,mode='sentinel',master_name='one');b=service(NAME,mode='sentinel',master_name='two',host_aliases={IP:NAME})
        self.store.save(a);self.store.save(b);self.assertEqual(len(self.store.list()),2)
    def test_old_names_preserved_during_ip_rescan(self):
        old=self.store.save(service(IP,host_aliases={IP:NAME}))
        result=self.store.save_many([service(IP)],preserve_labels=True)[0]
        self.assertEqual(result['host_aliases'],{IP:NAME});self.assertEqual(result['id'],old['id'])
    def test_latest_explicit_name_wins_for_same_address(self):
        old=self.store.save(service(IP,host_aliases={IP:NAME}))
        result=self.store.save_many([service('another-alias.test',host_aliases={IP:'another-alias.test'})])[0]
        self.assertEqual(result['id'],old['id']);self.assertEqual(result['host_aliases'][IP],'another-alias.test')
    def test_export_import_retains_labels(self):
        self.store.save(service(IP,host_aliases={IP:NAME}));data=self.store.export()
        other=Inventory(Path(self.tmp.name)/'other',resolver=self.resolver)
        self.assertEqual(other.import_data(data)[0]['host_aliases'],{IP:NAME})
    def test_label_does_not_change_endpoint(self):
        svc=service(IP,host_aliases={IP:NAME});row={'endpoint':svc['seed'],'connection':{'connect_host':IP}}
        before=deepcopy(row['endpoint']);HostLabels(self.resolver).annotate(svc,[row])
        self.assertEqual(row['display']['host'],NAME);self.assertEqual(row['display']['ip'],IP)
        self.assertEqual(row['display']['source'],'manual');self.assertEqual(row['endpoint'],before)
        self.resolver.reverse.assert_not_called()
    def test_peer_without_explicit_name_stays_ip(self):
        svc=service(IP,host_aliases={IP:NAME});ep=Endpoint('10.20.30.13',6379).to_dict();row={'endpoint':ep}
        HostLabels(self.resolver).annotate(svc,[row]);self.assertEqual(row['display']['host'],'10.20.30.13')
        self.resolver.reverse.assert_not_called()
    def test_same_host_redis_and_sentinel_share_label_not_endpoint(self):
        svc=service(IP,host_aliases={IP:NAME})
        rows=[{'endpoint':Endpoint(IP,p,k).to_dict()} for p,k in ((6379,'redis'),(26379,'sentinel'))]
        HostLabels(self.resolver).annotate(svc,rows)
        self.assertEqual({r['display']['host'] for r in rows},{NAME})
        self.assertNotEqual(rows[0]['endpoint']['id'],rows[1]['endpoint']['id'])
    def test_snapshot_does_not_reintroduce_dns_alias_endpoint(self):
        old=self.store.save(service(IP,host_aliases={IP:NAME}))
        value=self.store.merge_endpoints(old['id'],[Endpoint(NAME,6379).to_dict()])
        self.assertEqual(value['endpoints'],old['endpoints'])
    def test_dns_failure_does_not_guess_equivalence(self):
        self.store.save(service(IP));self.resolver.resolve.side_effect=FarmError('fixture DNS missing')
        self.store.save_many([service(NAME)])
        self.assertEqual(len(self.store.list()),2)
    def test_external_and_internal_addresses_not_inferred_equivalent(self):
        self.store.save(service(IP));self.store.save_many([service('162.248.160.157')])
        self.assertEqual(len(self.store.list()),2)
    def test_invalid_label_mapping_rejected(self):
        for labels in ({NAME:NAME},{IP:IP},{IP:'https://bad.test/'},'bad'):
            with self.subTest(labels=labels),self.assertRaises(FarmError):clean_service(service(IP,host_aliases=labels))
    def test_alias_rescan_preserves_default_endpoint_override(self):
        settings=Settings(Path(self.tmp.name));sessions=Sessions(settings);owner=sessions.create().id
        old=self.store.save(service(IP,auth='default'))
        sessions.set_default(owner,old['id'],'old-fixture',[old['seed']['id']])
        result=self.store.save_many([service(NAME,auth='default',host_aliases={IP:NAME})],preserve_labels=True)[0]
        self.assertEqual(sessions.credentials(owner,result['id']).for_endpoint(Endpoint.from_dict(result['seed'])),'old-fixture')
    def test_verified_discovery_credential_survives_canonical_endpoint(self):
        settings=Settings(Path(self.tmp.name));sessions=Sessions(settings);owner=sessions.create().id
        old=self.store.save(service(IP,auth='default'))
        seed=Endpoint(NAME,6379,auth='default');secret=DiscoveryCredentials(seed=seed,password='fixture',resolver=self.resolver)
        secret.mark_verified(seed)
        saved=self.store.save_many([service(NAME,auth='default',host_aliases={IP:NAME})],preserve_labels=True)
        secret.bind(sessions,owner,saved)
        self.assertEqual(sessions.credentials(owner,old['id']).for_endpoint(Endpoint(IP,6379,auth='default')),'fixture')
    def test_unverified_discovery_credential_is_not_saved(self):
        settings=Settings(Path(self.tmp.name));sessions=Sessions(settings);owner=sessions.create().id
        old=self.store.save(service(IP,auth='default'))
        secret=DiscoveryCredentials(seed=Endpoint(NAME,6379,auth='default'),password='fixture',resolver=self.resolver)
        secret.bind(sessions,owner,[old])
        self.assertIsNone(sessions.credentials(owner,old['id']).for_endpoint(Endpoint(IP,6379,auth='default')))

if __name__=='__main__':unittest.main()
