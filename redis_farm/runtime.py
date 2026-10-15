"""Process-local sessions and bounded background jobs; nothing is serialized."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import secrets
import threading
import time
from typing import Callable
from .models import FarmError
from .credentials import CredentialSet, validate_password
from .settings import Settings


@dataclass
class BrowserSession:
    id: str
    csrf: str
    created: float
    active: float
    username: str | None = None
    user_dn: str | None = None
    password: str | None = field(default=None, repr=False)
    generation: int = 0
    defaults: dict = field(default_factory=dict, repr=False)
    discovery_secrets: dict = field(default_factory=dict, repr=False)


class Sessions:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.items: dict[str, BrowserSession] = {}

    def get(self, sid: str | None) -> BrowserSession | None:
        with self.lock:
            item = self.items.get(sid or '')
            if item and (time.monotonic() - item.active > self.settings.session_idle or
                         time.monotonic() - item.created > self.settings.session_lifetime):
                return None
            return item

    def create(self, *, username=None, user_dn=None):
        with self.lock:
            if len(self.items) >= 64:
                raise FarmError('Too many browser sessions.', 'busy', 429)
            now = time.monotonic()
            item = BrowserSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, now, username=username, user_dn=user_dn)
            self.items[item.id] = item
            return item

    def password(self, sid: str):
        item = self.get(sid)
        if item is None:
            raise FarmError('Browser session expired. Reload the page.', 'session_expired', 401)
        return item.password

    def touch(self, sid: str):
        with self.lock:
            item = self.get(sid)
            if item:
                item.active = time.monotonic()

    def unlock(self, sid: str, password: str):
        validate_password(password)
        with self.lock:
            item = self.get(sid)
            if not item:
                raise FarmError('Browser session expired.', 'session_expired', 401)
            item.password = password
            item.generation += 1
            item.active = time.monotonic()

    def credentials(self, sid: str, service_id: str = ''):
        with self.lock:
            item = self.get(sid)
            if item is None:
                raise FarmError('Browser session expired.', 'session_expired', 401)
            values = item.defaults.get(service_id, {})
            return CredentialSet(item.password, values.get('service'), dict(values.get('endpoints', {})))

    def set_default(self, sid, service_id, password, endpoint_ids=None):
        validate_password(password)
        with self.lock:
            item = self.get(sid)
            if item is None:
                raise FarmError('Browser session expired.', 'session_expired', 401)
            values = item.defaults.setdefault(service_id, {'service': None, 'endpoints': {}})
            if endpoint_ids is None:
                values['service'] = password
            else:
                for eid in endpoint_ids:
                    values['endpoints'][eid] = password
            item.generation += 1
            item.active = time.monotonic()

    def forget_default(self, sid, service_id, endpoint_ids=None):
        with self.lock:
            item = self.get(sid)
            if item:
                if endpoint_ids is None:
                    item.defaults.pop(service_id, None)
                else:
                    values = item.defaults.get(service_id, {})
                    for eid in endpoint_ids:
                        values.get('endpoints', {}).pop(eid, None)
                item.generation += 1

    def credential_state(self, sid, service):
        credentials = self.credentials(sid, service['id'])
        return {'service_password': credentials.default is not None,
                'endpoint_overrides': list(credentials.overrides),
                'unlocked_endpoints': [e['id'] for e in service['endpoints']
                    if e['kind'] == 'redis' and credentials.available(e)]}

    def hold_discovery(self, sid, credentials):
        with self.lock:
            item = self.get(sid)
            if item is None:
                raise FarmError('Browser session expired.', 'session_expired', 401)
            self.reap_discovery()
            if len(item.discovery_secrets) >= 16:
                raise FarmError('Too many pending discoveries. Close unused scans.', 'busy', 429)
            token = secrets.token_hex(24)
            item.discovery_secrets[token] = (time.monotonic(), credentials)
            return token

    def discovery_credentials(self, sid, token):
        with self.lock:
            item = self.get(sid)
            entry = item.discovery_secrets.get(token) if item else None
            if not entry or time.monotonic() - entry[0] > 600:
                raise FarmError('Discovery credentials expired. Enter passwords in the catalog.', 'credentials_expired', 409)
            return entry[1]

    def forget_discovery(self, sid, token):
        with self.lock:
            item = self.items.get(sid)
            if item:
                item.discovery_secrets.pop(token, None)

    def reap_discovery(self):
        with self.lock:
            for item in self.items.values():
                for token, (created, _) in list(item.discovery_secrets.items()):
                    if time.monotonic() - created > 600:
                        item.discovery_secrets.pop(token, None)

    def lock_password(self, sid: str, *, all_profiles=True):
        with self.lock:
            item = self.items.get(sid)
            if item:
                item.password = None
                if all_profiles:
                    item.defaults.clear()
                    item.discovery_secrets.clear()
                item.generation += 1

    def remove(self, sid: str):
        with self.lock:
            item = self.items.pop(sid, None)
            if item:
                item.password = None
                item.defaults.clear()
                item.discovery_secrets.clear()

    def expired(self) -> list[str]:
        with self.lock:
            return [sid for sid in self.items if self.get(sid) is None]


@dataclass
class Job:
    id: str
    owner: str
    key: str
    state: str = 'queued'
    created: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    result: object = field(default=None, repr=False)
    error: str = ''
    code: str = ''
    future: object = field(default=None, repr=False)
    progress: dict = field(default_factory=dict)

    def public(self):
        return {'id': self.id, 'state': self.state, 'result': self.result if self.state == 'done' else None,
                'error': self.error, 'code': self.code, 'progress': dict(self.progress)}


class Jobs:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='farm-job')
        self.lock = threading.RLock()
        self.items: dict[str, Job] = {}

    def submit(self, owner: str, key: str, fn: Callable[[threading.Event], object], *, commit=None) -> Job:
        with self.lock:
            for old in self.items.values():
                if old.owner == owner and old.key == key and old.state in ('queued', 'running'):
                    return old
            live = [j for j in self.items.values() if j.owner == owner and j.state in ('queued', 'running')]
            if len(live) >= 4:
                raise FarmError('There are already four active operations in this session.', 'busy', 429)
            job = Job(secrets.token_hex(16), owner, key)
            self.items[job.id] = job
            job.future = self.pool.submit(self._run, job, fn, commit)
            return job

    def _run(self, job, fn, commit=None):
        with self.lock:
            if job.cancel.is_set():
                job.state = 'cancelled'
                return
            job.state = 'running'
        try:
            result = fn(job.cancel)
            with self.lock:
                if job.cancel.is_set():
                    job.state = 'cancelled'
                else:
                    job.result = commit(result) if commit else result
                    job.state = 'done'
        except FarmError as exc:
            with self.lock:
                job.error, job.code = str(exc), exc.code
                job.state = 'cancelled' if job.cancel.is_set() else 'error'
        except Exception as exc:
            # Never serialize tracebacks or request bodies: they may contain credentials.
            with self.lock:
                job.error = f'Internal operation error ({type(exc).__name__}).'
                job.code, job.state = 'internal_error', 'error'

    def report(self, owner, cancel, value):
        with self.lock:
            for item in self.items.values():
                if item.owner == owner and item.cancel is cancel and not cancel.is_set():
                    item.progress = dict(value)
                    break

    def get(self, owner: str, jid: str) -> Job:
        with self.lock:
            item = self.items.get(jid)
            if not item or item.owner != owner:
                raise FarmError('Operation was not found.', 'not_found', 404)
            return item

    def cancel_job(self, owner: str, jid: str):
        with self.lock:
            item = self.get(owner, jid)
            if item.state in ('done', 'error', 'cancelled'):
                return
            item.cancel.set()
            if item.future:
                item.future.cancel()
            item.state = 'cancelled'
            item.result = None

    def clear_owner(self, owner: str):
        with self.lock:
            for jid, item in list(self.items.items()):
                if item.owner == owner:
                    item.cancel.set()
                    if item.future:
                        item.future.cancel()
                    item.result = None
                    self.items.pop(jid, None)

    def reap(self):
        with self.lock:
            for jid, item in list(self.items.items()):
                if item.state not in ('queued', 'running') and time.monotonic() - item.created > 600:
                    self.items.pop(jid, None)

    def shutdown(self):
        with self.lock:
            for item in self.items.values():
                item.cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
