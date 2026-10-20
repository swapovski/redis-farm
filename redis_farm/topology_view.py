"""A read-only topology projection, shared by catalog previews and INFO snapshots.

No Redis commands, credentials, disk writes or PTR. Raw endpoint objects/IDs
are retained for operations; aliases are collapsed only in the rendered view.
"""
from copy import deepcopy
from .display import HostLabels, literal_ip


def _profile(ep):
    return (ep.get('kind', 'redis'), int(ep['port']), ep.get('auth', 'admin'),
            bool(ep.get('tls', False)), ep.get('cacert', ''), ep.get('sni', ''), ep.get('username', ''),
            ep.get('certfile', ''), ep.get('keyfile', ''))


def topology_view(service, rows, resolver=None, cancel=None):
    rows = deepcopy(rows)
    HostLabels(resolver).annotate(service, rows, cancel)
    # An observed IP is authoritative. A hostname is resolved only when no
    # connection address exists (including a locked, not-yet-sampled service).
    physical = {}
    for i, row in enumerate(rows):
        ep = row['endpoint']
        ip = literal_ip(row.get('display', {}).get('ip'))
        key = ('ip', ip, _profile(ep)) if ip else ('id', ep['id'], _profile(ep))
        physical.setdefault(key, []).append(i)
    groups = []
    for members in physical.values():
        run_ids = {rows[i].get('info', {}).get('run_id') for i in members}
        run_ids.discard(None)
        run_ids.discard('')
        # A DNS name may be a load balancer or may change mid-sample. Never
        # merge contradictory observations, even if the addresses coincide.
        groups.extend([[i] for i in members] if len(run_ids) > 1 else [members])
    # Preserve the existing run_id-based alias detection for an actual Redis
    # reached by two addresses, but never across ports or transport profiles.
    by_run = {}
    for members in groups:
        ids = {rows[i].get('info', {}).get('run_id') for i in members}
        ids.discard(None)
        ids.discard('')
        key = (_profile(rows[members[0]]['endpoint']), next(iter(ids))) if len(ids)==1 else None
        if key is not None and key in by_run:
            by_run[key].extend(members)
        elif key is not None:
            by_run[key] = members
    groups = [g for g in groups if not any(g is not v and set(g).issubset(v) for v in by_run.values())]
    groups.sort(key=min)
    output = []
    for members in groups:
        # The successful sample supplies statistics. The source IDs are kept
        # so the UI can preserve a selected alias without changing credentials.
        best = min(members, key=lambda i: (rows[i].get('reachable') is not True, i))
        row = rows[best]
        options, aliases = {}, []
        for i in members:
            ep = rows[i]['endpoint']
            options.setdefault(ep['id'], ep)
            for alias in [ep.get('address', ''), *rows[i].get('aliases', [])]:
                if alias and alias != row['endpoint'].get('address') and alias not in aliases:
                    aliases.append(alias)
        row['endpoint_options'] = list(options.values())
        row['endpoint_ids'] = list(options)
        row['aliases'] = aliases
        output.append(row)
    return output


def catalog_topology(service, resolver=None, cancel=None):
    rows = [dict(endpoint=ep, reachable=None, role='unknown', error='', info={})
            for ep in service.get('endpoints', [])]
    return {'service_id': service['id'], 'nodes': topology_view(service, rows, resolver, cancel)}
