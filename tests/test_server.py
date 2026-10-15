"""Network boundary and server-config tests; no remote Redis/DNS requests."""
from dataclasses import replace
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from redis_farm.access import AccessPolicy, normalize_host
from redis_farm.models import FarmError
from redis_farm.settings import Settings
from redis_farm.server import parse_config, load_config, check_certificate

ROOT = Path(__file__).resolve().parents[1]


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings = parse_config({'bind_host':'192.0.2.10','allowed_client_cidrs':['192.0.2.0/24','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8','::1/128']}).settings
        self.policy = AccessPolicy(self.settings)

    def denied(self, host='192.0.2.10:8765', peer='10.1.2.3', scheme='https', code=None):
        with self.assertRaises(FarmError) as exc:
            self.policy.check(host, peer, scheme)
        if code:
            self.assertEqual(exc.exception.code, code)
        self.assertEqual(exc.exception.status, 403)

    def test_internal_https_allowed(self):
        for peer in ('10.1.2.3', '172.16.1.2', '192.168.22.10', '127.0.0.1', '::1'):
            self.policy.check('192.0.2.10:8765', peer, 'https')

    def test_public_peer_denied(self):
        self.denied(peer='8.8.8.8', code='client_denied')

    def test_http_denied(self):
        self.denied(scheme='http', code='https_required')

    def test_wrong_host_denied(self):
        self.denied(host='attacker.example:8765', code='host_denied')

    def test_wrong_port_denied(self):
        self.denied(host='192.0.2.10:443', code='host_denied')
        self.denied(host='192.0.2.10', code='host_denied')

    def test_malformed_host_denied(self):
        for value in ('user@192.0.2.10:8765', '192.0.2.10:8765/path',
                      '192.0.2.10:8765#x', '192.0.2.10:8765?x',
                      '192.0.2.10:8765 invalid', 'http://192.0.2.10:8765',
                      '192.0.2.10:bad', '[::1', '192.0.2.10:99999'):
            with self.subTest(value=value):
                self.denied(host=value, code='host_denied')

    def test_invalid_peer_denied(self):
        self.denied(peer='', code='client_denied')

    def test_ipv4_mapped_peer(self):
        self.policy.check('192.0.2.10:8765', '::ffff:10.1.2.3', 'https')

    def test_custom_client_subnet(self):
        self.policy = AccessPolicy(replace(self.settings, allowed_client_cidrs=('10.25.0.0/16',)))
        self.denied(peer='10.26.0.1', code='client_denied')
        self.policy.check('192.0.2.10:8765', '10.25.0.1', 'https')

    def test_domain_can_be_added(self):
        settings = replace(self.settings, allowed_hosts=('192.0.2.10', 'redis-farm.example'))
        AccessPolicy(settings).check('Redis-Farm.Example:8765', '10.1.2.3', 'https')

    def test_explicit_server_hosts_and_networks_required(self):
        with self.assertRaises(ValueError):
            AccessPolicy(Settings(Path('/tmp'), server_mode=True))

    def test_local_mode_unchanged(self):
        policy = AccessPolicy(Settings(Path('/tmp')))
        policy.check('localhost', '127.0.0.1', 'http')
        policy.check('127.0.0.1:8765', '127.0.0.1', 'http')
        policy.check('[::1]:8765', '::1', 'http')
        with self.assertRaises(FarmError):
            policy.check('localhost:8765', '10.1.2.3', 'http')
        with self.assertRaises(FarmError):
            policy.check('192.0.2.10:8765', '127.0.0.1', 'https')

    def test_ip_and_fqdn_normalization(self):
        self.assertEqual(normalize_host('REDIS-FARM.example.'), 'redis-farm.example')
        self.assertEqual(normalize_host('2001:db8::1'), '2001:db8::1')
        for value in ('', '*', '.example', '-example', 'a..b', 'a:8765', 'foo/bar', 'example_1'):
            with self.assertRaises(ValueError):
                normalize_host(value)


class ServerConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = parse_config({})
        self.assertEqual(config.url, 'https://127.0.0.1:8765/')
        self.assertEqual(config.threads, 32)
        self.assertTrue(config.settings.server_mode)
        self.assertEqual(config.settings.dns_servers, ())
        self.assertEqual(config.settings.data_dir, Path('/var/lib/redis-farm'))

    def test_example_parses(self):
        config=parse_config(json.loads((ROOT/'deploy/ubuntu/server.example.json').read_text()))
        self.assertEqual(config.bind_host,'192.0.2.10')
        self.assertEqual(config.settings.dns_servers,())

    def test_unknown_fields_rejected(self):
        for name in ('workers', 'password', 'admin_password', 'allowed_host', 'proxy_fix'):
            with self.assertRaises(ValueError):
                parse_config({name: 'bad'})

    def test_wildcard_bind_rejected(self):
        for value in ('0.0.0.0', '::', '224.0.0.1', '*', 'hostname.example'):
            with self.assertRaises(ValueError):
                parse_config({'bind_host': value})

    def test_number_limits(self):
        for raw in ({'port': 0}, {'port': '8765'}, {'port': True}, {'port': 65536},
                    {'threads': 4}, {'scan_timeout': float('nan')}, {'scan_workers': 0},
                    {'dns_timeout': -1}, {'session_idle': 1}, {'threads': 129},
                    {'max_terminals': 32, 'threads': 32}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_config(raw)

    def test_no_empty_allowlists(self):
        for name in ('allowed_hosts', 'allowed_client_cidrs'):
            with self.assertRaises(ValueError):
                parse_config({name: []})

    def test_bind_must_be_allowed(self):
        with self.assertRaises(ValueError):
            parse_config({'allowed_hosts': ['other.example']})
        with self.assertRaises(ValueError):
            parse_config({'allowed_client_cidrs': ['192.168.0.0/16']})

    def test_dns_validated_no_hostname_servers(self):
        with self.assertRaises(ValueError):
            parse_config({'dns_servers': ['dns.example']})
        self.assertEqual(parse_config({'dns_servers': []}).settings.dns_servers, ())

    def test_paths_must_be_absolute(self):
        for name in ('data_dir', 'tls_cert', 'tls_key', 'cli_path'):
            with self.assertRaises(ValueError):
                parse_config({name: 'relative/path'})

    def test_scan_ranges(self):
        with self.assertRaises(ValueError):
            parse_config({'scan_redis_end': 6378})
        with self.assertRaises(ValueError):
            parse_config({'scan_redis_end': 9000})

    def test_lifetime_limits(self):
        with self.assertRaises(ValueError):
            parse_config({'session_lifetime': 100, 'session_idle': 200})

    def test_ipv6_bind_format(self):
        config = parse_config({'bind_host': '::1'})
        self.assertEqual(config.bind, '[::1]:8765')

    def test_file_and_env(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'server.json'
            path.write_text('{}')
            self.assertEqual(load_config(path), parse_config({}))
            with patch.dict(os.environ, {'REDIS_FARM_CONFIG': str(path)}):
                self.assertEqual(load_config(), parse_config({}))
            path.write_text('not-json')
            with self.assertRaises(ValueError):
                load_config(path)
            path.unlink()
            with self.assertRaises(ValueError):
                load_config(path)

    def test_systemd_dedicated_user_and_own_venv(self):
        unit = (ROOT/'deploy/ubuntu/redis-farm.service').read_text()
        for setting in ('User=redis-farm', '/opt/redis-farm/current/.venv/bin/gunicorn',
                        'LimitCORE=0', 'KillMode=control-group', 'ProtectSystem=strict'):
            self.assertIn(setting, unit)
        self.assertNotIn('other-service', unit)

    def test_gunicorn_configuration_single_worker(self):
        import runpy
        with patch('redis_farm.server.load_config', return_value=parse_config({})):
            config = runpy.run_path(str(ROOT/'deploy/ubuntu/gunicorn.conf.py'))
        self.assertEqual(config['workers'], 1)
        self.assertFalse(config['preload_app'])
        self.assertEqual(config['max_requests'], 0)
        self.assertEqual(config['worker_class'], 'gthread')
        self.assertIsNone(config['accesslog'])
        self.assertEqual(config['forwarded_allow_ips'], '')
        self.assertEqual(config['secure_scheme_headers'], {})
        self.assertEqual(config['ssl_context'](None, lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)).minimum_version,
                         ssl.TLSVersion.TLSv1_2)


@unittest.skipUnless(importlib.util.find_spec('flask'), 'Flask dependency is required for HTTP integration tests')
class ServerFlaskTests(unittest.TestCase):
    def setUp(self):
        from redis_farm.app import create_app
        from fixtures import FakeTopologyCLI
        self.tmp = tempfile.TemporaryDirectory()
        settings = replace(parse_config({'bind_host':'192.0.2.10','allowed_client_cidrs':['192.0.2.0/24','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','127.0.0.0/8','::1/128']}).settings, data_dir=Path(self.tmp.name))
        self.cli = FakeTopologyCLI()
        from types import SimpleNamespace
        self.app = create_app(settings, cli=self.cli, authenticator=SimpleNamespace())
        self.client = self.app.test_client()
        self.base = 'https://192.0.2.10:8765'
        self.peer = {'REMOTE_ADDR': '10.1.2.3'}
        self.signin(self.client)
        result = self.get('/api/bootstrap')
        self.csrf = result.get_json()['csrf']

    def signin(self, client):
        item = self.app.extensions['redis_farm']['sessions'].create(username='fixture', user_dn='uid=fixture,dc=test')
        client.set_cookie('__Host-redis_farm_server_session', item.id, domain='192.0.2.10', secure=True)

    def tearDown(self):
        self.app.extensions['redis_farm']['shutdown']()
        self.tmp.cleanup()

    def get(self, path, **kwargs):
        return self.client.get(path, base_url=self.base, environ_overrides=self.peer, **kwargs)

    def post(self, path, data, **kwargs):
        return self.client.post(path, base_url=self.base, environ_overrides=self.peer, json=data,
                                headers={'X-RF-CSRF': self.csrf, **kwargs.pop('headers', {})}, **kwargs)

    def test_bootstrap_does_not_rewrite_authenticated_cookie(self):
        # Login response issues the secure cookie; later bootstrap must not
        # replace it or race with another tab (HF7).
        self.assertNotIn('Set-Cookie', self.get('/api/bootstrap').headers)

    def test_server_ui_and_health(self):
        page = self.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('HTTPS', page.get_data(as_text=True))
        self.assertNotIn('127.0.0.1', page.get_data(as_text=True))
        before = len(self.app.extensions['redis_farm']['sessions'].items)
        response = self.get('/healthz')
        self.assertEqual(response.get_json()['status'], 'ok')
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(before, len(self.app.extensions['redis_farm']['sessions'].items))

    def test_remote_http_and_spoofed_proto_denied(self):
        result = self.client.get('/api/bootstrap', base_url='http://192.0.2.10:8765',
                                 environ_overrides=self.peer, headers={'X-Forwarded-Proto':'https'})
        self.assertEqual(result.status_code, 403)

    def test_spoofed_client_header_denied(self):
        result = self.client.get('/api/bootstrap', base_url=self.base,
                                 environ_overrides={'REMOTE_ADDR':'8.8.8.8'},
                                 headers={'X-Forwarded-For':'10.1.2.3'})
        self.assertEqual(result.status_code, 403)

    def test_csrf_and_origin_still_enforced(self):
        result = self.client.post('/api/session/unlock', base_url=self.base, environ_overrides=self.peer,
                                  json={'password':'fixture'})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.post('/api/session/unlock', {'password':'fixture'},
                                  headers={'Origin':'https://evil.example'}).status_code, 403)
        self.assertEqual(self.post('/api/session/unlock', {'password':'fixture'},
                                  headers={'Origin': self.base}).status_code, 200)

    def test_sessions_isolated_password_never_in_export(self):
        self.assertEqual(self.post('/api/session/unlock', {'password':'secret-fixture'}).status_code, 200)
        other = self.app.test_client()
        self.signin(other)
        other_boot = other.get('/api/bootstrap', base_url=self.base, environ_overrides=self.peer).get_json()
        self.assertFalse(other_boot['redis_unlocked'])
        self.assertNotIn(b'secret-fixture', self.get('/api/catalog/export').data)
        self.assertTrue(self.get('/api/bootstrap').get_json()['redis_unlocked'])

    def test_redis_commands_not_sent_by_bootstrap_or_health(self):
        self.get('/'); self.get('/healthz'); self.get('/api/bootstrap')
        self.assertEqual(self.cli.calls, [])


if __name__ == '__main__':
    unittest.main()
