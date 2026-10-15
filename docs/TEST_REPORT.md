# Redis Farm 0.2.0 - verification report

Build date: 2026-10-14. All test data are synthetic. No production endpoint or employee credential was used.

## What ran

| Check | Result | Scope |
|---|---:|---|
| Python tests, isolated unittest modules | 303 executed, passed | Models, parsing, storage, credentials, discovery, configuration, installer helpers, manual names, localization, LDAP logic with doubles |
| Python tests needing Flask | 60 skipped | Missing Flask in the preparation environment; not claimed as successful HTTP tests |
| Chromium UI assertions | 51 passed | Actual Jinja templates, project CSS/JavaScript and the supplied original artwork, English and Russian |
| Distribution/artwork preflight | 6 passed | Copy with nested release destination, required files, image size/alpha, exact original pixels |
| Python 3.10 grammar | 52 source files passed | ast.parse feature_version=(3,10); not execution under Python 3.10 |
| JavaScript syntax | Passed | node --check on every bundled JavaScript file |
| Shell syntax | Passed | bash -n install.sh start.sh install-server.sh |

The Python runner reports 363 discovered cases: 303 executed and 60 skipped. Skipped cases are not included in the pass count. Each module runs in a separate process to prevent optional-dependency test doubles from leaking between modules. Detailed results and raw output are in [verification/tests](verification/tests/).

## UI coverage and screenshot method

Both languages exercised: configured ACL username/tab, three topology nodes, original logo and add-image dimensions, expand/collapse-all, Edit placement, empty initial add fields, default-tab selection without an automatic password modal, deliberate service selection, localized empty-password validation, full-size setup image, automatic scrolling after revealing installation details, click-outside About closing, image pickup/source hiding/restoration, and a 390px layout without horizontal page or dialog overflow.

The execution environment blocks browser navigation, including loopback URLs. Consequently, Playwright loaded the real rendered templates in memory, injected the actual stylesheet/script contents, embedded the original images, and supplied fake API responses through an exposed binding to a loopback fixture server. This verifies DOM behavior and layout, **not the production HTTP path or CSP enforcement**. Screenshots are of that real frontend with synthetic data, not a painted mockup. No demo dataset is loaded into a user's catalog on installation.

Assertions: [browser-checks.json](browser-checks.json). Reproduce with `python3 tools/render_demo.py` after installing Jinja2, Playwright and Chromium; set `CHROMIUM` when needed.

## Installer/configuration coverage

Tests cover neutral fresh defaults, English/Russian selection, old configuration preservation (including implicit Russian/LDAP defaults), explicit no-login confirmation, separate client paths, error reporting, source/destination recursion prevention, locally bundled/recovered artwork, connectors as literal data, no LDAP settings in local mode, and stable catalog identity across hostname aliases. CLI processes and LDAP transport in unit tests are doubles where documented in the tests; no authentication to a real directory is implied.

The installer invokes `tests/test_web_access_020.py` **after** installing the release's web dependencies on the target host. Those 15 tests use a real Flask test client with fake identity/Redis objects. They are part of the 60 skipped here, not additional passes. It subsequently checks candidate configuration and, only for LDAP mode, LDAP transport before activating the release. Transport preflight is not a test of a real employee's password or group membership.

## Boundaries

The preparation environment had Python 3.13, Jinja2, Pillow, dnspython, Node and Chromium, but did not have Flask, Werkzeug, Gunicorn, waitress, ldap3 or redis-cli. Package download attempts failed due to unavailable network/DNS. Therefore this release has **not** been fully exercised as Gunicorn -> Flask -> actual redis-cli/Redis/LDAP here.

Also not executed: APT or systemd installation on Ubuntu, a live HTTPS/reverse-proxy deployment, Firefox, macOS, WSL, real split-DNS resolution, or production upgrades. In particular, successful template rendering is not evidence that authentication, proxies or CSP work end to end. The server installer provides additional gates, but it does not replace testing in your deployment.

No claim is made that changing a requirement range produces a reproducible dependency build. For controlled deployments, use your approved Python constraints and package source.

## Packaging

The release contains all three supplied illustrations plus SVG/PNG/ICO favicons, both READMEs, relative screenshot paths and synthetic fixtures. The first two PNGs are byte-identical to the uploads; setup.webp retains the third PNG's RGBA pixels losslessly. A scan of source, examples and docs found no known organization-specific production names, IPs or account names from the previous configuration. Such a scan is not a general-purpose secret audit.

Archives are unpacked and compared file-for-file by SHA-256 with the prepared distribution. Checksums for the final archives are provided beside the downloads. Runtime catalogs, connectors, local.json, server.json, private keys, virtual environments and compiled caches are excluded.
