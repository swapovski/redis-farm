#!/usr/bin/env python3
"""Configure the public origin and trusted proxy using existing server.json. No services are restarted."""
import argparse
from datetime import datetime, timezone
import importlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile

sys.dont_write_bytecode = True


def make_config(old, origin, proxy_ips, parse_config, parse_origin):
    existing = parse_config(old)
    parsed = parse_origin(origin)
    if parsed is None:
        raise ValueError('A public HTTPS origin is required')
    proxies = []
    for value in proxy_ips:
        address = ipaddress.ip_address(value)
        if address.is_unspecified or address.is_multicast:
            raise ValueError('Use exact unicast proxy IPs, not subnets or wildcard addresses')
        proxies.append(str(address))
    if not proxies:
        raise ValueError('At least one trusted proxy IP is required')
    new = dict(old)
    new['allowed_hosts'] = list(dict.fromkeys([*existing.settings.allowed_hosts, parsed[0]]))
    new['public_origin'] = parsed[2]
    new['trusted_proxy_ips'] = list(dict.fromkeys(proxies))
    parse_config(new)
    return new


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path('/opt/redis-farm/current'))
    p.add_argument('--config', type=Path, default=Path('/etc/redis-farm/server.json'))
    p.add_argument('--origin', required=True)
    p.add_argument('--proxy-ip', action='append', required=True,
                   help='Exact Nginx TCP source IP; repeat for multiple addresses')
    args = p.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.config.is_symlink():
            raise ValueError('Refusing to replace a symlink configuration')
        if not args.config.is_file():
            raise ValueError('Existing server.json is required')
        sys.path.insert(0, str(root))
        from redis_farm.server import parse_config
        from redis_farm.access import parse_public_origin
        before = args.config.read_bytes()
        raw = json.loads(before)
        new = make_config(raw, args.origin, args.proxy_ip, parse_config, parse_public_origin)
        if new == raw:
            print('Configuration already matches. No changes.')
            return 0
        st = args.config.stat()
        suffix = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(3)
        backup = args.config.with_name(args.config.name+'.before-proxy-'+suffix)
        fd = os.open(backup, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'wb') as f:
            f.write(before); f.flush(); os.fsync(f.fileno())
        fd, name = tempfile.mkstemp(prefix='.server-json-proxy-', dir=args.config.parent)
        try:
            with os.fdopen(fd,'wb') as f:
                f.write((json.dumps(new,ensure_ascii=False,indent=2)+'\n').encode())
                f.flush()
                if os.geteuid() == 0:
                    os.fchown(f.fileno(),st.st_uid,st.st_gid)
                os.fchmod(f.fileno(),stat.S_IMODE(st.st_mode))
                os.fsync(f.fileno())
            if args.config.read_bytes() != before:
                raise ValueError('Configuration changed while preparing update')
            os.replace(name,args.config)
        finally:
            if os.path.exists(name): os.unlink(name)
        print('Configured '+new['public_origin'])
        print('Trusted proxy peers: '+', '.join(new['trusted_proxy_ips']))
        print('Backup: '+str(backup))
        print('Listener, public_host, client CIDRs, TLS, LDAP and Redis settings preserved.')
        print('Set Nginx Host to $host and X-Real-IP to $remote_addr before restarting the panel.')
        return 0
    except (ValueError, OSError, ImportError, TypeError) as e:
        print('STOPPED: '+str(e),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
