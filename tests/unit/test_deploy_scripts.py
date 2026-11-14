# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Static and behavioural checks of the deploy scripts (they touch other people's containers on the NAS)."""
import os
import re
import shutil
import stat
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
REMOTE = os.path.join(DEPLOY, "nas-remote.sh")
LOCAL = os.path.join(DEPLOY, "deploy-nas.sh")

have_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
have_scripts = pytest.mark.skipif(not (os.path.exists(REMOTE) and os.path.exists(LOCAL)), reason="deploy scripts not present")


def scripts():
    return sorted(os.path.join(DEPLOY, f) for f in os.listdir(DEPLOY) if f.endswith(".sh")) if os.path.isdir(DEPLOY) else []


@have_bash
@have_scripts
@pytest.mark.parametrize("path", scripts() or [pytest.param("", marks=pytest.mark.skip)])
def test_every_script_has_valid_syntax(path):
    r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@have_scripts
def test_usage_lists_the_operations_commands():
    text = open(LOCAL, encoding="utf-8").read()
    for cmd in ("start", "diagnose", "fetch-logs", "rollback", "status"):
        assert f"deploy/deploy-nas.sh {cmd}" in text, cmd


@have_scripts
def test_diagnose_never_reads_other_containers_secrets():
    """PRIVACY (static): the NAS hosts other people's containers. Only metadata may be read from them."""
    text = open(REMOTE, encoding="utf-8").read()
    section = text[text.index("  diagnose)"):text.index("  container-log)")]
    for forbidden in (".Config.Env", ".Config.Cmd", ".Config.Entrypoint", ".Config.Labels", ".Mounts", ".Args", ".Path",
                      ".NetworkSettings", ".HostConfig.Binds", ".HostConfig.PortBindings", "docker-compose", "printenv", "/proc/*/environ"):
        assert forbidden not in section, forbidden
    # every inspect call uses an explicit template
    for line in section.splitlines():
        if re.search(r'"\$DOCKER" inspect', line):
            assert " -f '" in line and "{{" in line, line
    # container logs are read only from our own container
    for line in section.splitlines():
        if re.search(r'"\$DOCKER" (logs|exec)', line):
            assert '"$CONTAINER"' in line, line
    # the report is private to the ssh user
    assert "mkdir -m 700" in section


FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env bash
    # fake docker: plants secrets where a careless diagnose would leak them
    cmd="$1"; shift
    case "$cmd" in
      info)    echo "version=24.0.2 live_restore=false running=1 stopped=1 cgroup=cgroupfs" ;;
      ps)      printf 'aaa111\\nbbb222\\n' ;;
      inspect)
        if [[ "$*" != *"-f"* ]]; then echo '"Env": ["DB_PASSWORD=hunter2-LEAK", "API_KEY=sk-LEAK"]'; exit 0; fi
        id="${@: -1}"
        if [[ "$id" == ghost ]]; then echo "Error: No such object: ghost" >&2; exit 1; fi
        if [[ "$id" == watched-app ]]; then echo "/watched-app image=example/watched:1 restart=always status=running started=2026-11-14T06:32:37Z"; exit 0; fi
        if [[ "$id" == aaa111 ]]; then
          echo "/other-app image=example/other:1 restart=always status=running started=2026-11-14T06:32:29Z finished=2026-11-14T06:28:01Z exit=0 oom_killed=false restarts=0 mem_limit=0 error="
        else
          echo "/innr-autooff image=ghcr.io/me/innr-autooff:sha-1 restart=unless-stopped status=exited started=2026-11-13T05:00:00Z finished=2026-11-14T06:30:58Z exit=137 oom_killed=false restarts=0 mem_limit=201326592 error="
        fi ;;
      stats)   echo "other-app mem=410MiB / 1.8GiB cpu=3.1%" ;;
      logs)
        name="${@: -1}"
        if [[ "$name" == watched-app ]]; then
          for i in 1 2 3 4 5; do echo "2026-11-14T05:0$i:01.123456789Z ERROR could not reach server 1$i at 10.1.2.$i:443 (attempt $i) password=hunter2-LEAK"; done
          echo "2026-11-14T06:10:00.000000000Z INFO job finished ok"
          echo "2026-11-14T06:20:00.000000000Z WARN slow result"
          echo "2026-11-14T06:30:00.000000000Z Uncaught exception: database is locked token: abc123SECRET456"
        elif [[ "$name" == innr-autooff ]]; then
          echo "2026-11-14T06:30:58+00:00 INFO event=shutdown"
          echo "kind=signal event=push should-be-filtered"
        else
          echo "LEAK: other container log line with password=hunter2-LEAK"
        fi ;;
      exec)    echo "2026-11-14T06:29:00+00:00 INFO kind=decision event=timer_started plug=\\"Coffee machine\\"" ;;
      *)       echo "fake docker: unexpected $cmd" >&2; exit 1 ;;
    esac
''')

FAKE_SUDO = textwrap.dedent('''\
    #!/usr/bin/env bash
    [[ "$1" == "-v" ]] && exit 0
    exec "$@"
''')


@pytest.fixture
def fakebin(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("sudo", FAKE_SUDO)):
        p = b / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    return b, home


def run_remote(fakebin, *args):
    b, home = fakebin
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "HOME": str(home)}
    return subprocess.run(["bash", REMOTE, *args], capture_output=True, text=True, env=env, timeout=60), home


@have_bash
@have_scripts
def test_diagnose_collects_metadata_and_leaks_nothing(fakebin):
    r, home = run_remote(fakebin, "diagnose", "24")
    assert r.returncode == 0, r.stderr
    out = home / "innr-diag-export"
    assert oct(out.stat().st_mode & 0o777) == "0o700"
    report = (out / "diag.txt").read_text()
    # what the owner needs to decide the cause
    assert "restart=always" in report and "restart=unless-stopped" in report
    assert "exit=137" in report and "oom_killed=false" in report and "finished=2026-11-14T06:30:58Z" in report
    assert "live_restore=false" in report and "other-app mem=410MiB" in report
    assert "event=shutdown" in report                      # our own container's log is included
    assert "event=timer_started" in report                 # and the end of our decision log
    assert "### docker stack start times" in report and "### Docker service lifecycle" in report
    assert "### the last lines of the daemon before each stop or crash" in report and "### kernel: memory kills" in report
    # what must never be in it
    for leak in ("hunter2-LEAK", "sk-LEAK", "DB_PASSWORD", "API_KEY", "LEAK:"):
        assert leak not in report, leak
    assert "should-be-filtered" not in report              # signal lines are filtered out of the operational excerpt


@have_bash
@have_scripts
def test_diagnose_rejects_a_bad_hours_argument(fakebin):
    r, home = run_remote(fakebin, "diagnose", "1;rm -rf /")
    assert r.returncode != 0 and "hours must be a number" in r.stderr
    assert not (home / "innr-diag-export").exists()


@have_bash
@have_scripts
def test_unknown_command_is_refused_with_usage(fakebin):
    r, _ = run_remote(fakebin, "format-everything")
    assert r.returncode != 0 and "usage:" in r.stderr and "diagnose" in r.stderr


# ---------------------------------------------------------------- container-log (opt-in, owner names the container)
@have_bash
@have_scripts
def test_container_log_summarises_errors_and_masks_secrets(fakebin):
    r, home = run_remote(fakebin, "container-log", "watched-app", "24")
    assert r.returncode == 0, r.stderr
    report = (home / "innr-diag-export" / "container-log.txt").read_text()
    assert "### container: /watched-app image=example/watched:1 restart=always" in report
    assert "window: last 24 h" in report and re.search(r"lines:\s+8\b", report)
    # five identical errors collapse into one normalised message with its count
    assert re.search(r"^\s*5 ERROR could not reach server <n> at <ip> \(attempt <n>\) password=\*\*\*", report, re.M), report
    assert "database is locked" in report                       # the second kind of error is listed too
    assert "### errors per hour" in report and "2026-11-14T05" in report
    # secrets are masked before anything is written
    for leak in ("hunter2-LEAK", "abc123SECRET456"):
        assert leak not in report, leak
    assert "token: ***" in report or "token=***" in report


@have_bash
@have_scripts
@pytest.mark.parametrize("name", ["../etc", "a b", "x;rm -rf /", "$(id)", "", "-x", "a" * 70, "näme"])
def test_container_log_rejects_unsafe_names(fakebin, name):
    r, home = run_remote(fakebin, "container-log", name)
    assert r.returncode != 0 and "unexpected characters" in r.stderr
    assert not (home / "innr-diag-export" / "container-log.txt").exists()


@have_bash
@have_scripts
def test_container_log_unknown_container_is_a_clear_error(fakebin):
    r, home = run_remote(fakebin, "container-log", "ghost")
    assert r.returncode != 0 and "no container named 'ghost'" in r.stderr


@have_bash
@have_scripts
def test_diagnose_still_does_not_read_the_watched_container(fakebin):
    """The opt-in is per call: a plain diagnose must not read other containers' logs, even ones that have errors."""
    r, home = run_remote(fakebin, "diagnose", "24")
    assert r.returncode == 0, r.stderr
    report = (home / "innr-diag-export" / "diag.txt").read_text()
    assert "could not reach server" not in report and "database is locked" not in report


@have_scripts
def test_container_log_section_is_the_only_place_reading_other_logs():
    text = open(REMOTE, encoding="utf-8").read()
    section = text[text.index("  container-log)"):text.index("  fetch-logs)")]
    assert '[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]' in section      # strict name validation first
    for forbidden in (".Config.Env", ".Mounts", ".Config.Cmd", ".Config.Labels", "printenv"):
        assert forbidden not in section, forbidden
    logs_lines = [l for l in section.splitlines() if re.search(r'"\$DOCKER" logs', l)]
    assert logs_lines and all('"$NAME"' in l for l in logs_lines)
    assert "MASK=" in section and "sed -E \"$MASK\"" in section                  # masking is applied to every printed line
    # outside this section no other container's log is read
    others = text.replace(section, "")
    for line in others.splitlines():
        if re.search(r'"\$DOCKER" logs', line):
            assert '"$CONTAINER"' in line, line
