#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
# Deploy to the Synology NAS over an ssh alias (key-only login, BatchMode; see docs/DEPLOY.md).
# Run by the owner: sudo on the NAS asks for the password.
#
#   deploy/deploy-nas.sh preflight            read-only checks (ssh, image in GHCR)
#   deploy/deploy-nas.sh login                one-time: store a READ-ONLY GHCR token for sudo docker on the NAS
#   deploy/deploy-nas.sh secret               (re)set the INNR account password on the NAS
#   deploy/deploy-nas.sh deploy <sha-xxxxxxxx> pull, start, health check, automatic rollback on failure
#   deploy/deploy-nas.sh rollback             back to the previous tag
#   deploy/deploy-nas.sh status | logs
#   deploy/deploy-nas.sh fetch-logs [hours]   copy signals.log, decisions.log, ops.log to ./logs/nas/ (one sudo prompt)
#   deploy/deploy-nas.sh start                start the container if it is not running (after Container Manager restarted)
#   deploy/deploy-nas.sh diagnose [hours]     metadata-only report about the Docker host to ./logs/nas/diag.txt (one sudo prompt)
#   deploy/deploy-nas.sh container-log <name> [hours]   error summary of ONE other container you name (opt-in) to ./logs/nas/<name>.txt
#
# Nothing secret is passed as an argument: the INNR password and the GHCR token are typed
# silently on the Mac and streamed over ssh stdin into 0600 files.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Site-specific values (not in the repository): deploy/site.env, see deploy/site.env.example
[[ -f "$ROOT/deploy/site.env" ]] && source "$ROOT/deploy/site.env"
SERVER="${SERVER:-myNAS}"
NAS_IP="${NAS_IP:-}"
BRIDGE_IP="${BRIDGE_IP:-}"
GHCR_OWNER="${GHCR_OWNER:-}"
IMAGE="ghcr.io/${GHCR_OWNER}/innr-autooff"
STAGE='$HOME/innr-autooff-staging'
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 "$SERVER")
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*"; }

[[ "$GHCR_OWNER" =~ ^[a-z0-9]([a-z0-9-]{0,38})$ ]] || die "GHCR_OWNER is not set: add GHCR_OWNER=<your lower-case GitHub user or organisation> to deploy/site.env"
[[ "$NAS_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "NAS_IP is not set: copy deploy/site.env.example to deploy/site.env and fill in the NAS address"

upload_stage() {
  "${SSH[@]}" "umask 077 && mkdir -p $STAGE"
  "${SSH[@]}" "umask 077 && cat > $STAGE/compose.yaml" < "$ROOT/deploy/compose.yaml"
  "${SSH[@]}" "umask 077 && cat > $STAGE/config.example.toml" < "$ROOT/deploy/config.example.toml"
  "${SSH[@]}" "umask 077 && cat > $STAGE/nas-remote.sh" < "$ROOT/deploy/nas-remote.sh"
  "${SSH[@]}" "umask 077 && cat > $STAGE/merge-client-profile.sh" < "$ROOT/deploy/merge-client-profile.sh"
  "${SSH[@]}" "rm -f $STAGE/client.toml"
  if [[ -f "$ROOT/deploy/client.toml" ]]; then
    "${SSH[@]}" "umask 077 && cat > $STAGE/client.toml" < "$ROOT/deploy/client.toml"
  fi
}

remote() {  # interactive: sudo prompts on the NAS terminal
  ssh -t -o ConnectTimeout=10 "$SERVER" "bash $STAGE/nas-remote.sh $*"
}

preflight() {
  log "ssh ${SERVER}"
  "${SSH[@]}" true || die "ssh ${SERVER} failed (key in agent? ssh-add --apple-use-keychain ~/.ssh/id_ed25519_nas_ssh)"
  "${SSH[@]}" 'test "$(uname -m)" = x86_64' || die "NAS is not x86_64"
  log "NAS reachable, x86_64"
  if [[ -n "${1:-}" ]]; then
    local out
    out="$(docker buildx imagetools inspect "${IMAGE}:$1" 2>&1)" || die "image ${IMAGE}:$1 not found in GHCR (run deploy/build-push.sh --push): $(head -c 200 <<<"$out")"
    grep -q 'linux/amd64' <<<"$out" || die "${IMAGE}:$1 has no linux/amd64 variant"
    log "image ${IMAGE}:$1 exists with linux/amd64"
  fi
}

CMD="${1:-}"
case "$CMD" in
  preflight)
    preflight "${2:-}"
    ;;
  login)
    preflight
    upload_stage
    echo "Create a classic GitHub token with ONLY the read:packages scope (90-day expiry)."
    read -rsp "GHCR read-only token (input hidden): " T; echo
    [[ -n "$T" ]] || die "empty token"
    printf '%s' "$T" | "${SSH[@]}" "umask 077 && cat > $STAGE/ghcr_token.incoming"
    unset T
    remote login "$GHCR_OWNER"
    ;;
  secret)
    preflight
    upload_stage
    read -rsp "INNR account password (input hidden): " PW; echo
    [[ -n "$PW" ]] || die "empty password"
    printf '%s' "$PW" | "${SSH[@]}" "umask 077 && cat > $STAGE/innr_password.incoming"
    unset PW
    remote secret
    ;;
  deploy)
    TAG="${2:-}"
    [[ "$TAG" =~ ^sha-[0-9a-f]{8}$ ]] || die "usage: $0 deploy sha-xxxxxxxx"
    preflight "$TAG"
    upload_stage
    EMAIL="${INNR_EMAIL:-}"
    if [[ -z "$EMAIL" ]] && ! "${SSH[@]}" "test -s $STAGE/email"; then
      read -rp "INNR account e-mail (first deploy only): " EMAIL
    fi
    if [[ -n "$EMAIL" ]]; then
      [[ "$EMAIL" =~ ^[^[:space:]@\'\"]+@[^[:space:]@\'\"]+$ ]] || die "invalid e-mail"
      printf '%s' "$EMAIL" | "${SSH[@]}" "umask 077 && cat > $STAGE/email"
    fi
    remote deploy "$TAG" "$NAS_IP" "$BRIDGE_IP" "$GHCR_OWNER"
    log "Web UI: http://${NAS_IP}:8480"
    ;;
  rollback|status|logs)
    preflight
    upload_stage
    remote "$CMD"
    ;;
  start)
    preflight
    upload_stage
    remote start
    ;;
  diagnose)
    HOURS="${2:-72}"
    [[ "$HOURS" =~ ^[0-9]{1,4}$ ]] || die "usage: $0 diagnose [hours]"
    preflight
    upload_stage
    remote diagnose "$HOURS"
    DEST="$ROOT/logs/nas"
    mkdir -p "$DEST"
    "${SSH[@]}" 'cat "$HOME/innr-diag-export/diag.txt"' > "$DEST/diag.txt" || die "could not copy the report"
    "${SSH[@]}" 'rm -rf "$HOME/innr-diag-export"'
    log "report copied to $DEST/diag.txt ($(wc -l < "$DEST/diag.txt") lines); the temporary copy on the NAS is removed"
    ;;
  container-log)
    NAME="${2:-}"; HOURS="${3:-24}"
    [[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || die "usage: $0 container-log <container-name> [hours]"
    [[ "$HOURS" =~ ^[0-9]{1,4}$ ]] || die "usage: $0 container-log <container-name> [hours]"
    preflight
    upload_stage
    remote container-log "$NAME" "$HOURS"
    DEST="$ROOT/logs/nas"
    mkdir -p "$DEST"
    "${SSH[@]}" 'cat "$HOME/innr-diag-export/container-log.txt"' > "$DEST/$NAME.txt" || die "could not copy the summary"
    "${SSH[@]}" 'rm -rf "$HOME/innr-diag-export"'
    log "summary copied to $DEST/$NAME.txt ($(wc -l < "$DEST/$NAME.txt") lines); the temporary copy on the NAS is removed"
    ;;
  fetch-logs)
    HOURS="${2:-24}"
    [[ "$HOURS" =~ ^[0-9]{1,4}$ ]] || die "usage: $0 fetch-logs [hours]"
    preflight
    upload_stage
    remote fetch-logs "$HOURS"
    DEST="$ROOT/logs/nas"
    mkdir -p "$DEST"
    for f in signals decisions ops; do
      "${SSH[@]}" "cat \$HOME/innr-logs-export/$f.log" > "$DEST/$f.log" || die "could not copy $f.log"
    done
    "${SSH[@]}" 'rm -rf "$HOME/innr-logs-export"'
    log "copied to $DEST (the temporary copy on the NAS is removed):"
    wc -l "$DEST"/signals.log "$DEST"/decisions.log "$DEST"/ops.log
    ;;
  *)
    sed -n '2,20p' "$0"
    exit 2
    ;;
esac
