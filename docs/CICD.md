# CI/CD

```
pull request -> test + build both images (no push)
main         -> test + build + push to Docker Hub  (<user>/ai-for-erp:studio-<sha>, :erp-<sha>, and -latest)
server       -> docker compose pull && up -d       (pull-based; GitHub never connects to your server)
```

## Secrets
GitHub repository secrets: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` (access token, read and write).
The OpenAI key is never in CI. It is entered in the Studio (Models) and stored encrypted on the server.

## Images (one Docker Hub repository, two tags)
- `studio-*`: the Studio service and UI. It also carries the ERPNext compose template and app sources the provisioner ships to the target server.
- `erp-*`: `frappe/erpnext:v15` + `studio_bridge` + `csd_customizations`. This is the release image: it changes whenever the customization app changes.

## Gates (current and planned)
Current: unit tests, dependency audit (advisory), Dockerfile builds on every pull request.
Planned: recipe tests against a real ERPNext container, golden-set prompt evaluation on prompt changes (spend-capped), image scan, secret scan.

## Rollback
Compose tags are immutable shas. To roll back, set `STUDIO_TAG` or the ERPNext image tag to the previous sha and run `docker compose up -d`.
