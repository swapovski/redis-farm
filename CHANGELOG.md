# Changelog

## 0.2.0

GitHub publication includes a CI workflow for syntax checks, Python tests and bilingual demo screenshot generation, the catalog action buttons on one row, and LDAP sign-in screenshots in both READMEs.

Universal distribution: English default on new installations; Russian selectable in both installers and preserved on legacy upgrades. Localized templates, dynamic labels, password validation, panel errors and installer prompts. Redis CLI output and external package-manager diagnostics remain original.

Optional LDAP/no-login server mode with explicit acknowledgment for unauthenticated access; local mode remains independent of LDAP. Generic first-run IP/DNS/LDAP examples replace organization-specific defaults. Configurable named Redis ACL user, consistently shown in its tab and passed to CLI; default profile/password scopes unchanged.

Catalog expand/collapse-all control and Edit moved above catalog. All prior fixes through HF18-r1 integrated: FQDN/IP topology labels/dedup, access dialog spacing, addition confirmation, no automatic default password popup, session/CSRF recovery, nested LDAP groups, trusted proxy origin, illustration lift/follow/center/bob and installation-details reveal/scroll.

All three supplied illustrations and generated favicon are bundled. Installer preserves earlier custom artwork and can recover old filenames and backup releases; missing required artwork is no longer silently replaced with RF. Existing server config and complete LDAP connector are not rewritten without an explicit preference change. Improved CLI error diagnostics and an explicit standalone-client installation choice.

English and Russian READMEs contain actual UI screenshots with synthetic data and the bundled images. Tests record exact executed/skipped counts; no live infrastructure or systemd installation was performed in the build environment.
