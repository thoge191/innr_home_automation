# Tasks: INNR Auto-Off Service

Legend: [x] done and verified, [~] done, live check outstanding, [ ] open.

## M0 Preparation
- [x] Investigation (protocol, plugs, cloud) done; notes and throw-away scripts kept outside the repository
- [x] Pinned, hash-checked runtime dependency (`paho-mqtt`), pinned dev dependencies, Python 3.13
- [x] Spike S1: the cloud stream reports changes made elsewhere (observed live on the Mac, then the NAS)
- [ ] Spike S2: do local and cloud `idx` share a counter? (only affects optional de-duplication)
- [ ] Spike S3: how often does the cloud broker drop the long-lived connection? (watch `source_status` in `signals.log`)
- [ ] Spike S4: refresh-token lifetime (tokens have been refreshed every 2 h without trouble so far)
- [ ] Spike S5: shared second INNR account (R5.9)

## M1 Core (R1, R2, R3, R10)
- [x] `protocol`: frame codec and notification decoding, verified with a captured real frame
- [x] `engine`: state machine, source merge, retries, settings change, restart policies, discovery sync
- [x] `store`: atomic 0600 JSON state, corrupt-file tolerance, persistent terminal id
- [x] `config`: TOML validation, secrets from files, seeds, placeholder guard for `bridge.host`
- [x] `discovery`: plugs by type, untrusted text cleaned
- [x] `logging_util`: secret registry and masking, signal / decision streams

## M2 Adapters and service
- [x] `local_feed`: read-only TLS, legacy cipher scoped to the bridge, certificate pin (tested against a TLS fake bridge)
- [x] `innr_api`: login (persistent terminal), refresh, re-login, login rate limit
- [x] `cloud_mqtt`: command format identical to the proven prototype; long-lived feed with reconnect; feed pauses during commands
- [x] `app`: single-owner asyncio loop, reconcile / discovery loop, health file, snapshot for the UI
- [x] End-to-end tests with fakes: auto-off, confirmation, monitor-only, new plug, restart resume, retry

## M2b Web UI (R11)
- [x] Route table with deny-by-default and permission-matrix test
- [x] scrypt password via CLI, hashed in-memory sessions, CSRF on every POST, lockout, strict CSP
- [x] Escaping of untrusted names, no secrets in pages, request limits, audit log
- [x] Request smuggling through unread bodies fixed
- [x] Live updates in place (no page reload; clicks are never lost); polling does not extend the session

## M3 Packaging
- [x] Dockerfile: pinned multi-arch base digest, test stage, non-root runtime, read-only
- [x] compose file: hardening, secret file, LAN-only port, invalid default tag
- [x] Test suite green inside linux/arm64 and linux/amd64 (emulated) images
- [x] Offline smoke test passes for both architectures
- [x] Run on the Mac against the real bridge and cloud (auto-off, manual ON/OFF, restart, delay change)

## M5 NAS
- [x] Key-only ssh to the NAS with an alias; non-default port
- [x] Deploy scripts with health check and automatic rollback; first deploy done, Web UI reachable on the LAN
- [x] Live use on the NAS (found the incident of M7)
- [ ] Deploy the M7 image to the NAS and watch the first runs in the new logs
- [~] `deploy-nas.sh fetch-logs` (the file-reading part is tested offline; the sudo path needs a live run)

## M7 Incident 2026-11-12 (stale cloud snapshot, acknowledged command to an OFF plug)
Evidence (NAS container log, UTC): 10:16:28 the bridge confirmed OFF; 10:16:45 the cloud list still said ON and started a new 20 min timer; the next timer sent three accepted OFF commands to a plug that was already OFF and logged `auto_off_failed`.
- [x] Spec: R1.5, R1.7, R1.10, R2.5 to R2.7, R2.11, R2.12, R12; design 4.2, 4.3, 10
- [x] Snapshot trust rules (grace window, two sightings, asymmetric)
- [x] Verification after an accepted-but-unconfirmed command (`assumed_off`)
- [x] Escalating retry schedule, no timer restart, attempt ids, command timeout
- [x] UI: OVERDUE display; settings behaviour for overdue plugs
- [x] Logging: separate signal and decision streams, files, `/log/*`, `fetch-logs`
- [x] Tests incl. the incident replayed at engine and service level; mutation-checked (removing the grace rule, the verification or the gap schedule makes tests fail)
- [x] Image published (both architectures)

## M8 Repository hygiene
- [x] Site-specific values out of the repository: investigation notes, prototype scripts, device and gateway ids, LAN addresses, NAS login name (git-ignored or replaced by placeholders; `deploy/site.env` holds the local values)
- [x] Runbook and README rewritten for a reader without access to the author's machine

## M10 Incident 2026-11-14 (container down every morning)
Evidence (NAS journal, local time): 07:29 to 07:31 the Docker daemon's calls to its runtime time out and the health probe of the service container times out; 07:31:19 `dockerd` panics (nil pointer dereference, SIGSEGV); systemd restarts it after 2 s (`Restart=always`); the unit has `KillMode=process` so the containers keep running; the new daemon (live-restore off) stops the service container and logs `ShouldRestart failed, container will not be restarted` because its policy was `unless-stopped`; a container with `always` is started again. The system log shows no kernel out-of-memory kill. The NAS has 2 GB RAM and was deep in swap, with an antivirus scan running for 31 hours.
- [x] Tooling to find this without guessing: `deploy-nas.sh diagnose` (metadata only, privacy-tested), `start`, opt-in `container-log <name>`
- [x] Restart policy `always` (compose, requirement R7.3, pinned by a test)
- [ ] Deploy the compose change to the NAS (`deploy-nas.sh deploy <current tag>` recreates the container with the new policy)
- [ ] Read the panic stack trace of the daemon (new section in `diagnose`) to confirm whether the health probe is involved; if so, relax or drop the container health check on this host
- [ ] Reduce the trigger on the NAS: antivirus scan schedule, memory use (owner decision)
- Optional, owner decision: `"live-restore": true` in the Container Manager daemon config keeps containers running through daemon crashes (system configuration; DSM updates may overwrite it)

## M9 Public release preparation
- [x] GPL-3.0-only: license headers, `NOTICE`, `SECURITY.md`, package metadata
- [x] Image owner is configuration (`GHCR_OWNER`), not code
- [x] Cloud client identity is configuration (R13): `[cloud.client]`, validation, refusal to start, merge script for the deploy scripts, `docs/CLOUD_CLIENT.md`, a test that fails if the vendor's values reappear in the repository
- [x] Clean public history (single commit on a new repository)

## M6 (deferred) Home Assistant
- [ ] Only if Home Assistant is adopted (R6.3)
