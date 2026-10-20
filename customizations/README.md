# csd_customizations

The Frappe app the Studio writes its verified changes into. Each Studio run opens a pull request that changes
files under `csd_customizations/` (fixtures, DocType folders, patches, tests and `studio_manifest.json`).
After review and merge, CI bakes this app into the ERPNext image, so staging and production receive the changes
through a normal release, never through the Studio.

## Server Scripts need one bench setting
Frappe v15 runs Server Scripts only when `server_script_enabled` is `true` in `sites/common_site_config.json`
(`bench set-config -g server_script_enabled true`). If a release includes Server Scripts and the target bench has
it off, saving the affected documents fails with "Server Scripts are disabled". Enable it on staging and production
before releasing, and treat it as a security decision: Server Scripts are code that runs inside ERPNext.
