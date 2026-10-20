from concurrent.futures import ThreadPoolExecutor
from collections import deque
import threading
import time
from .cli import RedisCLI
from .models import Endpoint, FarmError, service_peer
from .parsers import info, pairs, number, cluster_nodes, client_list, acl_rows
from .settings import Settings
from .topology_view import topology_view
from .credentials import password_for


class Telemetry:
    def __init__(self, cli: RedisCLI, settings: Settings):
        self.cli, self.settings = cli, settings
        self.lock = threading.RLock()
        self.acl_cache = {}
        self.snapshots = {}
        self.events = {}

    def _run(self, ep, command, password, owner, cancel, output='raw'):
        return self.cli.run(ep, command, password_for(password, ep), owner=owner, cancel=cancel, output=output)

    def _acl_meta(self, ep, password, owner, cancel):
        key = (owner, ep)
        with self.lock:
            cached = self.acl_cache.get(key)
            if cached and time.monotonic() - cached[0] < 60:
                return cached[1]
        try:
            value = self._run(ep, ['ACL', 'USERS'], password, owner, cancel, 'json').value
            result = {'state': 'available', 'users': value if isinstance(value, list) else [], 'error': ''}
        except FarmError as exc:
            result = {'state': 'unsupported' if 'unknown command' in str(exc).lower() else 'unknown',
                      'users': [], 'error': str(exc)}
        with self.lock:
            if not cancel.is_set():
                self.acl_cache[key] = (time.monotonic(), result)
        return result

    def _sample(self, ep, password, owner, cancel, service):
        row = {'endpoint': ep.to_dict(), 'reachable': False, 'role': 'unknown', 'error': '',
               'info': {}, 'sample_ms': None, 'sampled_at': time.time(), 'aliases': [], 'access_state': 'unknown', 'authenticated': None, 'transport_reachable': None}
        try:
            reply = self._run(ep, ['INFO'], password, owner, cancel)
            data = info(reply.value)
            row.update(reachable=True, access_state='ready', authenticated=True, transport_reachable=True, info=data, sample_ms=reply.elapsed_ms, connection=reply.connection,
                       role='sentinel' if data.get('redis_mode') == 'sentinel' else data.get('role', 'unknown'))
            if row['role'] == 'slave':
                row['role'] = 'replica'
            if ep.kind == 'redis':
                row['acl'] = self._acl_meta(ep, password, owner, cancel)
                if data.get('cluster_enabled') == '1':
                    try:
                        row['cluster'] = info(self._run(ep, ['CLUSTER', 'INFO'], password, owner, cancel).value)
                        row['cluster_nodes'] = cluster_nodes(self._run(ep, ['CLUSTER', 'NODES'], password, owner, cancel).value)
                    except FarmError as exc:
                        row['cluster_error'] = str(exc)
            elif service.get('master_name'):
                try:
                    name = service['master_name']
                    row['sentinel'] = pairs(self._run(ep, ['SENTINEL', 'MASTER', name], password, owner, cancel, 'json').value)
                    try:
                        reps = self._run(ep, ['SENTINEL', 'REPLICAS', name], password, owner, cancel, 'json').value
                    except FarmError as exc:
                        if 'unknown' not in str(exc).lower():
                            raise
                        reps = self._run(ep, ['SENTINEL', 'SLAVES', name], password, owner, cancel, 'json').value
                    row['sentinel_replicas'] = [pairs(v) for v in reps]
                    peers = self._run(ep, ['SENTINEL', 'SENTINELS', name], password, owner, cancel, 'json').value
                    row['sentinel_peers'] = [pairs(v) for v in peers]
                except FarmError as exc:
                    row['sentinel_error'] = str(exc)
        except FarmError as exc:
            row['error'], row['error_code'] = str(exc), exc.code
            row['access_state'] = exc.code
            if exc.code in ('authentication_required', 'authentication_failed', 'permission_denied', 'unsupported_command'):
                row['transport_reachable'] = True
                row['authenticated'] = exc.code in ('permission_denied', 'unsupported_command')
            elif exc.code not in ('password_required', 'sentinel_password_required', 'default_password_required'):
                row['transport_reachable'] = False
        return row

    def snapshot(self, service, password, owner, cancel):
        endpoints = {e.id: e for e in map(Endpoint.from_dict, service['endpoints'])}
        profile = next((e for e in endpoints.values() if e.kind == 'redis'),
                       service_peer(service, service['seed']['host'], 6379))
        # At least one Redis auth policy is retained in Sentinel-discovered services.
        def sample(ep):
            return self._sample(ep, password, owner, cancel, service)
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(endpoints))), thread_name_prefix='farm-sample') as pool:
            rows = list(pool.map(sample, endpoints.values()))
            extra = {}
            for row in rows:
                for observed in [row.get('sentinel', {})] + row.get('sentinel_replicas', []):
                    if observed.get('ip') and observed.get('port'):
                        try:
                            ep = service_peer(service, observed['ip'], observed['port'])
                            extra[ep.id] = ep
                        except FarmError:
                            pass
                for observed in row.get('sentinel_peers', []):
                    if observed.get('ip') and observed.get('port'):
                        try:
                            ep = service_peer(service, observed['ip'], observed['port'], 'sentinel')
                            extra[ep.id] = ep
                        except FarmError:
                            pass
                for observed in row.get('cluster_nodes', []):
                    if observed.get('host') and observed.get('port') and 'noaddr' not in observed.get('flags', []):
                        try:
                            ep = service_peer(service, observed['host'], observed['port'])
                            extra[ep.id] = ep
                        except FarmError:
                            pass
                # Replication without Sentinel can also grow between refreshes.
                data = row.get('info', {})
                if row['endpoint']['kind'] == 'redis':
                    from .parsers import replicas
                    candidates = replicas(data)
                    if data.get('master_host') and data.get('master_port'):
                        candidates.append({'ip': data['master_host'], 'port': data['master_port']})
                    for observed in candidates:
                        try:
                            ep = service_peer(service, observed['ip'], observed['port'])
                            extra[ep.id] = ep
                        except (FarmError, KeyError):
                            pass
            new = [ep for eid, ep in extra.items() if eid not in endpoints]
            limit = max(0, self.settings.max_nodes - len(endpoints))
            new = new[:limit]
            if new:
                rows.extend(pool.map(sample, new))
                endpoints.update({ep.id: ep for ep in new})
        if cancel.is_set():
            raise FarmError('Snapshot cancelled.', 'cancelled')
        unique = topology_view(service, rows, getattr(self.cli, 'resolver', None), cancel)
        redis_rows = [r for r in unique if r['endpoint']['kind'] == 'redis']
        sent_rows = [r for r in unique if r['endpoint']['kind'] == 'sentinel']
        live = [r for r in redis_rows if r['reachable']]
        votes = []
        for row in sent_rows:
            view = row.get('sentinel', {})
            if view.get('ip') and view.get('port'):
                votes.append(f"{view['ip']}:{view['port']}")
        summary = {
            'redis_total': len(redis_rows), 'redis_up': len(live),
            'sentinel_total': len(sent_rows), 'sentinel_up': sum(r['reachable'] for r in sent_rows),
            'masters': [r['endpoint']['address'] for r in live if r['role'] == 'master'],
            'clients': sum(number(r['info'].get('connected_clients')) for r in live),
            'memory_all': sum(number(r['info'].get('used_memory')) for r in live),
            'memory_masters': sum(number(r['info'].get('used_memory')) for r in live if r['role'] == 'master'),
            'ops_all': sum(number(r['info'].get('instantaneous_ops_per_sec')) for r in live),
            'sentinel_views': sorted(set(votes)),
            'sentinel_agree': len(set(votes)) == 1 if votes else None,
            'replication_down': [r['endpoint']['address'] for r in live
                                 if r['role'] == 'replica' and r['info'].get('master_link_status') != 'up'],
            'acl_known': sum(r.get('acl', {}).get('state') == 'available' for r in live),
        }
        result = {'service_id': service['id'], 'sampled_at': time.time(), 'nodes': unique,
                  'summary': summary, 'endpoints': [e.to_dict() for e in endpoints.values()]}
        with self.lock:
            if cancel.is_set():
                raise FarmError('Snapshot cancelled.', 'cancelled')
            key = (owner, service['id'])
            previous = self.snapshots.get(key)
            events = self.events.setdefault(key, deque(maxlen=200))
            if previous:
                before = {n['endpoint']['id']: n for n in previous['nodes']}
                for row in unique:
                    old = before.get(row['endpoint']['id'])
                    if old:
                        for field in ('reachable', 'role'):
                            if old[field] != row[field]:
                                events.append({'time': result['sampled_at'], 'endpoint': row['endpoint']['address'],
                                               'field': field, 'before': old[field], 'after': row[field]})
                    else:
                        events.append({'time': result['sampled_at'], 'endpoint': row['endpoint']['address'],
                                       'field': 'discovered', 'before': '', 'after': row['role']})
            result['events'] = list(reversed(events))
            if not cancel.is_set():
                self.snapshots[key] = result
        return result

    def last(self, owner, sid):
        with self.lock:
            return self.snapshots.get((owner, sid))

    def clear_owner(self, owner):
        with self.lock:
            for mapping in (self.acl_cache, self.snapshots, self.events):
                for key in list(mapping):
                    if key[0] == owner:
                        mapping.pop(key, None)

    def detail(self, ep, topic, password, owner, cancel):
        def run(command, output='raw'):
            return self._run(ep, command, password, owner, cancel, output).value
        if topic == 'acl':
            rows = acl_rows(run(['ACL', 'LIST'], 'json'), admin_user=ep.username or self.settings.redis_admin_user)
            try:
                aclfile = pairs(run(['CONFIG', 'GET', 'aclfile'], 'json')).get('aclfile', '')
                file_state = 'external' if aclfile else 'not_configured'
                file_error = ''
            except FarmError as exc:
                aclfile, file_state, file_error = '', 'unknown', str(exc)
            payload = {'rows': rows, 'aclfile': aclfile, 'file_state': file_state, 'file_error': file_error}
        elif topic == 'clients':
            rows = client_list(run(['CLIENT', 'LIST']))
            payload = {'rows': rows[:1000], 'total': len(rows), 'limited': len(rows) > 1000}
        elif topic == 'config':
            values, errors = {}, {}
            for key in ('aclfile', 'maxmemory', 'maxmemory-policy', 'appendonly', 'appendfsync',
                        'save', 'dir', 'dbfilename', 'databases', 'replica-read-only', 'repl-backlog-size'):
                try:
                    values.update(pairs(run(['CONFIG', 'GET', key], 'json')))
                except FarmError as exc:
                    errors[key] = str(exc)
            payload = {'values': values, 'errors': errors}
        elif topic == 'slowlog':
            payload = run(['SLOWLOG', 'GET', '25'], 'json')
        elif topic == 'latency':
            payload = run(['LATENCY', 'LATEST'], 'json')
        elif topic == 'info':
            payload = run(['INFO', 'ALL'])
        else:
            raise FarmError('Unknown detail panel.', 'invalid_topic')
        return {'topic': topic, 'endpoint': ep.to_dict(), 'sampled_at': time.time(), 'payload': payload}
