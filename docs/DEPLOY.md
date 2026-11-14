# Deployment runbook: INNR auto-off

Two stages: **A** test on a Mac (Docker Desktop, arm64), then **B** production on a Synology NAS
(Container Manager, amd64). Each step says what it does and how you know it worked. Commands are run in the
repository root on the Mac unless stated otherwise.

Conventions used here: `<NAS-IP>` is the LAN address of the NAS, `<bridge-IP>` the LAN address of the INNR bridge,
`myNAS` the ssh alias of the NAS. Site-specific values live in `deploy/site.env` (git-ignored, template:
`deploy/site.env.example`), never in the repository.

Principles: immutable `sha-<8>` image tags (never `latest`), a private GHCR package, a read-only registry token on the
NAS, key-only ssh with the alias, no secret ever passed as a command-line argument or written into the image.

> Run only ONE instance against the INNR account at a time (stop the Mac instance before starting
> the NAS one). Two instances would both switch plugs off, which is harmless but noisy.

---

## A. Test on the Mac

### A0. Site file (once)
```bash
cp deploy/site.env.example deploy/site.env      # then fill in NAS_IP, BRIDGE_IP, SERVER
```
`GHCR_OWNER` is the lower-case GitHub user or organisation that owns your container image (`<owner>` below). `BRIDGE_IP` is written into a newly created `config.toml`. The service refuses to start while `bridge.host` still has
the placeholder value from `config.example.toml`.

```bash
cp deploy/client.toml.example deploy/client.toml      # git-ignored; fill in the cloud client identity
```
The cloud client identity (`[cloud.client]`) is not shipped with the project: see [CLOUD_CLIENT.md](CLOUD_CLIENT.md).
The deploy scripts merge `deploy/client.toml` into the target `config.toml` on every run, and the service refuses to start
without it.

### A1. Build the image locally (no registry)
```bash
deploy/build-push.sh --load
```
Runs the test suite inside a Linux container, then builds `ghcr.io/<owner>/innr-autooff:sha-<8>`
for this Mac and loads it. Prints the tag on the last line. Optional offline check of the image:
```bash
deploy/smoke-test.sh ghcr.io/<owner>/innr-autooff:sha-<8>
```

### A2. Start it
```bash
deploy/deploy-local.sh sha-<8>
```
- First run asks for the INNR e-mail and password (hidden). They are stored in
  `~/innr-autooff-local/` (`.env` and `secrets/innr_password`, mode 600), outside the repository.
- `~/innr-autooff-local/config.toml` is created from `deploy/config.example.toml`. Plugs are discovered
  automatically and start monitor-only; enable auto-off per plug in the Web UI.
- The script waits until the container is **healthy** (cloud login works and a state source is
  connected) and shows the last log lines.

### A3. Set the Web UI password (once)
```bash
docker exec -it innr-autooff innr-autooff set-password
```
At least 12 characters, unique to this page. Then open <http://127.0.0.1:8480>.

### A4. Acceptance checks on the Mac
Pick a harmless plug and set it to auto-off after **1 minute** in the Web UI.

| # | Do | Expect |
|---|---|---|
| A1 | Switch the plug ON in the INNR app | Dashboard shows ON and a countdown; after 1 min the plug goes OFF; decisions log: `timer_started`, `timer_expired`, `send_off`, `command_confirmed` |
| A2 | Switch ON, then OFF in the app after 20 s | Countdown disappears; `timer_cancelled`; no command |
| A3 | Switch ON, then `docker restart innr-autooff` after 30 s | Plug still switches off about 1 min after it was switched on (`startup_policy policy=resumed`) |
| A4 | Switch ON, turn Wi-Fi off on the Mac before expiry, turn it on after 2 min | Dashboard shows OVERDUE with growing retry gaps; the plug switches off after the network is back |
| A5 | Switch ON, change minutes from 1 to 3 in the Web UI | Countdown recomputed from the ON time |
| A6 | ON/OFF buttons in the Web UI | Plug follows within a few seconds |
| A7 | Add a plug in the INNR app (if you have one) | It appears on the dashboard within about a minute, auto-off disabled |
| A8 | After a completed auto-off, wait 3 minutes | No new countdown appears; `signals.log` may show `rest_snapshot` lines that differ, `decisions.log` shows `rest_ignored` for them |

Watch the log while testing:
```bash
docker logs -f innr-autooff
```
When done, set the test plug back to its intended setting and stop the Mac instance before deploying to the NAS:
```bash
deploy/deploy-local.sh down
```

---

## B. Production on the Synology NAS

### B0. One-time preparation
1. **Fixed addresses** in the router (DHCP reservation) for the NAS and the INNR bridge. Put both into `deploy/site.env`.
2. **Container Manager** installed in DSM (Package Center). It provides `docker` and `/volume1/docker`.
3. **SSH** enabled in DSM (Control Panel, Terminal & SNMP), key-only login for an administrator account, an alias
   `myNAS` in `~/.ssh/config`, and `ssh myNAS true` works on the Mac. You can switch SSH off again after deploying.
4. **Push access to GHCR from the Mac** (once):
   ```bash
   gh auth refresh -h github.com -s write:packages,read:packages
   gh auth token | docker login ghcr.io -u <owner> --password-stdin
   ```
5. **Read-only token for the NAS**: GitHub, Settings, Developer settings, Personal access tokens
   (classic), scope **only** `read:packages`, expiry 90 days (set a calendar reminder). Do not paste
   it anywhere except the hidden prompt in B2.

### B1. Publish the image
Needs a clean, committed tree (the tag is the commit).
```bash
deploy/build-push.sh --push
```
Runs the tests for linux/amd64 (emulated) and linux/arm64, pushes both to the **private** GHCR
package `ghcr.io/<owner>/innr-autooff`, verifies both variants exist, prints the tag.

### B2. Prepare the NAS (once)
```bash
deploy/deploy-nas.sh preflight            # ssh works, NAS is x86_64
deploy/deploy-nas.sh login                # paste the read-only token (hidden); sudo password on the NAS
deploy/deploy-nas.sh secret               # INNR password (hidden); stored mode 400 for uid 10001
```

### B3. Deploy
```bash
deploy/deploy-nas.sh deploy sha-<8>
```
- Asks for your NAS (sudo) password, and the INNR e-mail on the first deploy.
- Files on the NAS: `/volume1/docker/innr-autooff/` with `compose.yaml`, `config.toml` (created once from the
  example with your `BRIDGE_IP`), `.env` (root, 600), `secrets/innr_password` (uid 10001, 400), `deploy-history.log`.
- Pulls the image, starts it, waits up to 5 min for **healthy**. If it is not healthy and a previous
  version exists, it **rolls back automatically** (tag and compose file).
- Then set the Web UI password on the NAS (once):
  ```bash
  ssh -t myNAS sudo /usr/local/bin/docker exec -it innr-autooff innr-autooff set-password
  ```
- Open `http://<NAS-IP>:8480` and repeat checks A1, A2 and A3.

### B4. Operations
| Task | Command |
|---|---|
| Status and recent deploys | `deploy/deploy-nas.sh status` |
| Logs (last 200 lines) | `deploy/deploy-nas.sh logs` |
| Logs for analysis (copies signals, decisions and operational log to `./logs/nas/`) | `deploy/deploy-nas.sh fetch-logs 24` |
| Update to a new version | commit, `deploy/build-push.sh --push`, `deploy/deploy-nas.sh deploy sha-<new>` |
| Roll back | `deploy/deploy-nas.sh rollback` |
| Start the container if it is not running (for example after Container Manager restarted) | `deploy/deploy-nas.sh start` |
| Find out why the container stopped (host and container metadata only, copied to `./logs/nas/diag.txt`) | `deploy/deploy-nas.sh diagnose 72` |
| Summarise the errors in the log of ONE other container that you name (counts, normalised messages, secrets masked; copied to `./logs/nas/<name>.txt`) | `deploy/deploy-nas.sh container-log <name> 24` |
| Change the INNR password | `deploy/deploy-nas.sh secret` |
| Renew the GHCR token (every 90 days) | `deploy/deploy-nas.sh login` |
| Change the Web UI password | `ssh -t myNAS sudo /usr/local/bin/docker exec -it innr-autooff innr-autooff set-password` |
| Change delays / enable plugs | Web UI (stored in the data volume, survives updates) |
| Change `config.toml` (bridge address, region, refresh interval) | edit `/volume1/docker/innr-autooff/config.toml` with sudo, then restart: `ssh -t myNAS sudo /usr/local/bin/docker restart innr-autooff` (a changed mounted file does not recreate the container) |
| Rebuild for OS security fixes | every 1 to 2 months: refresh the base digest in `Dockerfile`, commit, B1, B3 |

The container restarts automatically after a NAS reboot and after a Docker daemon restart (`restart: always`; see "Container stopped or not restarted" below for why `unless-stopped` is not enough). Container
Manager lists the container `innr-autooff`; do not change it there, use the scripts (they are the
record of what is deployed). On DSM, `docker` is `/usr/local/bin/docker` (not on the ssh PATH) and
needs `sudo`; DSM 7.2.1 ships Docker 24.0.2 and Compose v2.20.1 (checked when this was written).

### B5. Reading the logs: signals and decisions

Two kinds of lines, separated on purpose (design section 10). Both go to `docker logs`, and each kind
also has its own file in the data volume (`/data/logs/signals.log`, `decisions.log`, 2 MB x 3 files, always with
DEBUG detail). The Web UI shows the last 500 lines of each: footer links "signals" and "decisions" on the dashboard.

| Kind | Meaning | Typical events |
|---|---|---|
| `kind=signal` | something the service **received** | `push` (every bridge/cloud notification: OnOff, or power values), `rest_snapshot` (what the cloud list says, each minute), `command_ack` (result of an OFF/ON command with the error text), `cloud_error` (why a cloud call failed), `source_status`, `ui_*` (what was clicked) |
| `kind=decision` | something the service **chose**, with the reason | `timer_started` / `timer_cancelled` / `timer_expired`, `send_off` (attempt number, why), `command_failed` (reason, `retry_in`), `rest_ignored` / `rest_pending` / `rest_accepted` (why a cloud snapshot was not / was believed), `verify_ordered`, `assumed_off`, `command_confirmed`, `auto_off_failed` |

Reading an incident: put both files side by side in time order. Example (a stale cloud answer):
```
10:16:28 signal   push source=local plug="Coffee machine" onoff=0          the bridge says OFF
10:16:44 signal   rest_snapshot plug="Coffee machine" snapshot=1 known=0    the cloud list still says ON
10:16:44 decision rest_ignored ... reason=within_push_grace push_age=16     not believed: the bridge spoke 16 s ago
```
Useful filters: `grep 'kind=decision' ...`, `grep 'plug="Coffee machine"' ...`, `grep -E 'cloud_error|command_ack' signals.log`.
`rest_ignored` lines are normal: the INNR cloud lags the bridge by tens of seconds.

**OVERDUE on the dashboard** means the timer has expired and the plug is not yet OFF. The line below it says what
the service is doing: sending the command, waiting for the plug, checking the cloud state, or waiting for the next
try (gaps 5 s x3, 10 s x3, 20 s x3, 30 s x3, 60 s x3, then every 2 minutes, until the plug is OFF). It never shows a
countdown again. Disabling auto-off for that plug on its settings page stops the retries.

### B6. Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Container exits at start with "cloud.client is not configured" | The `[cloud.client]` table of `config.toml` is empty: fill `deploy/client.toml` ([CLOUD_CLIENT.md](CLOUD_CLIENT.md)) and deploy again |
| Container exits at start with "bridge.host is still the example value" | `config.toml` still has the placeholder; set `bridge.host` (or `BRIDGE_IP` in `deploy/site.env` for a new config) |
| Unhealthy; `signals.log` has `cloud_error call=devices kind_of=auth` | Wrong INNR password: `deploy/deploy-nas.sh secret` |
| `local_feed_cert_error` in the container log | The bridge certificate changed (firmware update?). Check the bridge, then remove `bridge_cert_sha256` from `/data/state.json` or set `cert_sha256` in `config.toml` |
| Dashboard shows "Bridge (local): disconnected" all the time | Bridge address changed or unreachable from the NAS; fix the DHCP reservation / `config.toml` |
| `denied` on pull | GHCR token expired or wrong scope: `deploy/deploy-nas.sh login` |
| `exec format error` | Image without amd64 variant; republish with `build-push.sh --push` |
| Web UI not reachable | `WEB_BIND_IP` in `.env` must be the NAS LAN address; the DSM firewall (if enabled) must allow TCP 8480 from the LAN |
| Locked out of the Web UI after wrong passwords | Wait (up to 15 min) or restart the container |
| The container is not running in the morning, or after Container Manager restarted | Run `deploy/deploy-nas.sh start` at once (until it runs, nothing switches plugs off), then `deploy/deploy-nas.sh diagnose 72` and read the section "Container stopped or not restarted" below |
| Plug stays on although the timer expired | The dashboard shows OVERDUE with the reason. Internet or INNR cloud down: the service keeps retrying (every 2 minutes at most) and switches off as soon as the cloud is back (accepted risk Q7). Find the reason in `signals.log`: `grep command_ack` shows the error text of each attempt |
| `rest_ignored` / `rest_pending` lines in `decisions.log` | Normal. The INNR cloud list lags the bridge; the service did not believe a single differing answer. Only a worry if it happens for hours |

### Container stopped or not restarted

**Run `diagnose` soon after an incident:** the NAS journal is small and volatile, and the Docker daemon is verbose, so the evidence of a crash is gone after a few hours. With `restart: always` the service comes back on its own, so a restart can go unnoticed: the dashboard uptime and a `startup` line in `docker logs` show it.

What to look at, in this order (`deploy/deploy-nas.sh diagnose` collects 1 to 4 in one go):
1. **Did the NAS reboot?** `uptime` in the report. If the NAS is older than the container's stop time, it did not reboot, and the Docker daemon was restarted by something else.
2. **Did the Docker daemon restart?** In the report, compare the start times of `dockerd` / `containerd` with the boot time. A daemon that is newer than the NAS was restarted (package action, crash, or killed under memory pressure).
3. **How did the container end?** `exit=` and `oom_killed=` of the container, and the last lines of its log: `event=shutdown` means a graceful stop (`docker stop`, package stop); no such line and `exit=137` means it was killed (memory, or the daemon was killed).
4. **Restart policy.** `restart=unless-stopped` does **not** bring a container back when the daemon restarts underneath it and stops it (or when it was stopped first, including by the Container Manager stop script); `restart=always` does. This project uses `always`. To keep a container down on purpose: `docker update --restart=no <name>` and `docker stop <name>`.
5. **Memory.** A 2 GB NAS that is deep in swap (`free -m`, `pswpin` / `pswpout`) kills or stalls things. Big users on a DS218+ are Antivirus scans, Plex, Java-based containers. The report lists the biggest processes.

Reading logs of other containers is **opt-in per call**: `diagnose` never reads them, `container-log <name>` reads exactly the one you name,
validates the name, and masks values that follow words like password, token, secret, key, authorization or cookie before anything is written.

**What the daemon journal showed on the first real case** (a Docker daemon that died without stopping its containers, then started again):
the new daemon found the container still running, stopped it (`live_restore` was off), and logged `ShouldRestart failed, container will not be
restarted` for a container with `restart: unless-stopped`, while a container with `restart: always` was started again. So `unless-stopped` is not
enough on a host where the daemon can restart under you; `always` is.

The service resumes cleanly after any restart (`restart_policy = "resume"`): a plug that was on keeps its original deadline, and an overdue plug is switched off at once. What matters is only that the container runs again quickly.

### B7. Security notes
- The Web UI is LAN-only, password-only and plain HTTP (accepted for a home LAN). Never forward its port in the router.
  HTTPS can be added later by setting `tls_cert`/`tls_key` in `config.toml`.
- The INNR password is the most valuable secret: it is only on the NAS in
  `secrets/innr_password` (uid 10001, mode 400) and in the container's memory, never in the image,
  the repository, logs or the Web UI.
- `sudo docker` on the NAS is root-equivalent; whoever holds the ssh key and the sudo password
  controls the NAS.
- Keep `deploy/site.env`, `logs/`, `input/` and `prototype/` out of git (they are git-ignored). The git history of a
  repository that once contained them keeps them; see the note in the README.
