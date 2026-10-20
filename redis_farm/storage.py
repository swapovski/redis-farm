from __future__ import annotations
import json
import ipaddress
from contextlib import contextmanager
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from .models import FarmError, clean_service, Endpoint, host_display_policy, service_auth, service_connection


class Inventory:
    """Local metadata only. Transcripts, INFO output and credentials are not stored."""
    def __init__(self, directory: Path, max_nodes: int = 96, *, resolver=None):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'inventory.sqlite3'
        self.max_nodes = max_nodes
        self.resolver = resolver
        self.lock = threading.RLock()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS services '
                       '(id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _decode(row):
        value = json.loads(row['body'])
        return {**value, 'auth_mode': service_auth(value), 'id': row['id'], 'created_at': row['created'], 'updated_at': row['updated']}

    def list(self) -> list[dict]:
        with self.lock, self._db() as db:
            return sorted([self._decode(r) for r in db.execute('SELECT * FROM services')],
                          key=lambda s: (s['group'].lower(), s['name'].lower()))

    def get(self, sid: str) -> dict:
        with self.lock, self._db() as db:
            row = db.execute('SELECT * FROM services WHERE id=?', (sid,)).fetchone()
        if row is None:
            raise FarmError('Service was not found in the local catalog.', 'not_found', 404)
        return self._decode(row)

    def _endpoint_key(self, ep, addresses):
        host = ep['host']
        if host not in addresses:
            try:
                addresses[host] = str(ipaddress.ip_address(host))
            except ValueError:
                addresses[host] = host
                if self.resolver is not None:
                    try:
                        addresses[host] = str(ipaddress.ip_address(self.resolver.resolve(host).connect_host))
                    except (FarmError, OSError, ValueError):
                        pass
        return (ep['kind'], addresses[host], ep['port'], ep.get('auth', 'admin'), ep.get('username', ''))

    def _matches(self, a, b, addresses=None):
        addresses = {} if addresses is None else addresses
        if service_auth(a) != service_auth(b):
            return False
        if a['mode'] == b['mode'] == 'sentinel' and a['master_name'] != b['master_name']:
            return False
        if 'cluster' in (a['mode'], b['mode']) and a['mode'] != b['mode']:
            return False
        left = {self._endpoint_key(e, addresses) for e in a['endpoints'] if e['kind'] == 'redis'}
        right = {self._endpoint_key(e, addresses) for e in b['endpoints'] if e['kind'] == 'redis'}
        if left and right:
            return bool(left & right)
        return (a['mode'] == b['mode'] and a['master_name'] == b['master_name'] and
                bool({self._endpoint_key(e, addresses) for e in a['endpoints']} &
                     {self._endpoint_key(e, addresses) for e in b['endpoints']}))

    def _save_in(self, db, clean, sid=None, preserve_labels=False, addresses=None):
        addresses = {} if addresses is None else addresses
        old = None
        if sid:
            row = db.execute('SELECT * FROM services WHERE id=?', (sid,)).fetchone()
            if row is None:
                raise FarmError('Service was not found in the local catalog.', 'not_found', 404)
            old = self._decode(row)
        else:
            for row in db.execute('SELECT * FROM services ORDER BY created'):
                candidate = self._decode(row)
                if self._matches(candidate, clean, addresses):
                    old, sid = candidate, candidate['id']
                    # Prefer existing endpoint objects, IDs and TLS/auth settings.
                    # A manually entered DNS alias must not move a live console or
                    # lose a per-endpoint credential bound to its existing ID.
                    merged = {e['id']: e for e in candidate['endpoints']}
                    physical = {self._endpoint_key(e, addresses): e for e in candidate['endpoints']}
                    replacements = {}
                    for ep in clean['endpoints']:
                        key = self._endpoint_key(ep, addresses)
                        canonical = physical.get(key)
                        if canonical is None:
                            merged[ep['id']] = ep
                            physical[key] = ep
                        else:
                            replacements[ep['id']] = canonical
                    clean = {**clean, 'seed': replacements.get(clean['seed']['id'], clean['seed']),
                             'host_aliases': {**candidate.get('host_aliases', {}),
                                              **clean.get('host_aliases', {})}}

                    if candidate['mode'] == 'sentinel' and clean['mode'] != 'sentinel':
                        clean = {**clean, 'mode': candidate['mode'],
                                 'master_name': candidate['master_name'], 'seed': candidate['seed']}
                    clean = {**clean, 'endpoints': list(merged.values()),
                             'redis_connection': service_connection(candidate, 'redis'),
                             'sentinel_connection': service_connection(candidate, 'sentinel')}
                    if preserve_labels:
                        # Explicit non-empty scan group assigns the cluster folder.
                        # Empty group keeps a previously assigned folder.
                        old_mode, old_seed = host_display_policy(old)
                        clean.update(name=old['name'], group=clean['group'] or old['group'],
                                     display_mode=old_mode, display_seed_host=old_seed)
                    clean = clean_service(clean, self.max_nodes)
                    break
        sid = sid or uuid.uuid4().hex
        now = time.time()
        db.execute('INSERT OR REPLACE INTO services VALUES (?,?,?,?)',
                   (sid, json.dumps(clean, ensure_ascii=False), old['created_at'] if old else now, now))
        return sid

    def save(self, value: dict, sid: str | None = None) -> dict:
        clean = clean_service(value, self.max_nodes)
        with self.lock, self._db() as db:
            sid = self._save_in(db, clean, sid)
        return self.get(sid)

    def save_many(self, values: list[dict], *, preserve_labels=False) -> list[dict]:
        # A discovery result is saved in one SQLite transaction.
        clean = [clean_service(v, self.max_nodes) for v in values]
        with self.lock, self._db() as db:
            addresses = {}
            ids = [self._save_in(db, value, preserve_labels=preserve_labels, addresses=addresses) for value in clean]
        return [self.get(sid) for sid in dict.fromkeys(ids)]

    def merge_endpoints(self, sid: str, endpoints: list[dict]) -> dict:
        with self.lock:
            existing = self.get(sid)
            merged = {e['id']: e for e in existing['endpoints']}
            addresses = {}
            known = {self._endpoint_key(e, addresses) for e in existing['endpoints']}
            for value in endpoints:
                item = Endpoint.from_dict(value)
                key = self._endpoint_key(item.to_dict(), addresses)
                if key in known and item.id not in merged:
                    continue
                known.add(key)
                if len(merged) < self.max_nodes or item.id in merged:
                    merged.setdefault(item.id, item.to_dict())
            if len(merged) == len(existing['endpoints']):
                return existing
            return self.save({**existing, 'endpoints': list(merged.values())}, sid)

    def delete(self, sid: str):
        with self.lock, self._db() as db:
            db.execute('DELETE FROM services WHERE id=?', (sid,))

    def export(self) -> dict:
        return {'format': 'redis-farm-inventory', 'version': 1,
                'services': [clean_service(s, self.max_nodes) for s in self.list()]}

    def import_data(self, payload: dict) -> list[dict]:
        if not isinstance(payload, dict) or payload.get('format') != 'redis-farm-inventory' or payload.get('version') != 1:
            raise FarmError('Unsupported catalog format.', 'invalid_catalog')
        services = payload.get('services')
        if not isinstance(services, list) or len(services) > 500:
            raise FarmError('Catalog must contain at most 500 services.', 'invalid_catalog')
        clean = [clean_service(s, self.max_nodes) for s in services]
        return [self.save(s) for s in clean]
