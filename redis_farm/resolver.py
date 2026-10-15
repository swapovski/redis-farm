"""Application-scoped DNS; no monkey-patching, OS edits or Redis sockets.

Only DNS packets use dnspython. Redis data still goes exclusively through redis-cli.
Literal-IP connections bypass DNS. Topology uses IPs and never queries PTR records.
The configured DNS view is preferred. On DNS failure (not TCP failure), the
server's OS resolver is used. No Redis command is replayed. Explicit system mode
continues to leave connection resolution to redis-cli.
"""
from dataclasses import asdict, dataclass
import ipaddress
import math
import socket
import threading
import time
import dns.exception
import dns.resolver
from .models import FarmError, validate_host
from .settings import Settings

_RFC1918 = tuple(ipaddress.ip_network(cidr) for cidr in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


class ResolutionError(FarmError):
    pass


@dataclass(frozen=True)
class Resolution:
    requested_host: str
    connect_host: str
    source: str
    dns_servers: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()
    ttl_remaining: float | None = None
    fallback_reason: str = ''

    def to_dict(self):
        return asdict(self)


class HostResolver:
    def __init__(self, settings: Settings):
        try:
            self.servers = tuple(str(ipaddress.ip_address(server))
                                 for server in settings.dns_servers)
        except ValueError:
            raise ValueError('DNS servers must be literal IPv4 or IPv6 addresses.') from None
        if not math.isfinite(settings.dns_timeout) or not 0.25 <= settings.dns_timeout <= 30:
            raise ValueError('DNS timeout must be 0.25..30 seconds.')
        self.timeout = settings.dns_timeout
        self._locks = tuple(threading.Lock() for _ in range(32))
        self._resolver = None
        # OS getaddrinfo() has no portable per-call timeout. Run only bounded,
        # daemonized lookups so a stuck NSS backend cannot block a request forever
        # or accumulate unbounded threads. Neither path opens a Redis socket.
        self._system_slots = threading.BoundedSemaphore(8)
        self._fallback_lock = threading.Lock()
        self._fallback_cache = {}
        self._fallback_cache_seconds = 30.0
        self._fallback_error_seconds = 5.0
        if self.servers:
            # The primary view never reads OS configuration; OS lookup is fallback only.
            self._resolver = dns.resolver.Resolver(configure=False)
            self._resolver.nameservers = list(self.servers)
            self._resolver.timeout = min(1.5, self.timeout)
            self._resolver.lifetime = self.timeout
            self._resolver.use_search_by_default = False
            self._resolver.cache = dns.resolver.LRUCache(max_size=1024)

    def status(self):
        return {'mode': 'custom' if self.servers else 'system',
                'servers': list(self.servers), 'record_type': 'A' if self.servers else 'system',
                'timeout': self.timeout, 'system_fallback': bool(self.servers),
                'fallback_on': 'DNS failure only; never a TCP or Redis command error',
                'fallback_cache_seconds': self._fallback_cache_seconds,
                'selection': 'prefer RFC1918, then lowest numeric IPv4; no connection retries'}

    @staticmethod
    def _order(address):
        ip = ipaddress.ip_address(address)
        return (not any(ip in net for net in _RFC1918), int(ip))

    def resolve(self, host: str) -> Resolution:
        host = validate_host(host)
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            address = None
        if address is not None:
            return Resolution(host, address, 'literal', addresses=(address,))
        if self._resolver is None:
            # Opt-in compatibility mode. The child CLI uses the system resolver.
            return Resolution(host, host, 'system')
        # One in-flight lookup per stripe also deduplicates scans of many ports
        # on the same host. A successful fallback is cached briefly, not forever:
        # after expiry the internal DNS view gets first preference again.
        with self._locks[hash(host) % len(self._locks)]:
            with self._fallback_lock:
                cached = self._fallback_cache.get(host)
                if cached and cached[0] > time.monotonic():
                    if isinstance(cached[1], tuple):
                        raise ResolutionError(*cached[1]) from None
                    return cached[1]
                self._fallback_cache.pop(host, None)
            try:
                return self._resolve_primary(host)
            except ResolutionError as primary:
                try:
                    addresses = self._system_addresses(host)
                except ResolutionError as system:
                    code = 'dns_timeout' if system.status == 504 else 'dns_resolution_failed'
                    error = (f'{primary} System resolver: {system} '
                             'redis-cli was not started.', code, system.status)
                    self._cache_fallback(host, error, self._fallback_error_seconds)
                    raise ResolutionError(*error) from None
                result = Resolution(host, addresses[0], 'system_fallback', (),
                                    addresses, None, str(primary))
                # getaddrinfo does not expose DNS TTL. This is an application
                # cache window; ttl_remaining deliberately stays None.
                self._cache_fallback(host, result, self._fallback_cache_seconds)
                return result

    def _cache_fallback(self, host, result, seconds):
        with self._fallback_lock:
            if len(self._fallback_cache) >= 1024:
                oldest = min(self._fallback_cache, key=lambda k: self._fallback_cache[k][0])
                self._fallback_cache.pop(oldest, None)
            self._fallback_cache[host] = (time.monotonic() + seconds, result)

    def _resolve_primary(self, host):
        label = ', '.join(self.servers)
        try:
            answer = self._resolver.resolve(host + '.', 'A', search=False,
                                            lifetime=self.timeout)
            addresses = tuple(sorted({str(ipaddress.IPv4Address(row.address))
                                      for row in answer}, key=self._order))
            if not addresses:
                raise dns.resolver.NoAnswer
            ttl = round(max(0.0, answer.expiration - time.time()), 3)
            return Resolution(host, addresses[0], 'dns', self.servers, addresses, ttl)
        except dns.resolver.NXDOMAIN:
            reason, code, status = 'name does not exist', 'dns_not_found', 502
        except dns.resolver.NoAnswer:
            reason, code, status = 'no IPv4 A record', 'dns_no_answer', 502
        except dns.exception.Timeout:
            reason, code, status = 'DNS request timed out', 'dns_timeout', 504
        except dns.resolver.NoNameservers:
            reason, code, status = 'DNS servers unavailable or refused the query', 'dns_unavailable', 502
        except (dns.exception.DNSException, OSError, ValueError):
            reason, code, status = 'DNS lookup failed', 'dns_error', 502
        raise ResolutionError(f'{host}: {reason} via {label}.', code, status) from None

    def _system_addresses(self, host):
        """Use OS/NSS/VPN DNS and hosts configuration, without changing any of it.

        Keep IPv4 hostname semantics of this release. Literal IPv6 still bypasses
        DNS. No public resolver IP is hardcoded or added to the server's config.
        """
        if not self._system_slots.acquire(blocking=False):
            raise ResolutionError('resolver is busy; retry later.', 'dns_busy', 503)
        completed = threading.Event()
        outcome = {}

        def lookup():
            try:
                records = socket.getaddrinfo(host, None, socket.AF_INET,
                                             socket.SOCK_STREAM, socket.IPPROTO_TCP)
                addresses = tuple(sorted({str(ipaddress.IPv4Address(row[4][0]))
                                          for row in records}, key=self._order))
                if not addresses:
                    outcome['error'] = ('no IPv4 address returned.', 'dns_no_answer', 502)
                else:
                    outcome['addresses'] = addresses
            except socket.gaierror:
                outcome['error'] = ('name could not be resolved.', 'dns_not_found', 502)
            except Exception:
                # Do not include arbitrary NSS backend messages in the response.
                outcome['error'] = ('lookup failed.', 'dns_error', 502)
            finally:
                self._system_slots.release()
                completed.set()

        worker = threading.Thread(target=lookup, daemon=True, name='farm-system-dns')
        try:
            worker.start()
        except Exception:
            self._system_slots.release()
            raise ResolutionError('cannot start resolver worker.', 'dns_busy', 503) from None
        if not completed.wait(self.timeout):
            # The OS call may complete later. Its worker holds a slot until then,
            # so timeouts cannot create unlimited orphan threads or subprocesses.
            raise ResolutionError('lookup timed out.', 'dns_timeout', 504)
        if 'error' in outcome:
            raise ResolutionError(*outcome['error']) from None
        return outcome['addresses']

    def display_ip(self, host: str) -> str:
        """Forward lookup only; used when a sample has no numeric connection IP.

        Custom DNS and its OS fallback share the connection resolver. In explicit
        system mode the CLI still receives the original hostname, while the
        topology uses the bounded OS lookup without a second DNS implementation.
        """
        resolution = self.resolve(host)
        if resolution.source != 'system':
            return resolution.connect_host
        return self._system_addresses(validate_host(host))[0]
