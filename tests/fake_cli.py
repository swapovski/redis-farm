#!/usr/bin/env python3
"""Subprocess fixture, not a redis-cli replacement. Never contacts a network."""
import json
import os
import shlex
import sys
import time

args = sys.argv[1:]
if '--version' in args:
    print('redis-cli 7.2.0-test-fixture')
    raise SystemExit(0)
if '--help' in args:
    print('--json --user --no-raw -2 -e')
    raise SystemExit(0)

idx = 0
options = {}
while idx < len(args):
    arg = args[idx]
    if arg in ('-h', '-p', '--user', '--cacert', '--sni', '-n'):
        options[arg] = args[idx+1]
        idx += 2
    elif arg in ('-2', '-e', '--json', '--raw', '--no-raw', '-c', '--tls'):
        options[arg] = True
        idx += 1
    else:
        break
command = args[idx:]
state = {'db': options.get('-n', '0'), 'multi': False, 'commands': []}


def execute(cmd):
    if not cmd:
        return None
    op = cmd[0].upper()
    if op == 'SLEEP':
        time.sleep(float(cmd[1]))
        return 'awake'
    if op == 'BIG':
        return 'x' * int(cmd[1])
    if op == 'ERROR':
        print('ERR fixture error', flush=True)
        raise SystemExit(1)
    if op == 'AUTHERROR':
        print('AUTH failed: fixture', file=sys.stderr, flush=True)
        return 'OK'
    if op == 'OPTIONS':
        return options
    if op == 'ENV':
        return {'auth': os.getenv('REDISCLI_AUTH'), 'history': os.getenv('REDISCLI_HISTFILE'),
                'rcfile': os.getenv('REDISCLI_RCFILE'), 'user': options.get('--user')}
    if op == 'SELECT':
        state['db'] = cmd[1]
        return 'OK'
    if op == 'DB':
        return state['db']
    if op == 'MULTI':
        state['multi'] = True
        return 'OK'
    if op == 'EXEC':
        state['multi'] = False
        result = state['commands'][:]
        state['commands'].clear()
        return result
    if op in ('QUIT', 'EXIT'):
        raise SystemExit(0)
    if state['multi']:
        state['commands'].append(cmd)
        return 'QUEUED'
    if op == 'INFO':
        return '# Server\nredis_version:7.2.0\nredis_mode:standalone\n# Replication\nrole:master\n'
    return cmd[1] if op == 'ECHO' and len(cmd) > 1 else 'PONG'


def emit(value):
    if value is None:
        return
    if options.get('--json') or not isinstance(value, str):
        print(json.dumps(value), flush=True)
    else:
        print(value, flush=True)


if command:
    emit(execute(command))
else:
    for line in sys.stdin:
        # This parser is only for fixture input; the product sends text to real redis-cli.
        emit(execute(shlex.split(line)))
