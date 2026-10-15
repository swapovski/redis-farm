"""Process-memory credentials. No file, cookie or inventory serialization."""
from dataclasses import dataclass, field
import threading
from .models import Endpoint, FarmError


def validate_password(password):
    if not isinstance(password, str) or not password:
        raise FarmError('Enter the password.', 'invalid_password')
    if len(password) > 4096 or '\x00' in password:
        raise FarmError('The password must not exceed 4096 characters or contain NUL.', 'invalid_password')


def password_for(value, endpoint):
    if endpoint.kind == 'sentinel' or endpoint.auth == 'none':
        return None
    password = value.for_endpoint(endpoint) if hasattr(value, 'for_endpoint') else value
    if endpoint.auth == 'default' and password is None:
        raise FarmError('Enter the default password for this service or instance.', 'default_password_required', 401)
    return password


@dataclass(repr=False)
class CredentialSet:
    admin: str | None = field(default=None, repr=False)
    default: str | None = field(default=None, repr=False)
    overrides: dict = field(default_factory=dict, repr=False)

    def for_endpoint(self, endpoint):
        if endpoint.kind == 'sentinel' or endpoint.auth == 'none':
            return None
        if endpoint.auth == 'admin':
            return self.admin
        return self.overrides.get(endpoint.id, self.default)

    def available(self, value):
        ep = Endpoint.from_dict(value)
        return ep.auth == 'none' or self.for_endpoint(ep) is not None


class DiscoveryCredentials:
    """One entered password belongs to one Redis seed port and its known service.

    No global/default candidate-password list; unrelated scan ports are not tried.
    Host/IP aliases are based on the configured forward resolver, not PTR labels.
    """
    def __init__(self, admin=None, seed=None, password=None, resolver=None):
        self.admin = admin
        self.resolver = resolver
        self.lock = threading.RLock()
        self.values = {}
        self.verified = set()
        if password is not None:
            validate_password(password)
            self.values[(seed.host, seed.port)] = password
            if resolver is not None:
                resolution = resolver.resolve(seed.host)
                self.values[(resolution.connect_host, seed.port)] = password

    def for_endpoint(self, endpoint):
        if endpoint.kind == 'sentinel' or endpoint.auth == 'none':
            return None
        if endpoint.auth == 'admin':
            return self.admin
        with self.lock:
            return self.values.get((endpoint.host, endpoint.port))

    def inherit(self, parent, child):
        if child.kind != 'redis' or child.auth != 'default':
            return
        value = self.for_endpoint(parent)
        if value is not None:
            with self.lock:
                self.values.setdefault((child.host, child.port), value)

    def associate(self, endpoints):
        redis = [e for e in endpoints if e.kind == 'redis' and e.auth == 'default']
        known = {self.for_endpoint(e) for e in redis} - {None}
        if len(known) == 1:
            value = next(iter(known))
            with self.lock:
                for ep in redis:
                    self.values.setdefault((ep.host, ep.port), value)

    def mark_verified(self, ep):
        if ep.kind == 'redis' and ep.auth == 'default' and self.for_endpoint(ep) is not None:
            with self.lock:
                self.verified.add((ep.host, ep.port))

    def bind(self, sessions, owner, services):
        """Called only after inventory commit. Values remain in the owning session."""
        with self.lock:
            # Carry verified credentials across forward aliases without trying
            # a password at another port or applying unverified credentials.
            physical_verified = {}
            if self.resolver is not None:
                for host, port in self.verified:
                    try:
                        ip = self.resolver.resolve(host).connect_host
                        physical_verified[(ip, port)] = self.values[(host, port)]
                    except (FarmError, KeyError):
                        pass
                for service in services:
                    for item in service['endpoints']:
                        if item['kind'] != 'redis' or item['auth'] != 'default':
                            continue
                        try:
                            ip = self.resolver.resolve(item['host']).connect_host
                            value = physical_verified.get((ip, item['port']))
                            if value is not None:
                                key = (item['host'], item['port'])
                                self.values[key] = value
                                self.verified.add(key)
                        except FarmError:
                            pass
        for service in services:
            if service.get('auth_mode') != 'default':
                continue
            eps = [Endpoint.from_dict(e) for e in service['endpoints'] if e['kind'] == 'redis']
            # Do not remember an unconfirmed password as a service credential.
            verified = [e for e in eps if (e.host, e.port) in self.verified]
            known = {self.for_endpoint(e) for e in verified} - {None}
            if len(known) == 1:
                sessions.set_default(owner, service['id'], next(iter(known)))
            for ep in verified:
                password = self.for_endpoint(ep)
                if password is not None and len(known) > 1:
                    sessions.set_default(owner, service['id'], password, [ep.id])
