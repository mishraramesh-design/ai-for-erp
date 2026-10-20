FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY studio/requirements.txt .
RUN pip install -r requirements.txt
COPY studio/studio ./studio
COPY studio/ui ./ui
# Compose and bridge-app sources travel inside the image so the provisioner can ship them to the target server
COPY erpnext ./erpnext-src
COPY customizations ./customizations-src
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/health',timeout=4)"
CMD ["uvicorn", "studio.app:app", "--host", "0.0.0.0", "--port", "8100"]
