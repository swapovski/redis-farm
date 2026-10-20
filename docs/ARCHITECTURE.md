# Architecture

`redis_farm/app.py` is the Flask API/page factory. `run.py` starts the local Waitress application; `server.py` validates server.json and starts the server-only factory used by one Gunicorn worker. Threads handle concurrent requests. A bounded subprocess pool is the sole Redis transport.

`Settings` contains non-secret installation preferences. The stable endpoint auth profile `admin` selects the configured `redis_admin_user`; `default` never adds `--user`. Sentinel independently selects `none`, `default` or `user`; it never inherits global Redis credentials. `user` selects an endpoint-specific username. Redis and Sentinel profiles contain independent TLS and optional client-certificate/key paths. No programmatic Redis socket client or SSH library is used. HostResolver handles explicit internal DNS then OS fallback; literal addresses bypass DNS. CLI-native CONNECT/redirection is outside that resolver.

`Inventory` stores service/endpoint records in SQLite. A service represents Standalone, Replication, Sentinel or Redis Cluster; catalog groups are display organization, not a password scope. `HostLabels` and `topology_view` merge confirmed aliases for presentation without rewriting stored endpoint IDs or guessing PTR names.

`Sessions` stores opaque session IDs, LDAP identity, Redis passwords and endpoint overrides in one process. Jobs, snapshots and terminal buffers are scoped by session. An intentional credential change cancels that session's relevant outstanding reads/consoles. No cross-worker memory store is provided; increasing worker count is unsupported.

Server panel authentication is an explicit `panel_auth` choice. `ldap` installs the gate for pages/APIs and uses a directly bound employee identity with bounded nested-group traversal. `none` bypasses LDAP entirely but retains allowed-host/client, Origin and CSRF checks. Local mode is loopback-only and never loads the connector. Only trusted proxy peers may supply X-Real-IP. Forwarded Host/Origin is not globally trusted.

`locales/messages.json` supplies EN/RU text for Jinja and JavaScript. `errors.json` handles panel-owned error codes; raw Redis responses are not translated. `installer_text.json` and `locales/installer.json` cover installation prompts. The UI dictionary is embedded as non-executable JSON; no CDN, localization API or runtime language request is required.

Installation copies a fixed distribution allowlist into a new release, never the entire source tree with releases/current/bin. Existing settings and artwork have priority. Imports and HTTP gates with fake identities are checked in the candidate venv; transport preflight does not submit a Redis or employee LDAP command. Activation then switches `current` and restarts only the panel.

## Extending a widget

Use the shared CLI, parser and telemetry layers. Add a topic/widget to `commands.py` and the detail rendering/API rather than creating another unauthenticated subprocess path. Add EN/RU strings to the dictionaries, unit fixtures and browser checks. Never save captured passwords, full ACL hashes or command output in persistent logs. Document external commands' side effects rather than silently replaying them.

## Connection profiles (0.2.0 connections-r1)

`redis_connection` and `sentinel_connection` are allowlisted non-secret service defaults. Each endpoint has an effective profile. A service edit updates only endpoints inheriting the previous default, preserving explicit overrides and endpoint IDs. The new POST `/api/services/<id>/connection` is behind the same session, LDAP when enabled, Origin and CSRF gates as existing writes. No remote CONFIG/ACL command is issued by that endpoint.

`CredentialSet` retains legacy Redis global/default memory slots, and adds protocol-specific service/endpoint Secrets keyed by authentication identity. A changed username cannot consume the old user's password. Discovery passwords are scoped to selected seed ports and propagated only among same-protocol related nodes. Discovery results and inventory exports contain no secret. Global admin is never a fallback for Sentinel. Password-required and permission-denied states are not evidence of a dead TCP listener.

A protected manual port needs an explicit Redis/Sentinel type. An unauthenticated type probe cannot distinguish `NOAUTH` responses safely, so it does not try both credential sets. Normal node-range scanning declares a protocol per range. No PTR or public/private address guessing is introduced.
