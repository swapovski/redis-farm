# Troubleshooting

## Installer stops at redis-cli

Check the executable on the **panel host**:

```bash
command -v redis-cli
/usr/bin/redis-cli --version
/usr/bin/redis-cli --help
```

A file with executable permission may still be the wrong version or fail due to a missing shared library. The installer reports stderr/exit status and missing capabilities. Redis-cli 6.x generally cannot provide the required JSON output. Choose another absolute path or explicitly select `install` when prompted to obtain the official standalone client in `/opt/redis-farm/bin`. This does not upgrade remote Redis or replace the system client.

Resume from the unpacked project directory:

```bash
sudo bash install-server.sh --skip-apt --redis-cli /opt/redis-farm/bin/redis-cli
```

A stopped installation is not a successful installation. Existing settings and a generated certificate do not prove that a service was activated.

## HTTPS or login page does not open

```bash
sudo systemctl status redis-farm.service --no-pager -l
sudo journalctl -u redis-farm.service -n 80 --no-pager
sudo ss -lntp '( sport = :8765 )'
```

Use HTTPS for the server listener. Its initial self-signed certificate is separate from LDAP or Redis TLS certificates. Verify the locally printed fingerprint or use a trusted organizational certificate. Do not disable browser certificate validation globally.

A `host_denied` / `origin` error behind Nginx is not necessarily LDAP failure. Configure `public_origin`, allowed_hosts and the exact proxy IP; preserve browser Host and do not rewrite Origin. Refresh `/login` after a configuration change instead of re-submitting an old form. No Origin/CSRF bypass is necessary.

## LDAP

A complete connector is used as-is. Check its absolute path in server.json; a relative connectors directory in your shell may be unrelated. Direct login binds under BASE_DN, then reads group membership. Wrong nesting direction does not grant access: the existing user group belongs **inside** the required access group. Read restrictions or a TLS chain/name failure can prevent login even with a correct password.

StartTLS/LDAPS requires a trusted CA and matching hostname. Plain LDAP is a separate explicitly accepted transport, not a way to disable certificate verification on TLS. A panel configured with `panel_auth: none` intentionally does not read the connector.

## Redis connection refused / missing internal addresses

Confirm where the remote instance listens, its firewall/route and what your chosen DNS returns. The browser's computer may have connectivity that the panel server does not. Internal DNS resolves names but cannot create a route or whitelist rule. No PTR lookups or automatic mapping from a public IP to a private one are performed.

## Missing images

Clean 0.2.0 packages contain logo.png, add.png, setup.webp and favicon files. Upgrades prefer local files recovered from the previous release/known backup directories. Invalid image files cause an error rather than copying an HTML download failure. Check the release's `redis_farm/static/` and reload the page after correction. Do not run old hotfixes against 0.2.0.

## Restart implications

A panel restart ends its web sessions and CLI processes and discards their in-memory passwords and output. It does not restart Redis/Sentinel. Export non-secret catalog data before planned maintenance. Finish active administrative commands before restarting the panel: ending a client process does not undo a command already accepted by Redis.
