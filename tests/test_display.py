"""DNS display and catalog regressions. Never contacts a remote DNS or Redis."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
import dns.exception
import dns.message
import dns.rcode
import dns.rdatatype
import dns.rrset
from redis_farm.models import Endpoint, FarmError, clean_service, host_display_policy
from redis_farm.resolver import HostResolver
from redis_farm.display import HostLabels
from redis_farm.storage import Inventory
from redis_farm.discovery import Discovery
from redis_farm.settings import Settings
from test_discovery_auto import ScanFixtureCLI

SEED = 'redis-cache-01.example.net'
PEER = 'redis-cache-02.example.net'
IP1, IP2 = '10.20.30.11', '10.20.30.12'

def service(host=SEED):
    ep=Endpoint(host,6379).to_dict()
    return {'seed':ep,'endpoints':[ep],'name':'cache_app','group':'', 'mode':'standalone'}

def row(host, port=6379, ip=None):
    value={'endpoint':Endpoint(host,port,'sentinel' if port>20000 else 'redis').to_dict(), 'reachable':True}
    if ip:
        value['connection']={'connect_host':ip}
    return value

def ptr_response(query,names=(PEER+'.',),ttl=60):
    r=dns.message.make_response(query)
    if names:
        r.answer.append(dns.rrset.from_text(query.question[0].name,ttl,'IN','PTR',*names))
    return dns.message.from_wire(r.to_wire())

class ForwardLabelResolverTests(unittest.TestCase):
    def test_literal_addresses_do_not_query_dns(self):
        resolver=HostResolver(Settings(Path('/unused'),dns_servers=('192.0.2.53',)))
        with patch('socket.gethostbyaddr',side_effect=AssertionError('PTR forbidden')), \
             patch('dns.resolver.Resolver.resolve',side_effect=AssertionError('DNS forbidden')):
            for ip in (IP1,'162.248.160.157','fd00::21'):
                self.assertEqual(resolver.display_ip(ip),ip)
    def test_system_display_uses_bounded_os_forward_lookup(self):
        resolver=HostResolver(Settings(Path('/unused'),dns_servers=()))
        with patch.object(resolver,'_system_addresses',return_value=(IP2,)) as os_dns, \
             patch('dns.resolver.Resolver.resolve_address',side_effect=AssertionError('PTR forbidden')):
            self.assertEqual(resolver.display_ip(PEER),IP2)
        os_dns.assert_called_once_with(PEER)
    def test_custom_missing_uses_same_system_fallback_as_connection(self):
        resolver=HostResolver(Settings(Path('/unused'),dns_servers=('192.0.2.53',)))
        with patch.object(resolver._resolver,'resolve',side_effect=dns.resolver.NXDOMAIN), \
             patch.object(resolver,'_system_addresses',return_value=(IP2,)) as os_dns:
            self.assertEqual(resolver.display_ip(PEER),IP2)
            self.assertEqual(resolver.resolve(PEER).connect_host,IP2)
        os_dns.assert_called_once_with(PEER)
    def test_no_reverse_implementation_is_exposed(self):
        self.assertFalse(hasattr(HostResolver(Settings(Path('/unused'),dns_servers=('192.0.2.53',))),'reverse'))

class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.resolver=Mock()
        self.resolver.display_ip.side_effect=lambda h:{SEED:IP1,PEER:IP2}[h]
        self.resolver.reverse.side_effect=AssertionError('PTR forbidden')
    def decorate(self,rows,host=SEED,**extra):
        value={**service(host),**extra};HostLabels(self.resolver).annotate(value,rows);return rows
    def test_hostname_input_preserves_seed_and_labels_peers_by_ip(self):
        rows=[row(SEED,ip=IP1),row(IP2),row(IP2,26379)];before=deepcopy(rows)
        self.decorate(rows)
        self.assertEqual([r['display']['host'] for r in rows],[SEED,IP2,IP2])
        self.assertEqual([r['endpoint'] for r in rows],[r['endpoint'] for r in before])
        self.resolver.reverse.assert_not_called();self.resolver.display_ip.assert_not_called()
    def test_seed_hostname_and_ip_alias_share_named_card(self):
        rows=self.decorate([row(SEED,ip=IP1),row(IP1,26379)])
        self.assertEqual({r['display']['host'] for r in rows},{SEED})
        self.assertEqual({r['display']['host_key'] for r in rows},{IP1})
    def test_ip_input_resolves_advertised_hostnames_forward_only(self):
        rows=self.decorate([row(IP1),row(PEER)],host=IP1)
        self.assertEqual([r['display']['host'] for r in rows],[IP1,IP2])
        self.resolver.reverse.assert_not_called()
        self.resolver.display_ip.assert_called_once_with(PEER)
    def test_pure_ip_rows_recover_seed_by_forward_dns_only(self):
        self.decorate([row(IP1),row(IP2)])
        self.resolver.display_ip.assert_called_once_with(SEED);self.resolver.reverse.assert_not_called()
    def test_ip_no_note_and_no_change_in_reachability(self):
        r=self.decorate([row(IP2)])[0]
        self.assertEqual(r['display']['host'],IP2)
        self.assertFalse(r['display']['note']);self.assertTrue(r['reachable'])
    def test_unresolved_hostname_preserved_without_inventing_ip(self):
        self.resolver.display_ip.side_effect=FarmError('fixture DNS failure','dns_error')
        r=self.decorate([row(PEER)])[0]
        self.assertEqual(r['display']['host'],PEER)
        self.assertEqual(r['display']['ip'],'');self.assertTrue(r['display']['note'])
    def test_unreachable_ip_never_receives_provider_name(self):
        r=row(IP2);r['reachable']=False;self.decorate([r])
        self.assertEqual(r['display']['host'],IP2);self.assertFalse(r['reachable'])
        self.resolver.reverse.assert_not_called()
    def test_distinct_ips_are_not_merged_by_name(self):
        rows=self.decorate([row(IP2),row('10.20.30.13')])
        self.assertNotEqual(rows[0]['display']['host_key'],rows[1]['display']['host_key'])
    def test_public_peer_not_translated_to_private_ip(self):
        r=self.decorate([row('162.248.160.157')])[0]
        self.assertEqual(r['endpoint']['host'],'162.248.160.157')
        self.assertEqual(r['display']['host'],'162.248.160.157')
        self.resolver.display_ip.assert_called_once_with(SEED)
    def test_internal_only_cluster_needs_no_public_address(self):
        rows=self.decorate([row(IP1),row(IP2),row('10.20.30.13')])
        self.assertEqual([r['display']['host'] for r in rows],[SEED,IP2,'10.20.30.13'])
        self.resolver.display_ip.assert_called_once_with(SEED)
    def test_cancel_before_dns(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(FarmError):HostLabels(self.resolver).annotate(service(),[row(PEER)],cancel)
        self.resolver.display_ip.assert_not_called()
    def test_old_hostname_policy_no_longer_enables_ptr(self):
        rows=self.decorate([row(IP2)],display_mode='hostname',display_seed_host=SEED)
        self.assertEqual(rows[0]['display']['mode'],'ip')
        self.assertEqual(rows[0]['display']['host'],IP2)
    def test_old_catalog_policy_is_still_readable(self):
        self.assertEqual(host_display_policy(service()),('hostname',SEED))
        self.assertEqual(host_display_policy(service(IP1)),('ip',IP1))
    def test_invalid_display_mode_rejected(self):
        with self.assertRaises(FarmError):clean_service({**service(),'display_mode':'other'})
    def test_connection_ip_wins_over_new_dns_answer(self):
        rows=self.decorate([row(SEED,ip=IP2)])
        self.assertEqual(rows[0]['display']['host'],SEED)
        self.assertEqual(rows[0]['display']['ip'],IP2)
        self.resolver.display_ip.assert_not_called()
    def test_per_row_connection_address_is_retained(self):
        rows=self.decorate([row(SEED,ip=IP1),row(SEED,6380,ip=IP2)])
        self.assertEqual([r['display']['host'] for r in rows],[SEED,SEED])
        self.assertEqual([r['display']['ip'] for r in rows],[IP1,IP2])
    def test_forward_lookup_once_per_hostname_for_many_ports(self):
        rows=self.decorate([row(PEER),row(PEER,6380),row(PEER,26379)])
        self.assertEqual({r['display']['host'] for r in rows},{IP2})
        self.assertEqual([c.args[0] for c in self.resolver.display_ip.call_args_list],[PEER,SEED])
    def test_ipv6_format(self):
        r=self.decorate([row('fd00::21')])[0]
        self.assertEqual(r['display']['address'],'[fd00::21]:6379')
    def test_stale_display_from_old_ptr_is_replaced(self):
        r=row(IP2);r['display']={'host':'static.provider.example','source':'ptr'}
        self.decorate([r]);self.assertEqual(r['display']['host'],IP2)
    def test_no_resolver_cannot_invent_ip(self):
        r=row(PEER);HostLabels(None).annotate(service(),[r])
        self.assertEqual(r['display']['host'],PEER);self.assertEqual(r['display']['source'],'unresolved')

class DisplayStorageTests(unittest.TestCase):
    def test_origin_and_policy_survive_export_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Inventory(Path(tmp));s=store.save(service());payload=store.export()
            store.delete(s['id']);restored=store.import_data(payload)[0]
            self.assertEqual(restored['display_seed_host'],SEED);self.assertEqual(restored['display_mode'],'hostname')
    def test_display_metadata_not_saved_and_legacy_record_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Inventory(Path(tmp));r=service();r['display']={'host':'should-not-be-saved'}
            saved=store.save(r);self.assertNotIn('display',saved)
            body=clean_service(r);body.pop('display_mode');body.pop('display_seed_host')
            with store._db() as db:db.execute('UPDATE services SET body=? WHERE id=?',(json.dumps(body),saved['id']))
            self.assertEqual(host_display_policy(store.get(saved['id'])),('hostname',SEED))
    def test_rescan_assigns_explicit_cluster_group_but_preserves_origin_and_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Inventory(Path(tmp));first=store.save({**service(),'name':'custom-name'})
            updated=store.save_many([{**service(),'group':'redis-shared-{1-3}.prod.example.net',
                                    'display_seed_host':IP1,'display_mode':'ip'}],preserve_labels=True)[0]
            self.assertEqual(updated['id'],first['id']);self.assertEqual(updated['name'],'custom-name')
            self.assertEqual(updated['display_seed_host'],SEED);self.assertEqual(updated['display_mode'],'hostname')
            self.assertEqual(updated['group'],'redis-shared-{1-3}.prod.example.net')
            again=store.save_many([service()],preserve_labels=True)[0];self.assertEqual(again['group'],updated['group'])
    def test_scan_IP_origin_kept_even_when_topology_advertises_hostnames(self):
        settings=Settings(Path('/unused'),scan_redis_end=6383,scan_sentinel_end=26383)
        result=Discovery(ScanFixtureCLI(),settings).discover({'host':IP1},'fixture-password','test',threading.Event())
        self.assertTrue(result['services'])
        for entry in result['services']:
            self.assertEqual(entry['service']['display_seed_host'],IP1)
            self.assertEqual(entry['service']['display_mode'],'ip')

if __name__=='__main__':unittest.main()
