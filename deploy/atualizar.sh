#!/usr/bin/env bash
# Atualiza a plataforma na VPS: baixa o codigo novo, reconstroi e reinicia.
# Usado manualmente e tambem pelo pipeline de CD (GitHub Actions via SSH).
set -euo pipefail
cd "$(dirname "$0")/.."

git pull --ff-only
podman compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --remove-orphans
podman image prune -f >/dev/null
podman compose -f docker-compose.yml -f docker-compose.prod.yml ps
curl -fsS "http://127.0.0.1:${PORTA_GATEWAY:-8000}/saude" && echo " <- gateway no ar"
