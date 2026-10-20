# Verification — 0.2.0 connections-r1

This is the report for the repository overlay, not a claim of a live deployment.
The application version remains **0.2.0**. Source baseline: the issued
`redis-farm-0.2.0-release.tar.gz`; downstream Git history/configuration is not edited.

## Executed

- **339 Python tests executed successfully**, in isolated module processes.
- **72 tests explicitly skipped** because Flask is unavailable, including
  all 12 newly added real-Flask connection API tests. A skipped test is not passed.
- **115 Chromium UI assertions passed** in English and Russian with the real
  templates, CSS, JS and original illustrations; API responses are synthetic.
- Python syntax compile, all browser JS `node --check`, installer `bash -n`.
- EN/RU README relative links checked against the overlaid tree.
- Overlay archive readback, hash/mode verification, preservation checks for
  unrelated files (.git marker, configs, data, LICENSE and CI) on a temporary tree.

Logs: `verification/connections-r1/`. `test_all` records skip counts separately.

## New tests

`test_connection_profiles.py` verifies independent protocols, global Redis legacy
behavior, service/session isolation, username-bound secrets, endpoint precedence,
forget/logout, no credential leakage to JSON/exports, no authentication fallback,
Sentinel named/password auth, explicit extra Sentinel seeds, master selection,
mTLS arguments, redacted categorized errors, and protected-auto no-secret probing.

`test_connections_http.py` contains real Flask test-client tests for the new endpoint,
IDs, metadata vs secrets, CSRF, foreign Origin, global profile isolation, invalid
fields, endpoint overrides, protocol-specific forgetting and separate browser
sessions. These tests are included for dependency-complete CI but skipped here.

Two existing test modules were updated for explicit entry-type selection on protected
manual ports and retaining locked endpoints rather than pretending they are absent.
Wrong-password tests still assert that no open/global alternate profile is tried.

## Browser coverage

Catalog controls, EN/RU rendering, no automatic password dialog on switching to
default, separate Redis/Sentinel selectors, no global Redis option for Sentinel,
empty password input, independent TLS fields, compact checkboxes, desktop/mobile
overflow, real image natural dimensions, exact grip at leaf base minus 19 CSS px,
pointer travel outside source, sway and release restoration.

`tools/render_demo.py` renders in-memory because Chromium navigation to local URLs is
restricted in this environment. It injects the actual styles/scripts and proxies
fetch to local test responses. This does **not** test production HTTP headers/CSP,
a real authenticated API round-trip, TLS, Redis protocol behavior or Firefox.

## Not tested here

Flask, Gunicorn, ldap3 and real redis-cli/redis-server were unavailable. Attempts to
obtain dependencies failed due to network/DNS restrictions. No apt, systemd,
production DNS, LDAP, Redis, Sentinel or remote admin command was run.
The tests use local fixtures, not a replacement Redis transport in application code.
No claim of production-readiness, real-cluster integration or automatic CI success
is made. Run dependency-complete CI and test isolated Redis/Sentinel deployments
before publishing/redeploying this revision.

## Reproduce

```bash
python3 tools/test_all.py
python3 tools/render_demo.py
```

Use the project's requirements files for dependencies in an isolated environment.
Existing code and artwork licensing notices remain unchanged. The overlay does not
replace LICENSE, NOTICE.md or .github/workflows/ci.yml.
