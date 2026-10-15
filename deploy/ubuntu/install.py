#!/usr/bin/env python3
"""Install only Redis Farm; no Redis commands, firewall changes or other restarts.

Prepare an isolated release before switching the current symlink. Restore the
previous current symlink if the new HTTPS service does not become healthy.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from redis_farm import __version__
from redis_farm.install_wizard import (first_server_config, choose_language, confirm_no_auth, validate_admin_user)
from redis_farm.ldap_setup import prepare_connector, save_connector
from redis_farm.server import parse_config, check_certificate, DEFAULT_CLIENT_CIDRS

from redis_farm.installer_locale import localize_callable
import builtins
print = localize_callable(builtins.print)

ROOT = Path('/opt/redis-farm')
CONF_DIR = Path('/etc/redis-farm')
CONF = CONF_DIR / 'server.json'
STATE = Path('/var/lib/redis-farm')
SERVICE = Path('/etc/systemd/system/redis-farm.service')
SERVICE_NAME = 'redis-farm.service'


# An unpacked source may itself be /opt/redis-farm. Never copy the whole
# source tree: releases/current/bin/state can be children of that directory.
RELEASE_ENTRIES = (
    'redis_farm', 'deploy', 'docs', 'tests',
    'requirements.txt', 'requirements-server.txt', 'requirements-server-noauth.txt', 'requirements-dev.txt', 'run.py',
    'install.sh', 'start.sh', 'install-server.sh', 'README.md',
    'CHANGELOG.md', '.gitignore', 'README.ru.md', 'NOTICE.md', 'LICENSE', 'tools',
)
COPY_IGNORE = shutil.ignore_patterns(
    '.venv', 'data', '.git', '__pycache__', '*.pyc', '.pytest_cache',
)


def copy_release(source, destination):
    """Copy the distribution, never neighbouring releases or user files.

    Supports source == ROOT, sources elsewhere, and a source under releases/.
    Does not follow symlinks in the distribution. On copy failure remove only
    the new destination created here; never remove pre-existing directories.
    """
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve(strict=False)
    if destination == source or source.is_relative_to(destination):
        raise RuntimeError('Release destination must not contain the source directory.')
    if destination.exists():
        raise FileExistsError(f'Release destination already exists: {destination}')
    for name in RELEASE_ENTRIES:
        entry = source / name
        if not entry.exists() or entry.is_symlink():
            raise RuntimeError(f'Missing or symlinked distribution entry: {entry}')
        if destination == entry or destination.is_relative_to(entry):
            raise RuntimeError(f'Release destination must not be inside a copied entry: {entry}')
        if entry.is_dir():
            for folder, dirs, files in os.walk(entry, followlinks=False):
                ignored = set(COPY_IGNORE(folder, dirs + files))
                dirs[:] = [n for n in dirs if n not in ignored]
                for child in dirs + [n for n in files if n not in ignored]:
                    if (Path(folder) / child).is_symlink():
                        raise RuntimeError(f'Symlink in distribution is not supported: {Path(folder) / child}')
        elif not entry.is_file():
            raise RuntimeError(f'Not a regular distribution entry: {entry}')
    destination.mkdir(mode=0o755)  # parents must exist; do not reuse old releases
    try:
        for name in RELEASE_ENTRIES:
            entry = source / name
            if entry.is_dir():
                shutil.copytree(entry, destination / name, ignore=COPY_IGNORE)
            else:
                shutil.copy2(entry, destination / name)
    except Exception:
        shutil.rmtree(destination)
        raise
    return destination


def _asset_roots(previous):
    """Return local release roots in priority order; no network lookup is performed."""
    roots = []
    def add(path):
        if not path:
            return
        try:
            path = Path(path)
            if path.exists():
                resolved = path.resolve()
                if resolved not in roots:
                    roots.append(resolved)
        except OSError:
            return
    add(previous)
    # Older releases can still contain the artwork even when the immediately previous
    # release was produced by a build that omitted optional assets.
    releases = ROOT / 'releases'
    if releases.is_dir():
        for path in sorted((p for p in releases.iterdir() if p.is_dir()),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            add(path)
    # Common operator backup used before reinstalling /opt/redis-farm.
    backup = Path(str(ROOT) + '.bak')
    add(backup / 'current')
    backup_releases = backup / 'releases'
    if backup_releases.is_dir():
        for path in sorted((p for p in backup_releases.iterdir() if p.is_dir()),
                           key=lambda p: p.stat().st_mtime, reverse=True):
            add(path)
    return roots


def _find_asset(roots, names):
    for root in roots:
        static = root / 'redis_farm/static'
        if not static.is_dir():
            continue
        by_lower = {p.name.lower(): p for p in static.iterdir()
                    if p.is_file() and not p.is_symlink()}
        for name in names:
            candidate = by_lower.get(name.lower())
            if candidate is not None:
                return candidate
    return None


def _validated_image(path, signature):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise RuntimeError(f'Existing image exceeds 8 MiB: {path}')
    data = path.read_bytes()
    if not data.startswith(signature):
        raise RuntimeError(f'Unexpected image format: {path}')
    return data


def _make_svg_favicon(png_data):
    import base64
    encoded = base64.b64encode(png_data).decode('ascii')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<image width="64" height="64" preserveAspectRatio="xMidYMid meet" '
        f'href="data:image/png;base64,{encoded}"/></svg>\n'
    ).encode('utf-8')


def preserve_local_assets(previous, release):
    """Carry Redis Farm artwork across upgrades, including legacy/backup names."""
    target_dir = Path(release) / 'redis_farm/static'
    roots = [root for root in _asset_roots(previous) if root != Path(release).resolve()]
    specs = {
        'logo.png': (b'\x89PNG\r\n\x1a\n', ('logo.png', 'radish-sprite.png')),
        'add.png': (b'\x89PNG\r\n\x1a\n', ('add.png', 'HD_Rescue_Radish.png')),
        'setup.webp': (b'RIFF', ('setup.webp', 'Radish.webp')),
        'favicon.png': (b'\x89PNG\r\n\x1a\n', ('favicon.png',)),
        'favicon.svg': (b'<svg', ('favicon.svg',)),
    }
    copied = {}
    for name, (signature, aliases) in specs.items():
        target = target_dir / name
        source = _find_asset(roots, aliases)
        if source is not None:
            data = _validated_image(source, signature)
            target.write_bytes(data)
            copied[name] = source
            print(f'Preserved local image: {name} <- {source}')
        elif target.is_file():
            copied[name] = target
            print(f'Bundled image retained: {name}')

    # A logo is sufficient to create working favicons without Pillow/ImageMagick.
    logo = target_dir / 'logo.png'
    if logo.is_file():
        logo_data = _validated_image(logo, b'\x89PNG\r\n\x1a\n')
        favicon_png = target_dir / 'favicon.png'
        favicon_svg = target_dir / 'favicon.svg'
        if not favicon_png.is_file():
            favicon_png.write_bytes(logo_data)
            print('Generated favicon.png from logo.png')
        if 'logo.png' in copied and copied['logo.png'] != logo and _find_asset(roots, ('favicon.png',)) is None:
            favicon_png.write_bytes(logo_data)
        if not favicon_svg.is_file() or ('logo.png' in copied and copied['logo.png'] != logo and _find_asset(roots, ('favicon.svg',)) is None):
            favicon_svg.write_bytes(_make_svg_favicon(logo_data))
            print('Generated favicon.svg from logo.png')

    missing = [name for name in specs if not (target_dir / name).is_file()]
    if missing:
        raise RuntimeError('Required bundled artwork is missing: ' + ', '.join(missing))


def verify_cli(path):
    """Inspect the selected local executable only. No DNS or Redis calls."""
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f'redis-cli is not an executable absolute path: {path}')
    env = os.environ.copy()
    env.pop('REDISCLI_AUTH', None)
    env['REDISCLI_RCFILE'] = os.devnull
    env['REDISCLI_HISTFILE'] = os.devnull
    try:
        version = subprocess.run([str(path), '--version'], check=True, capture_output=True,
                                 text=True, timeout=5, env=env).stdout.strip()
        help_text = subprocess.run([str(path), '--help'], check=True, capture_output=True,
                                   text=True, timeout=5, env=env).stdout
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or '').strip()
        stdout = (exc.stdout or '').strip()
        detail = stderr or stdout or f'exit status {exc.returncode}'
        raise RuntimeError(f'Cannot inspect redis-cli at {path}: {detail[:300]}') from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'Cannot inspect redis-cli at {path}: {exc}') from None
    required = ('--json', '--user', '--no-raw', '-2', '-e')
    missing = [flag for flag in required
               if not re.search(r'(?<![\w-])' + re.escape(flag) + r'(?![\w-])', help_text)]
    if missing:
        raise RuntimeError(
            f'Incompatible client: {path} ({version}). Missing flags: {", ".join(missing)}. '
            'Install a separate redis-cli 7+ and pass --redis-cli /absolute/path/redis-cli. '
            'The remote Redis servers do not need upgrading. See docs/TROUBLESHOOTING.md. '
            'Stopped before creating a release or changing the service.'
        )
    print(f'Local client OK: {path} ({version}); no Redis connection was made.', flush=True)
    return version


def select_compatible_cli(config, args):
    """An absent/old system client is recoverable without replacing system Redis."""
    from redis_farm.install_wizard import LANGUAGE
    path = config.settings.cli_path
    while True:
        try:
            verify_cli(path)
            return path
        except RuntimeError as exc:
            if getattr(args, 'non_interactive', False) or not sys.stdin.isatty():
                raise
            print(str(exc))
            question = ('Path to another redis-cli, install (official standalone client), or abort: ' if LANGUAGE == 'en' else
                        '\u0414\u0440\u0443\u0433\u043e\u0439 \u043f\u0443\u0442\u044c \u043a redis-cli, install (\u043e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d\u044b\u0439 \u043a\u043b\u0438\u0435\u043d\u0442) \u0438\u043b\u0438 abort: ')
            answer = input(question).strip()
            if answer == 'install':
                import tempfile
                if not shutil.which('curl'):
                    raise RuntimeError('curl is required for the optional standalone client download.')
                dest = ROOT / 'bin'
                dest.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='redis-farm-cli-') as directory:
                    script = Path(directory) / 'install.sh'
                    run('curl', '-fSL', '--proto', '=https', '--connect-timeout', '10', '--max-time', '120',
                        'https://packages.redis.io/redis-cli/install.sh', '-o', script)
                    run('sh', script, env={**os.environ, 'REDIS_CLI_INSTALL_DIR':str(dest)})
                path = str(dest / 'redis-cli')
            elif answer and answer not in ('abort', 'cancel'):
                path = answer
            else:
                raise RuntimeError('Installation cancelled. Install a compatible redis-cli and rerun.')


def override_cli(raw, value):
    """Only an explicit installer flag may change an existing cli_path."""
    result = dict(raw)
    if value is not None:
        path = Path(value)
        if not path.is_absolute() or any(c in str(path) for c in ('\x00', '\n', '\r')):
            raise ValueError('--redis-cli must be an absolute executable path')
        # Do not silently resolve user-managed version symlinks into a fixed target.
        result['cli_path'] = str(path)
    return result


def short_error(exc):
    """Do not dump megabytes of recursively nested copytree error paths."""
    if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
        errors = exc.args[0]
        first = str(errors[0][2]) if errors else 'unknown copy error'
        return f'Copy failed for {len(errors)} entries: {first[:600]}'
    return str(exc)[:1500]


def run(*args, quiet=False, **kwargs):
    if quiet:
        return subprocess.run([str(v) for v in args], check=True, capture_output=True, text=True, **kwargs)
    print('+ ' + ' '.join(str(v) for v in args), flush=True)
    return subprocess.run([str(v) for v in args], check=True, **kwargs)


def active():
    return subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE_NAME]).returncode == 0


def atomic_text(path, text, mode=0o640, uid=0, gid=0):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        temp.write_text(text, encoding='utf-8')
        temp.chmod(mode)
        os.chown(temp, uid, gid)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def point_current(target):
    temp = ROOT / ('.current-' + uuid.uuid4().hex)
    temp.symlink_to(target)
    os.replace(temp, ROOT / 'current')


def certificate(config, gid):
    cert, key = config.tls_cert, config.tls_key
    if cert.exists() != key.exists():
        raise RuntimeError('Only one TLS file exists. Supply a matching pair; existing files were not overwritten.')
    if cert.exists():
        check_certificate(config)
        print('Existing TLS certificate/key preserved.')
        return
    if cert.parent != CONF_DIR / 'tls' or key.parent != CONF_DIR / 'tls':
        raise RuntimeError('Custom TLS paths must already contain a valid certificate/key pair.')
    cert.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(cert.parent, 0, gid)
    cert.parent.chmod(0o750)
    temp_cert = cert.with_name('.server-' + uuid.uuid4().hex + '.crt')
    temp_key = key.with_name('.server-' + uuid.uuid4().hex + '.key')
    try:
        run('openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-noenc',
            '-days', '90', '-subj', '/CN=' + config.bind_host,
            '-addext', 'subjectAltName=IP:' + config.bind_host,
            '-addext', 'basicConstraints=critical,CA:FALSE',
            '-addext', 'keyUsage=critical,digitalSignature,keyEncipherment',
            '-addext', 'extendedKeyUsage=serverAuth',
            '-keyout', temp_key, '-out', temp_cert, quiet=True)
        for item in (temp_cert, temp_key):
            item.chmod(0o640)
            os.chown(item, 0, gid)
        os.replace(temp_key, key)
        os.replace(temp_cert, cert)
    finally:
        temp_key.unlink(missing_ok=True)
        temp_cert.unlink(missing_ok=True)
    check_certificate(config)
    print('Created an IP-SAN self-signed certificate (90 days).')


def verify_bind(config, was_active):
    family = socket.AF_INET6 if ':' in config.bind_host else socket.AF_INET
    if not was_active:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            # Do not seize an occupied listener, including another app's port.
            sock.bind((config.bind_host, config.settings.port))


def health(config, seconds=25):
    # Trust this specific certificate, keep hostname/IP verification enabled.
    context = ssl.create_default_context(cafile=str(config.tls_cert))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        connection = http.client.HTTPSConnection(config.public_host, config.settings.port,
                                                 timeout=2, context=context)
        raw_socket = None
        try:
            # Always use this server's IP, but validate the configured DNS/IP identity.
            # Thus a later domain-only certificate works without relying on OS DNS.
            raw_socket = socket.create_connection((config.bind_host, config.settings.port), timeout=2)
            connection.sock = context.wrap_socket(raw_socket, server_hostname=config.public_host)
            connection.request('GET', '/healthz')
            reply = connection.getresponse()
            data = json.loads(reply.read(4096))
            if reply.status == 200 and data.get('status') == 'ok' and data.get('version') == __version__:
                return
        except (OSError, ValueError, ssl.SSLError, http.client.HTTPException):
            pass
        finally:
            connection.close()
            if raw_socket is not None:
                raw_socket.close()
        time.sleep(.5)
    raise RuntimeError('HTTPS health check failed. Inspect: journalctl -u redis-farm -n 80 --no-pager')


def install(args):
    if os.geteuid() != 0:
        raise RuntimeError('Run with sudo.')
    if sys.version_info < (3, 10):
        raise RuntimeError('Python 3.10+ is required (Ubuntu 22.04/24.04).')
    os.umask(0o027)
    release_info = Path('/etc/os-release').read_text()
    if 'ID=ubuntu' not in release_info and 'ID="ubuntu"' not in release_info:
        raise RuntimeError('This installer targets Ubuntu. The application still supports local Linux/macOS/WSL.')
    if not Path('/run/systemd/system').exists():
        raise RuntimeError('systemd is required for this server installer.')
    original_config = CONF.read_bytes() if CONF.exists() else None
    if original_config is not None:
        raw = json.loads(original_config)
        choose_language(args, existing=raw)
        existing = parse_config(raw)
        supplied = []
        if args.host is not None:
            supplied.append(str(ipaddress.ip_address(args.host)) == existing.bind_host)
        if args.port is not None:
            supplied.append(args.port == existing.settings.port)
        if args.dns_server is not None:
            supplied.append(tuple(str(ipaddress.ip_address(v)) for v in args.dns_server) == existing.settings.dns_servers)
        if getattr(args, 'system_dns', False):
            supplied.append(existing.settings.dns_servers == ())
        if args.allow_cidr is not None:
            local = ipaddress.ip_address(existing.bind_host)
            own_cidr = str(local) + ('/128' if local.version == 6 else '/32')
            proposed = {str(ipaddress.ip_network(v, strict=False)) for v in args.allow_cidr + [own_cidr]}
            supplied.append(proposed == set(existing.settings.allowed_client_cidrs))
        if not all(supplied):
            raise RuntimeError('server.json already exists with different settings. Edit it directly; the installer will not overwrite it.')
        print('Preserving existing /etc/redis-farm/server.json.')
    else:
        template = json.loads((PROJECT / 'deploy/ubuntu/server.example.json').read_text())
        raw = first_server_config(args, template)
    raw = override_cli(raw, args.redis_cli)
    if getattr(args, 'language', None) is not None: raw['language'] = args.language
    if getattr(args, 'admin_user', None) is not None: raw['redis_admin_user'] = validate_admin_user(args.admin_user)
    if getattr(args, 'use_acl_user', None) is not None: raw['use_acl_user'] = args.use_acl_user == 'yes'
    if getattr(args, 'panel_auth', None) is not None:
        if args.panel_auth == 'none' and original_config is not None and json.loads(original_config).get('panel_auth','ldap') != 'none':
            confirm_no_auth(args, interactive=sys.stdin.isatty() and not args.non_interactive)
        raw['panel_auth'] = args.panel_auth
    config = parse_config(raw)
    cli_changed = (original_config is not None and
                   json.loads(original_config) != raw)
    if config.settings.data_dir != STATE:
        raise RuntimeError('This systemd installer uses /var/lib/redis-farm. Keep data_dir there or adapt the unit yourself.')
    current = ROOT / 'current'
    if current.exists() and not current.is_symlink():
        raise RuntimeError('/opt/redis-farm/current exists but is not a symlink; refusing to overwrite it.')
    previous = current.resolve(strict=True) if current.is_symlink() else None
    connector_plan = None
    if config.settings.panel_auth == 'ldap':
        connector_plan = prepare_connector(
        config.settings.ldap_connector,
        interactive=False if getattr(args, 'non_interactive', False) else None, language=config.settings.language)
    was_active = active()
    if was_active and previous is None:
        raise RuntimeError('An unmanaged redis-farm.service is active; refusing to replace it.')
    verify_bind(config, was_active)
    if not args.skip_apt:
        # Ubuntu may restart services after package operations via needrestart;
        # request list-only mode, do not intentionally restart existing services.
        env = {**os.environ, 'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l'}
        run('apt-get', 'update', env=env)
        run('apt-get', 'install', '-y', '--no-install-recommends',
            'python3', 'python3-venv', 'redis-tools', 'openssl', 'ca-certificates', 'curl', env=env)
    for exe in ('openssl', 'systemctl', 'runuser'):
        if not shutil.which(exe):
            raise RuntimeError(f'{exe} is required; install dependencies or omit --skip-apt.')
    selected_cli = select_compatible_cli(config, args)
    raw['cli_path'] = selected_cli
    config = parse_config(raw)
    cli_changed = original_config is not None and json.loads(original_config) != raw
    try:
        account = pwd.getpwnam('redis-farm')
        if account.pw_uid == 0:
            raise RuntimeError('redis-farm must not be a root account.')
    except KeyError:
        run('useradd', '--system', '--user-group', '--home-dir', STATE,
            '--shell', '/usr/sbin/nologin', 'redis-farm')
        account = pwd.getpwnam('redis-farm')
    gid = account.pw_gid
    ROOT.mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o755)
    os.chown(ROOT, 0, 0)
    (ROOT / 'releases').mkdir(exist_ok=True)
    (ROOT / 'releases').chmod(0o755)
    os.chown(ROOT / 'releases', 0, 0)
    STATE.mkdir(parents=True, exist_ok=True)
    os.chown(STATE, account.pw_uid, gid)
    STATE.chmod(0o700)
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(CONF_DIR, 0, gid)
    CONF_DIR.chmod(0o750)
    if not CONF.exists():
        atomic_text(CONF, json.dumps(raw, indent=2) + '\n', gid=gid)
    certificate(config, gid)
    if connector_plan is not None:
        save_connector(connector_plan, gid)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    release = ROOT / 'releases' / f'{__version__}-{stamp}-{uuid.uuid4().hex[:6]}'
    print(f'Copying distribution: {PROJECT} -> {release}', flush=True)
    copy_release(PROJECT, release)
    preserve_local_assets(previous, release)
    # Code belongs to root and is not writable by the web worker.
    for root, dirs, files in os.walk(release):
        Path(root).chmod(0o755)
        for name in files:
            path = Path(root) / name
            path.chmod(0o755 if path.suffix == '.sh' else 0o644)
    print('Installing isolated Python dependencies.', flush=True)
    run(sys.executable, '-m', 'venv', release / '.venv')
    py = release / '.venv/bin/python'
    # Dependency installation is isolated to this release, not another application or system Python.
    run(py, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
        '-r', release / ('requirements-server.txt' if config.settings.panel_auth == 'ldap' else 'requirements-server-noauth.txt'))
    # venv was created under umask 027; let the dedicated worker traverse/read it.
    for root, dirs, files in os.walk(release / '.venv'):
        Path(root).chmod(0o755)
        for name in files:
            path = Path(root) / name
            if not path.is_symlink():
                mode = path.stat().st_mode
                path.chmod(0o755 if mode & 0o111 else 0o644)
    run(py, '-c', 'import flask, gunicorn; print("Web dependencies import OK")')
    if config.settings.panel_auth == 'ldap':
        run(py, '-c', 'import ldap3')
    print('Checking local HTTP access gates (test identities, no network).', flush=True)
    run('runuser', '-u', 'redis-farm', '--', 'env', 'PYTHONDONTWRITEBYTECODE=1',
        py, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_web_access_020.py', cwd=release)
    print('Validating configuration as redis-farm (no Redis connections).', flush=True)
    candidate = CONF_DIR / ('.candidate-' + uuid.uuid4().hex + '.json')
    try:
        atomic_text(candidate, json.dumps(raw, indent=2) + '\n', gid=gid)
        run('runuser', '-u', 'redis-farm', '--', 'env', 'PYTHONDONTWRITEBYTECODE=1',
            'REDIS_FARM_CONFIG=' + str(candidate), py, '-m', 'redis_farm.server', '--check-ldap', cwd=release)
    finally:
        candidate.unlink(missing_ok=True)
    old_unit = SERVICE.read_bytes() if SERVICE.exists() else None
    old_unit_mode = SERVICE.stat().st_mode & 0o777 if SERVICE.exists() else 0o644
    try:
        if cli_changed:
            backup = CONF_DIR / ('server.json.before-update-' + stamp + '-' + uuid.uuid4().hex[:6])
            atomic_text(backup, original_config.decode('utf-8'), gid=gid)
            atomic_text(CONF, json.dumps(raw, indent=2) + '\n', gid=gid)
            print(f'Configuration updated; original saved to {backup}.', flush=True)
        print('Registering redis-farm.service and activating the release.', flush=True)
        atomic_text(SERVICE, (release / 'deploy/ubuntu/redis-farm.service').read_text(), mode=0o644)
        point_current(release)
        run('systemctl', 'daemon-reload')
        if args.no_start:
            print('Prepared and selected release; running service was not restarted. Start/restart it explicitly.')
        else:
            run('systemctl', 'restart', SERVICE_NAME)
            health(config)
            run('systemctl', 'enable', SERVICE_NAME)
    except Exception:
        print('Activation failed. Restoring the previous release link and unit.', file=sys.stderr)
        if cli_changed:
            atomic_text(CONF, original_config.decode('utf-8'), gid=gid)
        if previous:
            point_current(previous)
        else:
            current.unlink(missing_ok=True)
        if old_unit is not None:
            atomic_text(SERVICE, old_unit.decode(), mode=old_unit_mode)
        else:
            SERVICE.unlink(missing_ok=True)
        run('systemctl', 'daemon-reload')
        if was_active and previous:
            run('systemctl', 'restart', SERVICE_NAME)
        else:
            subprocess.run(['systemctl', 'stop', SERVICE_NAME], check=False)
        raise
    print('\nRedis Farm installed: ' + config.url)
    print('Config: ' + str(CONF))
    print('Catalog: ' + str(STATE / 'inventory.sqlite3'))
    print('No firewall rules were changed. No commands were sent to Redis servers.')
    print('Panel sign-in: ' + config.settings.panel_auth)
    print('Language: ' + config.settings.language)
    if config.settings.panel_auth == 'ldap':
        print('Connector: ' + str(config.settings.ldap_connector))
        print('The installer checked LDAP transport, not employee credentials or group membership.')
    print('Allowed client networks: ' + ', '.join(config.settings.allowed_client_cidrs))
    print('Compare this fingerprint before trusting the certificate in a browser:')
    run('openssl', 'x509', '-in', config.tls_cert, '-noout', '-fingerprint', '-sha256')
    if previous:
        print('Previous release retained: ' + str(previous))
    if args.no_start:
        print('Start manually: sudo systemctl enable --now redis-farm')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=['en', 'ru'], help='UI and installer language; first install defaults to en')
    parser.add_argument('--panel-auth', choices=['ldap', 'none'], help='Server panel sign-in; default ldap')
    parser.add_argument('--allow-no-auth', action='store_true', help='Explicitly accept server mode without sign-in')
    parser.add_argument('--admin-user', help='Named Redis ACL user, default admin; never a password')
    parser.add_argument('--use-acl-user', choices=['yes','no'], help='Initial Redis tab; both modes remain available')
    parser.add_argument('--host', help='Specific local interface IP; asked on first installation')
    parser.add_argument('--port', type=int, help='HTTPS port; default 8765 (first installation only)')
    dns_group = parser.add_mutually_exclusive_group()
    dns_group.add_argument('--dns-server', action='append', help='Internal DNS IP; repeatable; asked on first installation')
    dns_group.add_argument('--system-dns', action='store_true', help='Use only OS DNS; no custom resolver')
    parser.add_argument('--allow-cidr', action='append', help='Permitted client subnet; repeatable (first installation only)')
    parser.add_argument('--redis-cli', help='Absolute path to a compatible CLI; explicitly updates only cli_path, with backup')
    parser.add_argument('--skip-apt', action='store_true', help='Do not run apt-get; required packages must exist')
    parser.add_argument('--non-interactive', action='store_true', help='Fail instead of asking for missing first-install or LDAP values')
    parser.add_argument('--no-start', action='store_true', help='Prepare files without enabling/restarting the service')
    args = parser.parse_args()
    try:
        install(args)
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'INSTALLATION STOPPED: {short_error(exc)}', file=sys.stderr)
        print('The installation did NOT complete. No success is implied by installed packages or preserved TLS files.', file=sys.stderr)
        print('Resolve the reported error, then rerun. No unrelated services were changed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
