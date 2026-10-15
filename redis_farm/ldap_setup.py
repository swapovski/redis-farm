"""Interactive connector preparation without importing user-supplied Python.

Only missing settings are requested. This module uses the standard library so
it can run before the installer creates a virtual environment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import pprint
import stat
import sys
import uuid
from .ldap_config import (LDAPConfigurationError, WIZARD_DEFAULTS,
                          missing_fields, parse_ldap_config, read_connector)


@dataclass(frozen=True)
class ConnectorPlan:
    path: Path
    original: str = field(repr=False)
    text: str = field(repr=False)
    raw: dict = field(repr=False)
    existed: bool

    @property
    def changed(self):
        return not self.existed or self.text != self.original


def prepare_connector(path, *, interactive=None, ask=input, say=print, language="en"):
    """Read an existing complete connector without prompts or rewriting it.

    Empty/missing required values are requested one by one. For a new connector
    the TLS mode and trust file are also requested. Missing TLS keys in a legacy
    dictionary use documented secure defaults, never silent plain-text bind.
    """
    from .installer_locale import localize_callable
    ask, say = localize_callable(ask, language), localize_callable(say, language)
    path = Path(path)
    if not path.is_absolute():
        raise LDAPConfigurationError('Connector path must be absolute.')
    original, raw, node = read_connector(path)
    raw = dict(raw)
    missing = missing_fields(raw)
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if (missing or node is None) and not interactive:
        raise LDAPConfigurationError(
            'Missing LDAP settings: ' + ', '.join(missing) +
            '. Create ' + str(path) + ' from deploy/ubuntu/connector_settings.example.py '
            'or run the installer from an interactive terminal.')
    for name in missing:
        default = WIZARD_DEFAULTS[name]
        try:
            answer = ask(f'LDAP {name} [{default}]: ').strip()
        except EOFError:
            raise LDAPConfigurationError('LDAP setup interrupted; connector was not written.') from None
        raw[name] = answer or default
    if node is None:
        default_mode = 'ldaps' if raw['URL'].startswith('ldaps://') else 'starttls'
        # Fresh config: make TLS choices visible, not implicit.
        try:
            mode = (ask(f'LDAP transport [{default_mode}] (starttls/ldaps/plain): ').strip() or default_mode)
            if mode not in ('starttls', 'ldaps', 'plain'):
                raise LDAPConfigurationError('Choose starttls, ldaps or explicitly acknowledged plain.')
            raw['TLS_MODE'] = mode
            if mode == 'plain':
                say('WARNING: plain LDAP sends employee passwords without TLS on the panel-to-LDAP connection.')
                if ask('Type ALLOW_PLAINTEXT to confirm: ').strip() != 'ALLOW_PLAINTEXT':
                    raise LDAPConfigurationError('Plain LDAP was not explicitly confirmed.')
                raw['ALLOW_PLAINTEXT'] = True
            else:
                raw['VERIFY_CERTIFICATE'] = True
                ca_default = '/etc/ipa/ca.crt' if Path('/etc/ipa/ca.crt').is_file() else ''
                ca = ask(f'LDAP trusted CA PEM path [{ca_default or "system trust"}]: ').strip()
                raw['CA_CERT_FILE'] = ca or ca_default or None

        except EOFError:
            raise LDAPConfigurationError('LDAP setup interrupted; connector was not written.') from None
    # PEM existence/readability is checked after dependency installation as the
    # service account. Validate schema before any configuration changes.
    config = parse_ldap_config(raw, check_ca=False)
    if config.transport == 'plain':
        say('LDAP transport: plain (without TLS).')
    else:
        say(f'LDAP transport: {config.transport}; trusted CA: {config.ca_file or "system trust"}.')
    if config.transport == 'plain':
        say('WARNING: explicit plain LDAP is enabled. Employee passwords will not be encrypted on the LDAP connection.')
    if raw.get('BIND_DN') or raw.get('BIND_PASSWORD'):
        say('Direct employee bind: BIND_DN/BIND_PASSWORD are not used and will not be requested.')
    if not missing and node is not None:
        say(f'Using existing LDAP_CONFIG from {path}; no questions and no content changes.')
        return ConnectorPlan(path, original, original, raw, True)
    value = 'LDAP_CONFIG = ' + pprint.pformat(raw, sort_dicts=False, width=95) + '\n'
    if node is None:
        text = original.rstrip() + ('\n\n' if original.strip() else '') + (
            '# Redis Farm server LDAP connector. Direct employee bind; no application bind password.\n'
            '# Local mode ignores this file. Keep it outside the release directories.\n' + value)
    else:
        # Preserve all unrelated declarations verbatim, including other application data.
        lines = original.splitlines(keepends=True)
        text = ''.join(lines[:node.lineno-1]) + value + ''.join(lines[node.end_lineno:])
    return ConnectorPlan(path, original, text, raw, path.exists())


def secure_parent(path, gid):
    parent = Path(path).parent
    if parent.is_symlink():
        raise LDAPConfigurationError('Connector directory must not be a symlink.')
    parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if os.geteuid() == 0:
        os.chown(parent, 0, gid)
    parent.chmod(0o750)


def atomic_private(path, text, gid):
    path = Path(path)
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        # The file is private from its first byte, including with a lax umask.
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
            if os.geteuid() == 0:
                os.fchown(out.fileno(), 0, gid)
            os.fchmod(out.fileno(), 0o640)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_connector(plan, gid, *, say=print):
    secure_parent(plan.path, gid)
    # Reject changed or replaced files rather than overwriting a concurrent edit.
    if plan.path.is_symlink():
        raise LDAPConfigurationError('Connector file must not be a symlink.')
    if plan.existed and (not plan.path.is_file() or plan.path.read_text(encoding='utf-8-sig') != plan.original):
        raise LDAPConfigurationError('Connector changed during setup; rerun the installer.')
    if not plan.existed and plan.path.exists():
        raise LDAPConfigurationError('Connector appeared during setup; rerun the installer.')
    backup = None
    if plan.changed:
        if plan.existed:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = plan.path.with_name(plan.path.name + '.before-' + stamp + '-' + uuid.uuid4().hex[:8])
            atomic_private(backup, plan.original, gid)
        atomic_private(plan.path, plan.text, gid)
        say(f'LDAP connector saved: {plan.path}')
        if backup:
            say(f'Original connector backup: {backup}')
    else:
        if os.geteuid() == 0:
            os.chown(plan.path, 0, gid)
        plan.path.chmod(0o640)
    return backup
