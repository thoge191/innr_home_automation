# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""protocol, discovery, config, store, logging masking (R1.3, R3.6, R5.1, R5.4, R10)."""
import json
import logging
import os
import secrets
import struct

import pytest

from innr_autooff import logging_util
from innr_autooff.config import ConfigError, load_credentials, parse_config
from innr_autooff.discovery import clean_text, discover
from innr_autooff.protocol import FrameParser, decode_notification, encode_frame, onoff_value
from innr_autooff.store import StateStore

GW = "000000000000000000000000000000ff"
P1 = "00000000000000000000000000000001"
P2 = "00000000000000000000000000000002"
REAL_FRAME_BODY = (b'{"service":"device","method":"setDevAttrNotif","seq":"395294","srcAddr":"1.' + GW.encode()
                   + b'","payload":{"devId":"' + P1.encode() + b'","idx":115406,"attr":{"Current":0}},'
                   b'"ack":{"code":200,"desc":"Success."}}')


# ---------------------------------------------------------------- protocol
def test_real_captured_frame_decodes():
    frame = bytes.fromhex("1eed0001000000eb") + REAL_FRAME_BODY
    assert len(REAL_FRAME_BODY) == 0xEB
    payloads = FrameParser().feed(frame)
    ev = decode_notification(payloads[0])
    assert ev.dev == P1 and ev.attrs == {"Current": 0} and ev.idx == 115406


def test_parser_byte_by_byte_two_frames_and_junk():
    m1 = encode_frame({"method": "setDevAttrNotif", "payload": {"devId": P1, "attr": {"OnOff": 1}}})
    m2 = encode_frame({"method": "setDevAttrNotif", "payload": {"devId": P2, "attr": {"Watts": 407}}})
    data = b"junk" + m1 + m2
    p = FrameParser()
    out = []
    for b in data:
        out += p.feed(bytes([b]))
    assert [decode_notification(x).dev for x in out] == [P1, P2]
    assert any("resync" in w for w in p.pop_warnings())


def test_parser_implausible_length_resyncs():
    good = encode_frame({"method": "setDevAttrNotif", "payload": {"devId": P1, "attr": {"OnOff": 0}}})
    bad = b"\x1e\xed\x00\x01" + struct.pack(">I", 50_000_000)
    out = FrameParser().feed(bad + good)
    assert len(out) == 1


def test_parser_partial_frame_waits():
    f = encode_frame({"a": 1})
    p = FrameParser()
    assert p.feed(f[:-3]) == []
    assert p.feed(f[-3:]) == [f[8:]]


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'{"method":"x"}', b'{"method":"setDevAttrNotif"}',
                                     b'{"method":"setDevAttrNotif","payload":{"devId":5,"attr":{}}}', b"\xff\xfe"])
def test_decode_rejects_garbage(payload):
    assert decode_notification(payload) is None


@pytest.mark.parametrize("v,expected", [(1, 1), (0, 0), ("1", 1), ("0", 0), (1.0, 1), (2, None), ("x", None), (None, None)])
def test_onoff_value(v, expected):
    assert onoff_value({"OnOff": v}) == expected
    assert onoff_value({"Watts": 3}) is None


# ---------------------------------------------------------------- discovery
def cloud_list():
    return [
        {"id": P2, "parentId": GW, "type": "plug", "name": "ON/OFF coffee machine", "modelId": "Innr.SP 240",
         "online": True, "properties": {"OnOff": "0", "Watts": "0"}, "password": "secret-pw-1"},
        {"id": P1, "parentId": GW, "type": "plug", "name": "Balcony_Plug_Outdoor", "modelId": "Innr.OSP 240",
         "online": True, "properties": {"OnOff": "1", "Watts": "12.5"}, "password": "secret-pw-2"},
        {"id": GW, "parentId": "", "type": "Mini_hub", "name": "Innr Bridge_1", "modelId": "Innr.BG-220-V1"},
        {"id": "zz", "parentId": GW, "type": "plug"},
        "garbage",
    ]


def test_discover_only_plugs_and_no_password():
    infos = discover(cloud_list())
    assert set(infos) == {P1, P2}
    assert infos[P1].onoff == 1 and infos[P1].power_w == 12.5 and infos[P1].gateway_id == GW
    assert infos[P2].onoff == 0 and infos[P2].model == "Innr.SP 240"
    assert "secret-pw" not in repr(infos)


def test_discover_seed_name_wins_and_text_is_cleaned():
    d = cloud_list()
    d[0]["name"] = "evil\x00\x1b[31mname" + "x" * 200
    infos = discover(d, {P1: "Balcony"})
    assert infos[P1].name == "Balcony"
    assert "\x00" not in infos[P2].name and "\x1b" not in infos[P2].name and len(infos[P2].name) <= 64
    assert clean_text(None) == ""


# ---------------------------------------------------------------- config
def test_config_defaults_and_seeds():
    c = parse_config({"plugs": [{"dev_id": P2, "name": "Coffee", "delay_seconds": 1800},
                                {"dev_id": P1, "auto_off_enabled": False}]}, env={})
    assert c.seeds[P2].delay_seconds == 1800.0 and c.seeds[P2].enabled
    assert c.seeds[P1].enabled is False
    assert c.seed_names == {P2: "Coffee"}
    assert c.new_plug_auto_off is False and c.restart_policy == "resume"
    assert c.web.port == 8480


@pytest.mark.parametrize("data", [
    {"plugs": [{"dev_id": "nope"}]},
    {"plugs": [{"dev_id": P1, "delay_seconds": 0}]},
    {"defaults": {"restart_policy": "maybe"}},
    {"cloud": {"region": "cn"}},
    {"cloud": {"reconcile_seconds": 1}},
    {"web": {"bind": "localhost"}},
    {"web": {"tls_cert": "/x"}},
    {"web": {"session_idle_seconds": 10}},
    {"web": {"refresh_seconds": 0}},
    {"web": {"refresh_seconds": 61}},
    {"bridge": {"port": "10000"}},
    {"bridge": {"cert_sha256": "abc"}},
    {"defaults": {"auto_off_enabled": "true"}},
])
def test_config_rejects_invalid(data):
    with pytest.raises(ConfigError):
        parse_config(data, env={})


def test_credentials_from_file(tmp_path):
    f = tmp_path / "pw"
    f.write_text("hunter2-secret\n")
    cr = load_credentials({"INNR_EMAIL": "a@b.c", "INNR_PASSWORD_FILE": str(f)})
    assert cr.password == "hunter2-secret"
    assert "hunter2" not in repr(cr)
    with pytest.raises(ConfigError):
        load_credentials({"INNR_EMAIL": "a@b.c", "INNR_PASSWORD_FILE": str(tmp_path / "missing")})
    with pytest.raises(ConfigError):
        load_credentials({"INNR_PASSWORD_FILE": str(f)})
    (tmp_path / "empty").write_text("  \n")
    with pytest.raises(ConfigError):
        load_credentials({"INNR_EMAIL": "a@b.c", "INNR_PASSWORD_FILE": str(tmp_path / "empty")})


def example_path():
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(root, "deploy", "config.example.toml")


def test_example_config_parses_and_contains_no_private_values():
    from innr_autooff.config import EXAMPLE_BRIDGE_HOST, load_config
    c = load_config(example_path(), env={})
    assert c.bridge_host == EXAMPLE_BRIDGE_HOST == "192.0.2.10"   # RFC 5737 documentation address
    assert c.seeds == {} and c.new_plug_auto_off is False and c.web.port == 8480
    assert c.client is None   # the template documents [cloud.client] but ships no values (R13.1)
    text = open(example_path(), encoding="utf-8").read()
    assert "192.168." not in text and "ccb81d" not in text


def test_bridge_host_is_required_and_the_example_value_is_refused(tmp_path):
    from innr_autooff.config import load_config, require_real_bridge_host
    f = tmp_path / "c.toml"
    f.write_text("[cloud]\nregion = 'eu'\n")
    with pytest.raises(ConfigError, match="bridge.host is not set"):
        load_config(f, env={})
    f.write_text("[bridge]\nhost = '192.0.2.10'\n")
    cfg = load_config(f, env={})
    with pytest.raises(ConfigError, match="still the example value"):
        require_real_bridge_host(cfg)
    f.write_text("[bridge]\nhost = ' 10.1.2.3 '\n")
    cfg = load_config(f, env={})
    assert cfg.bridge_host == "10.1.2.3"
    require_real_bridge_host(cfg)  # a real address passes


def test_cli_refuses_to_run_with_the_example_host(capsys):
    from innr_autooff.__main__ import main
    assert main(["--config", example_path(), "run"]) == 2
    assert "still the example value" in capsys.readouterr().err
    assert main(["--config", example_path(), "check-config"]) == 0   # validating is allowed, with a warning
    cap = capsys.readouterr()
    assert "config OK" in cap.out and "WARNING" in cap.err


# ---------------------------------------------------------------- store
def test_store_roundtrip_atomic_and_private(tmp_path):
    s = StateStore(tmp_path / "state.json")
    assert s.load() == {"version": 1}
    s.save({"plugs": {"x": 1}})
    assert oct((tmp_path / "state.json").stat().st_mode & 0o777) == "0o600"
    s2 = StateStore(tmp_path / "state.json")
    assert s2.load()["plugs"] == {"x": 1}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".state.")]


@pytest.mark.parametrize("content", ["{broken", "", "[1,2]"])
def test_store_corrupt_file_does_not_block_startup(tmp_path, content):
    p = tmp_path / "state.json"
    p.write_text(content)
    s = StateStore(p)
    assert s.load() == {"version": 1}
    if content.strip():
        assert (tmp_path / "state.json.bad").exists()


def test_terminal_id_is_persistent(tmp_path):
    s = StateStore(tmp_path / "state.json")
    s.load()
    t1 = s.terminal_id()
    s2 = StateStore(tmp_path / "state.json")
    s2.load()
    assert s2.terminal_id() == t1 and len(t1) == 21


# ---------------------------------------------------------------- logging masking (R5.4)
def test_mask_nested():
    m = logging_util.mask({"password": "x" * 12, "a": [{"accessToken": "t" * 30}], "aesKey": "k" * 6, "ok": 1})
    s = json.dumps(m)
    assert "xxxx" not in s and "tttt" not in s and "kkkk" not in s and '"ok": 1' in s


def test_registered_secrets_never_reach_log_output(caplog):
    logging_util.setup_logging("DEBUG")
    handler = logging.getLogger().handlers[0]
    caplog.handler.addFilter(logging_util.SecretFilter())
    for _ in range(20):
        secret = secrets.token_urlsafe(24)
        logging_util.register_secret(secret)
        logging.getLogger("t").info("value %s inside %s", secret, f"text-{secret}-end")
        assert secret not in caplog.text
    assert handler.filters


def test_short_values_not_registered():
    logging_util.register_secret("abc")
    assert logging_util.scrub("abc def") == "abc def"


def test_kv_quotes():
    assert logging_util.kv("x", a="b c", n=1, skip=None) == 'event=x a="b c" n=1'
