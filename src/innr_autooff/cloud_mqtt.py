# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Cloud MQTT: short-lived command connections and a long-lived state feed.

Message formats from the proven prototype (setDevAttrReq / setDevAttrNotif,
topic scheme iot/v1/...). paho-mqtt runs its own network thread; results are
handed back to asyncio through call_soon_threadsafe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

from .protocol import decode_notification

log = logging.getLogger(__name__)


def parse_mqtt_host(raw: str) -> tuple[str, int, str, bool]:
    """Split mqttConfig 'host' into (hostname, port, ws path, tls). Verified value: 'global-eu-mqtt.arnoo.com:8443'."""
    raw = raw.strip()
    u = urlparse(raw if "://" in raw else f"wss://{raw}")
    use_tls = u.scheme not in ("ws", "mqtt")
    port = u.port or (443 if use_tls else 80)
    path = u.path if u.path not in ("", "/") else "/mqtt"
    return u.hostname or raw, port, path, use_tls


def _seq() -> str:
    return str(random.randint(0, 10**9 - 1)).zfill(9)


def _ts() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


@dataclass
class MqttParams:
    host: str
    port: int
    path: str
    tls: bool
    client_id: str
    username: str
    password: str
    keepalive: int


def make_client(p: MqttParams) -> mqtt.Client:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=p.client_id, transport="websockets",
                    protocol=mqtt.MQTTv311, clean_session=True)
    c.ws_set_options(path=p.path)
    if p.tls:
        c.tls_set_context(ssl.create_default_context())  # normal verification for the cloud (R5.8)
    c.username_pw_set(p.username, p.password)
    return c


def user_msg(uid: str, method: str) -> str:
    return json.dumps({"service": "user", "method": method, "seq": _seq(), "srcAddr": f"0.{uid}",
                       "payload": {"timestamp": _ts()}})


def set_attr_msg(uid: str, dev: str, gateway: str, dev_password: str, attr: dict) -> str:
    return json.dumps({
        "method": "setDevAttrReq", "service": "device", "seq": _seq(), "tst": int(time.time() * 1000),
        "payload": {"devId": dev, "parentId": gateway, "userId": uid, "password": dev_password, "attr": attr},
        "srcAddr": f"0.{uid}",
    })


def send_command_blocking(p: MqttParams, uid: str, dev: str, gateway: str, dev_password: str,
                          attr: dict, wait: float = 10.0, client_factory=make_client) -> bool:
    """Publish one setDevAttrReq; True when the cloud answers with setDevAttrResp or the
    matching notification. (Final confirmation is the engine's job, R2.6.)"""
    connected, done = threading.Event(), threading.Event()
    errors: list[str] = []

    def on_connect(c, userdata, flags, rc, props=None):
        if rc.is_failure:
            errors.append(f"connect refused: {rc}")
        connected.set()

    def on_message(c, userdata, msg):
        try:
            m = json.loads(msg.payload.decode())
        except Exception:
            return
        if m.get("method") == "setDevAttrResp":
            done.set()
        ev = decode_notification(msg.payload)
        if ev and ev.dev == dev and all(str(ev.attrs.get(k)) == str(v) for k, v in attr.items()):
            done.set()

    c = client_factory(p)
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(p.host, p.port, keepalive=p.keepalive)
    c.loop_start()
    try:
        if not connected.wait(15) or errors:
            raise ConnectionError(errors[0] if errors else "MQTT connect timeout")
        c.publish(f"iot/v1/cb/{uid}/user/connect", user_msg(uid, "connect"), qos=1)
        c.subscribe(f"iot/v1/c/{uid}/#", qos=1)
        c.subscribe(f"iot/v1/cb/{gateway}/#", qos=1)
        time.sleep(0.5)
        c.publish(f"iot/v1/c/{gateway}/device/setDevAttrReq",
                  set_attr_msg(uid, dev, gateway, dev_password, attr), qos=1)
        ok = done.wait(wait)
        c.publish(f"iot/v1/cb/{uid}/user/disconnect", user_msg(uid, "disconnect"), qos=1)
        return ok
    finally:
        c.loop_stop()
        try:
            c.disconnect()
        except Exception:
            pass


class CloudFeed:
    """Long-lived subscription delivering setDevAttrNotif (D2). The broker drops idle
    connections about every 30 s; we reconnect after 0.5 s with a fresh mqttConfig."""

    def __init__(self, get_params: Callable[[], tuple[MqttParams, str, list[str]]],
                 on_event: Callable[[str, dict, int | None], None],
                 on_status: Callable[[bool], None], loop: asyncio.AbstractEventLoop,
                 client_factory=make_client) -> None:
        self._get_params = get_params
        self._on_event = on_event
        self._on_status = on_status
        self._loop = loop
        self._factory = client_factory
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._idle = threading.Event()  # set while no feed connection exists
        self._idle.set()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cloud-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self, timeout: float = 5.0) -> bool:
        """Disconnect the feed so a command connection can use the same MQTT client id.
        (MQTT 3.1.1: the broker drops an existing client when another connects with its id.)"""
        self._paused.set()
        return self._idle.wait(timeout)

    def resume(self) -> None:
        self._paused.clear()

    def _emit(self, fn, *args) -> None:
        self._loop.call_soon_threadsafe(fn, *args)

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            while self._paused.is_set() and not self._stop.is_set():
                self._stop.wait(0.2)
            if self._stop.is_set():
                break
            try:
                p, uid, gateways = self._get_params()
                closed = threading.Event()

                def on_connect(c, userdata, flags, rc, props=None):
                    if rc.is_failure:
                        log.warning("event=cloud_feed_refused rc=%s", rc)
                        closed.set()
                        return
                    c.publish(f"iot/v1/cb/{uid}/user/connect", user_msg(uid, "connect"), qos=1)
                    c.subscribe(f"iot/v1/c/{uid}/#", qos=1)
                    for g in gateways:
                        c.subscribe(f"iot/v1/cb/{g}/#", qos=1)
                    self._emit(self._on_status, True)

                def on_message(c, userdata, msg):
                    ev = decode_notification(msg.payload)
                    if ev:
                        self._emit(self._on_event, ev.dev, ev.attrs, ev.idx)

                def on_disconnect(c, userdata, flags, rc, props=None):
                    closed.set()

                c = self._factory(p)
                c.on_connect, c.on_message, c.on_disconnect = on_connect, on_message, on_disconnect
                self._idle.clear()
                try:
                    c.connect(p.host, p.port, keepalive=p.keepalive)
                    c.loop_start()
                    started = time.monotonic()
                    while not self._stop.is_set() and not self._paused.is_set() and not closed.wait(0.2):
                        pass
                    c.loop_stop()
                    try:
                        c.disconnect()
                    except Exception:
                        pass
                finally:
                    self._idle.set()
                self._emit(self._on_status, False)
                if self._paused.is_set():
                    backoff = 0.5
                    continue
                backoff = 0.5 if time.monotonic() - started > 10 else min(backoff * 2, 60.0)
            except Exception as e:  # network, auth, config; keep going
                log.warning("event=cloud_feed_error error=%s", type(e).__name__)
                self._emit(self._on_status, False)
                backoff = min(max(backoff * 2, 2.0), 60.0)
            self._stop.wait(backoff)
