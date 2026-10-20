"""Process-memory credentials, isolated by browser, service, endpoint and protocol.

Persistent metadata contains usernames and transport settings, never passwords.
No cross-protocol fallback: Redis credentials are never supplied to Sentinel.
"""
from dataclasses import dataclass, field
import threading
from .models import Endpoint, FarmError, auth_identity


def validate_password(password):
    if not isinstance(password, str) or not password:
        raise FarmError('Enter the password.', 'invalid_password')
    if len(password) > 4096 or '\x00' in password:
        raise FarmError('The password must not exceed 4096 characters or contain NUL.', 'invalid_password')


def required_error(endpoint):
    if endpoint.kind == 'sentinel':
        return FarmError('Enter the Sentinel password.', 'sentinel_password_required', 401)
    if endpoint.auth == 'default':
        return FarmError('Enter the password for this service or instance.', 'default_password_required', 401)
    return FarmError('Enter the password.', 'password_required', 401)


def password_for(value, endpoint):
    if endpoint.auth == 'none':
        return None
    password = value.for_endpoint(endpoint) if hasattr(value, 'for_endpoint') else value
    # Legacy callers may leave global admin checking to RedisCLI.environment.
    if endpoint.auth != 'admin' and password is None:
        raise required_error(endpoint)
    return password


@dataclass(frozen=True, repr=False)
class Secret:
    identity: tuple
    password: str = field(repr=False)

    def matches(self, endpoint):
        return self.identity == auth_identity(endpoint)


@dataclass(repr=False)
class CredentialSet:
    admin: str | None = field(default=None, repr=False)
    default: str | None = field(default=None, repr=False)
    overrides: dict = field(default_factory=dict, repr=False)
    scoped: dict = field(default_factory=dict, repr=False)

    def for_endpoint(self, endpoint):
        if endpoint.auth == 'none':
            return None
        if endpoint.kind == 'redis' and endpoint.auth == 'admin':
            return self.admin
        values = self.scoped.get(endpoint.kind, {})
        specific = values.get(endpoint.id)
        if specific is not None and specific.matches(endpoint):
            return specific.password
        if endpoint.kind == 'redis' and endpoint.auth == 'default' and endpoint.id in self.overrides:
            return self.overrides[endpoint.id]
        common = values.get('service')
        if common is not None and common.matches(endpoint):
            return common.password
        if endpoint.kind == 'redis' and endpoint.auth == 'default':
            return self.default
        return None

    def available(self, value):
        ep = Endpoint.from_dict(value)
        return ep.auth == 'none' or self.for_endpoint(ep) is not None


class DiscoveryCredentials:
    """Credentials start at selected seed ports, inherit only within that protocol.

    The redis/port password and sentinel/port password are unrelated. Additional
    advertised peers may inherit their parent's secret; failed secrets never
    become saved session access. Names and IPs use only forward DNS.
    """
    def __init__(self, admin=None, seed=None, password=None, resolver=None,
                 sentinel_seed=None, sentinel_password=None, sentinel_seeds=()):
        self.admin, self.resolver = admin, resolver
        self.lock = threading.RLock()
        self.values, self.verified = {}, set()
        self.seeds = []
        for ep, secret in [(seed, password), (sentinel_seed, sentinel_password)]:
            if secret is not None:
                validate_password(secret)
                if ep is None or ep.auth not in ('default', 'user'):
                    raise FarmError('A password requires a matching connection profile.', 'invalid_auth')
                self._set(ep, secret)
                self.seeds.append(ep)
        if sentinel_password is not None:
            for ep in sentinel_seeds:
                self._set(ep, sentinel_password)

    @staticmethod
    def key(endpoint):
        return (endpoint.kind, endpoint.host, endpoint.port, *auth_identity(endpoint))

    def _set(self, ep, password):
        self.values[self.key(ep)] = password
        if self.resolver is not None:
            try:
                self.values[(ep.kind, self.resolver.resolve(ep.host).connect_host, ep.port, *auth_identity(ep))] = password
            except FarmError:
                pass

    def for_endpoint(self, endpoint):
        if endpoint.auth == 'none':
            return None
        if endpoint.kind == 'redis' and endpoint.auth == 'admin':
            return self.admin
        with self.lock:
            return self.values.get(self.key(endpoint))

    def inherit(self, parent, child):
        if child.kind != parent.kind or auth_identity(child) != auth_identity(parent) or child.auth not in ('default', 'user'):
            return
        value = self.for_endpoint(parent)
        if value is not None:
            with self.lock:
                self.values.setdefault(self.key(child), value)

    def associate(self, endpoints):
        endpoints = list(endpoints)
        for kind in ('redis', 'sentinel'):
            candidates = [e for e in endpoints if e.kind == kind and e.auth in ('default', 'user')]
            # A Sentinel can advertise an IP instead of the entered Redis name.
            # Only the explicitly selected Redis port can seed its credentials.
            if self.resolver is not None:
                for ep in candidates:
                    try:
                        key = (ep.kind, self.resolver.resolve(ep.host).connect_host, ep.port, *auth_identity(ep))
                        value = self.values.get(key)
                        if value is not None:
                            self.values.setdefault(self.key(ep), value)
                    except FarmError:
                        pass
            known = {self.for_endpoint(e) for e in candidates} - {None}
            if len(known) == 1:
                value = next(iter(known))
                identities = {auth_identity(e) for e in candidates if self.for_endpoint(e) is not None}
                with self.lock:
                    for ep in candidates:
                        if auth_identity(ep) in identities:
                            self.values.setdefault(self.key(ep), value)

    def mark_verified(self, ep):
        if ep.auth in ('default', 'user') and self.for_endpoint(ep) is not None:
            with self.lock:
                self.verified.add(self.key(ep))

    def bind(self, sessions, owner, services):
        """Only verified credentials are transferred to the owning memory session."""
        with self.lock:
            physical = {}
            for key in self.verified:
                kind, host, port, auth, user = key
                try:
                    ip = self.resolver.resolve(host).connect_host if self.resolver else host
                    physical[(kind, ip, port, auth, user)] = self.values[key]
                except (FarmError, KeyError):
                    pass
            for service in services:
                for kind in ('redis', 'sentinel'):
                    verified = []
                    for raw in service['endpoints']:
                        ep = Endpoint.from_dict(raw)
                        if ep.kind != kind or ep.auth not in ('default', 'user'):
                            continue
                        value = self.values.get(self.key(ep)) if self.key(ep) in self.verified else None
                        if value is None and self.resolver is not None:
                            try:
                                value = physical.get((kind, self.resolver.resolve(ep.host).connect_host, ep.port, *auth_identity(ep)))
                            except FarmError:
                                pass
                        if value is not None:
                            verified.append((ep, value))
                    unique = {(auth_identity(ep), value) for ep, value in verified}
                    if len(unique) == 1:
                        ep, value = verified[0]
                        if kind == 'redis' and ep.auth == 'default':
                            sessions.set_default(owner, service['id'], value)
                        else:
                            sessions.set_access(owner, service['id'], ep, value)
                    elif verified:
                        for ep, value in verified:
                            if kind == 'redis' and ep.auth == 'default':
                                sessions.set_default(owner, service['id'], value, [ep.id])
                            else:
                                sessions.set_access(owner, service['id'], ep, value, [ep.id])
