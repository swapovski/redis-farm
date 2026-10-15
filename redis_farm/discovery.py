"""Host port ranges followed by bounded Redis/Sentinel topology discovery.

All Redis requests use redis-cli; peer hosts are visited at advertised endpoints,
not recursively port-scanned. Only local inventory is saved by the web layer.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import ipaddress
import threading
from .cli import RedisCLI, CliError
from .models import Endpoint, FarmError, sentinel_port, parse_ports, clean_service, validate_port
from .parsers import info, pairs, replicas, cluster_nodes
from .settings import Settings
from .display import HostLabels, literal_ip
from .credentials import password_for, DiscoveryCredentials


class Discovery:
    def __init__(self, cli: RedisCLI, settings: Settings):
        self.cli, self.settings = cli, settings

    def defaults(self):
        return {'redis_start': self.settings.scan_redis_start,
                'redis_end': self.settings.scan_redis_end,
                'sentinel_start': self.settings.scan_sentinel_start,
                'sentinel_end': self.settings.scan_sentinel_end,
                'max_ports_per_range': self.settings.max_scan_ports,
                'workers': self.settings.scan_workers,
                'probe_timeout': self.settings.scan_timeout}

    def plan(self, spec):
        if not isinstance(spec, dict):
            raise FarmError('Discovery specification must be an object.', 'invalid_spec')
        supplied = spec.get('ports', spec.get('port'))
        mode = spec.get('discovery_mode') or ('node' if supplied in (None, '') else 'ports')
        if mode not in ('node', 'ports'):
            raise FarmError('Choose node scan or explicit ports.', 'invalid_spec')
        kind, auth = spec.get('kind', 'auto'), spec.get('auth', 'admin')
        if kind not in ('auto', 'redis', 'sentinel'):
            raise FarmError('Choose auto, redis or sentinel.', 'invalid_kind')
        if auth not in ('admin', 'default', 'none'):
            raise FarmError('Choose admin, default or no authentication.', 'invalid_auth')
        profile = Endpoint(spec.get('host', ''), 6379, 'redis', auth,
                           spec.get('tls', False), spec.get('cacert', ''), spec.get('sni', ''))
        if not isinstance(spec.get('find_sentinel', True), bool):
            raise FarmError('find_sentinel must be true or false.', 'invalid_spec')
        ranges = None
        if mode == 'node':
            defaults = self.defaults()
            ranges = {k: validate_port(spec.get(k, defaults[k]))
                      for k in ('redis_start', 'redis_end', 'sentinel_start', 'sentinel_end')}
            targets = {}
            for family in ('redis', 'sentinel'):
                start, end = ranges[family + '_start'], ranges[family + '_end']
                if end < start or end - start + 1 > self.settings.max_scan_ports:
                    raise FarmError(f'Each range must contain 1..{self.settings.max_scan_ports} ports.', 'port_range')
                for offset, port in enumerate(range(start, end + 1)):
                    targets[port] = (offset, 'auto' if port in targets else family)
            ordered = sorted(targets.items(), key=lambda x: (x[1][0], x[0]))
            probes = [(replace(profile, port=port), hint) for port, (_, hint) in ordered]
        else:
            ports = parse_ports(supplied if supplied is not None else '', self.settings.max_scan_ports)
            explicit = spec.get('sentinel_port')
            if explicit not in (None, ''):
                validate_port(explicit)
                if len(ports) != 1:
                    raise FarmError('Explicit Sentinel port requires a single Redis seed port.', 'invalid_port')
            probes = [(replace(profile, port=port), kind) for port in ports]
        return {'mode': mode, 'profile': profile, 'probes': probes, 'ranges': ranges}

    def discover(self, spec, password, owner, cancel, progress=None):
        plan = self.plan(spec)
        ctx = _Probe(self.cli, self.settings, password, owner, cancel)
        status = {'phase': 'scan', 'completed': 0, 'total': len(plan['probes']),
                  'redis_found': 0, 'sentinel_found': 0, 'topology_completed': 0,
                  'topology_total': 0, 'services_found': 0, 'current': ''}
        def report(**values):
            status.update(values)
            if progress:
                progress(dict(status))
        def check_cancel():
            if cancel.is_set():
                raise FarmError('Discovery cancelled.', 'cancelled')
        check_cancel()
        report()
        resolver = getattr(self.cli, 'resolver', None)
        input_ip = ''
        if resolver is not None:
            input_ip = resolver.resolve(plan['profile'].host).connect_host
        hits, errors, services = [], [], []
        limit = min(self.settings.scan_workers, self.settings.max_processes)
        with ThreadPoolExecutor(max_workers=max(1, limit), thread_name_prefix='farm-scan') as pool:
            futures = {pool.submit(self._classify, ctx, ep, hint, plan['mode']): ep
                       for ep, hint in plan['probes']}
            try:
                for future in as_completed(futures):
                    check_cancel()
                    ep = futures[future]
                    try:
                        found = future.result()
                        hits.append(found)
                        status['sentinel_found' if found['endpoint'].kind == 'sentinel' else 'redis_found'] += 1
                    except FarmError as exc:
                        if exc.code == 'cancelled':
                            raise
                        errors.append({'address': ep.address, 'error': str(exc), 'code': exc.code})
                    report(completed=status['completed'] + 1, current=ep.address)
            finally:
                for future in futures:
                    future.cancel()
        hits.sort(key=lambda x: (x['endpoint'].kind != 'sentinel', x['endpoint'].port))
        report(phase='topology', topology_total=len(hits), current='')
        for index, hit in enumerate(hits):
            check_cancel()
            seed = hit['endpoint']
            report(current=seed.address)
            try:
                if seed.kind == 'sentinel':
                    found = ctx.from_sentinel(seed, plan['profile'])
                else:
                    known_ids = {e['id'] for s in services for e in s['endpoints'] if e['kind'] == 'redis'}
                    known_runs = {ctx.observe(Endpoint.from_dict(e)).get('run_id')
                                  for s in services for e in s['endpoints'] if e['kind'] == 'redis'}
                    run_id = hit['data'].get('run_id')
                    if seed.id in known_ids or (run_id and run_id in known_runs):
                        # Preserve the entered hostname when a verified run_id
                        # proves that Sentinel's IP alias is this same Redis.
                        for i, service in enumerate(services):
                            matches = any(e['kind'] == 'redis' and
                                (e['id'] == seed.id or (run_id and ctx.observe(Endpoint.from_dict(e)).get('run_id') == run_id))
                                for e in service['endpoints'])
                            if matches:
                                endpoints = {seed.id: seed, **{e['id']: Endpoint.from_dict(e) for e in service['endpoints']}}
                                services[i] = {**ctx._service(Endpoint.from_dict(service['seed']),
                                    service['mode'], endpoints, service['master_name']),
                                    'name': service['name'], 'group': service['group']}
                        found = []
                    elif hit.get('locked'):
                        found = [ctx._service(seed, 'standalone', {seed.id: seed})]
                    else:
                        found = ctx.from_redis(seed, spec.get('find_sentinel', True),
                                               spec.get('sentinel_port') if plan['mode'] == 'ports' else None)
                for service in found:
                    service['group'] = spec.get('group', '')
                    if spec.get('name') and len(found) == 1 and len(plan['probes']) == 1:
                        service['name'] = spec['name']
                    services.append(service)
                services = self._merge(services, ctx)
            except FarmError as exc:
                if exc.code == 'cancelled':
                    raise
                errors.append({'address': seed.address, 'error': str(exc), 'code': exc.code})
            report(topology_completed=index + 1, services_found=len(services))
        previews = []
        for service in services[:self.settings.max_services]:
            check_cancel()
            clean = clean_service({**service, 'display_seed_host': plan['profile'].host}, self.settings.max_nodes)
            observations = [dict(ctx.observe(Endpoint.from_dict(v))) for v in clean['endpoints']]
            # Only the entered host is an explicit user name. Peer names from
            # Sentinel/INFO are not labels, and no PTR request is made.
            entered = plan['profile'].host
            if input_ip and not literal_ip(entered):
                peer_ips = {literal_ip((r.get('connection') or {}).get('connect_host'))
                            or literal_ip(r['endpoint']['host']) for r in observations}
                if input_ip in peer_ips or any(e['host'] == entered for e in clean['endpoints']):
                    clean['host_aliases'] = {input_ip: entered}
            HostLabels(resolver).annotate(clean, observations, cancel)
            previews.append({'service': clean,
                             'observations': observations})
        if len(services) > self.settings.max_services:
            ctx.limited = True
        if len(previews) == 1 and spec.get('name'):
            previews[0]['service']['name'] = str(spec['name'])[:256]
        report(phase='done', current='', services_found=len(previews))
        return {'services': previews, 'errors': errors, 'notes': list(dict.fromkeys(ctx.notes)),
                'limited': ctx.limited, 'scan': {**status, 'mode': plan['mode'], 'ranges': plan['ranges']}}

    def _classify(self, ctx, seed, hint, mode):
        timeout = self.settings.scan_timeout if mode == 'node' else None
        ep = replace(seed, kind='sentinel', auth='none') if hint == 'sentinel' else seed
        unauth = mode == 'node' or hint == 'auto' or seed.auth == 'default'
        probe = replace(ep, auth='none') if unauth else ep
        try:
            data = info(ctx.run(probe, ['INFO', 'SERVER'], timeout=timeout))
        except CliError as exc:
            if hint == 'sentinel' or not unauth or 'NOAUTH' not in str(exc).upper():
                raise
            try:
                data = info(ctx.run(seed, ['INFO', 'SERVER'], timeout=timeout))
            except FarmError as auth_error:
                if seed.auth != 'default' or not (auth_error.code == 'default_password_required' or
                        any(v in str(auth_error).upper() for v in ('WRONGPASS', 'NOAUTH', 'AUTH FAILED', 'INVALID PASSWORD'))):
                    raise
                return {'endpoint': seed, 'data': {}, 'locked': True}
        if not data.get('redis_version'):
            raise FarmError('Port did not return Redis INFO metadata.', 'not_redis')
        ep = replace(seed, kind='sentinel', auth='none') if data.get('redis_mode') == 'sentinel' else seed
        return {'endpoint': ep, 'data': data}

    @staticmethod
    def _merge(services, ctx):
        def identity(service):
            result = set()
            redis = [e for e in service['endpoints'] if e['kind'] == 'redis']
            for value in redis or service['endpoints']:
                ep = Endpoint.from_dict(value)
                result.add(('endpoint', ep.id))
                if ep.auth == 'default':
                    result.add(('physical', ctx.physical_key(ep)))
                run_id = ctx.observe(ep).get('run_id')
                if run_id:
                    result.add((ep.kind, run_id))
            return result
        def compatible(a, b):
            if a['mode'] == 'cluster' or b['mode'] == 'cluster':
                return a['mode'] == b['mode']
            if a['mode'] == b['mode'] == 'sentinel':
                return a['master_name'] == b['master_name']
            return True
        merged = []
        for service in services:
            keys, index = identity(service), 0
            while index < len(merged):
                other = merged[index]
                if compatible(service, other) and keys.intersection(identity(other)):
                    preferred = other if other['mode'] == 'sentinel' else service
                    seed = Endpoint.from_dict(preferred['seed'])
                    endpoints = [Endpoint.from_dict(v) for v in other['endpoints'] + service['endpoints']]
                    def literal(ep):
                        try:
                            ipaddress.ip_address(ep.host)
                            return True
                        except ValueError:
                            return False
                    endpoints.sort(key=literal)
                    values = {}
                    for ep in endpoints:
                        ctx._add(values, ep)
                    service = {**ctx._service(seed, preferred['mode'], values, preferred['master_name']),
                               'name': preferred['name'], 'group': preferred['group']}
                    keys |= identity(other)
                    merged.pop(index)
                    index = 0
                else:
                    index += 1
            merged.append(service)
        return merged


class _Probe:
    def __init__(self, cli, settings, password, owner, cancel):
        self.cli, self.settings, self.password, self.owner, self.cancel = cli, settings, password, owner, cancel
        self.lock = threading.RLock()
        self.cache = {}
        self.observations = {}
        self.connections = {}
        self.notes = []
        self.limited = False
        self.touched = set()

    def run(self, ep, command, output='raw', timeout=None):
        if self.cancel.is_set():
            raise FarmError('Discovery cancelled.', 'cancelled')
        # Include auth in the cache key: an unauthenticated type probe is not admin verification.
        key = (ep, tuple(command), output)
        with self.lock:
            if key in self.cache:
                val = self.cache[key]
                if isinstance(val, FarmError):
                    raise val
                return val
            if ep.id not in self.touched and len(self.touched) >= self.settings.max_discovery_endpoints:
                self.limited = True
                raise FarmError('Discovery endpoint limit reached.', 'discovery_limit')
            self.touched.add(ep.id)
        try:
            kwargs = {'output': output, 'owner': self.owner, 'cancel': self.cancel}
            if timeout is not None:
                kwargs['timeout'] = timeout
            reply = self.cli.run(ep, command, password_for(self.password, ep), **kwargs)
            result = reply.value
            if isinstance(self.password, DiscoveryCredentials):
                self.password.mark_verified(ep)
            self.connections[ep] = reply.connection
            self.cache[key] = result
            return result
        except FarmError as exc:
            if exc.code != 'default_password_required':
                self.cache[key] = exc
            raise

    def observe(self, ep):
        if ep in self.observations:
            return self.observations[ep]
        try:
            data = info(self.run(ep, ['INFO']))
            role = 'sentinel' if data.get('redis_mode') == 'sentinel' else data.get('role', 'unknown')
            if role == 'slave':
                role = 'replica'
            result = {'endpoint': ep.to_dict(), 'reachable': True, 'role': role,
                      'version': data.get('redis_version', ''), 'run_id': data.get('run_id', ''),
                      'error': '', 'info': data, 'connection': self.connections.get(ep)}
        except FarmError as exc:
            if exc.code == 'cancelled':
                raise
            result = {'endpoint': ep.to_dict(), 'reachable': False, 'role': 'unknown',
                      'version': '', 'run_id': '', 'error': str(exc), 'info': {}}
        if not (ep.auth == 'default' and not result['reachable']):
            self.observations[ep] = result
        return result

    def physical_key(self, ep):
        resolver = getattr(self.cli, 'resolver', None)
        if ep.auth == 'default' and resolver is not None:
            try:
                return (ep.kind, resolver.resolve(ep.host).connect_host, ep.port)
            except FarmError:
                pass
        return (ep.kind, ep.host, ep.port)

    def _add(self, endpoints, ep):
        if ep.id not in endpoints and len(endpoints) >= self.settings.max_nodes:
            self.limited = True
            return False
        endpoints[ep.id] = ep
        return True

    def from_redis(self, seed, find_sentinel=True, explicit_port=None):
        data = info(self.run(seed, ['INFO']))
        if data.get('redis_mode') == 'sentinel':
            return self.from_sentinel(replace(seed, kind='sentinel', auth='none'), seed)
        if data.get('cluster_enabled') == '1' or data.get('redis_mode') == 'cluster':
            endpoints = {seed.id: seed}
            pending, visited = [seed], set()
            while pending:
                ep = pending.pop(0)
                if ep.id in visited:
                    continue
                visited.add(ep.id)
                try:
                    nodes = cluster_nodes(self.run(ep, ['CLUSTER', 'NODES']))
                except FarmError as exc:
                    if exc.code == 'cancelled':
                        raise
                    self.notes.append(f'Cluster {ep.address}: {exc}')
                    continue
                for node in nodes:
                    if not node['host'] or not 1 <= node['port'] <= 65535 or 'noaddr' in node['flags']:
                        self.notes.append('A Cluster node has no usable advertised address.')
                        continue
                    peer = seed.peer(node['host'], node['port'])
                    if isinstance(self.password, DiscoveryCredentials):
                        self.password.inherit(ep, peer)
                    if peer.id not in endpoints and self._add(endpoints, peer):
                        pending.append(peer)
            # Redis Cluster uses its own topology, not Sentinel.
            return [self._service(seed, 'cluster', endpoints)]
        endpoints = {seed.id: seed}
        queue, visited = [seed], set()
        while queue and len(visited) < self.settings.max_nodes:
            ep = queue.pop(0)
            if ep.id in visited:
                continue
            visited.add(ep.id)
            observation = self.observe(ep)
            rd = observation['info']
            peers = []
            if rd.get('master_host') and rd.get('master_port'):
                peers.append(ep.peer(rd['master_host'], rd['master_port']))
            for item in replicas(rd):
                if item.get('ip') and item.get('port'):
                    peers.append(ep.peer(item['ip'], item['port']))
            for peer in peers:
                if isinstance(self.password, DiscoveryCredentials):
                    self.password.inherit(ep, peer)
                if self._add(endpoints, peer) and peer.id not in visited:
                    queue.append(peer)
        if find_sentinel:
            candidates = []
            for ep in list(endpoints.values()):
                sport = explicit_port or sentinel_port(ep.port)
                if sport:
                    # Sentinel endpoints have their own authentication policy (none).
                    candidates.append(ep.peer(ep.host, sport, 'sentinel'))
            for sentinel in candidates:
                try:
                    result = self.from_sentinel(sentinel, seed,
                                                redis_ids={self.observe(e).get('run_id') for e in endpoints.values() if self.observe(e).get('run_id')},
                                                redis_addresses={(e.host, e.port) for e in endpoints.values()})
                    if result:
                        return result
                except FarmError as exc:
                    if exc.code == 'cancelled':
                        raise
                    # A convention-based port is optional; preserve the Redis topology.
                    self.notes.append(f'Sentinel {sentinel.address}: {exc}')
        return [self._service(seed, 'replication' if len(endpoints) > 1 or data.get('role') == 'slave' else 'standalone', endpoints)]

    def _replicas(self, sentinel, name):
        try:
            return self.run(sentinel, ['SENTINEL', 'REPLICAS', name], 'json')
        except CliError as exc:
            if 'unknown' not in str(exc).lower():
                raise
            return self.run(sentinel, ['SENTINEL', 'SLAVES', name], 'json')

    def from_sentinel(self, sentinel, profile, redis_ids=None, redis_addresses=None):
        masters = self.run(sentinel, ['SENTINEL', 'MASTERS'], 'json')
        if not isinstance(masters, list):
            raise FarmError('Unexpected Sentinel master list.', 'parse_error')
        result = []
        for raw in masters:
            master = pairs(raw)
            name = str(master.get('name', ''))
            if not name:
                continue
            reps = self._replicas(sentinel, name)
            if redis_ids is not None:
                known = [master] + [pairs(row) for row in reps]
                belongs = any((str(row.get('runid', '')) in redis_ids or
                               (str(row.get('ip', '')), int(row.get('port', 0))) in redis_addresses)
                              for row in known)
                if not belongs:
                    continue
            endpoints = {sentinel.id: sentinel}
            pending, visited = [sentinel], set()
            while pending and len(endpoints) <= self.settings.max_nodes:
                current = pending.pop(0)
                if current.id in visited:
                    continue
                visited.add(current.id)
                try:
                    record = pairs(self.run(current, ['SENTINEL', 'MASTER', name], 'json'))
                    rows = [record] + [pairs(x) for x in self._replicas(current, name)]
                    for row in rows:
                        if row.get('ip') and row.get('port'):
                            self._add(endpoints, profile.peer(row['ip'], row['port']))
                    peers = self.run(current, ['SENTINEL', 'SENTINELS', name], 'json')
                    for raw_peer in peers:
                        row = pairs(raw_peer)
                        if row.get('ip') and row.get('port'):
                            peer = current.peer(row['ip'], row['port'], 'sentinel')
                            if self._add(endpoints, peer) and peer.id not in visited:
                                pending.append(peer)
                except FarmError as exc:
                    if exc.code == 'cancelled':
                        raise
                    self.notes.append(f'Sentinel {current.address}: {exc}')
            # If the seed's MASTER command is denied, keep addresses returned by MASTERS/REPLICAS.
            for row in [master] + [pairs(x) for x in reps]:
                if row.get('ip') and row.get('port'):
                    self._add(endpoints, profile.peer(row['ip'], row['port']))
            if isinstance(self.password, DiscoveryCredentials):
                self.password.associate(endpoints.values())
            # Prefer the entered Redis hostname when INFO proves that an advertised
            # IP is the same Redis process. Do not infer aliases from names or PTR.
            seed_obs = self.observations.get(profile, {})
            if profile.kind == 'redis' and seed_obs.get('role') != 'sentinel' and seed_obs.get('run_id'):
                match = any(ep.kind == 'redis' and self.observe(ep).get('run_id') == seed_obs['run_id']
                            for ep in endpoints.values())
                if match:
                    endpoints = {profile.id: profile, **endpoints}
            result.append(self._service(sentinel, 'sentinel', endpoints, name))
        if not masters:
            self.notes.append(f'{sentinel.address} does not monitor any master groups.')
        return result

    def _service(self, seed, mode, endpoints, name=''):
        # Collapse DNS/IP aliases only after direct INFO proves identical process run_id.
        chosen, seen, physical = [], {}, {}
        for ep in endpoints.values():
            obs = self.observe(ep)
            run_id = obs.get('run_id')
            key = self.physical_key(ep) if ep.auth == 'default' else None
            if key is not None and key in physical:
                continue
            if run_id and (ep.kind, run_id) in seen:
                continue
            chosen.append(ep)
            if key is not None:
                physical[key] = ep
            if run_id:
                seen[(ep.kind, run_id)] = ep
        if seed.id not in {e.id for e in chosen}:
            seed_observation = self.observe(seed)
            same = seen.get((seed.kind, seed_observation.get('run_id'))) or physical.get(self.physical_key(seed))
            if same:
                seed = same
        return {'name': name or seed.address, 'group': '', 'mode': mode, 'master_name': name,
                'seed': seed.to_dict(), 'endpoints': [e.to_dict() for e in chosen]}
