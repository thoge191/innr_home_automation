# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""The cloud client identity is operator configuration, never code (R13)."""
import os
import shutil
import subprocess

import pytest

from innr_autooff.__main__ import main
from innr_autooff.config import (
    CLIENT_KEYS, ConfigError, load_config, parse_config, require_client_profile,
)

GOOD = {
    "app_id": "fake-app-1", "user_agent": "Fake/1.0 (tests)", "package_name": "example.fake.pkg",
    "app_version": "1.0.0", "web_version": "1.0.1", "build_number": 123, "os_version": "9",
    "web_view_version": "1.0.2", "device_model": "acme/rig", "device_name": "Rig", "origin": "https://o.example:1",
}


def cfg_with(client):
    return parse_config({"bridge": {"host": "10.0.0.1"}, "cloud": {"client": client}} if client is not None
                        else {"bridge": {"host": "10.0.0.1"}}, env={})


def test_profile_is_parsed_completely():
    c = cfg_with(GOOD)
    p = require_client_profile(c)
    for k, v in GOOD.items():
        assert getattr(p, k) == v
    assert set(GOOD) == set(CLIENT_KEYS)


def test_values_are_stripped():
    c = cfg_with({**GOOD, "app_id": "  fake-app-1  "})
    assert c.client.app_id == "fake-app-1"


@pytest.mark.parametrize("client", [None, {}, {k: "" for k in CLIENT_KEYS if k != "build_number"},
                                    {**{k: "" for k in CLIENT_KEYS if k != "build_number"}, "build_number": 0}])
def test_absent_or_empty_table_means_not_configured(client):
    c = cfg_with(client)
    assert c.client is None
    with pytest.raises(ConfigError, match="cloud.client is not configured") as e:
        require_client_profile(c)
    assert "CLOUD_CLIENT.md" in str(e.value)


@pytest.mark.parametrize("key", [k for k in CLIENT_KEYS])
def test_half_filled_profile_names_the_missing_key(key):
    broken = {k: v for k, v in GOOD.items() if k != key}
    with pytest.raises(ConfigError, match="incomplete or invalid") as e:
        cfg_with(broken)
    assert key in str(e.value)


def test_empty_value_in_a_filled_profile_is_an_error():
    with pytest.raises(ConfigError, match="app_id is missing or empty"):
        cfg_with({**GOOD, "app_id": "   "})


@pytest.mark.parametrize("bad", ["line\nbreak", "tab\there", "carriage\rreturn", "nul\x00byte", "uml\u00e4ut",
                                 "x" * 301, "\x7f"])
def test_header_injection_and_non_ascii_are_rejected(bad):
    with pytest.raises(ConfigError, match="printable ASCII"):
        cfg_with({**GOOD, "user_agent": bad})


@pytest.mark.parametrize("bn", ["123", 1.5, True, -1, 2**31, None])
def test_build_number_must_be_a_positive_whole_number(bn):
    with pytest.raises(ConfigError, match="build_number"):
        cfg_with({**GOOD, "build_number": bn})


def test_unknown_keys_are_typos_and_rejected():
    with pytest.raises(ConfigError, match="unknown key.*usre_agent"):
        cfg_with({**GOOD, "usre_agent": "x"})


def test_client_must_be_a_table():
    with pytest.raises(ConfigError, match="must be a table"):
        parse_config({"cloud": {"client": "nope"}}, env={})


def write_cfg(tmp_path, extra=""):
    f = tmp_path / "config.toml"
    f.write_text('[bridge]\nhost = "10.0.0.1"\n' + extra)
    return str(f)


def test_cli_run_refuses_without_a_client_profile(tmp_path, capsys):
    assert main(["--config", write_cfg(tmp_path), "run"]) == 2
    err = capsys.readouterr().err
    assert "cloud.client is not configured" in err and "docs/CLOUD_CLIENT.md" in err


def test_cli_check_config_warns_but_validates(tmp_path, capsys):
    assert main(["--config", write_cfg(tmp_path), "check-config"]) == 0
    cap = capsys.readouterr()
    assert "config OK" in cap.out and "WARNING: cloud.client is not configured" in cap.err


def test_cli_check_config_is_quiet_with_a_profile(tmp_path, capsys):
    body = "[cloud.client]\n" + "\n".join(f'{k} = {v}' if isinstance(v, int) else f'{k} = "{v}"' for k, v in GOOD.items())
    assert main(["--config", write_cfg(tmp_path, body), "check-config"]) == 0
    assert "WARNING" not in capsys.readouterr().err


def test_service_without_api_requires_the_profile(tmp_path):
    from innr_autooff.app import Service
    from innr_autooff.config import Credentials
    cfg = parse_config({"bridge": {"host": "10.0.0.1"}}, env={"INNR_DATA_DIR": str(tmp_path)})
    with pytest.raises(ConfigError, match="cloud.client is not configured"):
        Service(cfg, Credentials("a@b.c", "x"))


# ---------------------------------------------------------------- merge script (deploy)
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "deploy", "merge-client-profile.sh")
need_script = pytest.mark.skipif(not (os.path.exists(SCRIPT) and shutil.which("bash") and shutil.which("awk")),
                                 reason="merge script, bash or awk not available")

PROFILE_FILE = ("# comment above the table\n[cloud.client]\n"
                + "\n".join(f'{k} = {v}' if isinstance(v, int) else f'{k} = "{v}"' for k, v in GOOD.items()) + "\n")
TEMPLATE = ('[bridge]\nhost = "10.0.0.1"\n\n[cloud]\nregion = "eu"\n\n'
            '# describes the table below\n[cloud.client]\napp_id = ""\nuser_agent = "old"\n\n[web]\nport = 8480\n')


def merge(tmp_path, cfg_text, prof_text):
    (tmp_path / "c.toml").write_text(cfg_text)
    (tmp_path / "p.toml").write_text(prof_text)
    return subprocess.run(["bash", SCRIPT, str(tmp_path / "c.toml"), str(tmp_path / "p.toml")],
                          capture_output=True, text=True)


@need_script
def test_merge_replaces_only_the_client_table_and_keeps_the_rest(tmp_path):
    r = merge(tmp_path, TEMPLATE, PROFILE_FILE)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.count("[cloud.client]") == 1 and 'user_agent = "old"' not in out and "describes the table below" not in out
    assert '[bridge]\nhost = "10.0.0.1"' in out and '[web]\nport = 8480' in out and 'region = "eu"' in out
    (tmp_path / "m.toml").write_text(out)
    c = load_config(tmp_path / "m.toml", env={})
    assert c.client is not None and c.client.app_id == "fake-app-1" and c.web.port == 8480 and c.bridge_host == "10.0.0.1"
    # the inputs are untouched
    assert (tmp_path / "c.toml").read_text() == TEMPLATE


@need_script
def test_merge_is_idempotent(tmp_path):
    first = merge(tmp_path, TEMPLATE, PROFILE_FILE).stdout
    for _ in range(3):
        again = merge(tmp_path, first, PROFILE_FILE).stdout
        assert again == first
        first = again


@need_script
def test_merge_inserts_when_the_config_has_no_client_table(tmp_path):
    r = merge(tmp_path, '[bridge]\nhost = "10.0.0.1"\n[web]\nport = 1\n', PROFILE_FILE)
    assert r.returncode == 0 and r.stdout.count("[cloud.client]") == 1
    (tmp_path / "m.toml").write_text(r.stdout)
    assert load_config(tmp_path / "m.toml", env={}).client.build_number == 123


@need_script
def test_merge_keeps_comments_of_other_tables(tmp_path):
    cfg = '[bridge]\nhost = "10.0.0.1"\n\n# my note about web\n[web]\nport = 1\n\n[cloud.client]\napp_id = "old"\n'
    out = merge(tmp_path, cfg, PROFILE_FILE).stdout
    assert "# my note about web\n[web]" in out and 'app_id = "old"' not in out


@need_script
@pytest.mark.parametrize("profile", ["app_id = \"x\"\n", "[bridge]\nhost = \"1.2.3.4\"\n",
                                     PROFILE_FILE + "\n[web]\nport = 1\n", PROFILE_FILE + "[defaults]\ndelay_seconds = 1\n"])
def test_merge_refuses_a_profile_that_could_change_other_tables(tmp_path, profile):
    r = merge(tmp_path, TEMPLATE, profile)
    assert r.returncode != 0 and r.stdout == ""
    assert "cloud.client" in r.stderr


@need_script
def test_merge_reports_unreadable_input(tmp_path):
    r = subprocess.run(["bash", SCRIPT, str(tmp_path / "missing.toml"), str(tmp_path / "also-missing.toml")],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "cannot read" in r.stderr


# ---------------------------------------------------------------- the project ships no identity (R13.1, R13.5)
def _tracked_files():
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    if os.path.isdir(os.path.join(root, ".git")) and shutil.which("git"):
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout.split("\n")
        return root, [f for f in out if f]
    files = []  # inside the image test stage there is no git: scan what is there
    for top in ("src", "tests", "deploy"):
        for dp, _, fns in os.walk(os.path.join(root, top)):
            files += [os.path.relpath(os.path.join(dp, f), root) for f in fns if not f.endswith(".pyc")]
    return root, files


def test_no_vendor_identity_values_are_shipped():
    """Fails if the app identifier, package name, device strings or user agent of the vendor's app reappear in
    the repository. The literals are split so that this file does not contain them itself."""
    forbidden = ["12601" + "40618008576002", "com.innr" + ".lcng", "SM-A5" + "15F", "Galaxy" + " A51",
                 "133.0.6943" + ".137", "127.0.0.1:" + "10501", "Mobile Safari/537.36 Lee" + "darson"]
    root, files = _tracked_files()
    assert files
    hits = []
    for f in files:
        try:
            text = open(os.path.join(root, f), encoding="utf-8").read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        hits += [(f, bad) for bad in forbidden if bad in text]
    assert hits == [], f"vendor identity values found in tracked files: {hits}"
