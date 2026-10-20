# ai-for-erp: the ERPNext Studio

An AI development studio for ERPNext v15. You give it a server and your requirements; it installs a vanilla
ERPNext development site, then builds the requirements into it through typed, validated, verified, undoable changes.
Models only propose; a deterministic Engine decides, applies and verifies.

## Status

Pilot build in progress. See `docs/ROADMAP.md` for what is done and what is next.

## Repository layout

| Path | What it is |
|---|---|
| `studio/` | The Studio service (Python): kernel, Engine, recipes, agents, decision gateway, prompt registry, provisioner, API and UI |
| `erpnext/` | ERPNext image: `frappe/erpnext:v15` + `studio_bridge` + `csd_customizations` |
| `customizations/csd_customizations/` | The Frappe app the Studio writes verified changes into (via pull requests) |
| `deploy/` | Compose file and installer for the Studio on your server |
| `docs/` | Architecture, CI/CD and the change flow |
| `.github/workflows/ci.yml` | Test, build and push images to Docker Hub |

## Install on your server

1. In GitHub: Settings, Secrets and variables, Actions: `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with read and write). Already set.
2. Merge to `main`. CI pushes two tags to `<dockerhub-user>/ai-for-erp`: `studio-<sha>` and `erp-<sha>` (plus `studio-latest`, `erp-latest`).
3. On the server (Hostinger Docker Manager, or any Docker host): create a project from `deploy/pilot.compose.yml`.
   Change the three passwords near the top. One compose runs the Studio and a development ERPNext (db, redis, backend,
   websocket, worker, scheduler, nginx). The first start creates the site with developer mode on.
4. Open `http://<server>:18100`, sign in, add your model key under Models, then ERPNext, Prepare site.
   ERPNext itself is at `http://<server>:18080`.

Secrets never go into git or into chat. Model keys are entered in the Studio and stored encrypted in its data volume.

## Tests

```bash
cd studio && pip install -r requirements.txt pytest && python -m pytest -q
```
