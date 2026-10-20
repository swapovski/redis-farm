"""Persistent redis-cli processes with stdin/stdout pipes, not a system shell."""
import codecs
from collections import deque
import os
import secrets
import select
import subprocess
import threading
import time
from .cli import RedisCLI, terminate
from .models import Endpoint, FarmError
from .settings import Settings


class Terminal:
    def __init__(self, owner: str, endpoint: Endpoint, password: str | None, cli: RedisCLI,
                 settings: Settings, db: int, cluster_redirect: bool, service_id='', profile='admin'):
        self.id = secrets.token_hex(16)
        self.owner = owner
        self.endpoint = endpoint
        self.service_id = service_id
        self.profile = profile
        self.db = db
        self.cluster_redirect = cluster_redirect
        self.settings = settings
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.events = deque()
        self.seq, self.buffer_size = 0, 0
        self.last_input = time.monotonic()
        args, self.connection = cli.prepare_connection(endpoint, interactive=True, db=db,
                                                       cluster_redirect=cluster_redirect)
        env = cli.environment(endpoint, password)
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, env=env, bufsize=0,
                                            start_new_session=True)
        except OSError as exc:
            raise FarmError(f'Unable to start redis-cli ({type(exc).__name__}).', 'cli_error', 502) from None
        finally:
            env.pop('REDISCLI_AUTH', None)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, daemon=True, name='farm-terminal')
        self.reader.start()

    def _append(self, text):
        if not text:
            return
        size = len(text.encode('utf-8', 'replace'))
        with self.lock:
            self.seq += 1
            self.events.append((self.seq, text, size))
            self.buffer_size += size
            while self.events and self.buffer_size > self.settings.terminal_buffer:
                _, _, removed = self.events.popleft()
                self.buffer_size -= removed

    def _read(self):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 16384)
                if not chunk:
                    break
                self._append(decoder.decode(chunk))
            self._append(decoder.decode(b'', final=True))
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    def send(self, text: str):
        if not isinstance(text, str) or not text.strip() or '\x00' in text:
            raise FarmError('Enter a Redis command. Use escaped notation for binary data.', 'invalid_command')
        data = (text.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n') + '\n').encode('utf-8')
        if len(data) > 65536:
            raise FarmError('Console input is limited to 64 KiB per submission.', 'input_limit', 413)
        if self.process.poll() is not None:
            raise FarmError('redis-cli process has exited. Open a new console.', 'terminal_closed', 409)
        with self.write_lock:
            pos = 0
            deadline = time.monotonic() + 1.5
            try:
                while pos < len(data):
                    if time.monotonic() >= deadline:
                        self.close()
                        raise FarmError('Input stalled; console was closed. Do not automatically resend: '
                                        'some commands may have been delivered.', 'input_stalled', 409)
                    _, ready, _ = select.select([], [self.process.stdin], [], 0.1)
                    if ready:
                        try:
                            pos += os.write(self.process.stdin.fileno(), data[pos:])
                        except BlockingIOError:
                            continue
                self.last_input = time.monotonic()
            except (BrokenPipeError, OSError, ValueError):
                raise FarmError('Console connection is closed; delivery is not confirmed.', 'terminal_closed', 409) from None

    def poll(self, after: int):
        with self.lock:
            oldest = self.events[0][0] if self.events else self.seq + 1
            return {'id': self.id, 'cursor': self.seq, 'alive': self.process.poll() is None,
                    'exit_code': self.process.poll(), 'truncated': after < oldest - 1,
                    'chunks': [text for seq, text, _ in self.events if seq > after],
                    'endpoint': self.endpoint.to_dict(), 'initial_db': self.db,
                    'connection': self.connection, 'service_id': self.service_id, 'profile': self.profile,
                    'cluster_redirect': self.cluster_redirect}

    def clear(self):
        with self.lock:
            self.events.clear()
            self.buffer_size = 0
            return self.seq

    def close(self):
        terminate(self.process)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class Terminals:
    def __init__(self, cli: RedisCLI, settings: Settings):
        self.cli, self.settings = cli, settings
        self.lock = threading.RLock()
        self.items: dict[str, Terminal] = {}

    def open(self, owner, endpoint, password, db=0, cluster_redirect=False, *, service_id='', profile='admin'):
        if isinstance(db, bool) or not isinstance(db, int) or not 0 <= db <= 65535:
            raise FarmError('Invalid database number.', 'invalid_db')
        with self.lock:
            if sum(t.owner == owner for t in self.items.values()) >= self.settings.max_terminals:
                raise FarmError('Close an unused console before opening another one.', 'terminal_limit', 429)
            terminal = Terminal(owner, endpoint, password, self.cli, self.settings, db, bool(cluster_redirect), service_id, profile)
            self.items[terminal.id] = terminal
        return terminal

    def get(self, owner, tid):
        with self.lock:
            terminal = self.items.get(tid)
            if terminal is None or terminal.owner != owner:
                raise FarmError('Console was not found.', 'not_found', 404)
            return terminal

    def close(self, owner, tid):
        terminal = self.get(owner, tid)
        with self.lock:
            self.items.pop(tid, None)
        terminal.close()

    def close_owner(self, owner):
        with self.lock:
            ids = [tid for tid, terminal in self.items.items() if terminal.owner == owner]
        for tid in ids:
            try:
                self.close(owner, tid)
            except FarmError:
                pass

    def close_scope(self, owner, *, service_id=None, profile=None):
        with self.lock:
            ids = [tid for tid, t in self.items.items() if t.owner == owner and
                   (service_id is None or t.service_id == service_id) and
                   (profile is None or (profile == 'admin' and t.endpoint.kind == 'redis' and t.endpoint.auth == 'admin') or
                    (profile != 'admin' and t.profile == profile))]
        for tid in ids:
            try:
                self.close(owner, tid)
            except FarmError:
                pass

    def reap(self):
        with self.lock:
            expired = [(t.owner, tid) for tid, t in self.items.items()
                       if time.monotonic() - t.last_input > self.settings.terminal_idle]
        for owner, tid in expired:
            try:
                self.close(owner, tid)
            except FarmError:
                pass

    def shutdown(self):
        with self.lock:
            terminals, self.items = list(self.items.values()), {}
        for terminal in terminals:
            terminal.close()
