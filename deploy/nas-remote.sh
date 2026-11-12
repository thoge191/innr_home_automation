#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
# Runs ON the Synology NAS (uploaded by deploy-nas.sh to ~/innr-autooff-staging). Uses sudo for docker.
#   nas-remote.sh login | secret | deploy <tag> <nas-ip> | rollback | status | logs | fetch-logs [hours]
set -euo pipefail

APP_DIR="/volume1/docker/innr-autooff"
STAGE="$HOME/innr-autooff-staging"
CONTAINER="innr-autooff"
APP_UID=10001
HEALTH_WAIT=300
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*"; }

DOCKER="$(command -v docker || echo /usr/local/bin/docker)"
[[ -x "$DOCKER" ]] || die "docker not found (install Container Manager in DSM)"

sudo -v || die "sudo failed"

compose() {
  if sudo "$DOCKER" compose version >/dev/null 2>&1; then
    sudo "$DOCKER" compose --project-directory "$APP_DIR" -f "$APP_DIR/compose.yaml" --env-file "$APP_DIR/.env" "$@"
  elif command -v docker-compose >/dev/null; then
    sudo "$(command -v docker-compose)" --project-directory "$APP_DIR" -f "$APP_DIR/compose.yaml" --env-file "$APP_DIR/.env" "$@"
  else
    die "neither 'docker compose' nor 'docker-compose' found"
  fi
}

layout() {
  sudo mkdir -p "$APP_DIR/secrets"
  sudo chmod 755 "$APP_DIR"
  sudo chmod 700 "$APP_DIR/secrets"
  sudo chown "$APP_UID:$APP_UID" "$APP_DIR/secrets"
}

install_secret() {
  local f="$STAGE/innr_password.incoming"
  [[ -s "$f" ]] || die "no staged password"
  layout
  sudo install -m 400 -o "$APP_UID" -g "$APP_UID" "$f" "$APP_DIR/secrets/innr_password"
  rm -f "$f"
  log "INNR password installed (mode 400, owner uid $APP_UID)"
}

health_wait() {
  local s=""
  for _ in $(seq 1 $((HEALTH_WAIT / 5))); do
    s="$(sudo "$DOCKER" inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
    [[ "$s" == "healthy" || "$s" == "missing" ]] && break
    sleep 5
  done
  echo "$s"
}

write_env() {  # atomic, root-owned 600
  local tag="$1" ip="$2" email="$3" owner="$4" tmp
  tmp="$(mktemp)"
  printf 'GHCR_OWNER=%s\nAPP_TAG=%s\nINNR_EMAIL=%s\nWEB_BIND_IP=%s\nWEB_PORT=8480\nLOG_LEVEL=INFO\n' "$owner" "$tag" "$email" "$ip" > "$tmp"
  sudo install -m 600 -o root -g root "$tmp" "$APP_DIR/.env"
  rm -f "$tmp"
}

env_value() { sudo grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true; }

case "${1:-}" in
  login)
    OWNER="${2:-}"
    [[ "$OWNER" =~ ^[a-z0-9]([a-z0-9-]{0,38})$ ]] || die "bad registry owner"
    f="$STAGE/ghcr_token.incoming"
    [[ -s "$f" ]] || die "no staged token"
    sudo "$DOCKER" login ghcr.io -u "$OWNER" --password-stdin < "$f"
    rm -f "$f"
    sudo chmod 600 /root/.docker/config.json 2>/dev/null || true
    log "sudo docker is logged in to ghcr.io (read-only token)"
    ;;
  secret)
    install_secret
    if sudo "$DOCKER" inspect "$CONTAINER" >/dev/null 2>&1; then
      compose up -d --force-recreate
      log "container recreated with the new password"
    fi
    ;;
  deploy)
    TAG="${2:-}"; IP="${3:-}"; BRIDGE="${4:-}"; OWNER="${5:-}"
    [[ "$OWNER" =~ ^[a-z0-9]([a-z0-9-]{0,38})$ ]] || die "bad registry owner"
    [[ "$TAG" =~ ^sha-[0-9a-f]{8}$ ]] || die "bad tag"
    [[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "bad ip"
    layout
    [[ -s "$STAGE/innr_password.incoming" ]] && install_secret
    sudo test -s "$APP_DIR/secrets/innr_password" || die "INNR password missing on the NAS: run 'deploy/deploy-nas.sh secret' first"
    sudo grep -q 'ghcr.io' /root/.docker/config.json 2>/dev/null || die "not logged in to ghcr.io: run 'deploy/deploy-nas.sh login' first"
    EMAIL="$(cat "$STAGE/email" 2>/dev/null || true)"
    [[ -z "$EMAIL" ]] && EMAIL="$(env_value INNR_EMAIL)"
    [[ -n "$EMAIL" ]] || die "INNR e-mail unknown"
    if ! sudo test -f "$APP_DIR/config.toml"; then
      [[ "$BRIDGE" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "no config.toml on the NAS yet and BRIDGE_IP is not set: add BRIDGE_IP=<bridge address> to deploy/site.env"
      tmp="$(mktemp)"
      sed "s/192\.0\.2\.10/$BRIDGE/" "$STAGE/config.example.toml" > "$tmp"
      sudo install -m 644 "$tmp" "$APP_DIR/config.toml"
      rm -f "$tmp"
      log "created $APP_DIR/config.toml from the example (bridge $BRIDGE); plugs are configured in the Web UI"
    fi
    if [[ -s "$STAGE/client.toml" ]]; then
      tmp="$(mktemp)"
      bash "$STAGE/merge-client-profile.sh" "$APP_DIR/config.toml" "$STAGE/client.toml" > "$tmp" || { rm -f "$tmp"; die "could not merge the cloud client profile"; }
      sudo install -m 644 -o root -g root "$tmp" "$APP_DIR/config.toml"
      rm -f "$tmp" "$STAGE/client.toml"
      log "cloud client profile merged into $APP_DIR/config.toml"
    fi
    sudo grep -Eq '^app_id *= *".+"' "$APP_DIR/config.toml" \
      || die "config.toml on the NAS has no [cloud.client] values: create deploy/client.toml on the Mac (docs/CLOUD_CLIENT.md) and deploy again"
    PREV="$(env_value APP_TAG)"
    # keep everything the deploy changes, for rollback (tag AND compose file)
    if sudo test -f "$APP_DIR/.env"; then sudo cp -p "$APP_DIR/.env" "$APP_DIR/.env.prev"; fi
    if sudo test -f "$APP_DIR/compose.yaml"; then sudo cp -p "$APP_DIR/compose.yaml" "$APP_DIR/compose.yaml.prev"; fi
    sudo install -m 644 "$STAGE/compose.yaml" "$APP_DIR/compose.yaml"
    write_env "$TAG" "$IP" "$EMAIL" "$OWNER"
    log "deploying $TAG (previous: ${PREV:-none})"
    compose pull
    compose up -d
    S="$(health_wait)"
    compose logs --tail 40
    if [[ "$S" == "healthy" ]]; then
      echo "$(date -u +%FT%TZ) deploy $TAG ok (prev ${PREV:-none})" | sudo tee -a "$APP_DIR/deploy-history.log" >/dev/null
      log "healthy: $TAG"
      sudo "$DOCKER" exec "$CONTAINER" test -s /data/web_auth.json || \
        log "set the Web UI password: ssh -t myNAS sudo $DOCKER exec -it $CONTAINER innr-autooff set-password"
      exit 0
    fi
    echo "$(date -u +%FT%TZ) deploy $TAG FAILED status=$S" | sudo tee -a "$APP_DIR/deploy-history.log" >/dev/null
    if [[ -n "$PREV" && "$PREV" != "$TAG" ]] && sudo test -f "$APP_DIR/compose.yaml.prev"; then
      log "NOT healthy ($S): rolling back to $PREV"
      sudo cp -p "$APP_DIR/.env.prev" "$APP_DIR/.env"
      sudo cp -p "$APP_DIR/compose.yaml.prev" "$APP_DIR/compose.yaml"
      compose up -d
      echo "$(date -u +%FT%TZ) rollback to $PREV after failed $TAG" | sudo tee -a "$APP_DIR/deploy-history.log" >/dev/null
      die "deploy of $TAG failed; rolled back to $PREV"
    fi
    die "deploy of $TAG is not healthy ($S) and there is no previous version; container left running for diagnosis"
    ;;
  rollback)
    sudo test -f "$APP_DIR/.env.prev" || die "no previous deployment recorded"
    CUR="$(env_value APP_TAG)"
    sudo cp -p "$APP_DIR/.env.prev" "$APP_DIR/.env"
    sudo test -f "$APP_DIR/compose.yaml.prev" && sudo cp -p "$APP_DIR/compose.yaml.prev" "$APP_DIR/compose.yaml"
    compose up -d
    echo "$(date -u +%FT%TZ) manual rollback from $CUR to $(env_value APP_TAG); $CUR marked bad" | sudo tee -a "$APP_DIR/deploy-history.log" >/dev/null
    log "rolled back to $(env_value APP_TAG): $(health_wait)"
    ;;
  status)
    compose ps
    sudo "$DOCKER" inspect -f 'health={{.State.Health.Status}} started={{.State.StartedAt}}' "$CONTAINER" || true
    sudo tail -5 "$APP_DIR/deploy-history.log" 2>/dev/null || true
    ;;
  logs)
    compose logs --tail 200
    ;;
  fetch-logs)
    # Export the signal / decision files (all rotations, oldest first) and the operational log lines
    # into a private temp folder in the ssh user's home; deploy-nas.sh copies them to the Mac and removes it.
    H="${2:-24}"
    [[ "$H" =~ ^[0-9]{1,4}$ ]] || die "hours must be a number"
    OUT="$HOME/innr-logs-export"
    rm -rf "$OUT"
    mkdir -m 700 "$OUT"
    for k in signals decisions; do
      # the redirect runs as the ssh user, so the files belong to that user (no sudo needed to read them)
      sudo "$DOCKER" exec "$CONTAINER" sh -c "cat /data/logs/$k.log.3 /data/logs/$k.log.2 /data/logs/$k.log.1 /data/logs/$k.log 2>/dev/null; true" > "$OUT/$k.log"
    done
    sudo "$DOCKER" logs --since "${H}h" "$CONTAINER" 2>&1 | grep -v -e 'kind=signal' -e 'kind=decision' -e 'event=http ' > "$OUT/ops.log" || true
    log "exported to $OUT: $(wc -l < "$OUT/signals.log") signal, $(wc -l < "$OUT/decisions.log") decision, $(wc -l < "$OUT/ops.log") operational lines"
    ;;
  *)
    die "usage: nas-remote.sh login <owner>|secret|deploy <tag> <ip> <bridge-ip> <owner>|rollback|status|logs|fetch-logs [hours]"
    ;;
esac
