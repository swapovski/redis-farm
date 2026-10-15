"""Employee LDAP bind with direct or nested membership of the exact access group."""
from __future__ import annotations

from dataclasses import dataclass
import ssl
import threading
from .ldap_config import LDAPConfig


class LoginDenied(Exception):
    """Invalid credentials or missing group membership, indistinguishable to the user."""


class LoginUnavailable(Exception):
    """The directory/TLS could not be reached; contains no credentials."""


@dataclass(frozen=True)
class Identity:
    username: str
    dn: str


# LDAP-NESTED-HF8: follow member links upwards; never infer access from a CN.
# These limits apply only after the fast exact-group check failed to match.
_MAX_NESTED_DEPTH = 16
_MAX_NESTED_GROUPS = 256
_MAX_NESTED_SEARCHES = 128
_NESTED_SECONDS = 30.0
_GROUP_BATCH_SIZE = 16
_GROUP_PAGE_SIZE = 100
_PAGE_OID = '1.2.840.113556.1.4.319'


def _nested_required_group(connection, cfg, user_dn, ldap3):
    """Prove a path user -> group(s) -> REQUIRE_GROUP on this bound connection.

    Only group entries beneath GROUP_BASE_DN are traversed. Each step uses
    server-side member filters, not memberOf assumptions or AD-specific rules.
    Memberships are local to this login attempt; no cross-login cache is used.
    """
    from time import monotonic
    from ldap3.utils.conv import escape_filter_chars
    from ldap3.utils.dn import parse_dn

    def decoded(value):
        # LDAP DNs may use either escaped literals or UTF-8 hex octets.
        data = bytearray()
        i = 0
        while i < len(value):
            if value[i] == '\\':
                if i + 1 >= len(value):
                    raise LoginUnavailable()
                hexpair = value[i + 1:i + 3]
                if len(hexpair) == 2 and all(c in '0123456789abcdefABCDEF' for c in hexpair):
                    data.append(int(hexpair, 16))
                    i += 3
                    continue
                i += 1
            data.extend(value[i].encode('utf-8'))
            i += 1
        return data.decode('utf-8')

    def dn_key(dn):
        if not isinstance(dn, str) or not dn or len(dn) > 4096:
            raise LoginUnavailable()
        rdns, rdn = [], []
        for attr, value, separator in parse_dn(dn):
            rdn.append((attr.casefold(), decoded(value).casefold()))
            if separator != '+':
                rdns.append(tuple(sorted(rdn)))
                rdn = []
        if not rdns or rdn:
            raise LoginUnavailable()
        return tuple(rdns)

    base_key = dn_key(cfg.group_base_dn)
    required_key = dn_key(cfg.require_group)

    def in_group_base(key):
        return len(key) > len(base_key) and key[-len(base_key):] == base_key

    if not in_group_base(required_key):
        raise LoginUnavailable()
    deadline = monotonic() + _NESTED_SECONDS
    searches = 0

    def member_filter(dns):
        matches = ''.join('(' + cfg.group_member_attr + '=' + escape_filter_chars(dn) + ')' for dn in dns)
        return '(&(objectClass=' + cfg.group_object_class + ')(|' + matches + '))'

    def search(base, scope, dns, **limits):
        nonlocal searches
        if searches >= _MAX_NESTED_SEARCHES or monotonic() >= deadline:
            raise LoginUnavailable()
        searches += 1
        connection.search(base, member_filter(dns), search_scope=scope,
                          attributes=['1.1'], dereference_aliases=ldap3.DEREF_NEVER,
                          time_limit=cfg.receive_timeout, **limits)
        result = connection.result or {}
        if result.get('result') != 0 or monotonic() >= deadline:
            # No authorization on partial, timed-out or denied search results.
            raise LoginUnavailable()
        response = connection.response
        if not isinstance(response, (list, tuple)):
            raise LoginUnavailable()
        return response, result

    seen = {dn_key(user_dn)}
    discovered = 0
    frontier = [user_dn]
    for _ in range(_MAX_NESTED_DEPTH):
        parents = []
        for offset in range(0, len(frontier), _GROUP_BATCH_SIZE):
            batch = frontier[offset:offset + _GROUP_BATCH_SIZE]
            cookie, cookies = None, set()
            while True:
                response, result = search(
                    cfg.group_base_dn, ldap3.SUBTREE, batch,
                    size_limit=0, paged_size=_GROUP_PAGE_SIZE,
                    paged_criticality=True, paged_cookie=cookie)
                for entry in response:
                    if not isinstance(entry, dict):
                        raise LoginUnavailable()
                    if entry.get('type') != 'searchResEntry':
                        continue  # Do not follow referrals.
                    dn = entry.get('dn')
                    key = dn_key(dn)
                    if not in_group_base(key):
                        raise LoginUnavailable()
                    if key in seen:
                        continue
                    seen.add(key)
                    discovered += 1
                    if discovered > _MAX_NESTED_GROUPS:
                        raise LoginUnavailable()
                    parents.append(dn)
                controls = result.get('controls') or {}
                paging = controls.get(_PAGE_OID) or {}
                cookie = (paging.get('value') or {}).get('cookie')
                if not cookie:
                    break
                if not isinstance(cookie, bytes) or cookie in cookies:
                    raise LoginUnavailable()
                cookies.add(cookie)
        if not parents:
            return False
        for offset in range(0, len(parents), _GROUP_BATCH_SIZE):
            # Always ask the server about the EXACT configured target. A
            # similarly named group or one above it is not sufficient.
            response, _ = search(cfg.require_group, ldap3.BASE,
                                 parents[offset:offset + _GROUP_BATCH_SIZE], size_limit=1)
            if any(isinstance(entry, dict) and entry.get('type') == 'searchResEntry' for entry in response):
                return True
        frontier = parents
    raise LoginUnavailable()  # Depth limit reached, never grant on uncertainty.


class LDAPAuthenticator:
    def __init__(self, config: LDAPConfig):
        # Imported only for server mode. Local launch needs neither LDAP nor ldap3.
        import ldap3
        self.ldap3 = ldap3
        self.config = config
        self.slots = threading.BoundedSemaphore(4)

    def authenticate(self, username: str, password: str) -> Identity:
        if (not isinstance(username, str) or not isinstance(password, str) or
                not 1 <= len(username) <= 128 or not 1 <= len(password) <= 4096 or
                username != username.strip() or any(ord(c) < 32 for c in username) or '\x00' in password):
            raise LoginDenied()
        if not self.slots.acquire(blocking=False):
            raise LoginUnavailable()
        from ldap3.utils.dn import escape_rdn, parse_dn
        from ldap3.utils.conv import escape_filter_chars
        from ldap3.core.exceptions import LDAPException
        cfg, ldap3 = self.config, self.ldap3
        connection = None
        try:
            user_dn = f'{cfg.user_login_attr}={escape_rdn(username)},{cfg.base_dn}'
            parse_dn(user_dn)
            parse_dn(cfg.require_group)
            tls = None
            if cfg.transport != 'plain':
                tls = ldap3.Tls(validate=ssl.CERT_REQUIRED,
                                version=ssl.PROTOCOL_TLS_CLIENT,
                                ca_certs_file=cfg.ca_file, sni=cfg.host)
            server = ldap3.Server(cfg.host, port=cfg.port, use_ssl=cfg.transport == 'ldaps',
                                  tls=tls, get_info=ldap3.NONE, connect_timeout=cfg.connect_timeout)
            connection = ldap3.Connection(server, user=user_dn, password=password,
                                           authentication=ldap3.SIMPLE, auto_referrals=False,
                                           raise_exceptions=False, receive_timeout=cfg.receive_timeout,
                                           auto_bind=False, lazy=False, read_only=True)
            # StartTLS is completed and verified before transmitting credentials.
            connection.open()
            if connection.closed:
                raise LoginUnavailable()
            if cfg.transport == 'starttls' and not connection.start_tls():
                raise LoginUnavailable()
            if not connection.bind():
                # LDAP result 49: do not expose whether the account exists or is locked.
                if (connection.result or {}).get('result') == 49:
                    raise LoginDenied()
                raise LoginUnavailable()
            member = escape_filter_chars(user_dn)
            membership = f'(&(objectClass={cfg.group_object_class})({cfg.group_member_attr}={member}))'
            found = connection.search(cfg.require_group, membership, search_scope=ldap3.BASE,
                                       attributes=['1.1'], size_limit=1, time_limit=cfg.receive_timeout)
            if (connection.result or {}).get('result') != 0:
                raise LoginDenied()
            if not found or not any(r.get('type') == 'searchResEntry' for r in connection.response):
                if not _nested_required_group(connection, cfg, user_dn, ldap3):
                    raise LoginDenied()
            return Identity(username, user_dn)
        except (LoginDenied, LoginUnavailable):
            raise
        except (LDAPException, OSError, ssl.SSLError, ValueError):
            # Do not forward exception messages or directory responses to UI/logs.
            raise LoginUnavailable() from None
        finally:
            if connection is not None:
                try:
                    connection.unbind()
                except Exception:
                    pass
                connection.password = None
                connection.user = None
            self.slots.release()


def probe_transport(config: LDAPConfig):
    """Validate TCP/StartTLS/LDAPS without transmitting a bind or password.

    Exceptions are safe to print: this probe has no employee credentials and
    does not read BIND_PASSWORD from legacy connectors.
    """
    import ldap3
    connection = None
    try:
        tls = None if config.transport == 'plain' else ldap3.Tls(
            validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLS_CLIENT,
            ca_certs_file=config.ca_file, sni=config.host)
        server = ldap3.Server(config.host, port=config.port,
                              use_ssl=config.transport == 'ldaps', tls=tls,
                              get_info=ldap3.NONE, connect_timeout=config.connect_timeout)
        connection = ldap3.Connection(server, auto_bind=False, auto_referrals=False,
                                       receive_timeout=config.receive_timeout,
                                       read_only=True, raise_exceptions=True)
        connection.open()
        if connection.closed:
            raise RuntimeError('LDAP TCP connection did not open.')
        if config.transport == 'starttls' and not connection.start_tls():
            raise RuntimeError('LDAP server did not accept StartTLS.')
        return {'transport': config.transport, 'host': config.host, 'port': config.port}
    except (ldap3.core.exceptions.LDAPException, OSError, ssl.SSLError) as exc:
        raise RuntimeError(
            'LDAP transport check failed: ' + str(exc)[:900] + '. '
            'Check trusted CA_CERT_FILE and the URL hostname against the certificate SAN. '
            'Do not use the panel HTTPS certificate as the LDAP CA. No password was sent.'
        ) from None
    finally:
        if connection is not None:
            try:
                connection.unbind()
            except Exception:
                pass
