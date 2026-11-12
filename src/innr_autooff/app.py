# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Wiring and supervision. The asyncio loop is the single owner of the engine;
other threads (Web UI, paho) only reach it through call_soon_threadsafe /
run_coroutine_threadsafe, so the engine needs no locks.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .cloud_mqtt import CloudFeed, MqttParams, parse_mqtt_host, send_command_blocking
from .config import Config, Credentials, require_client_profile
from .discovery import DEV_ID_RE, discover
from .engine import Engine, State
from .innr_api import AuthError, InnrApi
from .local_feed import LocalFeed
from .logging_util import describe_error, kv, log_event, log_signal
from .models import Log, Observation, Persist, SendOff, VerifyState
from .protocol import onoff_value, watts_value
from .store import StateStore

log = logging.getLogger("innr_autooff")
HEALTH_FILE = Path(os.environ.get("INNR_HEALTH_FILE", "/tmp/innr-autooff.health"))
TICK = 1.0
UNKNOWN_DEV_DISCOVERY_MIN_INTERVAL = 15.0


@dataclass
class Health:
    local_connected: bool = False
    cloud_feed_connected: bool = False
    cloud_login_ok: bool | None = None
    cloud_error: str = ""
    last_reconcile_wall: float | None = None
    started_wall: float = field(default_factory=time.time)


class Service:
    def __init__(self, cfg: Config, creds: Credentials, *, api: InnrApi | None = None,
                 command_fn: Callable[..., bool] | None = None, local_feed_factory=None,
                 cloud_feed_factory=None, clock=time.monotonic, wall=time.time) -> None:
        self.cfg = cfg
        self.clock, self.wall = clock, wall
        self.store = StateStore(cfg.data_dir / "state.json")
        state = self.store.load()
        self.engine = Engine(defaults=cfg.defaults, new_plug_auto_off=cfg.new_plug_auto_off,
                             restart_policy=cfg.restart_policy, seeds=dict(cfg.seeds))
        self.engine.load_state(state)
        if api is None:
            api = InnrApi(creds.email, creds.password, cfg.region, self.store.terminal_id(),
                          require_client_profile(cfg))
        self.api = api
        self._command_fn = command_fn or send_command_blocking
        self._local_factory = local_feed_factory
        self._cloud_factory = cloud_feed_factory
        self.health = Health()
        self.house_id = ""
        self._devices: dict[str, dict] = {}   # raw records incl. device passwords; never exposed
        self._cmd_lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._discover_now = asyncio.Event()
        self._last_unknown_discovery = -1e9
        self.loop: asyncio.AbstractEventLoop | None = None
        self.local: LocalFeed | None = None
        self.cloud_feed: CloudFeed | None = None
        self.pin = cfg.bridge_cert_sha256 or str(state.get("bridge_cert_sha256") or "")
        self.ready = False

    # ------------------------------------------------------------------ actions
    def _apply(self, actions: list) -> None:
        persist = False
        for a in actions:
            if isinstance(a, Log):
                log_event(log, a.level, a.event, a.kind, **a.fields)
            elif isinstance(a, Persist):
                persist = True
            elif isinstance(a, SendOff):
                asyncio.get_running_loop().create_task(self._send(a.dev, 0, a.attempt))
            elif isinstance(a, VerifyState):
                asyncio.get_running_loop().create_task(self.reconcile_once("verify"))
        if persist:
            self._persist()

    def _persist(self) -> None:
        try:
            self.store.save({**self.engine.to_state(), "bridge_cert_sha256": self.pin or None})
        except OSError as e:
            log.error(kv("state_save_failed", error=e.strerror))

    async def _send(self, dev: str, onoff: int, attempt: int = 0, manual: bool = False) -> bool:
        """Serialized cloud command (R4.1). Device passwords stay inside this method."""
        async with self._cmd_lock:
            rec = self._devices.get(dev)
            plug = self.engine.plugs.get(dev)
            if rec is None or plug is None or rec.get("type") != "plug":
                if not manual:
                    self._apply(self.engine.command_result(dev, False, self.clock(), "plug not known", attempt))
                return False
            ok, detail = False, ""
            started = self.clock()
            feed = self.cloud_feed
            try:
                if feed is not None:  # same MQTT client id: the feed must step aside (see CloudFeed.pause)
                    await asyncio.to_thread(feed.pause)
                cfg = await asyncio.to_thread(self.api.mqtt_config, self.house_id)
                params = self._params(cfg)
                ok = await asyncio.to_thread(self._command_fn, params, self.api.user_id, dev,
                                             plug.gateway_id, str(rec.get("password", "")), {"OnOff": onoff})
                detail = "" if ok else "no acknowledgement from the cloud"
            except Exception as e:  # network, auth, broker
                detail = describe_error(e)
            finally:
                if feed is not None:
                    feed.resume()
            log_signal(log, "command_ack", "info" if ok else "warning", plug=plug.name, onoff=onoff, ok=ok,
                       attempt=attempt or None, manual=True if manual else None,
                       seconds=round(self.clock() - started, 1), reason=detail or None)
            if manual:
                plug.last_result = f"manual {'ON' if onoff else 'OFF'} {'sent' if ok else 'failed'}"
            else:
                self._apply(self.engine.command_result(dev, ok, self.clock(), detail, attempt))
            return ok

    def _params(self, cfg: dict) -> MqttParams:
        host, port, path, tls = parse_mqtt_host(str(cfg["host"]))
        return MqttParams(host, port, path, tls, self.api.client_id(cfg), self.api.user_id,
                          str(cfg["password"]), int(cfg.get("heartbeat") or 45))

    # ------------------------------------------------------------------ events
    def on_push(self, source: str, dev: str, attrs: dict, idx: int | None) -> None:
        now = self.clock()
        self.engine.source_alive(source, now)
        if not DEV_ID_RE.match(dev):
            return
        plug = self.engine.plugs.get(dev)
        v0 = onoff_value(attrs)
        log_signal(log, "push", "info" if v0 is not None else "debug", source=source,
                   plug=plug.name if plug else dev[-8:], known=None if plug else False, onoff=v0,
                   attrs=None if v0 is not None and len(attrs) == 1 else
                   ",".join(f"{k}={attrs[k]}" for k in list(attrs)[:6] if k != "OnOff"), idx=idx)
        if not self.engine.has_plug(dev):
            if dev not in self._devices and now - self._last_unknown_discovery > UNKNOWN_DEV_DISCOVERY_MIN_INTERVAL:
                self._last_unknown_discovery = now
                self._discover_now.set()  # R10.4
            return
        w = watts_value(attrs)
        if w is not None:
            self.engine.plugs[dev].power_w = w
        v = onoff_value(attrs)
        if v is not None:
            self._apply(self.engine.observe(Observation(dev, v, source, now, idx), now, self.wall()))

    def _local_status(self, up: bool) -> None:
        if up != self.health.local_connected:
            log_signal(log, "source_status", "debug", source="local", connected=up)
        self.health.local_connected = up
        if up:
            self.engine.source_alive("local", self.clock())

    def _cloud_status(self, up: bool) -> None:
        if up != self.health.cloud_feed_connected:
            log_signal(log, "source_status", "debug", source="cloud_feed", connected=up)
        self.health.cloud_feed_connected = up
        if up:
            self.engine.source_alive("cloud", self.clock())

    def _new_pin(self, fp: str) -> None:
        self.pin = fp
        if self.loop:
            self.loop.call_soon_threadsafe(self._persist)

    # ------------------------------------------------------------------ reconcile + discovery
    async def reconcile_once(self, trigger: str = "poll") -> bool:
        async with self._reconcile_lock:
            return await self._reconcile(trigger)

    async def _reconcile(self, trigger: str) -> bool:
        sent_at = self.clock()
        try:
            house_id, devices = await asyncio.to_thread(self.api.devices)
        except AuthError as e:
            reason = describe_error(e)
            self.health.cloud_login_ok, self.health.cloud_error = False, self.api.last_error or reason
            log_signal(log, "cloud_error", "error", call="devices", kind_of="auth", trigger=trigger,
                       reason=f"{self.api.last_error or ''} {reason}".strip())
            return False
        except Exception as e:
            reason = describe_error(e)
            self.health.cloud_error = reason
            log_signal(log, "cloud_error", "warning", call="devices", trigger=trigger, reason=reason)
            return False
        self.health.cloud_login_ok, self.health.cloud_error = True, ""
        self.health.last_reconcile_wall = self.wall()
        self.house_id = house_id or self.house_id
        self._devices = {d["id"]: d for d in devices if isinstance(d.get("id"), str)}
        infos = discover(devices, self.cfg.seed_names)
        now = self.clock()
        self.engine.source_alive("rest", now)
        self._apply(self.engine.sync_plugs(infos, now, self.wall()))
        for dev, info in infos.items():
            plug = self.engine.plugs[dev]
            if info.onoff is None:
                continue
            known = None if plug.state == State.UNKNOWN else (1 if plug.is_on else 0)
            log_signal(log, "rest_snapshot", "debug" if known == info.onoff else "info", plug=plug.name,
                       snapshot=info.onoff, known=known, trigger=trigger)
            if plug.state != State.UNKNOWN:
                self._apply(self.engine.observe(Observation(dev, info.onoff, "rest", sent_at),
                                                self.clock(), self.wall()))
        self.ready = True
        return True

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._discover_now.wait(), self.cfg.reconcile_seconds)
            except asyncio.TimeoutError:
                pass
            self._discover_now.clear()
            if self._stop.is_set():
                break
            await self.reconcile_once()

    async def _tick_loop(self) -> None:
        health_file = HEALTH_FILE
        while not self._stop.is_set():
            self._apply(self.engine.tick(self.clock(), self.wall()))
            if self.healthy():
                try:
                    health_file.write_text(str(int(self.wall())))
                except OSError:
                    pass
            try:
                await asyncio.wait_for(self._stop.wait(), TICK)
            except asyncio.TimeoutError:
                pass

    def healthy(self) -> bool:
        return bool(self.ready and self.health.cloud_login_ok and self.engine.sources_healthy(self.clock()))

    # ------------------------------------------------------------------ public API for the Web UI
    def snapshot(self) -> dict[str, Any]:
        """Read-only view for the UI. Contains NO secrets by construction."""
        now = self.clock()
        plugs = []
        for p in sorted(self.engine.plugs.values(), key=lambda x: x.name.lower()):
            rem = p.remaining(now)
            plugs.append({
                "dev": p.dev, "name": p.name, "model": p.model,
                "state": "on" if p.is_on else p.state.value if p.state != State.SWITCHING_OFF else "on",
                "switching": p.state == State.SWITCHING_OFF,
                "enabled": p.settings.enabled, "delay_minutes": round(p.settings.delay_seconds / 60, 1),
                "remaining": None if rem is None else int(rem),
                "power_w": p.power_w, "off_failed": p.off_failed, "last_result": p.last_result,
                "gone": p.gone_since_wall is not None, "stale": p.stale, "online": p.online,
                "phase": p.phase(), "attempt": p.attempt, "next_in": None if p.next_in(now) is None else int(p.next_in(now)),
                "last_error": p.last_error,
            })
        h = self.health
        return {
            "plugs": plugs, "version": __version__,
            "local_connected": h.local_connected, "cloud_feed_connected": h.cloud_feed_connected,
            "cloud_feed_enabled": self.cfg.cloud_state_feed,
            "cloud_login_ok": h.cloud_login_ok, "cloud_error": h.cloud_error,
            "reconcile_age": None if h.last_reconcile_wall is None else int(self.wall() - h.last_reconcile_wall),
            "uptime": int(self.wall() - h.started_wall), "healthy": self.healthy(),
        }

    def set_plug_settings(self, dev: str, enabled: bool, delay_minutes: float) -> None:
        if dev not in self.engine.plugs:
            raise KeyError(dev)
        self._apply(self.engine.set_settings(dev, self.clock(), enabled=enabled, delay_seconds=delay_minutes * 60))

    async def request_switch(self, dev: str, onoff: int) -> bool:
        if dev not in self.engine.plugs or onoff not in (0, 1):
            raise KeyError(dev)
        return await self._send(dev, onoff, manual=True)

    # thread-safe wrappers used by the Web UI thread
    def call(self, fn, *args, timeout: float = 30.0):
        assert self.loop is not None

        async def _wrap():
            r = fn(*args)
            return await r if asyncio.iscoroutine(r) else r
        return asyncio.run_coroutine_threadsafe(_wrap(), self.loop).result(timeout)

    # ------------------------------------------------------------------ lifecycle
    def _cloud_params(self) -> tuple[MqttParams, str, list[str]]:
        cfg = self.api.mqtt_config(self.house_id)
        gateways = sorted({p.gateway_id for p in self.engine.plugs.values()})
        return self._params(cfg), self.api.user_id, gateways

    async def run(self, web_start: Callable[["Service"], Any] | None = None) -> None:
        self.loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        log.info(kv("startup", version=__version__, bridge=self.cfg.bridge_host, region=self.cfg.region,
                    restart_policy=self.cfg.restart_policy, cloud_feed=self.cfg.cloud_state_feed))
        web = web_start(self) if web_start else None
        # R3.2: read the cloud state before acting; retry until the cloud answers
        backoff = 5.0
        while not self._stop.is_set() and not await self.reconcile_once():
            try:
                await asyncio.wait_for(self._stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 300.0)
        if self._stop.is_set():
            return
        loop = self.loop
        self.local = (self._local_factory or LocalFeed)(
            self.cfg.bridge_host, self.cfg.bridge_port,
            lambda d, a, i: self.on_push("local", d, a, i), self._local_status,
            pin_sha256=self.pin, on_new_pin=self._new_pin)
        tasks = [asyncio.create_task(self.local.run(), name="local-feed"),
                 asyncio.create_task(self._reconcile_loop(), name="reconcile"),
                 asyncio.create_task(self._tick_loop(), name="tick")]
        if self.cfg.cloud_state_feed:
            self.cloud_feed = (self._cloud_factory or CloudFeed)(
                self._cloud_params, lambda d, a, i: self.on_push("cloud", d, a, i), self._cloud_status, loop)
            self.cloud_feed.start()
        await self._stop.wait()
        log.info(kv("shutdown"))
        if self.local:
            self.local.stop()
        if self.cloud_feed:
            self.cloud_feed.stop()
        if web:
            web.shutdown()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._persist()

    def stop(self) -> None:
        self._stop.set()
