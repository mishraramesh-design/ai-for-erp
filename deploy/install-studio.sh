#!/usr/bin/env bash
# Install or update the Studio on this server. Safe to re-run.
set -uo pipefail
cd "$(dirname "$0")"
COMPOSE=(docker compose -f studio.compose.yml --env-file .env)
die() { echo "FAILED: $*" >&2; exit 1; }
rand() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-24}"; }

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"

touch .env && chmod 600 .env
getv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
setv() { grep -vE "^$1=" .env > .env.tmp || true; echo "$1=$2" >> .env.tmp; mv .env.tmp .env; chmod 600 .env; }

if [ -z "$(getv DOCKERHUB_NAMESPACE)" ]; then
  read -r -p "Docker Hub username or namespace: " NS; [ -n "$NS" ] || die "namespace is required"; setv DOCKERHUB_NAMESPACE "$NS"
fi
[ -n "$(getv STUDIO_TAG)" ]  || setv STUDIO_TAG studio-latest
[ -n "$(getv STUDIO_PORT)" ] || setv STUDIO_PORT 18100
[ -n "$(getv LLM_BASE_URL)" ] || setv LLM_BASE_URL https://api.openai.com/v1
[ -n "$(getv STUDIO_SECRET_KEY)" ] || setv STUDIO_SECRET_KEY "$(rand 48)"
if [ -z "$(getv STUDIO_ADMIN_PASSWORD)" ]; then setv STUDIO_ADMIN_PASSWORD "$(rand 20)"; NEWPW=1; fi
if [ -z "$(getv LLM_API_KEY)" ]; then
  read -r -s -p "OpenAI API key (input hidden, press Enter to skip and add it later in the Studio): " KEY; echo
  [ -z "$KEY" ] || setv LLM_API_KEY "$KEY"
fi

echo "Pulling the Studio image..."
"${COMPOSE[@]}" pull || die "could not pull the image. If the Docker Hub repository is private, run: docker login"
"${COMPOSE[@]}" up -d || die "could not start the Studio"

PORT="$(getv STUDIO_PORT)"
for i in $(seq 1 40); do
  curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 2
done
curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || die "the Studio does not answer on port ${PORT}; run: docker compose -f studio.compose.yml logs --tail 80"

IP=$(curl -s -m 5 https://api.ipify.org || hostname -I | awk '{print $1}')
echo
echo "============================================================"
echo " Studio:    http://${IP}:${PORT}"
[ -z "${NEWPW:-}" ] || echo " Password:  $(getv STUDIO_ADMIN_PASSWORD)   (also in $(pwd)/.env)"
echo "============================================================"
