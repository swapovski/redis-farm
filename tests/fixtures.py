from redis_farm.cli import Reply, CliError
from redis_farm.models import Endpoint


def arr(mapping):
    return [value for pair in mapping.items() for value in pair]


def text(mapping):
    return '# Server\n' + '\n'.join(f'{k}:{v}' for k, v in mapping.items()) + '\n'


class FakeTopologyCLI:
    """Deterministic INFO/Sentinel/Cluster replies; not a network transport."""
    def __init__(self):
        self.calls = []
        self.master = 'redis-a.example'
        self.down = {'redis-c.example'}
        self.version = 'redis-cli fixture'
        self.error = ''
        self.path = 'fixture-only'

    def status(self):
        return {'available': True, 'version': self.version, 'error': '', 'path': self.path}

    def close_owner(self, owner):
        pass

    def shutdown(self):
        pass

    def run(self, ep, command, password, *, output='raw', owner=None, cancel=None, timeout=None):
        self.calls.append((ep, tuple(command), output))
        if cancel is not None and cancel.is_set():
            raise CliError('cancelled', 'cancelled')
        if ep.kind == 'redis' and ep.port == 6389 and ep.host in self.down:
            raise CliError('Connection refused (fixture).', 'redis_error', 502)
        cmd = [part.upper() for part in command]
        if cmd[0] == 'INFO':
            if ep.port >= 26000:
                value = text({'redis_mode':'sentinel', 'redis_version':'7.2.0',
                              'run_id':f'sentinel-{ep.host}', 'config_file':f'/etc/redis/instances/sentinel-{ep.port}.conf'})
            else:
                if ep.port == 6389 and ep.auth == 'none':
                    raise CliError('NOAUTH Authentication required.', 'redis_error', 502)
                if ep.auth == 'admin' and password != 'fixture-password':
                    raise CliError('WRONGPASS invalid username-password pair.', 'redis_error', 502)
                role = 'master' if ep.host == self.master or ep.port != 6389 else 'slave'
                data = {'redis_mode':'cluster' if ep.port == 7000 else 'standalone', 'cluster_enabled':1 if ep.port == 7000 else 0,
                        'redis_version':'7.2.0', 'run_id':f'redis-{ep.host}-{ep.port}', 'role':role,
                        'config_file':f'/etc/redis/instances/redis-{ep.port}.conf',
                        'used_memory':134217728, 'used_memory_rss':150994944, 'maxmemory':1073741824,
                        'maxmemory_policy':'allkeys-lru', 'connected_clients':34, 'blocked_clients':0,
                        'instantaneous_ops_per_sec':4200, 'uptime_in_seconds':86400,
                        'rdb_last_bgsave_status':'ok', 'aof_enabled':1, 'aof_last_write_status':'ok',
                        'master_repl_offset':1000}
                if ep.port == 6389:
                    if role == 'master':
                        data.update(connected_slaves=2, slave0='ip=redis-b.example,port=6389,state=online,offset=1000,lag=0',
                                    slave1='ip=redis-c.example,port=6389,state=online,offset=999,lag=1')
                    else:
                        data.update(master_host=self.master, master_port=6389, master_link_status='up',
                                    master_sync_in_progress=0, slave_repl_offset=1000)
                value = text(data)
        elif cmd[:2] == ['SENTINEL','MASTERS']:
            value = [arr(self.master_record())]
        elif cmd[:2] == ['SENTINEL','MASTER']:
            value = arr(self.master_record())
        elif cmd[:2] in (['SENTINEL','REPLICAS'], ['SENTINEL','SLAVES']):
            value = [arr({'name':h+':6389','ip':h,'port':'6389','runid':f'redis-{h}-6389','flags':'slave'})
                     for h in ('redis-a.example','redis-b.example','redis-c.example') if h != self.master]
        elif cmd[:2] == ['SENTINEL','SENTINELS']:
            value = [arr({'ip':h, 'port':'26389', 'runid':f'sentinel-{h}', 'flags':'sentinel'})
                     for h in ('redis-a.example','redis-b.example','redis-c.example') if h != ep.host]
        elif cmd[:2] == ['CLUSTER','NODES']:
            value = '\n'.join(f'id{i} cluster-{i}.example:7000@17000 master - 0 0 1 connected {i*5000}-{min(16383,i*5000+4999)}'
                              for i in range(3))
        elif cmd[:2] == ['CLUSTER','INFO']:
            value = 'cluster_state:ok\ncluster_slots_assigned:16384\ncluster_slots_fail:0'
        elif cmd[:2] == ['ACL','USERS']:
            value = ['admin','default','monitoring','replica_user','sentinel_user','cache_app']
        elif cmd[:2] == ['ACL','LIST']:
            value = ['user default off', 'user admin on #'+'a'*64+' ~* &* +@all',
                     'user cache_app on #'+'b'*64+' ~* &* -@all +@read +@write']
        elif cmd[:2] == ['CONFIG','GET']:
            val = f'/etc/redis/instances/acl-{ep.port}.conf' if command[2] == 'aclfile' else 'fixture-value'
            value = [command[2], val]
        elif cmd[:2] == ['CLIENT','LIST']:
            value = 'id=1 addr=10.0.0.1:43210 name=frontend user=cache_app db=0 idle=2 cmd=get flags=N\n'
        elif cmd[0] in ('SLOWLOG','LATENCY'):
            value = []
        else:
            raise AssertionError(f'Unexpected automatic command: {command}')
        return Reply(value, 1.5)

    def master_record(self):
        return {'name':'cache_app', 'ip':self.master, 'port':'6389', 'runid':f'redis-{self.master}-6389',
                'flags':'master', 'quorum':'2', 'num-slaves':'2', 'num-other-sentinels':'2', 'config-epoch':'9'}
