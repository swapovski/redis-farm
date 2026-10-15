"""Insertion-only snippets. They are never run automatically or permission-tested."""
import json

REDIS_COMMANDS = [
    ('server', 'INFO SERVER'), ('replication', 'INFO REPLICATION'), ('role', 'ROLE'),
    ('memory', 'INFO MEMORY'), ('persistence', 'INFO PERSISTENCE'), ('statistics', 'INFO STATS'),
    ('clients', 'CLIENT LIST'), ('users', 'ACL USERS'), ('acl', 'ACL LIST'),
    ('acl_user', 'ACL GETUSER admin'), ('aclfile', 'CONFIG GET aclfile'),
    ('acl_save', 'ACL SAVE'), ('config_memory', 'CONFIG GET maxmemory'),
    ('config_rewrite', 'CONFIG REWRITE'), ('slowlog', 'SLOWLOG GET 20'),
    ('latency', 'LATENCY LATEST'), ('keyspace', 'INFO KEYSPACE'),
]
SENTINEL_COMMANDS = [
    ('masters', 'SENTINEL MASTERS'), ('master', 'SENTINEL MASTER {name}'),
    ('replicas', 'SENTINEL REPLICAS {name}'), ('sentinels', 'SENTINEL SENTINELS {name}'),
    ('master_addr', 'SENTINEL GET-MASTER-ADDR-BY-NAME {name}'),
    ('quorum', 'SENTINEL CKQUORUM {name}'), ('failover', 'SENTINEL FAILOVER {name}'),
    ('info', 'INFO SENTINEL'), ('flushconfig', 'SENTINEL FLUSHCONFIG'),
]


def snippets(kind, service, admin_user='admin'):
    items = list(SENTINEL_COMMANDS if kind == 'sentinel' else REDIS_COMMANDS)
    if kind == 'redis' and service.get('mode') == 'cluster':
        items += [('cluster_info', 'CLUSTER INFO'), ('cluster_nodes', 'CLUSTER NODES'),
                  ('cluster_shards', 'CLUSTER SHARDS')]
    items = [(label, ('ACL GETUSER ' + json.dumps(admin_user, ensure_ascii=False)) if label == 'acl_user' else command) for label, command in items]
    name = json.dumps(service.get('master_name') or 'master-name', ensure_ascii=False)
    return [{'label': label, 'command': command.replace('{name}', name)} for label, command in items]

# A small registry lets extensions declare their UI block and detail endpoint.
WIDGETS = [
    {'id': 'topology', 'width': 'full'}, {'id': 'replication', 'width': 'half'},
    {'id': 'resources', 'width': 'half'}, {'id': 'console', 'width': 'full'},
    *({'id': key, 'width': 'full', 'detail': True} for key in ('acl', 'clients', 'config', 'slowlog', 'latency', 'info')),
    {'id': 'events', 'width': 'full'},
]
