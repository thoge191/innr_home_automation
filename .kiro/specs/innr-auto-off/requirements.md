# Requirements: INNR Auto-Off Service

## Introduction

A small always-on service that gives INNR SP 240 / OSP 240 smart plugs (connected through an INNR BG 220 bridge) a configurable per-plug **auto-off timer**, and exposes the plugs to **Home Assistant**.

The existing INNR bridge, INNR app and Alexa setup stay in place and keep working.

**Scope note:** Home Assistant is not required. The Service is therefore a **standalone product first**; the Home Assistant integration (R6.3) is optional and deferred until an operator wants it. Configuration is by file; status is by logs and a `status` command (R6.6).

Evidence base (from the investigation, whose notes and throw-away scripts are kept outside this repository):

- The bridge pushes plug state (`OnOff`, `Watts`, `Current`, ...) over a local TLS connection on TCP 10000. **Verified.**
- The bridge ignores `setDevAttrReq` commands sent on that local port. **Verified, negative result.**
- Commands sent through the INNR cloud (REST login + MQTT over websockets) switch the plugs, and the local feed shows the result about 200 ms later. **Verified.**
- Therefore: local state, cloud commands (hybrid).

## Glossary

| Term | Meaning |
|---|---|
| Plug | One configured INNR smart plug, identified by its `devId` |
| Bridge | INNR BG 220 at the configured LAN address |
| Local feed | TLS connection to bridge port 10000 (read-only) |
| Cloud | INNR/Arnoo platform: REST API and MQTT broker |
| Cloud feed | MQTT subscription to the cloud's state notifications |
| Service | The software described here |
| Delay | Time a plug may stay ON before the Service switches it OFF |
| Observation | A report "plug X is ON/OFF" from any state source |

Priorities: **Must** (required for first production release), **Should** (planned, may follow), **Could** (nice to have).

---

## R1. Plug state monitoring

**User story:** As the operator, I want the Service to know reliably whether each plug is ON or OFF, so that timers start and stop at the right moments.

Acceptance criteria:

1. R1.1 (Must) THE Service SHALL keep a TLS connection to the bridge's local feed and reconnect automatically with exponential backoff (1 s to 60 s) whenever it drops. The bridge is known to close the connection about every 30 s.
2. R1.2 (Must) THE Service SHALL derive a plug's ON/OFF state only from the `OnOff` attribute (`1` = ON, `0` = OFF). Power attributes (`Watts`, `Current`, `Energy`, `Voltage`) SHALL NOT be used to infer state.
3. R1.3 (Must) THE Service SHALL handle partial frames, several frames in one read, garbage before a frame, and invalid JSON without crashing, and SHALL log framing anomalies.
4. R1.4 (Should) THE Service SHALL additionally keep a cloud feed (MQTT subscription) as a second, independent state source, so that an ON event missed during a local reconnect is still seen.
5. R1.5 (Must) THE Service SHALL periodically (default every 60 s) read the plug states from the cloud REST device list (the *snapshot*), use it for discovery (R10), and treat a difference to the known state as a *claim* that is subject to R1.10. The initial state of a plug of which nothing is known yet (startup, new plug) is taken from the snapshot directly.
6. R1.6 (Must) WHEN the same event arrives from more than one source, THE Service SHALL treat it as one state transition (no double timer start, no double command).
7. R1.7 (Must) A snapshot SHALL NOT override a push Observation (local or cloud feed) that is newer than, or within 180 s before, the instant the snapshot request was sent. Field evidence (2026-11-12): the INNR cloud kept reporting a plug as ON for at least 17 s after the bridge had reported OFF, which restarted a timer for a plug that was off.
8. R1.8 (Must) WHEN no state source has been connected for more than 120 s, THE Service SHALL mark the plug state `unknown` and report degraded health (R6).
9. R1.9 (Should) THE Service SHALL record measured power attributes per plug for publication (R6.3), without using them for logic.
10. R1.10 (Must) A snapshot that disagrees with the known ON/OFF state SHALL change the state only when (a) no push Observation for that plug lies within the grace window of R1.7, and (b) two snapshots at least 30 s apart agree on the new value. A push Observation or an agreeing snapshot in between discards the pending claim. When an ON claim is accepted, the timer counts from the first snapshot that showed it. A snapshot SHALL never cancel a timer or mark a plug OFF on a single sighting. Ignored and pending snapshots SHALL be logged with the reason (R12).

## R2. Auto-off timer

**User story:** As the operator, I want each plug to switch itself OFF a configurable time after being switched ON.

Acceptance criteria:

1. R2.1 (Must) THE Service SHALL support, per plug, `enabled` and `delay_seconds` (1 s to 24 h).
2. R2.2 (Must) WHEN a plug changes from OFF (or unknown) to ON and auto-off is enabled, THE Service SHALL start that plug's timer.
3. R2.3 (Must) WHEN a plug that is already ON reports ON again, THE Service SHALL NOT restart or duplicate its timer.
4. R2.4 (Must) WHEN a plug reports OFF, THE Service SHALL cancel its timer.
5. R2.5 (Must) WHEN a timer expires and the plug's state is ON or `unknown`, THE Service SHALL send an OFF command and the plug is *overdue* until it is OFF. (Bias towards OFF: switching OFF an already-OFF plug is harmless.) An overdue plug SHALL NOT get a new timer; its remaining time is never shown as a countdown.
6. R2.6 (Must) THE Service SHALL consider an OFF command successful when an `OnOff = 0` push Observation for that plug (from any source) arrives. The 15 s confirmation window starts when the cloud accepts the command (acknowledgement), not when the command is queued; a command without any result after 60 s is a failed attempt.
7. R2.7 (Must) IF an attempt fails (no connection, login error, no acknowledgement, command timeout), THEN THE Service SHALL retry with escalating gaps counted from the failure: 5 s for attempts 1 to 3, 10 s for 4 to 6, 20 s for 7 to 9, 30 s for 10 to 12, 60 s for 13 to 15, then 120 s for every further attempt, until the plug is OFF. After the 3rd failed attempt the plug is flagged `off_failed` and an ERROR is logged. The attempt counter is reset only when the plug is OFF.
8. R2.8 (Must) Timers for different plugs SHALL be independent.
9. R2.9 (Should) WHEN `delay_seconds` or `enabled` is changed at runtime, THE Service SHALL recompute the running timer as `on_since + new delay` (an already-passed deadline triggers OFF immediately; `enabled = false` cancels the timer).
10. R2.10 (Must) THE Service SHALL send commands only to discovered devices of type `plug` (R10). It SHALL NEVER send a command to the bridge or to any other device type.
11. R2.11 (Must) IF the cloud accepted an OFF command but no OFF push arrives within 15 s, THEN THE Service SHALL NOT count a failure at once (the plug may already have been OFF, in which case it reports no change). It SHALL read a fresh snapshot (R1.5) and, if that snapshot was requested at least 10 s after the acceptance and shows OFF, treat the plug as OFF (`assumed_off`, logged as a decision with its reason). If the snapshot shows ON, or no snapshot arrives within 30 s, the attempt counts as failed (R2.7).
12. R2.12 (Must) A late result of an older attempt SHALL be ignored. Disabling auto-off for an overdue plug SHALL stop the retries; a shorter delay SHALL have no effect on an overdue plug.

## R3. Restart behaviour

**User story:** As the operator, I want a restart of the Service or host to neither leave a plug on forever nor switch it off surprisingly early.

Acceptance criteria:

1. R3.1 (Must) THE Service SHALL persist, per plug, the last known state, `on_since` (UTC) and runtime overrides (R2.9) to a state file, written atomically after every change.
2. R3.2 (Must) ON startup THE Service SHALL read each plug's current state from the cloud REST API before acting.
3. R3.3 (Must) WITH `restart_policy = "resume"` (default): IF a plug is ON now and the state file says it was ON, THEN THE Service SHALL keep the original `on_since` (an expired deadline triggers OFF immediately).
4. R3.4 (Must) IF a plug is ON now but the state file has no matching ON record, THEN THE Service SHALL start a full timer from the startup time.
5. R3.5 (Should) WITH `restart_policy = "restart"`, THE Service SHALL always give an ON plug a fresh full timer at startup.
6. R3.6 (Must) A missing, empty or corrupt state file SHALL NOT prevent startup; it is treated as "no record" (R3.4) and a warning is logged.
7. R3.7 (Must) Timers SHALL use a monotonic clock for waiting and UTC wall-clock time only for persistence, so that clock jumps do not shorten or lengthen timers.

## R4. Control

**User story:** As the operator, I want to be able to switch plugs from Home Assistant too.

Acceptance criteria:

1. R4.1 (Must) THE Service SHALL send OFF commands through the INNR cloud (MQTT `setDevAttrReq`), serialized (one command in flight at a time).
2. R4.2 (Should) THE Service SHALL accept ON/OFF commands for configured plugs from Home Assistant (R6) and execute them the same way. An ON command then starts the timer through the normal state path (R2.2), not by a shortcut.
3. R4.3 (Must) THE Service SHALL NOT send any command to the local bridge port (negative result, see Introduction), unless a future, explicitly verified local protocol is added.
4. R4.4 (Must) Commands SHALL be limited to `OnOff` and SHALL NOT change pairing, naming, rooms, firmware or any other bridge/cloud configuration.

## R5. Credentials and security

Acceptance criteria:

1. R5.1 (Must) Account credentials SHALL come from a Docker secret / file with mode `0600` and an environment variable for the e-mail; they SHALL NOT be in the image, in the config file, in version control, or in logs.
2. R5.2 (Must) THE Service SHALL keep tokens in memory only, refresh the access token on 401/403, and perform a fresh login if the refresh fails.
3. R5.3 (Must) THE Service SHALL create the cloud "terminal" identity once and persist its id, instead of registering a new terminal at every login.
4. R5.4 (Must) Secrets (account password, device passwords, tokens, MQTT password) SHALL be masked in every log line and diagnostic output.
5. R5.5 (Must) THE Service SHALL expose no inbound network port other than the Web UI port (R11), published only on the configured LAN address, and the container health check which needs no port.
6. R5.6 (Must) THE container SHALL run as a non-root user with a read-only root filesystem, all capabilities dropped, and one writable volume for state.
7. R5.7 (Should) THE Service SHALL support pinning the bridge certificate by SHA-256 fingerprint (trust on first use, then enforce), because the bridge uses a private certificate and a legacy cipher (`AES256-SHA256`).
8. R5.8 (Must) The relaxed TLS settings required by the bridge SHALL apply only to the bridge connection, never to cloud connections.
9. R5.9 (Should) THE documentation SHALL describe how to use a dedicated INNR account that the home is shared with, so that the main account password is not stored on the NAS (feasibility to be verified).

## R6. Observability and Home Assistant (optional)

Acceptance criteria:

1. R6.1 (Must) THE Service SHALL run fully standalone from a config file, without Home Assistant.
2. R6.2 (Must) THE Service SHALL log in a single-line, timestamped key=value format at INFO by default and DEBUG on request, as specified in R12.
3. R6.3 (Could, deferred until Home Assistant is adopted) WHEN an MQTT broker is configured, THE Service SHALL publish Home Assistant MQTT-discovery entities per plug: `switch` (power), `sensor` (power W, current A, energy kWh, voltage V), `switch` (auto-off enabled), `number` (auto-off minutes), `sensor` (seconds remaining), and a `binary_sensor` for connectivity of each state source. Availability SHALL follow an MQTT last-will topic.
4. R6.4 (Must) THE container SHALL have a health check that fails when no state source has been connected for 120 s, or the last cloud login failed.
5. R6.5 (Could, not implemented) THE Service SHALL expose Prometheus-style counters (events, reconnects, commands, failures). The signal and decision logs (R12) cover the diagnostic need.
6. R6.6 (Should) THE Service SHALL provide a `status` command (run with `docker exec`) that prints, per plug: name, state, auto-off enabled and delay, seconds remaining, last command result, plus the connection status of each state source and the cloud login.
7. R6.7 (Could, not implemented) THE Service SHALL re-read its configuration file on `SIGHUP`. Per-plug delays are changed in the Web UI (R11.3); other settings take effect after a container restart.

## R7. Packaging and deployment

Acceptance criteria:

1. R7.1 (Must) THE Service SHALL be delivered as one container image built from one Dockerfile for `linux/arm64` (Mac, first deployment) and `linux/amd64` (Synology NAS, later).
2. R7.2 (Must) Base image and Python dependencies SHALL be pinned. Image tags SHALL be immutable `sha-<8-char git sha>`; `latest` SHALL NOT be used.
3. R7.3 (Must) A compose file SHALL start the Service with `restart: always` (not `unless-stopped`: field evidence 2026-11-14, a crashed Docker daemon was restarted by systemd with the containers still running and the new daemon stopped them without restarting `unless-stopped` ones; the Service resumes cleanly after any restart, so always restarting is safe), the state volume, the config file, the secret and the health check.
4. R7.4 (Must) THE Service SHALL be run and verified on the Mac (arm64, Docker) before the NAS, including a run of the `linux/amd64` image under emulation on the Mac.
5. R7.5 (Must) THE Service SHALL work from a container network that has outbound access to the bridge's LAN address (no host network or privileged mode required).
6. R7.6 (Should) Deployment SHALL be reversible: previous image tag recorded, health checked after start, rollback to the previous tag documented.
7. R7.7 (Must) NO build or deployment step SHALL require changes to the INNR bridge, plug pairing or INNR account settings.

## R8. Non-functional requirements

1. R8.1 (Must) Typical time from expiry to confirmed OFF is under 10 s with a healthy cloud connection.
2. R8.2 (Must) Memory use under 100 MB; negligible CPU when idle.
3. R8.3 (Must) THE Service SHALL recover on its own, without a restart, from: bridge reboot, bridge TLS drops, cloud MQTT drops, short internet loss, cloud token expiry.
4. R8.4 (Must) During an internet/cloud outage the Service cannot send OFF commands; it SHALL log this clearly, keep retrying, and switch the plug OFF as soon as the cloud is reachable again.
5. R8.5 (Must) The core logic (state machine, source merge, restart policy) SHALL be testable without network access, using a fake clock.

## R10. Automatic device discovery

**User story:** As the operator, I want new INNR plugs I add in the INNR app to show up in the Service by themselves, without editing files.

Acceptance criteria:

1. R10.1 (Must) THE Service SHALL discover plugs from the INNR cloud device list: every device whose `type` is `plug` (the bridge, type `Mini_hub`, and any other type are ignored). The command route to a plug (gateway id) SHALL come from that plug's own `parentId`.
2. R10.2 (Must) Discovery SHALL run at startup and with every reconcile cycle (default 60 s). A plug that appears SHALL be monitored within one cycle, with no restart and no configuration change.
3. R10.3 (Must) A newly discovered plug SHALL start **monitor-only**: auto-off disabled, with the default delay pre-filled, until the operator enables it (Web UI or `[[plugs]]` seed). The default is configurable (`defaults.new_plug_auto_off`).
4. R10.4 (Should) WHEN a local notification names an unknown device id, THE Service SHALL trigger an immediate (rate-limited) discovery, so a brand-new plug that is switched on straight away is not missed.
5. R10.5 (Must) A plug that disappears from the cloud list SHALL stop being managed: its timer is cancelled, a warning is logged, its settings are kept for 30 days in case it comes back, and nothing crashes. A running timer for a plug that is no longer visible SHALL NOT keep a command pending forever.
6. R10.6 (Must) Plug names and models come from the INNR cloud and are untrusted text: they SHALL be escaped wherever displayed (R11) and in logs.
7. R10.7 (Must) Per-plug settings are keyed by device id. Precedence: Web UI / runtime override over `[[plugs]]` seed in `config.toml` over `[defaults]`.

---

## R11. Web UI (LAN administration page)

**User story:** As the operator, I want a simple web page to see all plugs and change their auto-off settings, without touching files or a terminal.

Acceptance criteria:

1. R11.1 (Must) THE Service SHALL serve a server-rendered web UI on a configurable port (default 8480), published on the NAS LAN address only. It SHALL work on a phone-sized screen and without JavaScript (with a page reload as fallback); with JavaScript the dashboard SHALL update in place, so that buttons stay clickable while it refreshes.
2. R11.2 (Must) THE dashboard SHALL refresh itself about every second (configurable 1 to 60 s) and SHALL show per plug: name, model, ON/OFF state (and `unknown`), power in W when known, auto-off enabled and delay, seconds remaining, the last command result and an `off_failed` warning; and globally: state of the local feed, cloud login, cloud feed and last reconcile age, plus version and uptime.
3. R11.3 (Must) THE operator SHALL be able to change per plug: auto-off enabled/disabled and delay (1 to 1440 minutes). Changes SHALL be validated server-side, persisted (R3.1) and applied as in R2.9.
4. R11.4 (Should) THE operator SHALL be able to switch a plug ON or OFF manually from the page (executed through the normal command path, R4.2).
5. R11.5 (Must) THE Web UI SHALL be protected by one administrator password, set with a CLI command inside the container (no web setup wizard). WHILE no password is set, all pages except a "not configured" notice SHALL be refused.
6. R11.6 (Must) THE Web UI SHALL follow the reduced security profile of design section 17: deny-by-default routes, per-session CSRF token on every state-changing request, hashed server-side sessions with idle and absolute timeouts, progressive lockout, one generic login failure message, strict security headers and a CSP without scripts, secure cookie attributes, audit log entries, secrets never shown, logged or accepted.
7. R11.7 (Must) THE Web UI SHALL NOT accept, display or store INNR account credentials, device passwords or tokens.
8. R11.8 (Must) Every route SHALL be listed in a route table with its access level, and a test SHALL fail when a route is added without a decision (permission-matrix test), and when any state-changing route accepts a request without a valid CSRF token.
9. R11.9 (Should) THE Web UI MAY run over TLS when a certificate and key file are configured; otherwise it runs over plain HTTP and logs a startup warning that the password crosses the LAN unencrypted.
10. R11.10 (Must) THE Web UI SHALL be switchable off in the configuration (`[web] enabled = false`) without affecting the rest of the Service.

---

## R12. Signal and decision logging

**User story:** As the operator, I want to reconstruct afterwards what the Service saw and what it decided, without guessing (the 2026-11-12 incident needed both).

Acceptance criteria:

1. R12.1 (Must) Every log line from the monitoring logic SHALL carry `kind=signal` (an input the Service received) or `kind=decision` (a choice the Service made, with its reason); operational lines (startup, shutdown, Web UI access) have no kind.
2. R12.2 (Must) Signals SHALL include: every notification from the local feed and the cloud feed (OnOff at INFO, power attributes at DEBUG) with source, plug and `idx`; every REST snapshot per plug with its OnOff value (DEBUG when it agrees with the known state, INFO when it differs); source connect/disconnect; every command result with duration and the error reason; every cloud error with a reason text; Web UI actions (settings, manual switch, login).
3. R12.3 (Must) Decisions SHALL include, each with the reason: timer started / cancelled / recomputed / expired, state changes with their source, snapshots ignored or pending (R1.10), OFF attempts sent with attempt number, retries scheduled with the gap, verification ordered, `assumed_off`, confirmation, `off_failed`, plug discovered / missing / returned, startup policy.
4. R12.4 (Must) Signals and decisions SHALL be written to separate rotating files (`/data/logs/signals.log`, `/data/logs/decisions.log`, 2 MB x 3 files each), in addition to the combined container log on stdout. The files include DEBUG entries regardless of the configured stdout level.
5. R12.5 (Must) Secrets SHALL never appear in any log (R5.4). Cloud error texts SHALL be scrubbed and length-capped.
6. R12.6 (Should) The Web UI SHALL offer the tail of both files (`/log/signals`, `/log/decisions`, plain text, login required), and a helper script SHALL copy both files from the NAS to the workspace without opening a shell on the NAS by hand.

---

## R13. Cloud client identity is configuration

**User story:** As the operator, I want to decide how my installation identifies itself to the INNR cloud, and I want the
public repository to contain no other party's application identifiers.

Acceptance criteria:

1. R13.1 (Must) THE values by which the Service identifies itself to the INNR cloud (application identifier, user agent, package name, versions, build number, device model and name, web origin) SHALL come from the `[cloud.client]` table of the configuration, not from source code. THE repository SHALL NOT contain them (a test enforces this).
2. R13.2 (Must) THE Service SHALL refuse to start with a clear message while the table is empty; a half-filled table, unknown keys, non-printable or over-long values (header injection) and a non-numeric build number SHALL be rejected with the offending keys named. `check-config` SHALL warn instead of failing.
3. R13.3 (Should) THE deploy scripts SHALL take the values from a git-ignored local file and merge them idempotently into the target `config.toml`, replacing only the `[cloud.client]` table and refusing a profile file that contains any other table.
4. R13.4 (Must) THE documentation SHALL explain each key, where such values come from (without shipping them), and that using a third-party client may conflict with INNR's terms of service (`docs/CLOUD_CLIENT.md`).

---

## R9. Out of scope

- Factory reset, re-pairing or reconfiguring the bridge or plugs.
- Local (non-cloud) control of the bridge: the investigation found no accepted command. It is parked, not abandoned (see design, "Future: local control").
- Other device types (bulbs, remotes), multiple bridges, multiple INNR accounts.
- Multi-user accounts, roles, 2FA/TOTP, internet exposure of the Web UI, and a reverse proxy in front of it (see R11 and design section 17 for the deliberately reduced security profile).
- Entering or changing INNR account credentials in the Web UI. They stay in the secret file.

---

## Decisions

| # | Topic | Decision |
|---|---|---|
| Q1 | Home Assistant / MQTT broker | Not required. The Service is standalone first; R6.3 (HA integration, needs a broker) is deferred until someone needs it. |
| Q2 | INNR account | The operator's own account first. A dedicated shared account is a later option (R5.9). |
| Q3 | Restart policy | `resume` (default). |
| Q4 | Delays | Set per plug in the Web UI. A plug that powers something that must never be cut off stays monitor-only (`enabled = false`, still monitored); a plug for a convenience appliance (for example a coffee machine) gets a delay such as 30 minutes. New plugs start monitor-only. |
| Q5 | Image distribution | Private registry package, immutable `sha-<8>` tags, read-only token on the target host. |
| Q6 | Target hardware | A Synology NAS with an Intel CPU (`linux/amd64`); Docker Desktop on a Mac (`linux/arm64`) for trying it out. |
| Q7 | Cloud dependency | Accepted: if the internet or the INNR cloud is down at expiry, the plug stays on until it is reachable again (R8.4). The Service is a convenience, not a safety device. |
