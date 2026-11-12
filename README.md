# home_automation: INNR auto-off

Gives INNR smart plugs (SP 240 / OSP 240 behind a BG 220 bridge) a per-plug **auto-off timer**, with a small LAN
Web UI. The INNR app, Alexa and the bridge stay exactly as they are: the service only watches the plugs and switches
them off after a configurable time.

> **Unofficial and independent.** Not affiliated with, endorsed by or supported by INNR, Leedarson or anyone else. It reads
> the bridge's local notification port and uses the INNR cloud API with *your own* account, for *your own* devices. That API
> is undocumented, may change or block this client, and using a third-party client may be against INNR's terms of service.
> **This is a convenience, not a safety device** (see "Safety, legal and privacy" below). Use at your own risk.

[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

## What it does

- **Per-plug auto-off.** A plug that is switched on (app, Alexa, button) is switched off after its delay. Switching it
  off yourself cancels the timer. Delay and on/off are set per plug in the Web UI.
- **Plugs are discovered.** Every plug of your INNR account appears by itself; a new plug starts *monitor-only* until you
  enable auto-off for it.
- **Restart-safe.** A plug that was on keeps its original deadline across restarts and reboots.
- **Live Web UI** on your LAN: state, power, countdown, settings, manual ON/OFF, connection status, log viewer.
- **Robust against flaky clouds.** The INNR cloud lags the bridge, so a single differing cloud answer is never believed
  (two matching checks and a quiet bridge are required). A failed switch-off is retried with growing gaps (5 s up to
  every 2 minutes) until the plug is off; it never restarts the timer. A command the cloud accepted for a plug that
  was already off is verified instead of repeated.
- **Explainable.** Every log line is either a *signal* (something received) or a *decision* (something chosen, with the
  reason), in separate files.

## How it works

```text
 INNR bridge (LAN, TLS :10000) --push OnOff--->+
 INNR cloud MQTT stream -------push OnOff----->+--> engine (timers, retries) --OFF--> INNR cloud (MQTT) --> bridge --> plug
 INNR cloud REST (every 60 s: discovery, ------+          |
   weak evidence, verification)                           +--> Web UI (LAN) and signal/decision logs
```

State comes from the bridge's local notification port (about 200 ms latency, read-only) with the cloud MQTT stream as a
second source and a 60 s cloud check as a safety net. Commands go through the INNR cloud, because the bridge ignores
local commands (verified during the investigation).

## Requirements

- An INNR BG 220 bridge with SP 240 / OSP 240 plugs and an INNR account (the same login as the INNR app).
- A Docker host on the same LAN as the bridge (tested: Docker Desktop on a Mac for trying it out; a Synology NAS with
  Container Manager for production). Images exist for `linux/amd64` and `linux/arm64`.
- Outbound internet access to the INNR cloud (`prod-<region>-api.arnoo.com:443`, `global-<region>-mqtt.arnoo.com:8443`).
- The cloud client identity values, supplied by you (the project ships none): [docs/CLOUD_CLIENT.md](docs/CLOUD_CLIENT.md).

## Quick start

Full runbook with checks and troubleshooting: **[docs/DEPLOY.md](docs/DEPLOY.md)**.

```bash
cp deploy/site.env.example deploy/site.env          # fill in GHCR_OWNER, NAS_IP, BRIDGE_IP, SERVER (git-ignored)
cp deploy/client.toml.example deploy/client.toml    # fill in the cloud client identity (git-ignored, docs/CLOUD_CLIENT.md)
deploy/build-push.sh --load                          # tests in a Linux container, then build for this machine
deploy/deploy-local.sh sha-<8>                       # start on this Mac (asks for the INNR e-mail and password)
docker exec -it innr-autooff innr-autooff set-password     # Web UI password, then open http://127.0.0.1:8480
```

For the NAS: `deploy/build-push.sh --push` (publishes to **your own** private package `ghcr.io/<GHCR_OWNER>/innr-autooff`), then
`deploy/deploy-nas.sh login | secret | deploy sha-<8>`. The project does not publish a ready-made image; you build your own.

## Configuration

`config.toml` (template: [deploy/config.example.toml](deploy/config.example.toml); no secrets in it):

| Key | Meaning |
|---|---|
| `bridge.host` | **Required.** LAN address of the INNR bridge. The placeholder from the example is refused at startup. |
| `bridge.cert_sha256` | Optional pin of the bridge certificate (otherwise trust on first use, then pinned). |
| `cloud.region`, `cloud.state_feed`, `cloud.reconcile_seconds` | `eu` or `us`; cloud MQTT as second state source; check interval. |
| `[cloud.client]` | **Required.** How the service presents itself to the INNR cloud. No values are shipped; see [docs/CLOUD_CLIENT.md](docs/CLOUD_CLIENT.md). |
| `defaults.delay_seconds`, `defaults.new_plug_auto_off` | Delay for plugs without a setting; new plugs start monitor-only (`false`). |
| `defaults.restart_policy` | `resume` keeps the original deadline across restarts; `restart` gives a fresh timer. |
| `[[plugs]]` | Optional seeds (id, name, delay, enabled). Normally you use the Web UI; its settings win. |
| `[web]` | Port, session timeouts, `refresh_seconds`, optional `tls_cert` / `tls_key`. |

Secrets are never in the configuration: the INNR password is a Docker secret file (`INNR_PASSWORD_FILE`), the e-mail
comes from `INNR_EMAIL`, the Web UI password is set with `innr-autooff set-password` and stored as a scrypt hash.

## Web UI and security profile

Server-rendered, standard library only, one small static script for live updates. Password-only, LAN-only, plain HTTP
unless a certificate is configured: deny-by-default routes (checked by a test), CSRF token on every change, hashed
in-memory sessions with idle and absolute timeouts, progressive lockout, strict CSP without inline code, no secret ever
rendered or accepted. It can only change timers and switch plugs; it cannot read the INNR credentials. Never expose it
to the internet. Design rationale and the reduced profile: [design section 17](.kiro/specs/innr-auto-off/design.md).

## Logs

`kind=signal` and `kind=decision` lines go to `docker logs` and to `/data/logs/signals.log` and `decisions.log`
(also viewable in the Web UI at `/log/signals` and `/log/decisions`). `deploy/deploy-nas.sh fetch-logs 24` copies them
to `./logs/nas/` for analysis. How to read them: [docs/DEPLOY.md, section B5](docs/DEPLOY.md).

## Repository layout

| Path | Content |
|---|---|
| `src/innr_autooff/` | the service: `engine` (pure state machine), `protocol`, `local_feed`, `innr_api`, `cloud_mqtt`, `discovery`, `app`, `web/` |
| `tests/` | unit, integration (fake TLS bridge, fake cloud) and Web UI security tests; no network needed |
| `deploy/` | compose file, config template, build / deploy / smoke-test scripts |
| `docs/DEPLOY.md`, `docs/CLOUD_CLIENT.md` | step-by-step runbook (Mac test, then NAS); the cloud client identity |
| `.kiro/specs/innr-auto-off/` | [requirements](.kiro/specs/innr-auto-off/requirements.md), [design](.kiro/specs/innr-auto-off/design.md), [tasks](.kiro/specs/innr-auto-off/tasks.md) |
| `LICENSE`, `NOTICE`, `SECURITY.md` | GPL-3.0 text; credits and trademark notice; how to report a vulnerability |

## Development

```bash
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # offline, about 2 minutes (scrypt makes the Web UI tests slow); needs node for one UI test
```

Runtime dependency: `paho-mqtt` only (pinned with hash). Python 3.13. The image build runs the same tests inside Linux
for both architectures before anything is published.

## Caveats

- The INNR cloud API is undocumented and only accepts requests that look like the official app. The values by which the
  service identifies itself are therefore **your configuration** (`[cloud.client]`, see
  [docs/CLOUD_CLIENT.md](docs/CLOUD_CLIENT.md)); none are shipped with this project. The cloud may change or block this
  client; OFF commands then fail, are retried, and the logs say why.
- If the internet or the INNR cloud is down when a timer expires, the plug stays on until the cloud is reachable again.
- The bridge only offers an old TLS cipher (`AES256-SHA256`) and a private certificate. The relaxed TLS settings apply to
  that one connection only; a certificate pin is stored on first contact.

## Safety, legal and privacy

- **Not a safety device.** Never rely on this to prevent overheating, fire, flooding or any other hazard. Timers can fail
  (cloud down, plug offline, bug). Keep the safety measures the appliance itself provides, and do not automate appliances
  that must not be cut off or that must not run unattended.
- **Your account, your devices.** The service needs your own INNR login and only controls plugs of that account. It does not
  bypass authentication, rate limits or access controls. Check INNR's terms of service and the law in your country before
  using a third-party client; the authors make no statement about legality or about the terms of any third party, and give
  no warranty (see the license).
- **No telemetry.** The service sends nothing anywhere except to the INNR cloud (your credentials and commands, as the
  official app does) and, for the optional image workflow, to your own container registry.
- **Data kept locally** in the data volume: the state file (plug states, your settings, a random client id), the signal and
  decision logs (plug names, states, times, cloud error messages), and an audit log of Web UI logins and changes that includes
  the client IP addresses. Passwords and tokens are never written to logs. If other people live in the household, tell them
  what is logged; delete the volume to erase everything.
- **Web UI exposure.** LAN only. Never forward its port to the internet. See [SECURITY.md](SECURITY.md) to report problems.

## License

GPL-3.0-only, see [LICENSE](LICENSE). Credits and third-party notices are in [NOTICE](NOTICE): the way the INNR cloud API is
used was learned from the open-source Homey app [homey-innr-bridge](https://github.com/smarthomesven/homey-innr-bridge)
(GPL-3.0); this project is an independent Python implementation. INNR, Leedarson, Arnoo, Synology, Docker, Alexa and other
names are trademarks of their owners.
