#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
# Offline smoke test of a built image. No network: nothing reaches the INNR cloud or the bridge.
#   deploy/smoke-test.sh <image:tag> [platform]      e.g. ghcr.io/<owner>/innr-autooff:sha-1a2b3c4d linux/amd64
# Checks: non-root, read-only rootfs works, config validation, secret file readable as uid 10001,
# service starts without network and stays up, Web UI refuses login until a password is set,
# login + security headers work, health check reports unhealthy (no cloud) as designed.
set -euo pipefail

IMG="${1:?usage: $0 <image:tag> [platform]}"
PLATFORM="${2:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="innr-smoke-$$"
WORK="$(mktemp -d)"
PF=()
[[ -n "$PLATFORM" ]] && PF=(--platform "$PLATFORM")
fail() { echo "SMOKE FAIL: $*" >&2; docker logs "$NAME" 2>&1 | tail -20 >&2 || true; exit 1; }
ok() { echo "  ok  $*"; }
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/secrets"
sed 's/192\.0\.2\.10/192.0.2.99/' "$ROOT/deploy/config.example.toml" > "$WORK/config.nocli.toml"  # the example host is refused on purpose
# obviously fake client identity (the real one is operator configuration, never part of this project)
cat > "$WORK/client.toml" <<'PROFILE'
[cloud.client]
app_id = "smoke-test-app"
user_agent = "smoke-test/1.0"
package_name = "example.smoke.test"
app_version = "1.0.0"
web_version = "1.0.1"
build_number = 1
os_version = "1"
web_view_version = "1.0.2"
device_model = "example/model"
device_name = "Smoke Test"
origin = "https://127.0.0.1:1"
PROFILE
bash "$ROOT/deploy/merge-client-profile.sh" "$WORK/config.nocli.toml" "$WORK/client.toml" > "$WORK/config.toml"
( umask 077; printf 'dummy-not-a-real-password' > "$WORK/secrets/innr_password" )

RUN=(docker run ${PF[@]+"${PF[@]}"} --rm --network none --read-only --tmpfs /tmp:size=16m --cap-drop ALL
     --security-opt no-new-privileges:true -v "$WORK/config.toml:/config/config.toml:ro"
     -v "$WORK/secrets/innr_password:/run/secrets/innr_password:ro" -e INNR_EMAIL=smoke@example.invalid)

echo "smoke test: $IMG ${PLATFORM:-native}"
[[ "$("${RUN[@]}" --entrypoint id "$IMG" -u)" == "10001" ]] || fail "container does not run as uid 10001"
ok "runs as uid 10001"
ARCH="$("${RUN[@]}" --entrypoint uname "$IMG" -m)"
ok "architecture $ARCH"
"${RUN[@]}" "$IMG" check-config >/dev/null || fail "check-config failed"
ok "config.example.toml validates"
OUT="$(docker run ${PF[@]+"${PF[@]}"} --rm --network none -v "$ROOT/deploy/config.example.toml:/config/config.toml:ro" "$IMG" run 2>&1)" \
  && fail "the service started with the placeholder bridge.host"
grep -q "still the example value" <<<"$OUT" || fail "no clear message for the placeholder bridge.host: $OUT"
ok "the placeholder bridge.host from the template is refused with a clear message"
OUT="$(docker run ${PF[@]+"${PF[@]}"} --rm --network none -v "$WORK/config.nocli.toml:/config/config.toml:ro" "$IMG" run 2>&1)" \
  && fail "the service started without a cloud client profile"
grep -q "cloud.client is not configured" <<<"$OUT" || fail "no clear message for the missing cloud client profile: $OUT"
ok "a missing [cloud.client] profile is refused with a clear message"
"${RUN[@]}" --entrypoint python "$IMG" -c "open('/run/secrets/innr_password').read()" \
  || fail "secret file (mode 600 on the host) is not readable by uid 10001 in the container"
ok "secret file readable as uid 10001"
"${RUN[@]}" --entrypoint python "$IMG" -c "open('/opt/app/x','w')" 2>/dev/null && fail "root filesystem is writable"
ok "root filesystem is read-only"

docker run ${PF[@]+"${PF[@]}"} -d --name "$NAME" --network none --read-only --tmpfs /tmp:size=16m --cap-drop ALL \
  --security-opt no-new-privileges:true -v "$WORK/config.toml:/config/config.toml:ro" \
  -v "$WORK/secrets/innr_password:/run/secrets/innr_password:ro" -e INNR_EMAIL=smoke@example.invalid \
  "$IMG" >/dev/null
sleep 6
[[ "$(docker inspect -f '{{.State.Running}}' "$NAME")" == "true" ]] || fail "service exited without network"
ok "service keeps running without network (retries the cloud)"

PY='
import http.client, re, sys
def req(m, p, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", 8480, timeout=10)
    h = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
    if cookie: h["Cookie"] = "innr_session=" + cookie
    c.request(m, p, body=body, headers=h); r = c.getresponse(); d = r.read().decode()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, d
step = sys.argv[1]
if step == "unconfigured":
    st, h, d = req("GET", "/")
    assert st == 503 and "set-password" in d, (st, d[:200])
    assert req("GET", "/health")[0] == 503
    assert "default-src" in h["content-security-policy"]
elif step == "login":
    st, h, _ = req("POST", "/login", "password=wrong-password-x")
    assert st == 401, st
    st, h, _ = req("POST", "/login", "password=smoke+test+password+1")
    assert st == 303, st
    ck = re.search(r"innr_session=([^;]+)", h["set-cookie"]).group(1)
    st, h, d = req("GET", "/", cookie=ck)
    assert st == 200 and "INNR auto-off" in d and h["x-frame-options"] == "DENY", st
elif step == "logs":
    st, h, _ = req("POST", "/login", "password=smoke+test+password+1")
    ck = re.search(r"innr_session=([^;]+)", h["set-cookie"]).group(1)
    st, h, d = req("GET", "/log/signals", cookie=ck)
    assert st == 200 and h["content-type"].startswith("text/plain"), (st, h.get("content-type"))
    assert "kind=signal event=cloud_error" in d and "call=devices" in d, d[:300]
    assert "Temporary failure in name resolution" in d or "Network is unreachable" in d or "URLError" in d, d[:300]
    st, h, d = req("GET", "/log/decisions", cookie=ck)
    assert st == 200 and h["content-type"].startswith("text/plain")
    assert req("GET", "/log/other", cookie=ck)[0] == 404
print("ok")
'
docker exec "$NAME" python -c "$PY" unconfigured >/dev/null || fail "Web UI should refuse access until a password is set"
ok "Web UI refuses access until a password is set; health 503 without cloud"
docker exec "$NAME" python -c "from innr_autooff.web.auth import PasswordFile; PasswordFile(__import__('pathlib').Path('/data/web_auth.json')).set('smoke test password 1')" \
  || fail "cannot write the password file to /data"
docker exec "$NAME" python -c "$PY" login >/dev/null || fail "login / dashboard failed"
ok "password set in /data; login, dashboard and security headers work"
docker exec "$NAME" python -c "$PY" logs >/dev/null || fail "/log/signals or /log/decisions page wrong"
ok "signal log written (cloud error with its reason) and served at /log/signals"
docker exec "$NAME" test -s /data/logs/signals.log || fail "signals.log missing or empty in /data/logs"
docker exec "$NAME" test -e /data/logs/decisions.log || fail "decisions.log missing in /data/logs"
EXPORT="$(docker exec "$NAME" sh -c 'cat /data/logs/signals.log.3 /data/logs/signals.log.2 /data/logs/signals.log.1 /data/logs/signals.log 2>/dev/null; true')"
[[ "$EXPORT" == *"kind=signal event=cloud_error"* ]] || fail "fetch-logs export command returns no signal lines"
grep -q 'kind=decision' <<<"$(docker exec "$NAME" cat /data/logs/signals.log)" && fail "decision line in signals.log"
ok "log files readable the way 'deploy-nas.sh fetch-logs' reads them; signals.log holds no decisions"
docker exec "$NAME" innr-autooff healthcheck && fail "healthcheck must fail without cloud access"
ok "container healthcheck reports unhealthy without cloud (by design)"
echo "SMOKE OK ($ARCH)"
