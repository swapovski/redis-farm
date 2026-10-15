"""Single-hop HTTPS reverse proxy support without trusting forwarded Host/Origin.

Nginx must overwrite X-Real-IP with its actual TCP client address. Only exact
configured proxy peers may supply this header. Origin, Host, scheme and port
are never rewritten here. Gunicorn continues to use its real TLS transport.
"""
import ipaddress
import json


def _ip(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


class TrustedClientIP:
    def __init__(self, app, trusted_proxy_ips):
        self.app = app
        self.peers = frozenset(_ip(v) for v in trusted_proxy_ips)

    @staticmethod
    def _reject(start_response):
        body = json.dumps({'code': 'proxy_header', 'error':
            'Trusted proxy must supply one valid X-Real-IP address.'}).encode('utf-8')
        start_response('403 Forbidden', [
            ('Content-Type', 'application/json'), ('Content-Length', str(len(body))),
            ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')])
        return [body]

    def __call__(self, environ, start_response):
        try:
            peer = _ip(environ.get('REMOTE_ADDR', ''))
        except (ValueError, TypeError):
            # The existing AccessPolicy produces client_denied for invalid peers.
            return self.app(environ, start_response)
        if peer not in self.peers:
            # Do not trust any forwarded address from a direct client.
            return self.app(environ, start_response)
        value = environ.get('HTTP_X_REAL_IP')
        if value is None and environ.get('PATH_INFO') == '/healthz':
            # Preserve direct local TLS health checks used by the installer.
            return self.app(environ, start_response)
        try:
            if (not isinstance(value, str) or not value or
                    any(c.isspace() for c in value) or ',' in value or '%' in value):
                raise ValueError
            client = _ip(value)
            if client.is_unspecified or client.is_multicast:
                raise ValueError
        except (ValueError, TypeError):
            return self._reject(start_response)
        adjusted = environ.copy()
        adjusted['redis_farm.proxy_peer'] = str(peer)
        adjusted['REMOTE_ADDR'] = str(client)
        return self.app(adjusted, start_response)
