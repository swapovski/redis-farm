"""IP topology labels with explicit user-supplied names, independent of connection targets and catalog IDs.

Use the connection's observed address where available. Resolve an advertised
hostname forward only when there is no observed numeric address. Never issue
PTR requests or replace addresses with provider hostnames. Manual names are
metadata only; connection targets and stable endpoint IDs remain unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import dns.exception
from .models import FarmError


def literal_ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except (ValueError, TypeError):
        return ''


def address(host, port):
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


class HostLabels:
    def __init__(self, resolver):
        self.resolver = resolver

    def annotate(self, service, rows, cancel=None):
        if not rows:
            return

        def check_cancel():
            if cancel is not None and cancel.is_set():
                raise FarmError('Host label lookup cancelled.', 'cancelled')

        check_cancel()
        # Remember each actual connection separately: the same hostname could
        # have changed its answer between samples. Never overwrite an observed
        # IP with a fresh DNS answer or another row's observation.
        observed = [literal_ip((row.get('connection') or {}).get('connect_host'))
                    or literal_ip(row['endpoint']['host']) for row in rows]
        known = {}
        for row, ip in zip(rows, observed):
            if ip:
                known.setdefault(row['endpoint']['host'], ip)
        hosts = sorted({row['endpoint']['host'] for row, ip in zip(rows, observed)
                        if not ip and row['endpoint']['host'] not in known})

        def lookup(host):
            check_cancel()
            ip = ''
            if self.resolver is not None:
                try:
                    ip = literal_ip(self.resolver.display_ip(host))
                except (FarmError, OSError, ValueError, IndexError, dns.exception.DNSException):
                    pass
            return host, ip

        if hosts:
            with ThreadPoolExecutor(max_workers=min(6, len(hosts)),
                                    thread_name_prefix='farm-host-label') as pool:
                known.update(pool.map(lookup, hosts))
        check_cancel()
        # HF9: catalogs created before host_aliases still retain the exact input
        # name in display_seed_host (or, in older catalogs, seed.host). This is
        # user input, not a name advertised by Redis or obtained through PTR.
        manual_names = dict(service.get('host_aliases') or {})
        entered = service.get('display_seed_host') or (service.get('seed') or {}).get('host', '')
        if entered and not literal_ip(entered):
            # Prefer the address actually observed for the entered name. A new
            # DNS answer must not relabel an unrelated row after an address change.
            entered_ips = {actual or known.get(row['endpoint']['host'], '')
                           for row, actual in zip(rows, observed)
                           if row['endpoint']['host'] == entered}
            entered_ips.discard('')
            if not entered_ips and entered not in manual_names.values():
                # The initial endpoint may have been deduplicated to its IP.
                # Recover only the saved input name, using the existing forward
                # resolver; never infer names for other peers from a pattern.
                if entered in known:
                    entered_ip = known[entered]
                else:
                    _, entered_ip = lookup(entered)
                if entered_ip:
                    entered_ips.add(entered_ip)
            for entered_ip in entered_ips:
                # A later explicitly assigned name wins over the original seed.
                manual_names.setdefault(entered_ip, entered)
        check_cancel()
        for row, actual in zip(rows, observed):
            ep = row['endpoint']
            ip = actual or known.get(ep['host'], '')
            manual = manual_names.get(ip, '')
            label = manual or ip or ep['host']
            # Retain the original name only as a truthful last resort when
            # forward resolution failed. Do not guess an internal/external IP.
            row['display'] = {
                'host': label, 'address': address(label, ep['port']),
                'ip': ip, 'host_key': ip or ep['host'],
                'source': 'manual' if manual else 'ip' if ip else 'unresolved',
                'mode': 'hostname' if manual else 'ip',
                'note': '' if ip else 'IP-адрес не получен: показан исходный адрес',
            }
