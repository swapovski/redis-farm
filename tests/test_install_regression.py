"""0.1.5 deployment regressions. Real files/process fixtures, mocked systemd/APT."""
import argparse
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_regression', PROJECT / 'deploy/ubuntu/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def make_source(path):
    path.mkdir(parents=True)
    for name in installer.RELEASE_ENTRIES:
        entry = path / name
        if name in ('redis_farm', 'deploy', 'docs', 'tests', 'tools'):
            entry.mkdir()
            (entry / 'sample.txt').write_text('distribution only')
        else:
            entry.write_text('distribution only')
    (path / 'deploy/ubuntu').mkdir()
    (path / 'deploy/ubuntu/redis-farm.service').write_text('[Service]\nType=simple\n')
    static = path / 'redis_farm/static'
    static.mkdir()
    for asset in ('logo.png','add.png','setup.webp','favicon.png','favicon.svg'):
        shutil.copy2(PROJECT / 'redis_farm/static' / asset, static / asset)
    return path


class ReleaseCopyRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = make_source(self.base / 'redis-farm')

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_from_opt_root_with_previous_recursive_release(self):
        old = self.source / 'releases/old/releases/old/releases/old'
        old.mkdir(parents=True)
        (old / 'do-not-copy.txt').write_text('old failed installation')
        (self.source / 'current').symlink_to(self.source / 'releases/old')
        (self.source / 'bin').mkdir()
        (self.source / 'bin/redis-cli').write_text('not in release')
        (self.source / 'server.json').write_text('local settings')
        result = installer.copy_release(self.source, self.source / 'releases/new')
        self.assertEqual(set(p.name for p in result.iterdir()), set(installer.RELEASE_ENTRIES))
        self.assertFalse((result / 'releases').exists())
        self.assertFalse((result / 'current').exists())
        self.assertFalse((result / 'bin').exists())
        self.assertTrue((old / 'do-not-copy.txt').exists())
        self.assertEqual((self.source / 'server.json').read_text(), 'local settings')

    def test_install_from_outside_destination(self):
        dest = self.base / 'releases'
        dest.mkdir()
        installer.copy_release(self.source, dest / 'new')
        self.assertTrue((dest / 'new/redis_farm/sample.txt').is_file())

    def test_install_from_existing_release_via_current_symlink(self):
        dest = self.base / 'releases'
        dest.mkdir()
        current = self.base / 'current'
        current.symlink_to(self.source)
        installer.copy_release(current, dest / 'new')
        self.assertTrue((dest / 'new/run.py').exists())

    def test_does_not_overwrite_existing_destination(self):
        dest = self.base / 'existing'
        dest.mkdir()
        (dest / 'catalog').write_text('preserve')
        with self.assertRaises(FileExistsError):
            installer.copy_release(self.source, dest)
        self.assertEqual((dest / 'catalog').read_text(), 'preserve')

    def test_reject_destination_inside_copied_package(self):
        with self.assertRaises(RuntimeError):
            installer.copy_release(self.source, self.source / 'redis_farm/release')
        self.assertFalse((self.source / 'redis_farm/release').exists())

    def test_reject_source_as_destination(self):
        with self.assertRaises(RuntimeError):
            installer.copy_release(self.source, self.source)
        self.assertTrue((self.source / 'run.py').exists())

    def test_reject_symlink_within_distribution(self):
        (self.source / 'docs/link').symlink_to(self.base)
        with self.assertRaises(RuntimeError):
            installer.copy_release(self.source, self.base / 'new')
        self.assertFalse((self.base / 'new').exists())

    def test_missing_distribution_file_fails_before_creation(self):
        (self.source / 'requirements-server.txt').unlink()
        with self.assertRaises(RuntimeError):
            installer.copy_release(self.source, self.base / 'new')
        self.assertFalse((self.base / 'new').exists())

    def test_ignores_runtime_data_and_bytecode(self):
        (self.source / 'data').mkdir()
        (self.source / 'data/inventory.sqlite3').write_text('preserve data')
        (self.source / '.venv').mkdir()
        (self.source / 'redis_farm/__pycache__').mkdir()
        (self.source / 'redis_farm/__pycache__/a.pyc').write_text('cache')
        dest = installer.copy_release(self.source, self.base / 'new')
        self.assertFalse((dest / 'data').exists())
        self.assertFalse((dest / '.venv').exists())
        self.assertFalse((dest / 'redis_farm/__pycache__').exists())

    def test_partial_failed_copy_removed_but_source_preserved(self):
        with patch.object(installer.shutil, 'copytree', side_effect=OSError('copy failure')):
            with self.assertRaises(OSError):
                installer.copy_release(self.source, self.base / 'new')
        self.assertFalse((self.base / 'new').exists())
        self.assertTrue((self.source / 'run.py').exists())


class ClientProbeTests(unittest.TestCase):
    def test_modern_fixture_inspection_uses_no_connection(self):
        fixture = PROJECT / 'tests/fake_cli.py'
        self.assertIn('7.2', installer.verify_cli(fixture))

    def test_redis_cli_6_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as td:
            client = Path(td) / 'redis-cli'
            client.write_text('#!/bin/sh\ncase "$1" in\n--version) echo "redis-cli 6.0.16";;\n--help) echo "--user --no-raw --raw";;\n*) exit 90;;\nesac\n')
            client.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'Missing flags: --json, -2, -e'):
                installer.verify_cli(client)

    def test_missing_binary_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'not an executable'):
            installer.verify_cli('/no-such-redis-farm-client')

    def test_no_prefix_option_matching(self):
        fake = subprocess.CompletedProcess([], 0, stdout='--json-extra --user --no-raw -2 -e', stderr='')
        with patch.object(installer.subprocess, 'run', return_value=fake):
            with self.assertRaisesRegex(RuntimeError, 'Missing flags: --json'):
                installer.verify_cli(PROJECT / 'tests/fake_cli.py')

    def test_secret_removed_from_local_probe_environment(self):
        fake = subprocess.CompletedProcess([], 0, stdout='--json --user --no-raw -2 -e', stderr='')
        with patch.dict(os.environ, {'REDISCLI_AUTH': 'example-secret'}):
            with patch.object(installer.subprocess, 'run', return_value=fake) as run:
                installer.verify_cli(PROJECT / 'tests/fake_cli.py')
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self.assertNotIn('REDISCLI_AUTH', call.kwargs['env'])
            self.assertIn(call.args[0][1], ['--version', '--help'])

    def test_explicit_cli_override_changes_no_other_field(self):
        original = {'bind_host': '192.0.2.10', 'dns_servers': ['192.0.2.53'],
                    'cli_path': '/usr/bin/redis-cli', 'data_dir': '/var/lib/redis-farm'}
        changed = installer.override_cli(original, '/opt/redis-farm/bin/redis-cli')
        self.assertEqual(original['cli_path'], '/usr/bin/redis-cli')
        self.assertEqual({k: v for k, v in changed.items() if k != 'cli_path'},
                         {k: v for k, v in original.items() if k != 'cli_path'})
        self.assertEqual(installer.override_cli(original, None), original)

    def test_cli_override_requires_absolute_path(self):
        with self.assertRaises(ValueError):
            installer.override_cli({}, 'redis-cli')

    def test_copy_error_message_is_bounded(self):
        exc = shutil.Error([('source', 'target', 'nested/' * 2000)] * 10)
        text = installer.short_error(exc)
        self.assertLess(len(text), 700)
        self.assertIn('10 entries', text)


class InstallFlowTests(unittest.TestCase):
    """Actual copy/config/symlink operations; no apt, systemd or user changes."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = make_source(self.root / 'opt/redis-farm')
        self.state = self.root / 'var/lib/redis-farm'
        self.state.mkdir(parents=True)
        (self.state / 'inventory.sqlite3').write_bytes(b'existing catalog')
        self.conf_dir = self.root / 'etc/redis-farm'
        self.conf_dir.mkdir(parents=True)
        self.conf = self.conf_dir / 'server.json'
        self.raw = {'bind_host': '127.0.0.1', 'port': 8765, 'data_dir': str(self.state),
                    'cli_path': '/usr/bin/redis-cli', 'dns_servers': ['192.0.2.53']}
        connector = self.project / 'connectors/connector_settings.py'
        connector.parent.mkdir()
        from redis_farm.ldap_config import WIZARD_DEFAULTS
        connector.write_text('LDAP_CONFIG = ' + repr(dict(WIZARD_DEFAULTS)))
        self.raw['ldap_connector'] = str(connector)
        self.original = json.dumps(self.raw, indent=4).encode()
        self.conf.write_bytes(self.original)
        self.unit = self.root / 'etc/redis-farm.service'
        self.stack = ExitStack()
        for key, value in {'PROJECT': self.project, 'ROOT': self.project, 'STATE': self.state,
                           'CONF_DIR': self.conf_dir, 'CONF': self.conf, 'SERVICE': self.unit}.items():
            self.stack.enter_context(patch.object(installer, key, value))
        read_text, exists = Path.read_text, Path.exists
        self.stack.enter_context(patch.object(Path, 'read_text', lambda p, *a, **k:
            'ID=ubuntu\nVERSION_ID="22.04"\n' if str(p) == '/etc/os-release' else read_text(p, *a, **k)))
        self.stack.enter_context(patch.object(Path, 'exists', lambda p:
            True if str(p) == '/run/systemd/system' else exists(p)))
        self.stack.enter_context(patch.object(installer.os, 'geteuid', return_value=0))
        self.stack.enter_context(patch.object(installer.os, 'chown'))
        self.stack.enter_context(patch.object(installer.os, 'umask'))
        self.stack.enter_context(patch.object(installer.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=12345, pw_gid=12345)))
        self.stack.enter_context(patch.object(installer.shutil, 'which', return_value='/fixture/executable'))
        self.stack.enter_context(patch.object(installer, 'active', return_value=False))
        self.stack.enter_context(patch.object(installer, 'verify_bind'))
        self.cert = self.stack.enter_context(patch.object(installer, 'certificate'))
        self.probe = self.stack.enter_context(patch.object(installer, 'verify_cli', return_value='redis-cli 8.x fixture'))
        self.commands = self.stack.enter_context(patch.object(installer, 'run'))
        self.stack.enter_context(patch.object(installer.subprocess, 'run'))
        self.health = self.stack.enter_context(patch.object(installer, 'health'))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.args = argparse.Namespace(host='127.0.0.1', port=8765, dns_server=['192.0.2.53'],
            allow_cidr=None, skip_apt=True, no_start=False, redis_cli='/opt/redis-farm/bin/redis-cli')

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_recovers_install_from_opt_root_preserving_data(self):
        (self.project / 'releases/broken/releases/broken').mkdir(parents=True)
        installer.install(self.args)
        release = (self.project / 'current').resolve(strict=True)
        self.assertFalse((release / 'releases').exists())
        self.assertEqual(json.loads(self.conf.read_bytes())['cli_path'], self.args.redis_cli)
        self.assertEqual((self.state / 'inventory.sqlite3').read_bytes(), b'existing catalog')
        self.assertTrue(self.unit.exists())
        backups = list(self.conf_dir.glob('server.json.before-update-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertFalse(list(self.conf_dir.glob('.candidate-*')))
        self.assertIn(('systemctl', 'daemon-reload'), [c.args for c in self.commands.call_args_list])
        self.assertIn(('systemctl', 'restart', 'redis-farm.service'), [c.args for c in self.commands.call_args_list])
        self.assertIn(('systemctl', 'enable', 'redis-farm.service'), [c.args for c in self.commands.call_args_list])
        self.health.assert_called_once()

    def test_health_failure_rolls_back_config_link_and_unit(self):
        self.health.side_effect = RuntimeError('health failure fixture')
        with self.assertRaisesRegex(RuntimeError, 'health failure'):
            installer.install(self.args)
        self.assertEqual(self.conf.read_bytes(), self.original)
        self.assertFalse((self.project / 'current').is_symlink())
        self.assertFalse(self.unit.exists())
        self.assertEqual((self.state / 'inventory.sqlite3').read_bytes(), b'existing catalog')

    def test_unsupported_client_fails_before_copy_config_or_certificate(self):
        self.probe.side_effect = RuntimeError('Incompatible client')
        with self.assertRaisesRegex(RuntimeError, 'Incompatible client'):
            installer.install(self.args)
        self.assertFalse((self.project / 'releases').exists())
        self.assertFalse(self.unit.exists())
        self.assertEqual(self.conf.read_bytes(), self.original)
        self.cert.assert_not_called()
        self.commands.assert_not_called()

    def test_ldap_preflight_failure_does_not_activate_or_restart(self):
        def fail(*args, **kwargs):
            if '--check-ldap' in args:
                raise RuntimeError('LDAP preflight fixture failed')
        self.commands.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'LDAP preflight'):
            installer.install(self.args)
        self.assertFalse((self.project / 'current').exists())
        self.assertFalse(self.unit.exists())
        self.assertFalse(any(c.args[:2] == ('systemctl', 'restart')
                             for c in self.commands.call_args_list))
        self.assertEqual(self.conf.read_bytes(), self.original)
        self.assertTrue((self.project / 'connectors/connector_settings.py').exists())

    def test_no_override_preserves_config_byte_for_byte(self):
        self.args.redis_cli = None
        installer.install(self.args)
        self.assertEqual(self.conf.read_bytes(), self.original)
        self.assertFalse(list(self.conf_dir.glob('server.json.before-update-*')))


if __name__ == '__main__':
    unittest.main()

class AssetMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'opt/redis-farm'
        self.backup = self.base / 'opt/redis-farm.bak'
        self.release = self.base / 'new'
        (self.release / 'redis_farm/static').mkdir(parents=True)
        for name in ('logo.png','add.png','setup.webp'):
            shutil.copy2(PROJECT / 'redis_farm/static' / name, self.release / 'redis_farm/static' / name)
        self.patcher = patch.object(installer, 'ROOT', self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _png(self, path, tail=b'x'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + tail)

    def test_recovers_new_names_from_backup_release_and_generates_missing_favicon(self):
        old = self.backup / 'releases/0.1.7-old/redis_farm/static'
        self._png(old / 'logo.png', b'logo')
        self._png(old / 'add.png', b'add')
        installer.preserve_local_assets(None, self.release)
        static = self.release / 'redis_farm/static'
        self.assertEqual((static / 'logo.png').read_bytes(), b'\x89PNG\r\n\x1a\nlogo')
        self.assertEqual((static / 'add.png').read_bytes(), b'\x89PNG\r\n\x1a\nadd')
        self.assertEqual((static / 'favicon.png').read_bytes(), (static / 'logo.png').read_bytes())
        self.assertIn(b'data:image/png;base64,', (static / 'favicon.svg').read_bytes())

    def test_recovers_legacy_case_insensitive_names(self):
        old = self.root / 'releases/legacy/redis_farm/static'
        self._png(old / 'Radish-sprite.png', b'legacy-logo')
        self._png(old / 'HD_Rescue_Radish.png', b'legacy-add')
        installer.preserve_local_assets(None, self.release)
        static = self.release / 'redis_farm/static'
        self.assertTrue((static / 'logo.png').read_bytes().endswith(b'legacy-logo'))
        self.assertTrue((static / 'add.png').read_bytes().endswith(b'legacy-add'))

    def test_existing_local_art_overrides_bundled_placeholder(self):
        target = self.release / 'redis_farm/static/logo.png'
        self._png(target, b'bundled')
        old_root = self.root / 'releases/old'
        self._png(old_root / 'redis_farm/static/logo.png', b'operator')
        installer.preserve_local_assets(old_root, self.release)
        self.assertTrue(target.read_bytes().endswith(b'operator'))
