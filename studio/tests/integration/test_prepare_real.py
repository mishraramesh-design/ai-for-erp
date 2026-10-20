"""Prepare a FRESH real site (wizard not yet run). Needs STUDIO_IT_PREP_URL / _SITE / _ADMIN_PASSWORD."""
import os

import pytest

from studio.erp import HttpErp
from studio.prepare import prepare

URL = os.environ.get("STUDIO_IT_PREP_URL")


@pytest.mark.skipif(not URL, reason="STUDIO_IT_PREP_URL not set")
def test_prepare_fresh_site_then_use_service_user():
    cfg = {"url": URL, "site": os.environ["STUDIO_IT_PREP_SITE"], "admin_password": os.environ["STUDIO_IT_PREP_PASSWORD"],
           "company": "Prep Co"}
    steps = []
    keys = prepare(cfg, lambda k, s, n: steps.append((k, s)))
    assert ("verify", "ok") in steps
    erp = HttpErp(URL, keys["api_key"], keys["api_secret"], host_header=cfg["site"])
    assert erp.get_meta("Supplier")["fields"]
    prepare(cfg, lambda *a: None)  # idempotent re-run
