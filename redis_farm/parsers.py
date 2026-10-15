"""Parsers for redis-cli output. No Redis socket protocol implementation."""
import re
from typing import Any


def info(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if line and not line.startswith('#') and ':' in line:
            key, val = line.split(':', 1)
            result[key] = val.rstrip('\r')
    return result


def pairs(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, list) or len(value) % 2:
        return {}
    return {str(value[i]): value[i + 1] for i in range(0, len(value), 2)}


def number(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def replicas(data: dict) -> list[dict]:
    result = []
    for key, value in data.items():
        if re.fullmatch(r'slave\d+', key):
            result.append(dict(part.split('=', 1) for part in value.split(',') if '=' in part))
    return result


def cluster_nodes(text: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        row = line.split()
        if len(row) < 8:
            continue
        parts = row[1].split(',')
        network = parts[0].split('@', 1)[0]
        try:
            host, port = network.rsplit(':', 1)
            host = host.strip('[]')
            port = int(port)
        except (ValueError, TypeError):
            continue
        flags = row[2].split(',')
        slots = []
        for item in row[8:]:
            if item.startswith('['):
                continue
            try:
                span = [int(n) for n in item.split('-', 1)]
                slots.append([span[0], span[-1]])
            except ValueError:
                pass
        result.append({'node_id': row[0], 'host': host or (parts[1] if len(parts) > 1 else ''),
                       'port': port, 'hostname': parts[1] if len(parts) > 1 else '',
                       'flags': flags, 'role': 'master' if 'master' in flags else 'replica',
                       'master_id': row[3], 'link_state': row[7], 'slots': slots,
                       'migration': [item for item in row[8:] if item.startswith('[')]})
    return result


def client_list(text: str) -> list[dict]:
    return [dict(x.split('=', 1) for x in line.split() if '=' in x)
            for line in text.splitlines() if line.strip()]


def acl_rows(value: Any, admin_user: str = 'admin') -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    system = {'default', admin_user, 'monitoring', 'replica_user', 'sentinel_user'}
    for line in value:
        # ACL LIST returns password hashes; ordinary widgets never expose those hashes.
        safe = re.sub(r'(?<!\S)#[0-9a-fA-F]{64}(?!\S)', '#[hidden]', str(line))
        tokens = safe.split()
        name = tokens[1] if len(tokens) > 1 else ''
        result.append({'name': name, 'enabled': 'on' in tokens[2:],
                       'category': 'system' if name in system else 'application', 'rules': safe})
    return result
