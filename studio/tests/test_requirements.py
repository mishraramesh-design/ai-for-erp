import glob, os, tempfile
import pytest
from studio.requirements import RequirementStore

XLSX = glob.glob("/root/.claude/uploads/*/*Fitment*.xlsx") + glob.glob(os.path.join(os.path.dirname(__file__), "*Fitment*.xlsx"))


@pytest.mark.skipif(not XLSX, reason="RFP matrix not present")
def test_real_matrix_counts():
    s = RequirementStore(os.path.join(tempfile.mkdtemp(), "r.json"))
    out = s.import_matrix(open(XLSX[0], "rb").read())
    t = s.stats()["totals"]
    assert out["total"] == t["total"] == 1544
    assert (t["fit"], t["partial"], t["no"], t["gap"]) == (915, 499, 130, 629)
    # re-import keeps progress
    s.mark([next(r.id for r in s.all() if r.is_gap)], "built", "CS1")
    s.import_matrix(open(XLSX[0], "rb").read())
    assert s.stats()["totals"]["built"] == 1


def test_free_text_import():
    s = RequirementStore(os.path.join(tempfile.mkdtemp(), "r.json"))
    r = s.import_text("1. The system shall record vendor site visits with date and officer.\n- Short\n2) Approval needed for POs above 5 lakh.", "Uploaded: fr.docx")
    assert r["added"] == 2 and all(x.id > 100000 for x in s.all())
    assert s.import_text("1. The system shall record vendor site visits with date and officer.", "Uploaded: fr.docx")["added"] == 0
