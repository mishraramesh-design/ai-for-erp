---
keywords: erpnext, general
---
# ERPNext v15 core: how to judge fit

Fit categories used in this studio:
- Standard: ERPNext already does it with no setup beyond master data. Name the DocType or feature.
- Configuration: settings, naming series, roles and permissions, workflow, print format, custom fields, notifications, reports, dashboards. No code.
- Customization: needs a client script, server script or a new DocType (a custom app change).
- Not possible: needs an external system, hardware, native mobile app, or behaviour the framework does not allow. Give the workaround or integration.

Standard capabilities worth knowing (Buying and Stock):
- Buying cycle: Material Request -> Request for Quotation -> Supplier Quotation -> Purchase Order -> Purchase Receipt -> Purchase Invoice -> Payment Entry.
- Supplier: Supplier Group, Supplier Scorecard (periodic scoring with standings), Supplier Quotation comparison report, Blanket Order, hold/block on Supplier, Tax Withholding Category, Address and Contact links, Portal users.
- Approvals: Workflow (states, transitions by role, conditions), Authorization Rule (value based), Assignment Rule, ToDo, Notification (event or date based, email and system), Email Alert via Email Account.
- Documents: attachments on any document, Print Format (Jinja), Letter Head, Web Form for portal capture (needs Supplier role permissions), Data Import, Bulk Update.
- Reporting: Report Builder, Script Report, Query Report, Number Card, Dashboard Chart, Dashboard, Workspace.
- Stock: Warehouse, Item Price, Price List, Pricing Rule, Landed Cost Voucher, Quality Inspection, Batch and Serial No, Stock Reconciliation, reorder levels.
- Accounts: Bank Guarantee (a standard DocType with amount, validity, bank and linked reference), Payment Terms Template, Cost Center, Budget.
- Customisation points: Custom Field, Property Setter, Client Script, Server Script, Custom DocType (module Custom), Workflow, Notification.

Not standard in ERPNext (call these out): e-sign with a licensed provider, OTP over SMS without a gateway, bank or GST portal integrations without an app, native mobile apps, biometrics, advanced BI joins across many DocTypes, bulk outbound email without an Email Account.

Gathering style:
- One atomic requirement per statement: a single actor, action and outcome that can be tested.
- Always ask about: who does it, when it triggers, which data is captured, approvals, notifications, exceptions, volumes, reports needed, and how success is checked.
