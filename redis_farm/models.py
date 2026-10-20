from dataclasses import dataclass, asdict
import hashlib
import ipaddress
import re
from typing import Any


class FarmError(Exception):
    def __init__(self, message: str, code: str = 'error', status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def validate_host(value: Any) -> str:
    if not isinstance(value, str):
        raise FarmError('Host must be a string.', 'invalid_host')
    host = value.strip().strip('[]').rstrip('.').lower()
    if not host or len(host) > 253 or host.startswith('-'):
        raise FarmError('Invalid hostname or IP address.', 'invalid_host')
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        if not re.fullmatch(r'[a-z0-9_][a-z0-9_.-]*', host):
            raise FarmError('Use a hostname or IP, without a URL or port.', 'invalid_host')
    return host


def validate_port(value: Any) -> int:
    if isinstance(value, bool):
        raise FarmError('Port must be an integer from 1 to 65535.', 'invalid_port')
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        port = int(value)
    except (TypeError, ValueError):
        raise FarmError('Port must be an integer from 1 to 65535.', 'invalid_port') from None
    if not 1 <= port <= 65535:
        raise FarmError('Port must be an integer from 1 to 65535.', 'invalid_port')
    return port


def sentinel_port(redis_port: int) -> int | None:
    result = int('2' + str(validate_port(redis_port)))
    return result if result <= 65535 else None


def parse_ports(value: str, limit: int = 32) -> list[int]:
    result: set[int] = set()
    for part in str(value).split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            first, last = part.split('-', 1)
            a, b = validate_port(first), validate_port(last)
            if b < a or b - a + 1 > limit:
                raise FarmError('Invalid or too large port range.', 'port_range')
            result.update(range(a, b + 1))
        else:
            result.add(validate_port(part))
        if len(result) > limit:
            raise FarmError(f'At most {limit} explicitly selected ports per discovery.', 'port_range')
    if not result:
        raise FarmError('At least one port is required.', 'invalid_port')
    return sorted(result)


CONNECTION_FIELDS = ('auth', 'username', 'tls', 'cacert', 'sni', 'certfile', 'keyfile')


def validate_username(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise FarmError('Enter a valid Redis username.', 'invalid_username')
    return value.strip()


def connection_profile(value, kind='redis'):
    """Allowlisted persistent connection metadata; never includes a password."""
    if not isinstance(value, dict):
        raise FarmError('Invalid connection settings.', 'invalid_endpoint')
    data = {k: value[k] for k in CONNECTION_FIELDS if k in value}
    data.setdefault('auth', 'none' if kind == 'sentinel' else 'admin')
    if kind == 'sentinel' and data['auth'] == 'admin':
        raise FarmError('Sentinel needs its own authentication profile.', 'invalid_auth')
    ep = Endpoint('localhost', 6379, kind=kind, **data)
    return {k: getattr(ep, k) for k in CONNECTION_FIELDS}


def service_connection(service, kind='redis'):
    value = service.get(kind + '_connection')
    if value is None:
        value = next((e for e in service.get('endpoints', []) if e.get('kind', 'redis') == kind), {})
    return connection_profile(value, kind)


def service_peer(service, host, port, kind='redis'):
    return Endpoint(host, port, kind=kind, **service_connection(service, kind))


def auth_identity(endpoint):
    return (endpoint.auth, endpoint.username if endpoint.auth == 'user' else '')


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    kind: str = 'redis'
    auth: str = 'admin'
    tls: bool = False
    cacert: str = ''
    sni: str = ''
    username: str = ''
    certfile: str = ''
    keyfile: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'host', validate_host(self.host))
        object.__setattr__(self, 'port', validate_port(self.port))
        if self.kind not in ('redis', 'sentinel') or self.auth not in ('admin', 'user', 'default', 'none'):
            raise FarmError('Unsupported endpoint kind or authentication profile.', 'invalid_endpoint')
        # Old catalogs and constructors used the default 'admin' for Sentinel.
        # Preserve their no-auth meaning; explicit named Sentinel uses 'user'.
        if self.kind == 'sentinel' and self.auth == 'admin':
            object.__setattr__(self, 'auth', 'none')
        if self.auth == 'user':
            object.__setattr__(self, 'username', validate_username(self.username))
        else:
            object.__setattr__(self, 'username', '')
        if not isinstance(self.tls, bool):
            raise FarmError('TLS must be true or false.', 'invalid_endpoint')
        for name in ('cacert', 'sni', 'certfile', 'keyfile'):
            val = getattr(self, name)
            if not isinstance(val, str) or len(val) > 2048 or '\x00' in val or '\n' in val:
                raise FarmError('Invalid TLS setting.', 'invalid_endpoint')
        if bool(self.certfile) != bool(self.keyfile):
            raise FarmError('Provide both the client certificate and private key paths.', 'invalid_tls')

    @property
    def id(self) -> str:
        return hashlib.sha256(f'{self.kind}|{self.host}|{self.port}'.encode()).hexdigest()[:24]

    @property
    def address(self) -> str:
        host = f'[{self.host}]' if ':' in self.host else self.host
        return f'{host}:{self.port}'

    def to_dict(self) -> dict:
        return {**asdict(self), 'id': self.id, 'address': self.address}

    def peer(self, host: str, port: Any, kind: str = 'redis') -> 'Endpoint':
        if kind == self.kind:
            return Endpoint(host, port, kind, **connection_profile(self.to_dict(), kind))
        # Credentials and TLS must never cross Redis/Sentinel boundaries.
        return Endpoint(host, port, kind, auth='none' if kind == 'sentinel' else 'admin')

    @classmethod
    def from_dict(cls, value: dict) -> 'Endpoint':
        if not isinstance(value, dict):
            raise FarmError('Invalid endpoint object.', 'invalid_endpoint')
        if 'host' not in value or 'port' not in value:
            raise FarmError('Endpoint host and port are required.', 'invalid_endpoint')
        return cls(**{k: value[k] for k in ('host', 'port', 'kind', 'auth', 'tls', 'cacert', 'sni', 'username', 'certfile', 'keyfile') if k in value})


def host_display_policy(value: dict) -> tuple[str, str]:
    """Infer legacy catalogs from their seed; new scans retain the actual input host."""
    seed = value.get('display_seed_host') or (value.get('seed') or {}).get('host', '')
    seed = validate_host(seed)
    try:
        ipaddress.ip_address(seed)
        inferred = 'ip'
    except ValueError:
        inferred = 'hostname'
    mode = value.get('display_mode', inferred)
    if mode not in ('hostname', 'ip'):
        raise FarmError('Invalid host display mode.', 'invalid_service')
    return mode, seed


def service_auth(value: dict) -> str:
    mode = (value.get('redis_connection') or {}).get('auth', value.get('auth_mode'))
    if mode is None:
        mode = next((e.get('auth', 'admin') for e in value.get('endpoints', [])
                     if e.get('kind', 'redis') == 'redis'), 'admin')
    if mode not in ('admin', 'user', 'default', 'none'):
        raise FarmError('Unsupported service authentication profile.', 'invalid_auth')
    return mode


def clean_host_aliases(value, max_nodes=96):
    """Explicit user labels only; never a reverse-DNS result or connection target."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > max_nodes:
        raise FarmError('Invalid manual host labels.', 'invalid_service')
    result = {}
    for ip, hostname in value.items():
        try:
            ip = str(ipaddress.ip_address(ip))
        except (ValueError, TypeError):
            raise FarmError('Manual host labels must refer to numeric IPs.', 'invalid_service') from None
        hostname = validate_host(hostname)
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            result[ip] = hostname
        else:
            raise FarmError('A manual host label must be a hostname.', 'invalid_service')
    return result


def clean_service(value: dict, max_nodes: int = 96) -> dict:
    if not isinstance(value, dict):
        raise FarmError('Invalid service object.', 'invalid_service')
    mode = value.get('mode', 'standalone')
    if mode not in ('standalone', 'replication', 'sentinel', 'cluster'):
        raise FarmError('Invalid service mode.', 'invalid_service')
    endpoints = list({e.id: e for e in map(Endpoint.from_dict, value.get('endpoints', []))}.values())
    if not endpoints or len(endpoints) > max_nodes:
        raise FarmError(f'A service needs 1..{max_nodes} endpoints.', 'invalid_service')
    seed = Endpoint.from_dict(value.get('seed') or endpoints[0].to_dict())
    if seed.id not in {e.id for e in endpoints}:
        endpoints.insert(0, seed)
    if len(endpoints) > max_nodes:
        raise FarmError('Too many endpoints.', 'invalid_service')
    def text(key, default=''):
        item = value.get(key, default)
        if not isinstance(item, str) or len(item) > 256 or any(ord(c) < 32 for c in item):
            raise FarmError(f'Invalid {key}.', 'invalid_service')
        return item.strip()
    master_name = text('master_name')
    if mode == 'sentinel' and not master_name:
        raise FarmError('Sentinel master name is required.', 'invalid_service')
    display_mode, display_seed_host = host_display_policy({**value, 'seed': seed.to_dict()})
    auth_mode = service_auth({**value, 'endpoints': [e.to_dict() for e in endpoints]})
    profiles = {}
    for kind in ('redis', 'sentinel'):
        first = next((e.to_dict() for e in endpoints if e.kind == kind), {})
        default = {'auth': auth_mode} if kind == 'redis' else {'auth': 'none'}
        profiles[kind + '_connection'] = connection_profile(value.get(kind + '_connection', first or default), kind)
    # Explicit per-endpoint overrides may differ from the service profile.
    auth_mode = profiles['redis_connection']['auth']
    # Deliberately whitelist persistent fields. No observations, passwords or raw replies.
    return {'name': text('name', seed.address) or seed.address, 'group': text('group'),
            'mode': mode, 'auth_mode': auth_mode, 'master_name': master_name, 'seed': seed.to_dict(),
            'display_mode': display_mode, 'display_seed_host': display_seed_host,
            'host_aliases': clean_host_aliases(value.get('host_aliases'), max_nodes),
            'endpoints': [e.to_dict() for e in endpoints], **profiles}
