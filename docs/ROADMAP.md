# Roadmap

- [x] Repository, CI, images, installer, docs
- [x] Engine core carried over from the pilot (typed specs, validators, compiler, applier, snapshot, ledger, verifier, requirement register)
- [x] Kernel: dependency-driven task graph, risk gates with approvals, bounded repair with escalation, run budget, per-run event log, undo (tested on a real ERPNext site with a scripted model)
- [x] Requirements import (Excel / Word / text) and Build from the UI; Runs screen with live graph and approvals
- [ ] Recipe library: notification, number card, dashboard chart, report, web form, SLA and more (9 ops exist today)
- [x] One-server compose (Studio + dev ERPNext) and Prepare site (wizard, service user) over ERPNext's own API; no SSH or Docker socket
- [ ] Agents, model router, decision gateway, prompt registry, Langflow adapter
- [ ] Exporter: customization app and pull request
- [x] Studio shell: login, Models, ERPNext, Workbench preview (scripted)
- [ ] Studio Workbench wired to the kernel
