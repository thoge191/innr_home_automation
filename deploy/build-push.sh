#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
# Build the INNR auto-off image (immutable sha-<8> tags, private GHCR package, no 'latest').
#
#   deploy/build-push.sh --load     build for THIS Mac (linux/arm64) and load it locally; no registry
#   deploy/build-push.sh --push     run the tests, build linux/amd64 + linux/arm64, push to GHCR (private)
#
# Tag = sha-<8-char git sha>. Tags are immutable; never 'latest'.
# --push refuses a dirty tree and refuses to overwrite an existing tag.
# --load on a dirty tree tags sha-<8>-dirty (local only, can never be pushed by this script).
set -euo pipefail

BUILDER="innr-builder"
BUILDKIT_IMAGE="moby/buildkit:v0.33.1"   # pinned build tool for the multi-arch builder
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*"; }

# Site-specific values (not in the repository): deploy/site.env, see deploy/site.env.example
[[ -f "$ROOT/deploy/site.env" ]] && source "$ROOT/deploy/site.env"
OWNER="${GHCR_OWNER:-}"
[[ "$OWNER" =~ ^[a-z0-9]([a-z0-9-]{0,38})$ ]] || die "GHCR_OWNER is not set: add GHCR_OWNER=<your lower-case GitHub user or organisation> to deploy/site.env"
IMAGE="ghcr.io/${OWNER}/innr-autooff"

MODE="${1:-}"
[[ "$MODE" == "--load" || "$MODE" == "--push" ]] || die "usage: $0 --load | --push"
command -v docker >/dev/null || die "docker not found"
docker buildx version >/dev/null 2>&1 || die "docker buildx not available"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git repository"
git rev-parse HEAD >/dev/null 2>&1 || die "no commit yet; commit first"

SHA="$(git rev-parse --short=8 HEAD)"
REV="$(git rev-parse HEAD)"
TAG="sha-${SHA}"
DIRTY=""
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then DIRTY="yes"; fi

if [[ "$MODE" == "--load" ]]; then
  [[ -n "$DIRTY" ]] && TAG="${TAG}-dirty" && log "working tree is dirty: local tag ${TAG}"
  log "running the test suite in a Linux container"
  docker buildx build --builder desktop-linux --target test --progress quiet . >/dev/null
  log "building ${IMAGE}:${TAG} for this machine and loading it"
  docker buildx build --builder desktop-linux --target runtime --load \
    --build-arg "REVISION=${REV}" -t "${IMAGE}:${TAG}" .
  echo "${TAG}"
  exit 0
fi

# ---------------------------------------------------------------- --push
[[ -z "$DIRTY" ]] || die "working tree has uncommitted changes; commit first (tags must match a commit)"
grep -q 'ghcr.io' "${HOME}/.docker/config.json" 2>/dev/null || die \
  "not logged in to ghcr.io. Run once: gh auth refresh -h github.com -s write:packages,read:packages && gh auth token | docker login ghcr.io -u ${OWNER} --password-stdin"

# The tag must not exist yet. "not found" means free; any other error aborts (never read a failure as 'free').
set +e
INSPECT_OUT="$(docker buildx imagetools inspect "${IMAGE}:${TAG}" 2>&1)"
INSPECT_RC=$?
set -e
if [[ $INSPECT_RC -eq 0 ]]; then
  die "${IMAGE}:${TAG} already exists; tags are immutable. Make a new commit."
elif ! grep -qiE 'not found|manifest unknown|404' <<<"$INSPECT_OUT"; then
  die "cannot check the registry (network/auth/rate limit?): $(head -c 300 <<<"$INSPECT_OUT")"
fi

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  log "creating multi-arch builder ${BUILDER} (${BUILDKIT_IMAGE})"
  docker buildx create --name "$BUILDER" --driver docker-container --driver-opt "image=${BUILDKIT_IMAGE}" >/dev/null
fi

log "running the test suite (linux/amd64 under emulation and linux/arm64)"
docker buildx build --builder "$BUILDER" --platform linux/amd64,linux/arm64 --target test --progress quiet . >/dev/null

log "building and pushing ${IMAGE}:${TAG} (linux/amd64, linux/arm64)"
docker buildx build --builder "$BUILDER" --platform linux/amd64,linux/arm64 --target runtime \
  --build-arg "REVISION=${REV}" -t "${IMAGE}:${TAG}" --push .

log "verifying the pushed manifest"
MANIFEST="$(docker buildx imagetools inspect "${IMAGE}:${TAG}")"
grep -q 'linux/amd64' <<<"$MANIFEST" || die "pushed image has no linux/amd64 variant"
grep -q 'linux/arm64' <<<"$MANIFEST" || die "pushed image has no linux/arm64 variant"
log "pushed. The GHCR package is private by default; keep it private."
echo "${TAG}"
