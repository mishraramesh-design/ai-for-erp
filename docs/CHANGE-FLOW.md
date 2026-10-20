# How changes reach ERPNext

1. **Dev site, live.** The Engine applies typed operations through the ERPNext REST API as the `Studio Agent` service user, with a snapshot, a read-back verification and undo. Fast loop; development site only.
2. **Git.** Each verified change set is also written into `customizations/csd_customizations/` (fixtures, DocType files, patches, tests, `studio_manifest.json`). The Studio commits to a branch and opens a pull request.
3. **Review and merge.** A person reviews the pull request. CI builds the ERPNext image with the app inside.
4. **Promotion.** The release site pulls the new image and runs `bench migrate`, which imports the fixtures. Production is never written by an agent.
5. **Drift.** The Studio compares the dev site with the app and flags edits made by hand.

Known risk: exporting workflows and app-owned DocTypes is tested against a real ERPNext in CI before it is relied on.
