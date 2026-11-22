"""Make the theme CSS reachable under /assets even when sites/assets is a persistent volume
that was created before this app shipped static files (bench build is not run for new apps)."""
import os
import frappe


def ensure():
    src = os.path.join(os.path.dirname(__file__), "public")
    dst = frappe.get_site_path("..", "assets", "csd_customizations")
    dst = os.path.abspath(dst)
    try:
        if os.path.islink(dst):
            if os.path.realpath(dst) == os.path.realpath(src):
                return
            os.remove(dst)
        elif os.path.isdir(dst):
            import shutil
            shutil.rmtree(dst)
        os.symlink(src, dst)
    except OSError:
        frappe.log_error("csd theme asset link failed", "csd_customizations")
