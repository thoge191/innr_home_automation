#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
# Run the service on THIS Mac for testing (Docker Desktop, linux/arm64).
#
#   deploy/deploy-local.sh <tag>      e.g. sha-1a2b3c4d (from deploy/build-push.sh --load)
#   deploy/deploy-local.sh down       stop and remove the local container (data volume is kept)
#
# Files live outside the repository in $INNR_LOCAL_DIR (default ~/innr-autooff-local, mode 700):
#   compose.yaml, config.toml, .env, secrets/innr_password (600, typed silently, never echoed)
# The Web UI is published on 127.0.0.1:8480 only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$ROOT/deploy/site.env" ]] && source "$ROOT/deploy/site.env"
DIR="${INNR_LOCAL_DIR:-$HOME/innr-autooff-local}"
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*"; }
OWNER="${GHCR_OWNER:-}"
[[ "$OWNER" =~ ^[a-z0-9]([a-z0-9-]{0,38})$ ]] || die "GHCR_OWNER is not set: add GHCR_OWNER=<your lower-case GitHub user or organisation> to deploy/site.env"
IMAGE="ghcr.io/${OWNER}/innr-autooff"

ARG="${1:-}"
[[ -n "$ARG" ]] || die "usage: $0 <sha-xxxxxxxx[-dirty]> | down"
command -v docker >/dev/null || die "docker not found"

if [[ "$ARG" == "down" ]]; then
  [[ -f "$DIR/compose.yaml" ]] || die "nothing deployed in $DIR"
  (cd "$DIR" && docker compose down)
  exit 0
fi
[[ "$ARG" =~ ^sha-[0-9a-f]{8}(-dirty)?$ ]] || die "tag must look like sha-xxxxxxxx"
docker image inspect "${IMAGE}:${ARG}" >/dev/null 2>&1 || die "image ${IMAGE}:${ARG} not found locally; run deploy/build-push.sh --load"

umask 077
mkdir -p "$DIR/secrets"
chmod 700 "$DIR" "$DIR/secrets"
cp "$ROOT/deploy/compose.yaml" "$DIR/compose.yaml"

if [[ ! -f "$DIR/config.toml" ]]; then
  if [[ "${BRIDGE_IP:-}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    sed "s/192\.0\.2\.10/$BRIDGE_IP/" "$ROOT/deploy/config.example.toml" > "$DIR/config.toml"
    log "created $DIR/config.toml from the example (bridge $BRIDGE_IP)"
  else
    cp "$ROOT/deploy/config.example.toml" "$DIR/config.toml"
    die "created $DIR/config.toml: set bridge.host to the LAN address of your INNR bridge (or add BRIDGE_IP to deploy/site.env), then run this script again"
  fi
fi

# the cloud client identity lives in the git-ignored deploy/client.toml (docs/CLOUD_CLIENT.md); merge it every run
if [[ -f "$ROOT/deploy/client.toml" ]]; then
  bash "$ROOT/deploy/merge-client-profile.sh" "$DIR/config.toml" "$ROOT/deploy/client.toml" > "$DIR/config.toml.new" \
    && mv "$DIR/config.toml.new" "$DIR/config.toml"
fi
grep -Eq '^app_id *= *".+"' "$DIR/config.toml" \
  || die "$DIR/config.toml has no [cloud.client] values: copy deploy/client.toml.example to deploy/client.toml, fill it in (docs/CLOUD_CLIENT.md), run again"
if [[ ! -s "$DIR/secrets/innr_password" ]]; then
  read -rsp "INNR account password (input hidden): " PW; echo
  [[ -n "$PW" ]] || die "empty password"
  printf '%s' "$PW" > "$DIR/secrets/innr_password"
  unset PW
  chmod 600 "$DIR/secrets/innr_password"
fi

EMAIL=""
[[ -f "$DIR/.env" ]] && EMAIL="$(grep -E '^INNR_EMAIL=' "$DIR/.env" | head -1 | cut -d= -f2- || true)"
if [[ -z "$EMAIL" ]]; then
  read -rp "INNR account e-mail: " EMAIL
fi
[[ "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] || die "invalid e-mail"
TMP="$(mktemp "$DIR/.env.XXXXXX")"
cat > "$TMP" <<EOF
GHCR_OWNER=${OWNER}
APP_TAG=${ARG}
INNR_EMAIL=${EMAIL}
WEB_BIND_IP=127.0.0.1
WEB_PORT=8480
LOG_LEVEL=INFO
EOF
mv "$TMP" "$DIR/.env"

cd "$DIR"
log "starting ${IMAGE}:${ARG}"
docker compose up -d --pull never

log "waiting for the container to become healthy (up to 4 minutes)"
STATUS=""
for _ in $(seq 1 48); do
  STATUS="$(docker inspect -f '{{.State.Health.Status}}' innr-autooff 2>/dev/null || echo missing)"
  [[ "$STATUS" == "healthy" ]] && break
  [[ "$STATUS" == "missing" ]] && break
  sleep 5
done
docker compose logs --tail 30 innr-autooff
if [[ "$STATUS" != "healthy" ]]; then
  die "container is '${STATUS}'. Check the log above (wrong INNR password? bridge unreachable?)"
fi
log "healthy."
if ! docker exec innr-autooff test -s /data/web_auth.json; then
  log "set the Web UI password now:"
  echo "   docker exec -it innr-autooff innr-autooff set-password"
fi
log "Web UI: http://127.0.0.1:8480"
