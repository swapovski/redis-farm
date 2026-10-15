#!/usr/bin/env python3
"""Run the local Redis Farm workspace. No Flask development server or reloader."""
import argparse
import ipaddress
import math
import os
from pathlib import Path
import sys
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description='Redis Farm local web GUI (redis-cli transport only)')
    parser.add_argument('--language', choices=['en','ru'])
    parser.add_argument('--admin-user')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent / 'data')
    parser.add_argument('--redis-cli', default=os.environ.get('REDIS_FARM_CLI'))
    dns_group = parser.add_mutually_exclusive_group()
    dns_group.add_argument('--dns-server', action='append', metavar='IP',
                           help='Internal DNS server IP, repeatable. Default: local.json, otherwise OS DNS')
    dns_group.add_argument('--system-dns', action='store_true',
                           help='Use the OS resolver instead of the configured internal DNS')
    parser.add_argument('--dns-timeout', type=float, default=3.0)
    parser.add_argument('--scan-redis-start', type=int, default=6379)
    parser.add_argument('--scan-redis-end', type=int, default=6478)
    parser.add_argument('--scan-sentinel-start', type=int, default=26379)
    parser.add_argument('--scan-sentinel-end', type=int, default=26478)
    parser.add_argument('--scan-timeout', type=float, default=2.0)
    parser.add_argument('--scan-workers', type=int, default=8)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--check', action='store_true', help='Inspect local dependencies; do not contact Redis')
    parser.add_argument('--command-timeout', type=float, default=6.0)
    parser.add_argument('--session-idle', type=int, default=3600)
    parser.add_argument('--refresh', type=int, default=10)
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('Use Linux, macOS, or run the entire project inside WSL on Windows.')
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be in the range 1024..65535')
    if args.command_timeout < 1 or args.session_idle < 60 or args.refresh < 5:
        parser.error('Minimums: command timeout 1s, session idle 60s, refresh 5s')
    try:
        from redis_farm.install_wizard import load_local
        local = load_local(Path(__file__).resolve().parent / 'local.json')
        args.redis_cli = args.redis_cli or local.get('cli_path', 'redis-cli')
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        dns_servers = () if args.system_dns else tuple(str(ipaddress.ip_address(server))
                           for server in (args.dns_server if args.dns_server is not None else local.get('dns_servers', [])))
    except ValueError:
        parser.error('--dns-server requires an IP address, not a hostname or URL')
    if not math.isfinite(args.dns_timeout) or not 0.25 <= args.dns_timeout <= 30:
        parser.error('--dns-timeout must be 0.25..30 seconds')
    if not math.isfinite(args.scan_timeout) or not 0.25 <= args.scan_timeout <= 30:
        parser.error('--scan-timeout must be 0.25..30 seconds')
    if not 1 <= args.scan_workers <= 8:
        parser.error('--scan-workers must be 1..8')
    for a, b in ((args.scan_redis_start, args.scan_redis_end),
                 (args.scan_sentinel_start, args.scan_sentinel_end)):
        if not 1 <= a <= b <= 65535 or b - a + 1 > 512:
            parser.error('Scan range must contain 1..512 valid ports')
    os.umask(0o077)
    try:
        import flask
        import dns.resolver
        from waitress import serve
    except ImportError:
        print('Dependencies missing. Run: python3 -m pip install -r requirements.txt', file=sys.stderr)
        return 1
    from redis_farm.settings import Settings
    from redis_farm.cli import RedisCLI
    from redis_farm.install_wizard import validate_admin_user
    settings = Settings(data_dir=args.data_dir.expanduser().resolve(), cli_path=args.redis_cli,
                        language=args.language or local.get('language', 'ru' if local else 'en'),
                        redis_admin_user=validate_admin_user(args.admin_user or local.get('redis_admin_user','admin')),
                        use_acl_user=local.get('use_acl_user',True), panel_auth='none',
                        port=args.port, command_timeout=args.command_timeout,
                        session_idle=args.session_idle, refresh_seconds=args.refresh,
                        dns_servers=dns_servers, dns_timeout=args.dns_timeout,
                        scan_redis_start=args.scan_redis_start, scan_redis_end=args.scan_redis_end,
                        scan_sentinel_start=args.scan_sentinel_start, scan_sentinel_end=args.scan_sentinel_end,
                        scan_timeout=args.scan_timeout, scan_workers=args.scan_workers)
    cli = RedisCLI(settings)
    print(f'Redis Farm | {cli.version or "redis-cli unavailable"}')
    print(f'Client: {cli.path or "not found"}')
    print('DNS: ' + (', '.join(dns_servers) + ' (A records; system DNS fallback enabled)'
                     if dns_servers else 'system resolver'))
    if cli.error:
        print(cli.error, file=sys.stderr)
    if args.check:
        return 1 if cli.error else 0
    from redis_farm.app import create_app
    app = create_app(settings, cli=cli)
    url = f'http://127.0.0.1:{args.port}'
    print(f'Local panel: {url}')
    print(f'Catalog: {settings.data_dir}')
    print('Stop with Ctrl+C. Redis credentials and terminal output are memory-only.')
    if not args.no_browser:
        timer = threading.Timer(1.0, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        serve(app, host='127.0.0.1', port=args.port, threads=16, clear_untrusted_proxy_headers=True,
              max_request_body_size=2 * 1024 * 1024)
    except OSError as exc:
        print(f'Unable to bind localhost port {args.port}: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions['redis_farm']['shutdown']()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
