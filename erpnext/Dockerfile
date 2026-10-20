# ERPNext v15 + studio_bridge (provisioning helpers) + csd_customizations (the app the Studio writes into).
# Build context: the repository root.   docker build -f erpnext/Dockerfile .
FROM frappe/erpnext:v15
USER frappe
COPY --chown=frappe:frappe erpnext/apps/studio_bridge /home/frappe/frappe-bench/apps/studio_bridge
COPY --chown=frappe:frappe customizations/csd_customizations /home/frappe/frappe-bench/apps/csd_customizations
# A .pth file makes each app importable without pip or network access at build time.
RUN SP="$(/home/frappe/frappe-bench/env/bin/python -c 'import site;print(site.getsitepackages()[0])')" \
 && echo /home/frappe/frappe-bench/apps/studio_bridge > "$SP/studio_bridge.pth" \
 && echo /home/frappe/frappe-bench/apps/csd_customizations > "$SP/csd_customizations.pth" \
 && printf '%s\n' frappe erpnext studio_bridge csd_customizations > /home/frappe/frappe-bench/sites/apps.txt
