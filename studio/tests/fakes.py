"""An in-memory ERPNext stand-in: enough surface for the studio's deterministic layers to be tested without a server."""
from __future__ import annotations

import copy
import itertools

from studio.erp import ErpError

STD_FIELDS = {
    "Sales Order": [("customer", "Customer", "Link", "Customer"), ("transaction_date", "Date", "Date", None),
                    ("delivery_date", "Delivery Date", "Date", None), ("grand_total", "Grand Total", "Currency", None),
                    ("po_no", "Customer's Purchase Order", "Data", None), ("items", "Items", "Table", "Sales Order Item")],
    "Purchase Order": [("supplier", "Supplier", "Link", "Supplier"), ("transaction_date", "Date", "Date", None),
                       ("grand_total", "Grand Total", "Currency", None), ("items", "Items", "Table", "Purchase Order Item")],
    "Customer": [("customer_name", "Customer Name", "Data", None), ("customer_group", "Customer Group", "Link", "Customer Group"),
                 ("credit_limit", "Credit Limit", "Currency", None)],
    "Supplier": [("supplier_name", "Supplier Name", "Data", None)],
    "Item": [("item_code", "Item Code", "Data", None), ("item_name", "Item Name", "Data", None)],
    "Sales Order Item": [("item_code", "Item Code", "Link", "Item"), ("qty", "Quantity", "Float", None), ("rate", "Rate", "Currency", None)],
    "Purchase Order Item": [("item_code", "Item Code", "Link", "Item"), ("qty", "Quantity", "Float", None)],
    "Customer Group": [("customer_group_name", "Customer Group Name", "Data", None)],
    "Selling Settings": [("cust_master_name", "Customer Naming By", "Select", "Customer Name\nNaming Series"),
                         ("allow_multiple_items", "Allow Item to be Added Multiple Times", "Check", None)],
}
CHILD = {"Sales Order Item", "Purchase Order Item"}
SINGLE = {"Selling Settings"}
SUBMITTABLE = {"Sales Order", "Purchase Order"}


class FakeErp:
    def __init__(self):
        self.docs: dict = {}  # (doctype, name) -> dict
        self.perms: list = []  # (doctype, role, permlevel, {ptype: 1})
        self.calls: list = []
        self._ids = itertools.count(1)
        self.fail_on: set = set()  # (doctype) inserts that should fail
        for dt in STD_FIELDS:
            self._put("DocType", dt, {"name": dt, "module": "Selling", "istable": int(dt in CHILD), "issingle": int(dt in SINGLE), "custom": 0})
        for r in ("System Manager", "Sales Manager", "Sales User", "Purchase Manager", "Accounts Manager", "Stock Manager"):
            self._put("Role", r, {"name": r})
        for m in ("Selling", "Buying", "Stock", "Accounts", "Custom"):
            self._put("Module Def", m, {"name": m})
        self._put("Selling Settings", "Selling Settings", {"name": "Selling Settings", "cust_master_name": "Customer Name"})

    def _put(self, dt, name, d):
        self.docs[(dt, name)] = d

    # -- protocol
    def list_docs(self, doctype, fields=None, filters=None, limit=0):
        rows = [copy.deepcopy(v) for (dt, _), v in self.docs.items() if dt == doctype]
        for f in filters or []:
            k, op, val = f[-3], f[-2], f[-1]
            rows = [r for r in rows if (r.get(k) == val if op == "=" else r.get(k) != val)]
        if fields:
            rows = [{k: r.get(k) for k in fields} for r in rows]
        return rows[:limit] if limit else rows

    def get_doc(self, doctype, name):
        d = self.docs.get((doctype, name))
        return copy.deepcopy(d) if d else None

    def insert(self, doctype, data):
        if doctype in self.fail_on:
            raise ErpError(417, f"simulated failure inserting {doctype}")
        d = copy.deepcopy(data)
        if doctype == "Custom Field":
            name = f"{d['dt']}-{d['fieldname']}"
        elif doctype == "Property Setter":
            name = f"{d['doc_type']}-{d.get('field_name') or 'main'}-{d['property']}-property"
        elif doctype == "Workflow":
            name = d["workflow_name"]
        elif doctype == "Workflow State":
            name = d["workflow_state_name"]
        elif doctype == "Workflow Action Master":
            name = d["workflow_action_name"]
        elif doctype == "Role":
            name = d["role_name"]
        else:
            name = d.get("name") or f"{doctype}-{next(self._ids)}"
        if (doctype, name) in self.docs:
            raise ErpError(409, f"{doctype} {name} already exists")
        d["name"] = name
        self._put(doctype, name, d)
        if doctype == "DocType":
            self.docs[("DocType", name)].update({"istable": int(bool(d.get("istable"))), "issingle": 0, "custom": 1})
        return copy.deepcopy(d)

    def update(self, doctype, name, data):
        if (doctype, name) not in self.docs:
            raise ErpError(404, "not found")
        self.docs[(doctype, name)].update(copy.deepcopy(data))
        return copy.deepcopy(self.docs[(doctype, name)])

    def delete(self, doctype, name):
        if (doctype, name) not in self.docs:
            raise ErpError(404, "not found")
        del self.docs[(doctype, name)]

    def set_value(self, doctype, name, field, value):
        self.docs.setdefault((doctype, name), {"name": name})[field] = value

    def call_method(self, method, data):
        self.calls.append((method, data))
        if method.endswith("permission_manager.add"):
            self.perms.append([data["parent"], data["role"], data.get("permlevel", 0), {}])
        elif method.endswith("permission_manager.update"):
            for p in self.perms:
                if p[:3] == [data["doctype"], data["role"], data.get("permlevel", 0)]:
                    p[3][data["ptype"]] = data["value"]
        elif method.endswith("permission_manager.get_permissions"):
            return [{"role": p[1], "permlevel": p[2], **p[3]} for p in self.perms if p[0] == data["doctype"]]
        elif method.endswith("permission_manager.remove"):
            self.perms = [p for p in self.perms if p[:3] != [data["doctype"], data["role"], data.get("permlevel", 0)]]
        return None

    def get_meta(self, doctype):
        fields = [{"fieldname": f[0], "label": f[1], "fieldtype": f[2], "options": f[3], "reqd": 0, "hidden": 0, "read_only": 0}
                  for f in STD_FIELDS.get(doctype, [])]
        # include applied custom fields and custom doctype fields
        for (dt, _), v in self.docs.items():
            if dt == "Custom Field" and v["dt"] == doctype:
                fields.append({k: v.get(k) for k in ("fieldname", "label", "fieldtype", "options")} | {"reqd": v.get("reqd", 0), "hidden": 0, "read_only": 0})
        if (("DocType", doctype) in self.docs) and doctype not in STD_FIELDS:
            for f in self.docs[("DocType", doctype)].get("fields", []):
                fields.append({k: f.get(k) for k in ("fieldname", "label", "fieldtype", "options")} | {"reqd": f.get("reqd", 0), "hidden": 0, "read_only": 0})
        d = self.docs.get(("DocType", doctype))
        if not d:
            return None
        return {"name": doctype, "module": d.get("module"), "issingle": bool(d.get("issingle")), "istable": bool(d.get("istable")),
                "custom": bool(d.get("custom")), "is_submittable": doctype in SUBMITTABLE or bool(d.get("is_submittable")), "fields": fields}
