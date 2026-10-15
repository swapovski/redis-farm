"""Literal connector settings, parsed as data, never executed.

Only LDAP_CONFIG is used. SMTP/SSH/Flask secrets and service bind credentials
from another application's connector file are not needed for direct user bind.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

DEFAULT_CONNECTOR = Path('/opt/redis-farm/connectors/connector_settings.py')
REQUIRED_FIELDS = ('URL', 'BASE_DN', 'USER_LOGIN_ATTR', 'GROUP_BASE_DN', 'REQUIRE_GROUP')
WIZARD_DEFAULTS = {
    'URL': 'ldap://ldap.example.net:389',
    'BASE_DN': 'ou=people,dc=example,dc=net',
    'USER_LOGIN_ATTR': 'uid',
    'GROUP_BASE_DN': 'ou=groups,dc=example,dc=net',
    'REQUIRE_GROUP': 'cn=redis_farm_users,ou=groups,dc=example,dc=net',
}


class LDAPConfigurationError(ValueError):
    """A sanitized configuration error; never include configuration values."""


@dataclass(frozen=True)
class LDAPConfig:
    url: str
    host: str
    port: int
    transport: str
    base_dn: str
    user_login_attr: str
    group_base_dn: str
    require_group: str
    ca_file: str | None = None
    connect_timeout: int = 5
    receive_timeout: int = 10
    group_member_attr: str = 'member'
    group_object_class: str = 'groupOfNames'


def read_connector(path: Path | str) -> tuple[str, dict, ast.Assign | ast.AnnAssign | None]:
    """Return source, literal LDAP_CONFIG and its node. Do not import this file."""
    path = Path(path)
    if path.is_symlink():
        raise LDAPConfigurationError('connector_settings.py must be a regular file, not a symlink.')
    if not path.exists():
        return '', {}, None
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise LDAPConfigurationError('connector_settings.py must be a regular UTF-8 file up to 1 MiB.')
        source = path.read_text(encoding='utf-8-sig')
        tree = ast.parse(source, filename='connector_settings.py')
    except (OSError, UnicodeError, SyntaxError, RecursionError):
        raise LDAPConfigurationError('Cannot read/parse connector_settings.py. Check its syntax and permissions.') from None
    matches = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == 'LDAP_CONFIG' for t in targets):
            matches.append(node)
    if len(matches) > 1:
        raise LDAPConfigurationError('Use one literal LDAP_CONFIG assignment in connector_settings.py.')
    if not matches:
        return source, {}, None
    node = matches[0]
    try:
        value = ast.literal_eval(node.value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise LDAPConfigurationError('LDAP_CONFIG must be a literal dictionary, without calls or variable references.') from None
    if not isinstance(value, dict) or len(value) > 128 or any(not isinstance(k, str) for k in value):
        raise LDAPConfigurationError('LDAP_CONFIG must be a dictionary with string keys.')
    return source, value, node


def missing_fields(raw: dict) -> list[str]:
    return [name for name in REQUIRED_FIELDS if name not in raw or raw[name] is None or raw[name] == '']


def _text(raw, name, default=None):
    value = raw.get(name, default)
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise LDAPConfigurationError(f'Invalid LDAP_CONFIG[{name!r}]. Use a non-empty string without surrounding whitespace.')
    return value


def _attribute(raw, name, default=None):
    value = _text(raw, name, default)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}', value):
        raise LDAPConfigurationError(f'Invalid LDAP attribute in {name}.')
    return value


def parse_ldap_config(raw: dict, *, check_ca=True) -> LDAPConfig:
    missing = missing_fields(raw)
    if missing:
        raise LDAPConfigurationError('Missing LDAP settings: ' + ', '.join(missing))
    url = _text(raw, 'URL')
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ('ldap', 'ldaps') or not parts.hostname or
                parts.username is not None or parts.password is not None or parts.path not in ('', '/') or
                parts.query or parts.fragment):
            raise ValueError()
        host = parts.hostname.encode('idna').decode('ascii')
        if any(c.isspace() or c in '\\/?#' for c in host):
            raise ValueError()
        port = parts.port if parts.port is not None else (636 if parts.scheme == 'ldaps' else 389)
        if not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise LDAPConfigurationError('URL must be ldap://hostname:389 or ldaps://hostname:636, without credentials or a DN.') from None
    if 'START_TLS' in raw and type(raw['START_TLS']) is not bool:
        raise LDAPConfigurationError('START_TLS must be True or False.')
    explicit = raw.get('TLS_MODE')
    if explicit is not None and explicit not in ('plain', 'starttls', 'ldaps'):
        raise LDAPConfigurationError('TLS_MODE must be plain, starttls or ldaps.')
    # Secure default for legacy connector dictionaries without TLS keys.
    inferred = 'ldaps' if parts.scheme == 'ldaps' else ('starttls' if raw.get('START_TLS', True) else 'plain')
    mode = explicit or inferred
    if (parts.scheme == 'ldaps') != (mode == 'ldaps'):
        raise LDAPConfigurationError('TLS_MODE conflicts with URL scheme.')
    if parts.scheme == 'ldaps' and raw.get('START_TLS'):
        raise LDAPConfigurationError('START_TLS cannot be combined with an ldaps:// URL.')
    if 'START_TLS' in raw and parts.scheme == 'ldap' and (raw['START_TLS'] != (mode == 'starttls')):
        raise LDAPConfigurationError('TLS_MODE conflicts with START_TLS.')
    if mode == 'plain' and raw.get('ALLOW_PLAINTEXT') is not True:
        raise LDAPConfigurationError('Plain LDAP requires explicit ALLOW_PLAINTEXT=True. Prefer StartTLS or LDAPS.')
    if raw.get('VERIFY_CERTIFICATE', True) is not True:
        raise LDAPConfigurationError('Disabling LDAP certificate verification is not supported.')
    if raw.get('AUTH_MODE', 'direct') != 'direct':
        raise LDAPConfigurationError('Only AUTH_MODE=direct is supported (bind with the employee credentials).')
    # An explicitly empty CA_CERT_FILE means system trust. An absent key can
    # use the already trusted CA deployed by FreeIPA on this host.
    if raw.get('CA_CERT_FILE') is not None and not isinstance(raw['CA_CERT_FILE'], str):
        raise LDAPConfigurationError('CA_CERT_FILE must be an absolute path, an empty string or None.')
    ca = raw.get('CA_CERT_FILE') or None
    if 'CA_CERT_FILE' not in raw and mode != 'plain' and Path('/etc/ipa/ca.crt').is_file():
        ca = '/etc/ipa/ca.crt'
    if ca is not None:
        if not isinstance(ca, str) or not Path(ca).is_absolute() or any(ord(c) < 32 for c in ca):
            raise LDAPConfigurationError('CA_CERT_FILE must be an absolute path or None.')
        if mode == 'plain':
            raise LDAPConfigurationError('CA_CERT_FILE requires StartTLS or LDAPS.')
        if check_ca:
            try:
                ssl.create_default_context(cafile=ca)
            except (OSError, ssl.SSLError):
                raise LDAPConfigurationError('Cannot load LDAP CA_CERT_FILE. Check PEM data and access for redis-farm.') from None
    values = {}
    for key, default in (('CONNECT_TIMEOUT', 5), ('RECEIVE_TIMEOUT', 10)):
        number = raw.get(key, default)
        if type(number) is not int or not 1 <= number <= 60:
            raise LDAPConfigurationError(f'{key} must be an integer in 1..60 seconds.')
        values[key] = number
    base = _text(raw, 'BASE_DN')
    groups = _text(raw, 'GROUP_BASE_DN')
    require = _text(raw, 'REQUIRE_GROUP')
    for key, value in (('BASE_DN', base), ('GROUP_BASE_DN', groups), ('REQUIRE_GROUP', require)):
        if '=' not in value or len(value) > 4096:
            raise LDAPConfigurationError(f'{key} must be a distinguished name (DN).')
    # The actual membership check uses BASE scope at this exact group DN.
    if not require.lower().endswith(',' + groups.lower()):
        raise LDAPConfigurationError('REQUIRE_GROUP must be a full group DN under GROUP_BASE_DN.')
    return LDAPConfig(url, host, port, mode, base,
                      _attribute(raw, 'USER_LOGIN_ATTR'), groups, require, ca,
                      values['CONNECT_TIMEOUT'], values['RECEIVE_TIMEOUT'],
                      _attribute(raw, 'GROUP_MEMBER_ATTR', 'member'),
                      _attribute(raw, 'GROUP_OBJECT_CLASS', 'groupOfNames'))


def load_ldap_config(path=DEFAULT_CONNECTOR, *, check_ca=True) -> LDAPConfig:
    _, raw, _ = read_connector(path)
    return parse_ldap_config(raw, check_ca=check_ca)
