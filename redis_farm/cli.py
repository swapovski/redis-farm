"""The only Redis transport. Never uses shell=True, SSH, redis-py or Redis sockets."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import threading
import time
from .models import Endpoint, FarmError
from .settings import Settings
from .credentials import required_error
from .resolver import HostResolver, ResolutionError


class CliError(FarmError):
    pass


@dataclass
class Reply:
    value: object
    elapsed_ms: float
    connection: dict | None = None


def terminate(process: subprocess.Popen):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.6)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


class RedisCLI:
    def __init__(self, settings: Settings, *, resolver=None):
        self.settings = settings
        self.resolver = resolver if resolver is not None else HostResolver(settings)
        self.path = shutil.which(settings.cli_path) or (str(Path(settings.cli_path).resolve()) if Path(settings.cli_path).is_file() else '')
        self.version = ''
        self.error = ''
        self.limiter = threading.BoundedSemaphore(settings.max_processes)
        self.active: dict[subprocess.Popen, str | None] = {}
        self.active_lock = threading.RLock()
        self._inspect()

    def _inspect(self):
        if not self.path:
            self.error = 'redis-cli was not found. Install redis-tools or specify --redis-cli.'
            return
        try:
            version = subprocess.run([self.path, '--version'], capture_output=True, timeout=3, check=True)
            help_text = subprocess.run([self.path, '--help'], capture_output=True, timeout=3, check=True).stdout.decode('utf-8', 'replace')
            self.version = version.stdout.decode('utf-8', 'replace').strip()
            if '--json' not in help_text or '--user' not in help_text or '--no-raw' not in help_text:
                self.error = 'This build requires redis-cli with --json, --user and --no-raw (use redis-cli 7+).'
        except (OSError, subprocess.SubprocessError):
            self.error = 'Unable to start the selected redis-cli binary.'

    def status(self):
        return {'available': bool(self.path and not self.error), 'path': self.path,
                'version': self.version, 'error': self.error, 'dns': self.resolver.status()}

    def environment(self, endpoint: Endpoint, password: str | None) -> dict:
        env = os.environ.copy()
        for key in ('REDISCLI_AUTH', 'REDISCLI_HISTFILE', 'REDISCLI_RCFILE', 'FAKETTY'):
            env.pop(key, None)
        env['REDISCLI_HISTFILE'] = os.devnull
        env['REDISCLI_RCFILE'] = os.devnull
        env['TERM'] = 'dumb'
        if endpoint.auth != 'none':
            if password is None:
                exc = required_error(endpoint)
                raise CliError(str(exc), exc.code, exc.status)
            env['REDISCLI_AUTH'] = password
        return env

    def arguments(self, endpoint: Endpoint, *, output: str = 'raw', interactive: bool = False,
                  db: int = 0, cluster_redirect: bool = False) -> list[str]:
        return self.prepare_connection(endpoint, output=output, interactive=interactive,
                                       db=db, cluster_redirect=cluster_redirect)[0]

    def prepare_connection(self, endpoint: Endpoint, *, output: str = 'raw',
                           interactive: bool = False, db: int = 0,
                           cluster_redirect: bool = False) -> tuple[list[str], dict]:
        if self.error:
            raise CliError(self.error, 'cli_unavailable', 503)
        try:
            resolution = self.resolver.resolve(endpoint.host)
        except ResolutionError as exc:
            raise CliError(str(exc), exc.code, exc.status) from None
        connection = resolution.to_dict()
        ip = resolution.connect_host
        connection['port'] = endpoint.port
        connection['address'] = f'[{ip}]:{endpoint.port}' if ':' in ip else f'{ip}:{endpoint.port}'
        args = [self.path, '-h', ip, '-p', str(endpoint.port)]
        if endpoint.kind == 'redis' and endpoint.auth == 'admin':
            args += ['--user', self.settings.redis_admin_user]
        elif endpoint.auth == 'user':
            args += ['--user', endpoint.username]
        if endpoint.tls:
            args += ['--tls']
            if endpoint.cacert:
                args += ['--cacert', endpoint.cacert]
            if endpoint.certfile:
                args += ['--cert', endpoint.certfile, '--key', endpoint.keyfile]
            # Preserve explicit SNI; otherwise retain the hostname when -h becomes an IP.
            sni = endpoint.sni or (endpoint.host if resolution.source in ('dns', 'system_fallback') else '')
            if sni:
                args += ['--sni', sni]
        if interactive:
            args += ['--no-raw']
            if db and endpoint.kind == 'redis':
                args += ['-n', str(db)]
            if cluster_redirect and endpoint.kind == 'redis':
                args += ['-c']
        else:
            # JSON output does not require RESP3 on the server.
            args += ['-2', '-e', '--json' if output == 'json' else '--raw']
        return args, connection

    @staticmethod
    def _safe_error(text: str, password: str | None) -> str:
        if password:
            text = text.replace(password, '[hidden]')
        return text[:1600]

    @classmethod
    def reply_error(cls, text, password=None):
        upper = text.upper()
        code = ('authentication_failed' if any(v in upper for v in ('WRONGPASS', 'AUTH FAILED', 'INVALID PASSWORD')) else
                'authentication_required' if 'NOAUTH' in upper else
                'permission_denied' if 'NOPERM' in upper else
                'unsupported_command' if 'UNKNOWN COMMAND' in upper else
                'connection_failed' if any(v in upper for v in ('COULD NOT CONNECT', 'CONNECTION REFUSED', 'CONNECTION RESET')) else
                'redis_error')
        return CliError(cls._safe_error(text, password), code, 502)

    def run(self, endpoint: Endpoint, command: list[str], password: str | None, *,
            output: str = 'raw', owner: str | None = None,
            cancel: threading.Event | None = None, timeout: float | None = None) -> Reply:
        if not command or any(not isinstance(x, str) or '\x00' in x for x in command):
            raise CliError('Invalid command arguments.', 'invalid_command')
        if cancel is not None and cancel.is_set():
            raise CliError('Operation cancelled.', 'cancelled')
        args, connection = self.prepare_connection(endpoint, output=output)
        args += command
        env = self.environment(endpoint, password)
        started = time.monotonic()
        process = None
        acquired = self.limiter.acquire(timeout=self.settings.command_timeout)
        if not acquired:
            raise CliError('redis-cli process limit reached; try again.', 'busy', 429)
        try:
            if cancel is not None and cancel.is_set():
                raise CliError('Operation cancelled.', 'cancelled')
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, bufsize=0, start_new_session=True)
            env.pop('REDISCLI_AUTH', None)
            with self.active_lock:
                self.active[process] = owner
            stdout, stderr = self._collect(process, cancel, timeout)
            text = stdout.decode('utf-8', 'replace').strip()
            error = stderr.decode('utf-8', 'replace').strip()
            if process.returncode != 0 or 'AUTH failed' in error:
                raise self.reply_error(error or text or 'redis-cli exited with an error.', password)
            # Older CLI builds may return zero for Redis errors. Do not mark them successful.
            if text.startswith(('NOAUTH ', 'WRONGPASS ', 'NOPERM ', 'ERR ', '(error) ', 'error:')):
                raise self.reply_error(text, password)
            if output == 'json':
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    raise CliError(self._safe_error('Invalid JSON reply from redis-cli: ' + text[:250], password),
                                   'parse_error', 502) from None
                if isinstance(value, dict) and set(value) == {'error'}:
                    raise self.reply_error(str(value['error']), password)
            else:
                value = text
            return Reply(value, round((time.monotonic() - started) * 1000, 1), connection)
        except OSError as exc:
            raise CliError(f'Unable to execute redis-cli ({exc.__class__.__name__}).', 'cli_error', 502) from None
        finally:
            env.pop('REDISCLI_AUTH', None)
            if process is not None:
                terminate(process)
                for stream in (process.stdout, process.stderr):
                    if stream:
                        stream.close()
                with self.active_lock:
                    self.active.pop(process, None)
            self.limiter.release()

    def _collect(self, process, cancel, timeout=None):
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        total = 0
        deadline = time.monotonic() + (self.settings.command_timeout if timeout is None else timeout)
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                if cancel is not None and cancel.is_set():
                    raise CliError('Operation cancelled.', 'cancelled')
                if time.monotonic() >= deadline:
                    raise CliError('redis-cli timed out; the server response was not received.', 'timeout', 504)
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > self.settings.max_output:
                        raise CliError('Reply exceeded the configured output limit.', 'output_limit', 413)
                    buffers[key.data].extend(chunk)
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise CliError('redis-cli timed out.', 'timeout', 504) from None
        return bytes(buffers['stdout']), bytes(buffers['stderr'])

    def close_owner(self, owner: str):
        with self.active_lock:
            processes = [p for p, sid in self.active.items() if sid == owner]
        for process in processes:
            terminate(process)

    def shutdown(self):
        with self.active_lock:
            processes = list(self.active)
        for process in processes:
            terminate(process)
