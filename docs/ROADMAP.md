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
