from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cli_path: str = 'redis-cli'
    port: int = 8765
    command_timeout: float = 6.0
    max_output: int = 4 * 1024 * 1024
    max_processes: int = 8
    max_nodes: int = 96
    max_scan_ports: int = 512
    max_services: int = 512
    max_discovery_endpoints: int = 4096
    scan_redis_start: int = 6379
    scan_redis_end: int = 6478
    scan_sentinel_start: int = 26379
    scan_sentinel_end: int = 26478
    scan_timeout: float = 2.0
    scan_workers: int = 8
    session_idle: int = 3600
    session_lifetime: int = 12 * 3600
    max_terminals: int = 8
    terminal_buffer: int = 1024 * 1024
    terminal_idle: int = 3600
    refresh_seconds: int = 10
    dns_servers: tuple[str, ...] = ()
    dns_timeout: float = 3.0

    # Remote access is opt-in; run.py retains loopback-only HTTP.
    # Stable internal profile name remains 'admin'; this is the actual ACL user.
    redis_admin_user: str = 'admin'
    use_acl_user: bool = True
    language: str = 'en'
    panel_auth: str = 'ldap'  # meaningful only when server_mode=True
    server_mode: bool = False
    ldap_connector: Path = Path('/opt/redis-farm/connectors/connector_settings.py')
    allowed_hosts: tuple[str, ...] = ()
    allowed_client_cidrs: tuple[str, ...] = ()

    # Optional exact external HTTPS origin and exact trusted proxy addresses.
    public_origin: str = ''
    trusted_proxy_ips: tuple[str, ...] = ()
