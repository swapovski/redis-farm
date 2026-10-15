"""DNS regression tests. Only local fixture sockets are used; no infrastructure calls."""
from dataclasses import replace
from pathlib import Path
import socket
import socketserver
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.resolver
import dns.rrset

from redis_farm.cli import RedisCLI, CliError
from redis_farm.discovery import Discovery
from redis_farm.models import Endpoint
from redis_farm.resolver import HostResolver, ResolutionError
from redis_farm.settings import Settings
from redis_farm.telemetry import Telemetry
from redis_farm.terminal import Terminals

FIXTURE = str(Path(__file__).with_name('fake_cli.py').resolve())
HOST = 'redis-a.internal.test'


def make_response(query, addresses=('10.20.30.11',), ttl=60, *, cname=False):
    response = dns.message.make_response(query)
    name = query.question[0].name
    if cname:
        response.answer.append(dns.rrset.from_text(name, 20, 'IN', 'CNAME', 'target.internal.test.'))
        name = 'target.internal.test.'
    if addresses:
        response.answer.append(dns.rrset.from_text(name, ttl, 'IN', 'A', *addresses))
    return dns.message.from_wire(response.to_wire())


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(Path('/unused'),dns_servers=('192.0.2.53',))
        self.resolver = HostResolver(self.settings)

    def test_default_nameserver_and_no_system_config(self):
        with patch.object(dns.resolver.Resolver, 'read_resolv_conf', side_effect=AssertionError('OS config read')):
            resolver = HostResolver(self.settings)
        self.assertEqual(resolver._resolver.nameservers, ['192.0.2.53'])
        self.assertTrue(resolver.status()['system_fallback'])

    def test_invalid_dns_configuration(self):
        for server in ('resolver.example', 'https://dns.example', 'not-an-ip'):
            with self.assertRaises(ValueError):
                HostResolver(replace(self.settings, dns_servers=(server,)))
        for value in (0, -1, 31, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                HostResolver(replace(self.settings, dns_timeout=value))

    def test_private_ip_is_literal_without_any_dns(self):
        with patch.object(self.resolver._resolver, 'resolve', side_effect=AssertionError('DNS not needed')):
            result = self.resolver.resolve('10.20.30.11')
        self.assertEqual(result.connect_host, '10.20.30.11')
        self.assertEqual(result.source, 'literal')

    def test_explicit_public_ip_is_not_reverse_resolved(self):
        with patch.object(self.resolver._resolver, 'resolve', side_effect=AssertionError('no PTR')):
            result = self.resolver.resolve('198.51.100.7')
        self.assertEqual(result.connect_host, '198.51.100.7')
        self.assertEqual(result.source, 'literal')

    def test_ipv6_literal_is_preserved(self):
        result = self.resolver.resolve('[fd00::1]')
        self.assertEqual(result.connect_host, 'fd00::1')
        self.assertEqual(result.source, 'literal')

    def test_system_mode_is_explicit_and_does_not_call_python_dns(self):
        resolver = HostResolver(replace(self.settings, dns_servers=()))
        with patch('socket.getaddrinfo', side_effect=AssertionError('let redis-cli resolve')):
            result = resolver.resolve(HOST)
        self.assertEqual(result.connect_host, HOST)
        self.assertEqual(result.source, 'system')

    def test_A_query_only_to_configured_server_and_absolute_name(self):
        def udp(query, where, **kwargs):
            self.assertEqual(where, '192.0.2.53')
            self.assertEqual(query.question[0].rdtype, dns.rdatatype.A)
            self.assertEqual(str(query.question[0].name), HOST + '.')
            return make_response(query)
        with patch('dns.query.udp', side_effect=udp) as wire, patch('socket.getaddrinfo', side_effect=AssertionError('OS DNS')):
            result = self.resolver.resolve(HOST)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(result.connect_host, '10.20.30.11')
        self.assertEqual(result.requested_host, HOST)
        self.assertEqual(result.dns_servers, ('192.0.2.53',))
        self.assertGreater(result.ttl_remaining, 0)

    def test_split_dns_ignores_public_system_answer(self):
        # Simulate split DNS without contacting either DNS view or a Redis server.
        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.51.100.7', 6379))]
        with patch('socket.getaddrinfo', return_value=public_answer) as system, \
             patch('dns.query.udp', side_effect=lambda q, where, **kw: make_response(q)):
            result = self.resolver.resolve(HOST)
        self.assertEqual(result.connect_host, '10.20.30.11')
        system.assert_not_called()

    def test_multiple_records_prefer_RFC1918_then_stable_numeric_order(self):
        def udp(query, where, **kwargs):
            return make_response(query, ('198.51.100.1', '10.20.30.13', '10.20.30.11'))
        with patch('dns.query.udp', side_effect=udp):
            result = self.resolver.resolve(HOST)
        self.assertEqual(result.connect_host, '10.20.30.11')
        self.assertEqual(result.addresses, ('10.20.30.11', '10.20.30.13', '198.51.100.1'))

    def test_public_only_answer_is_reported_not_guessed_or_blocked(self):
        with patch('dns.query.udp', side_effect=lambda q, where, **kw: make_response(q, ('198.51.100.8',))):
            result = self.resolver.resolve(HOST)
        self.assertEqual(result.connect_host, '198.51.100.8')
        self.assertEqual(result.source, 'dns')

    def test_CNAME_chain_preserves_original_name_and_shortest_ttl(self):
        with patch('dns.query.udp', side_effect=lambda q, where, **kw: make_response(q, cname=True)):
            result = self.resolver.resolve(HOST)
        self.assertEqual(result.requested_host, HOST)
        self.assertEqual(result.connect_host, '10.20.30.11')
        self.assertLessEqual(result.ttl_remaining, 20)

    def test_ttl_cache_reused_and_expired_answer_refreshes(self):
        count = []
        def udp(query, where, **kwargs):
            count.append(1)
            return make_response(query, ('10.20.30.11' if len(count) == 1 else '10.20.30.12',))
        with patch('dns.query.udp', side_effect=udp):
            first = self.resolver.resolve(HOST)
            again = self.resolver.resolve(HOST)
            key = (dns.name.from_text(HOST), dns.rdatatype.A, dns.rdataclass.IN)
            self.resolver._resolver.cache.get(key).expiration = 0
            changed = self.resolver.resolve(HOST)
        self.assertEqual(first.connect_host, again.connect_host)
        self.assertEqual(changed.connect_host, '10.20.30.12')
        self.assertEqual(len(count), 2)

    def test_dns_timeout_uses_system_answer(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.51.100.7', 0))]
        with patch.object(self.resolver._resolver, 'resolve', side_effect=dns.exception.Timeout), \
             patch('socket.getaddrinfo', return_value=records) as fallback:
            result = self.resolver.resolve(HOST)
        self.assertEqual(result.connect_host, '198.51.100.7')
        self.assertEqual(result.source, 'system_fallback')
        self.assertEqual(fallback.call_count, 1)

    def test_nxdomain_noanswer_and_servfail_use_system_resolver(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.51.100.7', 0))]
        for exc in (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            resolver = HostResolver(self.settings)
            with self.subTest(exc=exc), patch.object(resolver._resolver, 'resolve', side_effect=exc), \
                 patch('socket.getaddrinfo', return_value=records):
                self.assertEqual(resolver.resolve(HOST).source, 'system_fallback')

    def test_no_stale_address_after_expired_cache_and_both_dns_fail(self):
        with patch('dns.query.udp', side_effect=lambda q, where, **kw: make_response(q)):
            self.resolver.resolve(HOST)
        key = (dns.name.from_text(HOST), dns.rdatatype.A, dns.rdataclass.IN)
        self.resolver._resolver.cache.get(key).expiration = 0
        with patch.object(self.resolver._resolver, 'resolve', side_effect=dns.exception.Timeout), \
             patch('socket.getaddrinfo', side_effect=socket.gaierror):
            with self.assertRaises(ResolutionError) as caught:
                self.resolver.resolve(HOST)
        self.assertEqual(caught.exception.code, 'dns_resolution_failed')


class TransportDNSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name), cli_path=FIXTURE,dns_servers=('192.0.2.53',))
        self.resolver = HostResolver(self.settings)
        self.wire = patch('dns.query.udp', side_effect=lambda q, where, **kw: make_response(q))
        self.wire.start()
        self.cli = RedisCLI(self.settings, resolver=self.resolver)
        self.endpoint = Endpoint(HOST, 6379)

    def tearDown(self):
        self.cli.shutdown()
        self.wire.stop()
        self.tmp.cleanup()

    def test_command_uses_resolved_IP_without_changing_persistent_endpoint(self):
        reply = self.cli.run(self.endpoint, ['OPTIONS'], 'fixture-only', output='json')
        self.assertEqual(reply.value['-h'], '10.20.30.11')
        self.assertEqual(reply.connection['address'], '10.20.30.11:6379')
        self.assertEqual(self.endpoint.host, HOST)
        self.assertEqual(self.endpoint.to_dict()['host'], HOST)
        self.assertNotIn('fixture-only', str(reply.connection))

    def test_sentinel_resolution_keeps_port_and_omits_credentials(self):
        ep = Endpoint(HOST, 26379, 'sentinel')
        args, connection = self.cli.prepare_connection(ep)
        self.assertEqual(args[args.index('-h') + 1], '10.20.30.11')
        self.assertEqual(args[args.index('-p') + 1], '26379')
        self.assertNotIn('--user', args)
        self.assertNotIn('REDISCLI_AUTH', self.cli.environment(ep, 'secret'))
        self.assertEqual(connection['port'], 26379)

    def test_terminal_connects_by_IP_and_records_initial_target(self):
        manager = Terminals(self.cli, self.settings)
        try:
            terminal = manager.open('owner', self.endpoint, 'fixture-only')
            self.assertEqual(terminal.poll(0)['connection']['connect_host'], '10.20.30.11')
            self.assertEqual(terminal.poll(0)['endpoint']['host'], HOST)
            terminal.send('OPTIONS')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                output = ''.join(terminal.poll(0)['chunks'])
                if '10.20.30.11' in output:
                    break
                time.sleep(.02)
            self.assertIn('10.20.30.11', output)
        finally:
            manager.shutdown()

    def test_failed_DNS_starts_no_command_subprocess(self):
        with patch.object(self.resolver._resolver, 'resolve', side_effect=dns.exception.Timeout), \
             patch('socket.getaddrinfo', side_effect=socket.gaierror), \
             patch('redis_farm.cli.subprocess.Popen', side_effect=AssertionError('CLI must not start')):
            with self.assertRaises(CliError) as caught:
                self.cli.run(self.endpoint, ['INFO'], 'fixture-only')
        self.assertEqual(caught.exception.code, 'dns_resolution_failed')

    def test_failed_DNS_starts_no_terminal_subprocess(self):
        manager = Terminals(self.cli, self.settings)
        with patch.object(self.resolver._resolver, 'resolve', side_effect=dns.resolver.NXDOMAIN), \
             patch('socket.getaddrinfo', side_effect=socket.gaierror), \
             patch('redis_farm.terminal.subprocess.Popen', side_effect=AssertionError('CLI must not start')):
            with self.assertRaises(CliError):
                manager.open('owner', self.endpoint, 'fixture-only')
        self.assertFalse(manager.items)

    def test_TLS_retains_hostname_SNI_and_explicit_SNI_override(self):
        args, _ = self.cli.prepare_connection(replace(self.endpoint, tls=True))
        self.assertEqual(args[args.index('-h') + 1], '10.20.30.11')
        self.assertEqual(args[args.index('--sni') + 1], HOST)
        args, _ = self.cli.prepare_connection(replace(self.endpoint, tls=True, sni='tls.internal.test'))
        self.assertEqual(args[args.index('--sni') + 1], 'tls.internal.test')

    def test_cancelled_job_does_not_resolve_or_start(self):
        cancel = threading.Event(); cancel.set()
        with patch.object(self.resolver, 'resolve', side_effect=AssertionError('no DNS')):
            with self.assertRaises(CliError) as caught:
                self.cli.run(self.endpoint, ['INFO'], 'fixture-only', cancel=cancel)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_discovery_retains_hostname_and_exposes_connection(self):
        result = Discovery(self.cli, self.settings).discover(
            {'host': HOST, 'ports': '6379', 'kind': 'redis', 'find_sentinel': False},
            'fixture-only', 'owner', threading.Event())
        self.assertFalse(result['errors'])
        entry = result['services'][0]
        self.assertEqual(entry['service']['seed']['host'], HOST)
        self.assertEqual(entry['observations'][0]['connection']['connect_host'], '10.20.30.11')
        self.assertEqual(entry['service']['host_aliases'], {'10.20.30.11': HOST})
        self.assertEqual(entry['observations'][0]['display']['host'], HOST)

    def test_scan_hostname_then_save_updates_existing_ip_without_duplicate(self):
        from redis_farm.storage import Inventory
        store = Inventory(Path(self.tmp.name) / 'alias-inventory', resolver=self.resolver)
        ip = Endpoint('10.20.30.11', 6379).to_dict()
        original = store.save({'mode': 'standalone', 'name': 'Existing', 'seed': ip, 'endpoints': [ip]})
        result = Discovery(self.cli, self.settings).discover(
            {'host': HOST, 'ports': '6379', 'kind': 'redis', 'find_sentinel': False},
            'fixture-only', 'owner', threading.Event())
        saved = store.save_many([item['service'] for item in result['services']], preserve_labels=True)[0]
        self.assertEqual(saved['id'], original['id'])
        self.assertEqual(saved['endpoints'], original['endpoints'])
        self.assertEqual(saved['host_aliases']['10.20.30.11'], HOST)
        self.assertEqual(len(store.list()), 1)

    def test_telemetry_records_IP_used_for_INFO(self):
        row = Telemetry(self.cli, self.settings)._sample(
            self.endpoint, 'fixture-only', 'owner', threading.Event(), {})
        self.assertTrue(row['reachable'])
        self.assertEqual(row['connection']['connect_host'], '10.20.30.11')


class LocalDNSWireTests(unittest.TestCase):
    def test_real_local_DNS_UDP_with_TCP_truncation_fallback(self):
        # Bind loopback only. The fixture returns a reserved Redis address; no Redis
        # connection is attempted. This exercises dnspython's actual wire transport.
        calls = []
        class UDP(socketserver.BaseRequestHandler):
            def handle(self):
                packet, sock = self.request
                query = dns.message.from_wire(packet)
                response = dns.message.make_response(query)
                response.flags |= dns.flags.TC
                calls.append('udp')
                sock.sendto(response.to_wire(), self.client_address)
        class TCP(socketserver.BaseRequestHandler):
            def handle(self):
                def exact(size):
                    result = b''
                    while len(result) < size:
                        part = self.request.recv(size - len(result))
                        if not part:
                            raise ConnectionError('truncated fixture query')
                        result += part
                    return result
                query = dns.message.from_wire(exact(struct.unpack('!H', exact(2))[0]))
                wire = make_response(query).to_wire()
                calls.append('tcp')
                self.request.sendall(struct.pack('!H', len(wire)) + wire)
        with socketserver.TCPServer(('127.0.0.1', 0), TCP) as tcp:
            with socketserver.UDPServer(('127.0.0.1', tcp.server_address[1]), UDP) as udp:
                threads = [threading.Thread(target=s.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
                           for s in (tcp, udp)]
                for thread in threads: thread.start()
                try:
                    resolver = HostResolver(Settings(Path('/unused'), dns_servers=('127.0.0.1',)))
                    resolver._resolver.port = tcp.server_address[1]
                    result = resolver.resolve(HOST)
                    self.assertEqual(result.connect_host, '10.20.30.11')
                    self.assertEqual(calls, ['udp', 'tcp'])
                finally:
                    tcp.shutdown(); udp.shutdown()
                    for thread in threads: thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
