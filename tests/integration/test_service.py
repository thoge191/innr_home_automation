# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""The whole Service against fakes: discovery, auto-off, confirmation, restart policy, Web UI bridge."""
import asyncio
import http.client
import json
import re
import threading
from urllib.parse import urlencode

import pytest

from innr_autooff.app import Service
from innr_autooff.config import Credentials, WebConfig, parse_config
from innr_autooff.web.auth import PasswordFile
from innr_autooff.web.server import start_web

GW = "000000000000000000000000000000ff"
COFFEE = "00000000000000000000000000000002"
BALCONY = "00000000000000000000000000000001"
NEW = "00000000000000000000000000000003"
DEV_PW = {COFFEE: "DEVPW-coffee-1", BALCONY: "DEVPW-balcony-2", NEW: "DEVPW-new-3"}


def dev(i, name, onoff="0", typ="plug"):
    return {"id": i, "parentId": GW, "type": typ, "name": name, "modelId": "Innr.SP 240", "online": True,
            "properties": {"OnOff": onoff, "Watts": "0"}, "password": DEV_PW.get(i, "x")}


class FakeApi:
    def __init__(self, devices):
        self.list = devices
        self.user_id = "u" * 32
        self.last_error = ""
        self.lock = threading.Lock()

    def devices(self):
        with self.lock:
            return "1000001", [dict(d) for d in self.list] + [dev(GW, "Innr Bridge_1", typ="Mini_hub")]

    def mqtt_config(self, house_id):
        return {"host": "global-eu-mqtt.arnoo.com:8443", "password": "MQTT-PW-xyz", "heartbeat": 45, "clientId": "42-x"}

    def client_id(self, cfg):
        return "42-" + self.user_id


class FakeLocal:
    last = None

    def __init__(self, host, port, on_event, on_status, pin_sha256="", on_new_pin=None):
        self.on_event, self.on_status = on_event, on_status
        self._stop = asyncio.Event()
        FakeLocal.last = self

    async def run(self):
        self.on_status(True)
        await self._stop.wait()

    def stop(self):
        self._stop.set()


def make_service(tmp_path, devices, seeds, bridge_echo=True, cmd_ok=True):
    cfg = parse_config({"cloud": {"state_feed": False, "reconcile_seconds": 15},
                        "web": {"enabled": False}, "plugs": seeds}, env={"INNR_DATA_DIR": str(tmp_path)})
    api = FakeApi(devices)
    commands = []
    holder = {}

    def command(params, uid, dev_id, gateway, dev_password, attr):
        commands.append((dev_id, gateway, dev_password, attr, params.password))
        if bridge_echo and cmd_ok:
            # the bridge reports the new state on the local feed shortly afterwards
            holder["svc"].loop.call_soon_threadsafe(FakeLocal.last.on_event, dev_id, dict(attr), None)
        return cmd_ok

    svc = Service(cfg, Credentials("a@b.c", "x"), api=api, command_fn=command, local_feed_factory=FakeLocal)
    holder["svc"] = svc
    return svc, api, commands


async def until(cond, timeout=8.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def test_auto_off_end_to_end_with_discovery_and_monitor_only(tmp_path, monkeypatch):
    monkeypatch.setenv("INNR_HEALTH_FILE", str(tmp_path / "health"))
    seeds = [{"dev_id": COFFEE, "name": "Coffee machine", "delay_seconds": 2},
             {"dev_id": BALCONY, "auto_off_enabled": False}]
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "ON/OFF coffee machine"), dev(BALCONY, "Balcony")], seeds)

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: svc.ready and FakeLocal.last is not None)
        assert set(svc.engine.plugs) == {COFFEE, BALCONY}  # the bridge (Mini_hub) is not a plug
        assert svc.engine.plugs[COFFEE].name == "Coffee machine"  # seed name wins

        # coffee machine ON -> OFF after 2 s, confirmed by the bridge echo
        FakeLocal.last.on_event(COFFEE, {"OnOff": 1}, None)
        FakeLocal.last.on_event(BALCONY, {"OnOff": 1}, None)
        assert await until(lambda: svc.engine.plugs[COFFEE].state.value == "off", 8)
        assert svc.engine.plugs[COFFEE].last_result == "auto-off confirmed"
        assert len(commands) == 1
        d, gw, pw, attr, mqtt_pw = commands[0]
        assert (d, gw, pw, attr, mqtt_pw) == (COFFEE, GW, DEV_PW[COFFEE], {"OnOff": 0}, "MQTT-PW-xyz")

        # balcony is monitor-only: still ON, never commanded
        await asyncio.sleep(2.5)
        assert svc.engine.plugs[BALCONY].is_on and all(c[0] != BALCONY for c in commands)

        # a plug added in the INNR app: event for an unknown id triggers discovery (R10.4)
        with api.lock:
            api.list.append(dev(NEW, "New plug", onoff="1"))
        FakeLocal.last.on_event(NEW, {"OnOff": 1}, None)
        assert await until(lambda: NEW in svc.engine.plugs, 5)
        assert svc.engine.plugs[NEW].settings.enabled is False

        # enable it at runtime via the public API (as the Web UI does)
        svc.set_plug_settings(NEW, True, 1 / 60)  # 1 second
        assert await until(lambda: any(c[0] == NEW for c in commands), 5)

        snap = svc.snapshot()
        assert {p["name"] for p in snap["plugs"]} == {"Coffee machine", "Balcony", "New plug"}
        assert "DEVPW" not in json.dumps(snap) and "MQTT-PW" not in json.dumps(snap)
        svc.stop()
        await asyncio.wait_for(task, 5)

    asyncio.run(main())
    state = (tmp_path / "state.json").read_text()
    assert "DEVPW" not in state and "MQTT-PW" not in state
    data = json.loads(state)
    assert data["plugs"][NEW]["overrides"]["enabled"] is True


def test_restart_resume_switches_overdue_plug_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("INNR_HEALTH_FILE", str(tmp_path / "health"))
    import time as _t
    (tmp_path / "state.json").write_text(json.dumps({"version": 1, "plugs": {
        COFFEE: {"state": "on", "on_since_utc": _t.time() - 3600}}}))
    seeds = [{"dev_id": COFFEE, "delay_seconds": 1800}]
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee", onoff="1")], seeds)

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: len(commands) == 1, 5)
        svc.stop()
        await asyncio.wait_for(task, 5)
    asyncio.run(main())


def test_failed_command_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("INNR_HEALTH_FILE", str(tmp_path / "health"))
    seeds = [{"dev_id": COFFEE, "delay_seconds": 1}]
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee")], seeds, cmd_ok=False)

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: svc.ready and FakeLocal.last is not None)
        FakeLocal.last.on_event(COFFEE, {"OnOff": 1}, None)
        assert await until(lambda: len(commands) >= 2, 12)  # attempt 1 fails, attempt 2 after 5 s
        assert svc.engine.plugs[COFFEE].is_on
        svc.stop()
        await asyncio.wait_for(task, 5)
    asyncio.run(main())


def test_web_ui_talks_to_running_service(tmp_path, monkeypatch):
    monkeypatch.setenv("INNR_HEALTH_FILE", str(tmp_path / "health"))
    seeds = [{"dev_id": COFFEE, "name": "Coffee machine", "delay_seconds": 1800}]
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee")], seeds)
    PasswordFile(tmp_path / "web_auth.json").set("correct horse battery")
    holder = {}

    def web_start(s):
        holder["web"] = start_web(s, WebConfig(port=0, bind="127.0.0.1"), tmp_path)
        return holder["web"]

    def call(method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", holder["web"].port, timeout=30)
        h = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if cookie:
            h["Cookie"] = f"innr_session={cookie}"
        c.request(method, path, body=urlencode(body).encode() if body else None, headers=h)
        r = c.getresponse()
        out = r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode()
        c.close()
        return out

    def client_flow():
        st, h, _ = call("POST", "/login", {"password": "correct horse battery"})
        ck = re.search(r"innr_session=([^;]*)", h["set-cookie"]).group(1)
        st, _, page = call("GET", "/", cookie=ck)
        csrf = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page).group(1)
        st2, _, _ = call("POST", f"/plug/{COFFEE}/settings", {"csrf_token": csrf, "enabled": "on", "minutes": "45"}, ck)
        st3, _, _ = call("POST", f"/plug/{COFFEE}/switch", {"csrf_token": csrf, "onoff": "on"}, ck)
        health = call("GET", "/health")[0]
        api = json.loads(call("GET", "/api/state", cookie=ck)[2])
        assert [p["name"] for p in api["plugs"]] == ["Coffee machine"]
        assert api["plugs"][0]["auto"] == "auto-off after 45.0 min"
        return st, page, st2, st3, health

    async def main():
        task = asyncio.create_task(svc.run(web_start))
        assert await until(lambda: svc.ready and FakeLocal.last is not None)
        st, page, st2, st3, health = await asyncio.to_thread(client_flow)
        assert st == 200 and "Coffee machine" in page
        assert st2 == 303 and svc.engine.plugs[COFFEE].settings.delay_seconds == 45 * 60
        assert st3 == 303 and commands[-1][3] == {"OnOff": 1}
        assert health == 200
        svc.stop()
        await asyncio.wait_for(task, 5)
    asyncio.run(main())
    assert json.loads((tmp_path / "state.json").read_text())["plugs"][COFFEE]["overrides"]["delay_seconds"] == 2700.0
