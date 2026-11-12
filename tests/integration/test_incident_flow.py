# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Service-level tests for the 2026-11-12 incident fixes and the separated logs (R1.10, R2.5 to R2.12, R12).

They run the whole Service against fakes (same helpers as test_service.py); engine constants are shortened
with monkeypatch so the scenarios take seconds instead of minutes.
"""
import asyncio
import json
import re

import pytest

from innr_autooff import engine as eng
from innr_autooff import logging_util as lu
from test_service import COFFEE, DEV_PW, FakeLocal, GW, dev, make_service, until


@pytest.fixture(autouse=True)
def health_file(tmp_path, monkeypatch):
    monkeypatch.setenv("INNR_HEALTH_FILE", str(tmp_path / "health"))
    import innr_autooff.app as app_mod
    monkeypatch.setattr(app_mod, "HEALTH_FILE", tmp_path / "health")


def seeds(delay):
    return [{"dev_id": COFFEE, "name": "Coffee machine", "delay_seconds": delay}]


def read(path):
    return path.read_text(encoding="utf-8")


def test_incident_replay_stale_cloud_snapshot_does_not_restart_the_timer(tmp_path):
    """10:16:28 the bridge says OFF, 10:16:45 the cloud still says ON: no new timer, no phantom OFF commands."""
    lu.setup_logging("DEBUG", tmp_path / "logs")
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee")], seeds(1))

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: svc.ready and FakeLocal.last is not None)
        FakeLocal.last.on_event(COFFEE, {"OnOff": 1}, None)
        assert await until(lambda: svc.engine.plugs[COFFEE].state.value == "off", 8)  # auto-off confirmed by the echo
        assert len(commands) == 1
        api.list[0]["properties"]["OnOff"] = "1"              # the cloud still shows the old state
        for _ in range(3):                                    # three cloud checks in a row
            assert await svc.reconcile_once("test")
            assert svc.engine.plugs[COFFEE].state.value == "off"
            assert svc.engine.plugs[COFFEE].deadline is None
        await asyncio.sleep(2.5)                              # longer than the 1 s delay would need
        assert len(commands) == 1, "a phantom timer sent another OFF command"
        svc.stop()
        await asyncio.wait_for(task, 5)

    try:
        asyncio.run(main())
        dec = read(tmp_path / "logs" / "decisions.log")
        assert dec.count("event=rest_ignored") == 3 and "reason=within_push_grace" in dec
        assert dec.count("event=timer_started") == 1
        sig = read(tmp_path / "logs" / "signals.log")
        assert "event=rest_snapshot" in sig and "snapshot=1 known=0" in sig  # the lie is visible as a signal
    finally:
        lu.teardown_logging()


def test_accepted_command_to_an_off_plug_is_verified_then_assumed_off(tmp_path, monkeypatch):
    """10:18:46: the cloud accepts the OFF command, the plug was already OFF, nothing to confirm."""
    monkeypatch.setattr(eng, "CONFIRM_TIMEOUT", 0.6)
    monkeypatch.setattr(eng, "REST_VERIFY_MIN_AFTER_ACK", 0.1)
    lu.setup_logging("DEBUG", tmp_path / "logs")
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee")], seeds(1), bridge_echo=False, cmd_ok=True)

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: svc.ready and FakeLocal.last is not None)
        FakeLocal.last.on_event(COFFEE, {"OnOff": 1}, None)   # a (phantom) ON: the plug is really OFF in the cloud
        assert await until(lambda: svc.engine.plugs[COFFEE].state.value == "off", 10)
        p = svc.engine.plugs[COFFEE]
        assert "already OFF" in p.last_result and p.attempt == 0 and not p.off_failed
        assert len(commands) == 1, "one accepted command is enough; no blind retries"
        svc.stop()
        await asyncio.wait_for(task, 5)

    try:
        asyncio.run(main())
        dec = read(tmp_path / "logs" / "decisions.log")
        for ev in ("timer_expired", "send_off", "command_unconfirmed", "verify_ordered", "assumed_off"):
            assert f"event={ev}" in dec, ev
        assert "auto_off_failed" not in dec and "event=command_failed" not in dec
    finally:
        lu.teardown_logging()


def test_failing_cloud_retries_with_growing_gaps_then_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(eng, "RETRY_GAPS", (1.0, 3.0))
    monkeypatch.setattr(eng, "ATTEMPTS_PER_GAP", 2)
    lu.setup_logging("DEBUG", tmp_path / "logs")
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee")], seeds(1))
    mode = {"fail": True}

    def command(params, uid, dev_id, gateway, dev_password, attr):
        commands.append((dev_id, gateway, dev_password, attr, params.password))
        if mode["fail"]:
            raise ConnectionError("connect to global-eu-mqtt.arnoo.com failed")
        svc.loop.call_soon_threadsafe(FakeLocal.last.on_event, dev_id, dict(attr), None)
        return True

    svc._command_fn = command

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: svc.ready and FakeLocal.last is not None)
        FakeLocal.last.on_event(COFFEE, {"OnOff": 1}, None)
        assert await until(lambda: len(commands) >= 4, 20)
        snap = svc.snapshot()["plugs"][0]
        assert snap["switching"] and snap["attempt"] >= 3 and snap["remaining"] is None
        assert "ConnectionError" in snap["last_error"] and snap["phase"] in ("retry_wait", "sending")
        mode["fail"] = False                                     # the cloud is back
        assert await until(lambda: svc.engine.plugs[COFFEE].state.value == "off", 12)
        p = svc.engine.plugs[COFFEE]
        assert p.attempt == 0 and not p.off_failed and p.last_result == "auto-off confirmed"
        svc.stop()
        await asyncio.wait_for(task, 5)

    try:
        asyncio.run(main())
        sig = read(tmp_path / "logs" / "signals.log")
        dec = read(tmp_path / "logs" / "decisions.log")
        assert "event=command_ack" in sig
        assert 'reason="ConnectionError: connect to global-eu-mqtt.arnoo.com failed"' in sig
        gaps = [int(g) for g in re.findall(r"event=command_failed .*?retry_in=(\d+)", dec)]
        assert gaps[:3] == [1, 1, 3], gaps                       # the engine's decision: growing gaps
        assert "event=auto_off_failed" in dec and dec.count("event=auto_off_failed") == 1
        assert dec.count("event=send_off") >= 5 and "event=command_confirmed" in dec
        assert dec.count("event=timer_started") == 1             # failures never restarted the timer
    finally:
        lu.teardown_logging()


def test_signals_and_decisions_are_separate_complete_and_secret_free(tmp_path):
    lu.setup_logging("INFO", tmp_path / "logs")
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee")], seeds(1))

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: svc.ready and FakeLocal.last is not None)
        FakeLocal.last.on_event(COFFEE, {"Watts": 407.0, "Current": 1.5}, 11)     # attribute-only signal
        FakeLocal.last.on_event(COFFEE, {"OnOff": 1}, 12)
        assert await until(lambda: svc.engine.plugs[COFFEE].state.value == "off", 8)
        FakeLocal.last.on_event("c" * 32, {"OnOff": 1}, 13)                     # unknown device
        await asyncio.sleep(0.2)
        svc.stop()
        await asyncio.wait_for(task, 5)

    try:
        asyncio.run(main())
        sig = read(tmp_path / "logs" / "signals.log").splitlines()
        dec = read(tmp_path / "logs" / "decisions.log").splitlines()
        assert sig and dec
        assert all("kind=signal" in l for l in sig) and all("kind=decision" in l for l in dec)
        sigtxt, dectxt = "\n".join(sig), "\n".join(dec)
        # signals: what came in
        assert 'event=push source=local plug="Coffee machine" onoff=1 idx=12' in sigtxt
        assert 'attrs="Watts=407.0,Current=1.5"' in sigtxt and "idx=11" in sigtxt
        assert "known=False" in sigtxt                                         # unknown device id
        assert "event=rest_snapshot" in sigtxt and "event=command_ack" in sigtxt and "ok=True" in sigtxt
        assert "event=source_status" in sigtxt
        # decisions: what was chosen, with reasons
        for ev in ("plug_discovered", "timer_started", "timer_expired", "send_off", "command_confirmed"):
            assert f"event={ev}" in dectxt, ev
        assert "reason=\"timer expired\"" in dectxt
        # nothing crosses over, nothing secret
        assert "event=push" not in dectxt and "event=timer_started" not in sigtxt
        for secret in list(DEV_PW.values()) + ["MQTT-PW-xyz"]:
            assert secret not in sigtxt and secret not in dectxt
        state = (tmp_path / "state.json").read_text()
        assert all(secret not in state for secret in DEV_PW.values())
    finally:
        lu.teardown_logging()


def test_cloud_error_reason_is_logged_and_scrubbed(tmp_path):
    lu.setup_logging("DEBUG", tmp_path / "logs")
    svc, api, commands = make_service(tmp_path, [dev(COFFEE, "Coffee")], seeds(60))
    lu.register_secret("ACCESS-TOKEN-LEAK-1234")

    async def main():
        task = asyncio.create_task(svc.run())
        assert await until(lambda: svc.ready)
        original = api.devices

        def broken():
            raise OSError("<urlopen error [Errno -3] Temporary failure in name resolution> token=ACCESS-TOKEN-LEAK-1234")
        api.devices = broken
        assert await svc.reconcile_once("test") is False
        assert svc.health.cloud_error.startswith("OSError: <urlopen error")
        assert "ACCESS-TOKEN-LEAK" not in svc.health.cloud_error  # the text shown in the UI is scrubbed too
        api.devices = original
        svc.stop()
        await asyncio.wait_for(task, 5)

    try:
        asyncio.run(main())
        sig = read(tmp_path / "logs" / "signals.log")
        assert "event=cloud_error" in sig and "call=devices" in sig and "trigger=test" in sig
        assert "Temporary failure in name resolution" in sig
        assert "ACCESS-TOKEN-LEAK-1234" not in sig
    finally:
        lu.teardown_logging()
