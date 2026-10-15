"""Ubuntu HTTPS entry point; the default local launcher is unchanged.

LDAP identity login is an explicit server-mode setting. Optional proxy IP trust is explicit.
Only one Gunicorn application worker may own the memory-only sessions.
"""
import argparse
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import ssl
from .access import normalize_host, parse_public_origin
from .settings import Settings

CONFIG_PATH = '/etc/redis-farm/server.json'
DEFAULT_CLIENT_CIDRS = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                        '127.0.0.0/8', '::1/128')


@dataclass(frozen=True)
class ServerConfig:
    settings: Settings
    bind_host: str
    tls_cert: Path
    tls_key: Path
    threads: int
    public_host: str

    @property
    def bind(self):
        host = '[' + self.bind_host + ']' if ':' in self.bind_host else self.bind_host
        return f'{host}:{self.settings.port}'

    @property
    def url(self):
        if self.settings.public_origin:
            return self.settings.public_origin + '/'
        host = '[' + self.public_host + ']' if ':' in self.public_host else self.public_host
        return f'https://{host}:{self.settings.port}/'


def _integer(raw, name, default, low, high):
    value = raw.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def _number(raw, name, default, low, high):
    value = raw.get(name, default)
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be a finite number in {low}..{high}')
    return value


def _strings(raw, name, default, *, empty=False):
    value = raw.get(name, list(default))
    if not isinstance(value, list) or (not value and not empty) or len(value) > 128:
        raise ValueError(f'{name} must be a list of strings')
    if any(not isinstance(v, str) or not v or v != v.strip() for v in value):
        raise ValueError(f'{name} contains an invalid string')
    return value


def _path(raw, name, default):
    value = raw.get(name, default)
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError(f'{name} must be an absolute path')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f'{name} must be an absolute path')
    return path


def parse_config(raw: dict) -> ServerConfig:
    if not isinstance(raw, dict):
        raise ValueError('Server configuration must be a JSON object')
    known = {'bind_host', 'public_host', 'port', 'allowed_hosts', 'allowed_client_cidrs', 'tls_cert', 'tls_key',
             'threads', 'data_dir', 'cli_path', 'dns_servers', 'dns_timeout', 'command_timeout',
             'scan_timeout', 'scan_workers', 'scan_redis_start', 'scan_redis_end',
             'scan_sentinel_start', 'scan_sentinel_end', 'session_idle', 'session_lifetime',
             'refresh_seconds', 'max_terminals', 'terminal_idle', 'ldap_connector',
             'public_origin', 'trusted_proxy_ips', 'language', 'redis_admin_user', 'use_acl_user', 'panel_auth'}
    if set(raw) - known:
        raise ValueError('Unknown server configuration fields: ' + ', '.join(sorted(set(raw) - known)))
    bind_host = str(ipaddress.ip_address(raw.get('bind_host', '127.0.0.1')))
    ip = ipaddress.ip_address(bind_host)
    if ip.is_unspecified or ip.is_multicast:
        raise ValueError('bind_host must be a specific interface IP, not a wildcard/multicast address')
    hosts = tuple(dict.fromkeys(normalize_host(v) for v in _strings(raw, 'allowed_hosts', [bind_host])))
    if bind_host not in hosts:
        raise ValueError('allowed_hosts must include bind_host (needed for the local HTTPS health check)')
    public_host = normalize_host(raw.get('public_host', bind_host))
    if public_host not in hosts:
        raise ValueError('public_host must be included in allowed_hosts')
    public_origin = parse_public_origin(raw.get('public_origin', ''))
    if public_origin and public_origin[0] not in hosts:
        raise ValueError('public_origin hostname must be included in allowed_hosts')
    proxies = []
    for value in _strings(raw, 'trusted_proxy_ips', [], empty=True):
        proxy = ipaddress.ip_address(value)
        if proxy.is_unspecified or proxy.is_multicast:
            raise ValueError('trusted_proxy_ips requires exact unicast IP addresses')
        proxies.append(str(proxy))
    cidrs = tuple(str(ipaddress.ip_network(v, strict=False))
                  for v in _strings(raw, 'allowed_client_cidrs', DEFAULT_CLIENT_CIDRS))
    # Include the local bind address explicitly so installation can check its own HTTPS listener.
    if not any(ip in ipaddress.ip_network(v) for v in cidrs):
        raise ValueError('allowed_client_cidrs must permit the server bind_host itself')
    dns_servers = tuple(str(ipaddress.ip_address(v))
                        for v in _strings(raw, 'dns_servers', [], empty=True))
    starts = {}
    for kind, default in (('redis', 6379), ('sentinel', 26379)):
        a = _integer(raw, f'scan_{kind}_start', default, 1, 65535)
        b = _integer(raw, f'scan_{kind}_end', default + 99, 1, 65535)
        if not 1 <= b - a + 1 <= 512:
            raise ValueError(f'{kind} scan range must contain 1..512 ports')
        starts[f'scan_{kind}_start'], starts[f'scan_{kind}_end'] = a, b
    language = raw.get('language', 'ru')  # Legacy configs were Russian-only.
    if language not in ('en', 'ru'):
        raise ValueError('language must be en or ru')
    panel_auth = raw.get('panel_auth', 'ldap')  # Never disable legacy login silently.
    if panel_auth not in ('none', 'ldap'):
        raise ValueError('panel_auth must be none or ldap')
    from .install_wizard import validate_admin_user
    admin_user = validate_admin_user(raw.get('redis_admin_user', 'admin'))
    if type(raw.get('use_acl_user', True)) is not bool:
        raise ValueError('use_acl_user must be true or false')
    settings = Settings(
        language=language, panel_auth=panel_auth, redis_admin_user=admin_user,
        use_acl_user=raw.get('use_acl_user', True),
        data_dir=_path(raw, 'data_dir', '/var/lib/redis-farm'),
        cli_path=str(_path(raw, 'cli_path', '/usr/bin/redis-cli')),
        port=_integer(raw, 'port', 8765, 1024, 65535),
        server_mode=True, allowed_hosts=hosts, allowed_client_cidrs=cidrs,
        public_origin=public_origin[2] if public_origin else '',
        trusted_proxy_ips=tuple(dict.fromkeys(proxies)),
        ldap_connector=_path(raw, 'ldap_connector', '/opt/redis-farm/connectors/connector_settings.py'),
        dns_servers=dns_servers, dns_timeout=_number(raw, 'dns_timeout', 3, .25, 30),
        command_timeout=_number(raw, 'command_timeout', 6, 1, 120),
        scan_timeout=_number(raw, 'scan_timeout', 2, .25, 30),
        scan_workers=_integer(raw, 'scan_workers', 8, 1, 8),
        session_idle=_integer(raw, 'session_idle', 3600, 60, 86400),
        session_lifetime=_integer(raw, 'session_lifetime', 43200, 60, 604800),
        refresh_seconds=_integer(raw, 'refresh_seconds', 10, 5, 3600),
        max_terminals=_integer(raw, 'max_terminals', 8, 1, 32),
        terminal_idle=_integer(raw, 'terminal_idle', 3600, 60, 86400), **starts)
    if settings.session_lifetime < settings.session_idle:
        raise ValueError('session_lifetime must not be shorter than session_idle')
    threads = _integer(raw, 'threads', 32, 16, 128)
    if threads < settings.max_terminals + 8:
        raise ValueError('threads must allow max_terminals plus at least eight other requests')
    return ServerConfig(settings, bind_host,
                        _path(raw, 'tls_cert', '/etc/redis-farm/tls/server.crt'),
                        _path(raw, 'tls_key', '/etc/redis-farm/tls/server.key'), threads, public_host)


def load_config(path=None) -> ServerConfig:
    filename = Path(path or os.environ.get('REDIS_FARM_CONFIG', CONFIG_PATH))
    try:
        raw = json.loads(filename.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot load {filename}: {type(exc).__name__}') from None
    return parse_config(raw)


def check_certificate(config):
    # Checks readability, PEM syntax, and key/cert matching; no network calls.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(config.tls_cert), str(config.tls_key))


def create_server_app():
    from .cli import RedisCLI
    from .app import create_app
    config = load_config()
    check_certificate(config)
    cli = RedisCLI(config.settings)
    if cli.error:
        raise RuntimeError(cli.error)
    app = create_app(config.settings, cli=cli)
    if config.settings.trusted_proxy_ips:
        from .proxy import TrustedClientIP
        app.wsgi_app = TrustedClientIP(app.wsgi_app, config.settings.trusted_proxy_ips)
    return app


def main():
    parser = argparse.ArgumentParser(description='Validate Redis Farm server configuration; no Redis connections')
    parser.add_argument('--config', default=None)
    parser.add_argument('--check-ldap', action='store_true', help='Check LDAP transport/certificate; never performs a credential bind')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        check_certificate(config)
        from .cli import RedisCLI
        cli = RedisCLI(config.settings)
        if cli.error:
            raise ValueError(cli.error)
        if config.settings.panel_auth == 'ldap':
            from .ldap_config import load_ldap_config
            ldap = load_ldap_config(config.settings.ldap_connector)
            # Load ldap3 and validate direct-bind settings locally before activation.
            from .ldap_auth import LDAPAuthenticator, probe_transport
            LDAPAuthenticator(ldap)
            if args.check_ldap:
                probe_transport(ldap)
                print(f'LDAP transport verified: {ldap.transport}; {ldap.host}:{ldap.port}. No password sent.')
        print(f'Configuration and TLS key pair OK: {config.url}')
        print(f'Client: {cli.version}; DNS: {config.settings.dns_servers or "system"}')
        print('No Redis commands were executed. LDAP checks run only with --check-ldap.')
        cli.shutdown()
    except (ValueError, OSError, ssl.SSLError, RuntimeError, ImportError) as exc:
        parser.exit(1, f'Preflight failed: {exc}\n')


if __name__ == '__main__':
    main()
