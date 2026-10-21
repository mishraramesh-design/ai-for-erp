# Roadmap

- [x] Repository, CI, images, installer, docs
- [x] Engine core carried over from the pilot (typed specs, validators, compiler, applier, snapshot, ledger, verifier, requirement register)
- [x] Kernel: dependency-driven task graph, risk gates with approvals, bounded repair with escalation, run budget, per-run event log, undo (tested on a real ERPNext site with a scripted model)
- [x] Requirements import (Excel / Word / text) and Build from the UI; Runs screen with live graph and approvals
- [ ] Recipe library: notification, number card, dashboard chart, report, web form, SLA and more (9 ops exist today)
- [x] One-server compose (Studio + dev ERPNext) and Prepare site (wizard, service user) over ERPNext's own API; no SSH or Docker socket
- [ ] Agents, model router, decision gateway, prompt registry, Langflow adapter
- [x] Exporter: ledger to csd_customizations fixtures, studio_apply, manifest, traceability; zip download; GitHub pull request; drift check (round trip proven on a real ERPNext)
- [x] Studio shell: login, Models, ERPNext, Workbench preview (scripted)
- [ ] Studio Workbench wired to the kernel

## After the first demo
- [ ] Pluggable delivery (publisher setting): zip (done), GitHub (done), GitLab / Bitbucket / Azure DevOps merge requests,
      generic git push over HTTPS or SSH deploy key to any remote, write to a mounted folder or share. Kernel and export content unchanged.
- [ ] Recipe library: notification, report, number card, dashboard chart, print format, web form, SLA, scheduled job
- [ ] Decision gateway, prompt registry with golden-set gate, Langflow adapter
- [ ] Workbench wired to the kernel; reviewer scope control; stronger tester
- [ ] Cost and speed: smaller context, parallel steps; production hardening and operator guide

## Architecture: more deterministic strength, two modes per agent (decided 2026-10-21)
Lesson from the SRM pilot: most failures were the model guessing about the live site, not weak models. The code takes more of the job.

Done:
- [x] Site-facts digest in every plan prompt (custom fields, workflow, scripts, notifications, reports, web forms already on the doctypes in play)
- [x] Pre-flight health check: a plan on a doctype whose table is missing is refused; `create_doctype rebuild=true` repairs it
- [x] Cost guards: output-token cap per run, stop on the same error twice (split first), repairs stay bounded
- [x] Op library now 17: web form (login-only Supplier Portal pages) and DocType rebuild added

Next:
- [ ] Post-flight health sweep: after apply, read every touched doctype/workspace card and fail (and undo) on any broken one
- [ ] Reusable, tested building blocks (declaration form, approval workflow, expiry alert, portal page) filled in by the model instead of written from scratch
- [ ] Engine-written behavioural tests for server scripts and workflows (so items reach "verified", not just "built")
- [ ] Two modes per agent. Build mode: change the dev site, test, export. Guide mode: a documenter agent turns the SAME tested change set into a step-by-step manual (screen, field, value, how to check), and the steps are replayed on a clean test site before the person gets them; both modes share one plan so they cannot drift apart
- [ ] Documenter agent role and a Guide export per requirement (docx/markdown) in the traceability pack
