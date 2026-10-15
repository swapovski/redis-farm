"""HTTP boundaries, independent of Redis commands or user identity login."""
import ipaddress
from urllib.parse import urlsplit
from .models import FarmError
from .settings import Settings


def normalize_host(value: str) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError('Invalid hostname')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    value = value.rstrip('.').lower()
    # No URL, wildcard, username, port or IPv6 brackets in a configured host.
    if any(c in value for c in '/\\@:?#[]%*'):
        raise ValueError('Use an exact hostname or IP, not a URL or wildcard')
    value = value.encode('idna').decode('ascii')
    if len(value) > 253:
        raise ValueError('Hostname is too long')
    for label in value.split('.'):
        if (not 1 <= len(label) <= 63 or label[0] == '-' or label[-1] == '-'
                or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in label)):
            raise ValueError('Invalid hostname label')
    return value


def parse_public_origin(value: str):
    """Return (host, effective_port, canonical_origin), or None if disabled."""
    if value == '':
        return None
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError('public_origin must be an exact HTTPS origin')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.username is not None or
            parsed.password is not None or parsed.path not in ('', '/') or
            parsed.query or parsed.fragment or value.endswith(':')):
        raise ValueError('public_origin must be https://host[:port], without user, path or query')
    host = normalize_host(parsed.hostname or '')
    port = 443 if parsed.port is None else parsed.port
    if not 1 <= port <= 65535:
        raise ValueError('Invalid public_origin port')
    authority = '[' + host + ']' if ':' in host else host
    if port != 443:
        authority += ':' + str(port)
    return host, port, 'https://' + authority


class AccessPolicy:
    def __init__(self, settings: Settings):
        self.server = settings.server_mode
        self.port = settings.port
        self.public = parse_public_origin(settings.public_origin) if self.server else None
        if self.server:
            if not settings.allowed_hosts or not settings.allowed_client_cidrs:
                raise ValueError('Server mode requires allowed_hosts and allowed_client_cidrs')
            self.hosts = {normalize_host(v) for v in settings.allowed_hosts}
            self.networks = tuple(ipaddress.ip_network(v, strict=False)
                                  for v in settings.allowed_client_cidrs)
        else:
            self.hosts = {'127.0.0.1', 'localhost', '::1'}
            self.networks = ()

    def check(self, host_header: str, remote_addr: str, scheme: str):
        try:
            if not host_header or any(c.isspace() for c in host_header):
                raise ValueError
            parsed = urlsplit('//' + host_header)
            if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
                raise ValueError
            host = normalize_host(parsed.hostname or '')
            port = parsed.port
            if host not in self.hosts:
                raise ValueError
            effective_port = port if port is not None else (443 if scheme == 'https' else 80)
            if self.server and effective_port != self.port:
                if (self.public is None or scheme != 'https' or
                        (host, effective_port) != self.public[:2]):
                    raise ValueError
            if not self.server and port not in (None, self.port):
                raise ValueError
        except (ValueError, UnicodeError):
            raise FarmError('Host or port is not allowed for this panel.', 'host_denied', 403) from None
        try:
            peer = ipaddress.ip_address(remote_addr)
            if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
                peer = peer.ipv4_mapped
        except ValueError:
            raise FarmError('Cannot determine the client network address.', 'client_denied', 403) from None
        if self.server:
            if not any(peer in network for network in self.networks):
                raise FarmError('Client address is outside allowed_client_cidrs.', 'client_denied', 403)
            if scheme != 'https':
                raise FarmError('This server requires HTTPS.', 'https_required', 403)
        elif not peer.is_loopback:
            raise FarmError('Local mode is available only on localhost.', 'local_only', 403)
