# csd_customizations

The Frappe app the Studio writes its verified changes into. Each Studio run opens a pull request that changes
files under `csd_customizations/` (fixtures, DocType folders, patches, tests and `studio_manifest.json`).
After review and merge, CI bakes this app into the ERPNext image, so staging and production receive the changes
through a normal release, never through the Studio.
