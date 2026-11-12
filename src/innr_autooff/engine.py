# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Pure auto-off state machine (design section 4). No I/O, no clock access.

Every public method takes `now` (monotonic seconds) and returns a list of
actions for the caller to execute. Wall-clock time (`wall`, UTC epoch seconds)
is only used for persistence (R3.7). All Log actions are *decisions* (R12): each
carries the reason; the matching *signals* are logged by the adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Action, Log, Observation, Persist, PlugInfo, PlugSettings, SendOff, VerifyState

CONFIRM_TIMEOUT = 15.0            # R2.6: OFF push expected this long after the cloud accepted the command
COMMAND_TIMEOUT = 60.0            # R2.6: no result at all from a command
VERIFY_TIMEOUT = 30.0             # R2.11: a fresh snapshot must arrive within this
REST_VERIFY_MIN_AFTER_ACK = 10.0  # R2.11: the snapshot must be requested this long after the acceptance
RETRY_GAPS = (5.0, 10.0, 20.0, 30.0, 60.0, 120.0)  # R2.7, three attempts per step, last step repeats
ATTEMPTS_PER_GAP = 3
FAILED_FLAG_AFTER = 3             # R2.7: off_failed + ERROR after this many failed attempts
SOURCE_LOST_AFTER = 120.0         # R1.8
REST_GRACE = 180.0                # R1.7: a snapshot this close to a push is not believed
REST_MIN_SPACING = 30.0           # R1.10: second matching snapshot at least this much later
GONE_KEEP_SECONDS = 30 * 86400    # R10.5


def retry_gap(attempt: int) -> float:
    """Wait after failed attempt number `attempt` (1-based): 5 s x3, 10 s x3, 20 s x3, 30 s x3, 60 s x3, then 120 s."""
    idx = min((max(attempt, 1) - 1) // ATTEMPTS_PER_GAP, len(RETRY_GAPS) - 1)
    return RETRY_GAPS[idx]


class State(str, Enum):
    UNKNOWN = "unknown"
    OFF = "off"
    ON = "on"
    SWITCHING_OFF = "switching_off"   # shown as OVERDUE in the UI


def L(level: str, event: str, plug: "Plug | None" = None, **fields) -> Log:
    if plug is not None:
        fields = {"plug": plug.name, **fields}
    return Log(level, event, fields)


@dataclass
class Plug:
    dev: str
    name: str
    model: str
    gateway_id: str
    settings: PlugSettings
    state: State = State.UNKNOWN
    on_since_mono: float | None = None
    on_since_wall: float | None = None
    deadline: float | None = None          # monotonic; None = no timer
    # overdue / retry bookkeeping
    attempt: int = 0
    next_attempt_at: float | None = None
    cmd_deadline: float | None = None      # a command without any result by then has failed
    confirm_by: float | None = None        # set on cloud acceptance
    verify_by: float | None = None         # set when a fresh snapshot was ordered
    acked_at: float | None = None
    overdue_since: float | None = None
    off_failed: bool = False
    last_error: str = ""
    last_result: str = ""
    # source evidence
    last_push_at: float | None = None      # last local/cloud OnOff observation
    rest_pending_onoff: int | None = None  # R1.10: first sighting of a disagreeing snapshot
    rest_pending_first: float | None = None
    power_w: float | None = None
    online: bool = True
    gone_since_wall: float | None = None
    stale: bool = False

    @property
    def is_on(self) -> bool:
        return self.state in (State.ON, State.SWITCHING_OFF)

    def remaining(self, now: float) -> float | None:
        if self.deadline is None or self.state != State.ON:
            return None
        return max(0.0, self.deadline - now)

    def phase(self) -> str | None:
        """Sub-phase of an overdue plug: retry_wait | verifying | waiting_plug | sending."""
        if self.state != State.SWITCHING_OFF:
            return None
        if self.next_attempt_at is not None:
            return "retry_wait"
        if self.verify_by is not None:
            return "verifying"
        if self.confirm_by is not None:
            return "waiting_plug"
        return "sending"

    def next_in(self, now: float) -> float | None:
        return None if self.next_attempt_at is None else max(0.0, self.next_attempt_at - now)


@dataclass
class Engine:
    defaults: PlugSettings
    new_plug_auto_off: bool = False
    restart_policy: str = "resume"
    seeds: dict[str, PlugSettings] = field(default_factory=dict)       # from config.toml
    overrides: dict[str, dict] = field(default_factory=dict)           # runtime (UI), persisted
    plugs: dict[str, Plug] = field(default_factory=dict)
    source_seen: dict[str, float] = field(default_factory=dict)        # source -> monotonic
    restored: dict[str, dict] = field(default_factory=dict)            # from state file

    # ------------------------------------------------------------------ settings
    def effective_settings(self, dev: str) -> PlugSettings:
        base = self.seeds.get(dev)
        if base is None:
            base = PlugSettings(self.new_plug_auto_off, self.defaults.delay_seconds)
        s = PlugSettings(base.enabled, base.delay_seconds)
        o = self.overrides.get(dev, {})
        if "enabled" in o:
            s.enabled = bool(o["enabled"])
        if "delay_seconds" in o:
            s.delay_seconds = float(o["delay_seconds"])
        return s

    def set_settings(self, dev: str, now: float, *, enabled: bool | None = None,
                     delay_seconds: float | None = None) -> list[Action]:
        """Runtime change (Web UI). Recomputes a running timer (R2.9); an overdue plug is
        only affected by disabling auto-off (R2.12)."""
        plug = self.plugs.get(dev)
        if plug is None:
            return []
        o = self.overrides.setdefault(dev, {})
        if enabled is not None:
            o["enabled"] = bool(enabled)
        if delay_seconds is not None:
            if not 1 <= delay_seconds <= 86400:
                raise ValueError("delay_seconds out of range")
            o["delay_seconds"] = float(delay_seconds)
        plug.settings = self.effective_settings(dev)
        actions: list[Action] = [Persist(), L("info", "settings_changed", plug, enabled=plug.settings.enabled,
                                              delay=int(plug.settings.delay_seconds))]
        if plug.state == State.ON:
            if plug.settings.enabled and plug.on_since_mono is not None:
                plug.deadline = plug.on_since_mono + plug.settings.delay_seconds
                actions.append(L("info", "timer_recomputed", plug,
                                 remaining=int(max(0, plug.deadline - now)), reason="settings changed"))
            elif not plug.settings.enabled and plug.deadline is not None:
                plug.deadline = None
                actions.append(L("info", "timer_cancelled", plug, reason="auto-off disabled"))
        elif plug.state == State.SWITCHING_OFF and not plug.settings.enabled:
            attempts = plug.attempt
            self._clear_overdue(plug)
            plug.state = State.ON
            plug.deadline = None
            actions.append(L("info", "overdue_cancelled", plug, reason="auto-off disabled", attempts=attempts))
        return actions

    # ------------------------------------------------------------------ discovery
    def sync_plugs(self, infos: dict[str, PlugInfo], now: float, wall: float) -> list[Action]:
        """Apply a discovery result (R10.2, R10.5)."""
        actions: list[Action] = []
        changed = False
        for dev, info in infos.items():
            plug = self.plugs.get(dev)
            if plug is None:
                plug = Plug(dev=dev, name=info.name, model=info.model, gateway_id=info.gateway_id,
                            settings=self.effective_settings(dev), online=info.online)
                self.plugs[dev] = plug
                changed = True
                actions.append(L("info", "plug_discovered", plug, dev=dev, model=info.model,
                                 auto_off=plug.settings.enabled, delay=int(plug.settings.delay_seconds)))
                if info.onoff is not None:
                    actions += self._startup_state(plug, info.onoff, now, wall)
            else:
                if plug.gone_since_wall is not None:
                    actions.append(L("info", "plug_returned", plug))
                    plug.gone_since_wall = None
                    changed = True
                plug.name, plug.model, plug.gateway_id, plug.online = (
                    info.name, info.model, info.gateway_id, info.online)
            if info.power_w is not None:
                plug.power_w = info.power_w
        for dev, plug in list(self.plugs.items()):
            if dev in infos:
                continue
            if plug.gone_since_wall is None:
                plug.gone_since_wall = wall
                plug.deadline = None
                if plug.state == State.SWITCHING_OFF:
                    plug.state = State.ON
                self._clear_overdue(plug)
                plug.rest_pending_first = plug.rest_pending_onoff = None
                changed = True
                actions.append(L("warning", "plug_missing", plug, dev=dev, reason="not in the cloud device list"))
            elif wall - plug.gone_since_wall > GONE_KEEP_SECONDS:
                del self.plugs[dev]
                self.overrides.pop(dev, None)
                changed = True
                actions.append(L("info", "plug_forgotten", plug, dev=dev))
        if changed:
            actions.append(Persist())
        return actions

    def _startup_state(self, plug: Plug, onoff: int, now: float, wall: float) -> list[Action]:
        """First state for a plug: apply the restart policy (R3.2 to R3.5)."""
        if onoff == 0:
            plug.state = State.OFF
            return [Persist()]
        rec = self.restored.get(plug.dev, {})
        plug.state = State.ON
        on_wall = rec.get("on_since_utc") if rec.get("state") == "on" else None
        if self.restart_policy == "resume" and isinstance(on_wall, (int, float)) and on_wall <= wall:
            plug.on_since_wall = float(on_wall)
            plug.on_since_mono = now - (wall - float(on_wall))
            policy = "resumed"
        else:
            plug.on_since_wall, plug.on_since_mono = wall, now
            policy = "fresh"
        actions: list[Action] = [Persist()]
        if plug.settings.enabled:
            plug.deadline = plug.on_since_mono + plug.settings.delay_seconds
            actions.append(L("info", "startup_policy", plug, policy=policy,
                             remaining=int(max(0, plug.deadline - now))))
        return actions

    def has_plug(self, dev: str) -> bool:
        return dev in self.plugs

    # ------------------------------------------------------------------ observations
    def observe(self, obs: Observation, now: float, wall: float) -> list[Action]:
        plug = self.plugs.get(obs.dev)
        if plug is None or plug.gone_since_wall is not None:
            return []
        if obs.source == "rest":
            return self._observe_rest(plug, obs, now, wall)
        # push (local / cloud feed): strongest evidence, applied in arrival order
        plug.last_push_at = now
        plug.rest_pending_first = plug.rest_pending_onoff = None
        plug.stale = False
        if plug.state == State.UNKNOWN:
            acts = self._startup_state(plug, obs.onoff, now, wall)
            return acts + [L("info", "observation", plug, onoff=obs.onoff, source=obs.source)]
        if obs.onoff == 1:
            return self._on(plug, obs, now, wall)
        return self._off(plug, obs, now)

    def _observe_rest(self, plug: Plug, obs: Observation, now: float, wall: float) -> list[Action]:
        """Cloud snapshots are claims, not facts (R1.7, R1.10): the cloud lags the bridge."""
        plug.stale = False
        if plug.state == State.UNKNOWN:  # nothing better exists
            acts = self._startup_state(plug, obs.onoff, now, wall)
            return acts + [L("info", "observation", plug, onoff=obs.onoff, source="rest")]
        # answer to the verification question after an accepted OFF command (R2.11)
        if plug.state == State.SWITCHING_OFF and plug.verify_by is not None and plug.acked_at is not None:
            if obs.observed_at >= plug.acked_at + REST_VERIFY_MIN_AFTER_ACK:
                if obs.onoff == 0:
                    return self._assume_off(plug, obs, now)
                return self._attempt_failed(plug, now, "cloud still reports the plug ON after the command")
        known = 1 if plug.is_on else 0
        if obs.onoff == known:
            plug.rest_pending_first = plug.rest_pending_onoff = None
            return []
        if plug.last_push_at is not None and obs.observed_at - plug.last_push_at < REST_GRACE:
            plug.rest_pending_first = plug.rest_pending_onoff = None
            return [L("info", "rest_ignored", plug, snapshot=obs.onoff, known=known, reason="within_push_grace",
                      push_age=int(obs.observed_at - plug.last_push_at), grace=int(REST_GRACE))]
        if plug.rest_pending_onoff == obs.onoff and plug.rest_pending_first is not None:
            spacing = obs.observed_at - plug.rest_pending_first
            if spacing < REST_MIN_SPACING:
                return [L("debug", "rest_pending", plug, snapshot=obs.onoff, known=known,
                          reason="second sighting too soon", spacing=int(spacing))]
            first = plug.rest_pending_first
            plug.rest_pending_first = plug.rest_pending_onoff = None
            return self._accept_rest(plug, obs, first, now, wall)
        plug.rest_pending_onoff, plug.rest_pending_first = obs.onoff, obs.observed_at
        return [L("info", "rest_pending", plug, snapshot=obs.onoff, known=known,
                  reason="first sighting, waiting for a second matching snapshot")]

    def _accept_rest(self, plug: Plug, obs: Observation, first: float, now: float, wall: float) -> list[Action]:
        age = max(0.0, now - first)
        acts: list[Action] = [L("info", "rest_accepted", plug, snapshot=obs.onoff, since_first_sighting=int(age),
                                reason="two matching snapshots, no recent push")]
        if obs.onoff == 0:
            return acts + self._off(plug, obs, now)
        # a missed ON: count from the first snapshot that showed it
        plug.state = State.ON
        plug.on_since_mono, plug.on_since_wall = now - age, wall - age
        self._clear_overdue(plug)
        acts += [Persist(), L("info", "reconcile_diff", plug, onoff=1)]
        if plug.settings.enabled:
            plug.deadline = plug.on_since_mono + plug.settings.delay_seconds
            acts.append(L("info", "timer_started", plug, delay=int(plug.settings.delay_seconds), source="rest",
                          counted_from="first snapshot", remaining=int(max(0, plug.deadline - now))))
        else:
            plug.deadline = None
            acts.append(L("info", "plug_on", plug, source="rest", auto_off=False))
        return acts

    def _on(self, plug: Plug, obs: Observation, now: float, wall: float) -> list[Action]:
        if plug.is_on:
            return []  # R2.3: repeated ON changes nothing
        plug.state = State.ON
        plug.on_since_mono, plug.on_since_wall = now, wall
        self._clear_overdue(plug)
        actions: list[Action] = [Persist()]
        if plug.settings.enabled:
            plug.deadline = now + plug.settings.delay_seconds
            actions.append(L("info", "timer_started", plug, delay=int(plug.settings.delay_seconds),
                             source=obs.source, counted_from="push"))
        else:
            plug.deadline = None
            actions.append(L("info", "plug_on", plug, source=obs.source, auto_off=False))
        return actions

    def _off(self, plug: Plug, obs: Observation, now: float) -> list[Action]:
        if plug.state == State.OFF:
            return []
        was_switching = plug.state == State.SWITCHING_OFF
        attempts = plug.attempt
        had_timer = plug.deadline is not None
        plug.state = State.OFF
        plug.deadline = None
        plug.on_since_mono = plug.on_since_wall = None
        self._clear_overdue(plug)
        actions: list[Action] = [Persist()]
        if obs.source == "rest":
            actions.append(L("info", "reconcile_diff", plug, onoff=0))
        if was_switching:
            plug.last_result = "auto-off confirmed"
            actions.append(L("info", "command_confirmed", plug, source=obs.source, attempts=attempts))
        elif had_timer:
            actions.append(L("info", "timer_cancelled", plug, reason="switched off", source=obs.source))
        else:
            actions.append(L("info", "plug_off", plug, source=obs.source))
        return actions

    def _assume_off(self, plug: Plug, obs: Observation, now: float) -> list[Action]:
        """R2.11: the cloud accepted the command, no push followed, and a fresh snapshot says OFF."""
        attempts = plug.attempt
        plug.state = State.OFF
        plug.deadline = None
        plug.on_since_mono = plug.on_since_wall = None
        self._clear_overdue(plug)
        plug.last_result = "auto-off: plug was already OFF (assumed from cloud state)"
        return [Persist(), L("info", "assumed_off", plug, attempts=attempts,
                             reason="command accepted, no push, fresh snapshot shows OFF")]

    def _clear_overdue(self, plug: Plug) -> None:
        plug.attempt = 0
        plug.next_attempt_at = plug.cmd_deadline = plug.confirm_by = plug.verify_by = plug.acked_at = None
        plug.overdue_since = None
        plug.off_failed = False
        plug.last_error = ""

    # ------------------------------------------------------------------ commands
    def command_result(self, dev: str, ok: bool, now: float, detail: str = "",
                       attempt: int | None = None) -> list[Action]:
        """Result of a SendOff from the app: the cloud accepted it (ok) or not. The effect
        arrives as a push Observation (R2.6). Results of older attempts are ignored (R2.12)."""
        plug = self.plugs.get(dev)
        if plug is None or plug.state != State.SWITCHING_OFF:
            return []
        if (attempt is not None and attempt != plug.attempt) or plug.cmd_deadline is None:
            return [L("info", "command_result_stale", plug, attempt=attempt, current=plug.attempt, ok=ok,
                      reason="result of an attempt that is no longer current")]
        if ok:
            plug.cmd_deadline = None
            plug.acked_at = now
            plug.confirm_by = now + CONFIRM_TIMEOUT
            plug.last_result = f"OFF attempt {plug.attempt} accepted, waiting for the plug"
            return [L("debug", "command_acknowledged", plug, attempt=plug.attempt)]
        return self._attempt_failed(plug, now, detail or "command failed")

    def _attempt_failed(self, plug: Plug, now: float, reason: str) -> list[Action]:
        n = plug.attempt
        plug.cmd_deadline = plug.confirm_by = plug.verify_by = plug.acked_at = None
        gap = retry_gap(n)
        plug.next_attempt_at = now + gap
        plug.last_error = reason[:160]
        plug.last_result = f"OFF attempt {n} failed: {plug.last_error}"
        acts: list[Action] = [L("warning", "command_failed", plug, attempt=n, reason=plug.last_error,
                                retry_in=int(gap))]
        if n >= FAILED_FLAG_AFTER and not plug.off_failed:
            plug.off_failed = True
            acts.append(L("error", "auto_off_failed", plug, attempt=n, reason=plug.last_error,
                          note="keeps retrying with growing gaps until the plug is OFF"))
        return acts

    # ------------------------------------------------------------------ time
    def tick(self, now: float, wall: float) -> list[Action]:
        actions: list[Action] = []
        for plug in self.plugs.values():
            if plug.gone_since_wall is not None:
                continue
            if plug.state == State.ON and plug.deadline is not None and now >= plug.deadline:
                plug.state = State.SWITCHING_OFF
                plug.attempt = 0
                plug.deadline = None
                plug.overdue_since = now
                actions.append(L("info", "timer_expired", plug, stale=plug.stale))
                actions += self._send(plug, now, "timer expired")
            elif plug.state == State.SWITCHING_OFF:
                actions += self._tick_overdue(plug, now)
        # R1.8: all sources lost -> mark plugs stale (state kept, timers keep running)
        if self.source_seen and all(now - t > SOURCE_LOST_AFTER for t in self.source_seen.values()):
            for plug in self.plugs.values():
                if not plug.stale:
                    plug.stale = True
        return actions

    def _tick_overdue(self, plug: Plug, now: float) -> list[Action]:
        if plug.cmd_deadline is not None and now >= plug.cmd_deadline:
            return self._attempt_failed(plug, now, f"no result from the command after {int(COMMAND_TIMEOUT)} s")
        if plug.confirm_by is not None and now >= plug.confirm_by:
            plug.confirm_by = None
            plug.verify_by = now + VERIFY_TIMEOUT
            plug.last_result = f"OFF attempt {plug.attempt} accepted, plug did not report OFF; checking cloud state"
            return [L("info", "command_unconfirmed", plug, attempt=plug.attempt,
                      reason=f"no OFF push {int(CONFIRM_TIMEOUT)} s after the cloud accepted the command"),
                    L("info", "verify_ordered", plug, attempt=plug.attempt,
                      reason="the plug may already have been OFF; asking the cloud for a fresh snapshot"),
                    VerifyState(plug.dev)]
        if plug.verify_by is not None and now >= plug.verify_by:
            return self._attempt_failed(plug, now, "no fresh cloud snapshot arrived for verification")
        if plug.next_attempt_at is not None and now >= plug.next_attempt_at:
            return self._send(plug, now, "retry")
        return []

    def _send(self, plug: Plug, now: float, reason: str) -> list[Action]:
        plug.attempt += 1
        plug.next_attempt_at = plug.confirm_by = plug.verify_by = plug.acked_at = None
        plug.cmd_deadline = now + COMMAND_TIMEOUT
        plug.last_result = f"OFF attempt {plug.attempt} sent"
        return [SendOff(plug.dev, plug.attempt),
                L("info", "send_off", plug, attempt=plug.attempt, reason=reason,
                  overdue_for=int(now - (plug.overdue_since or now)))]

    # ------------------------------------------------------------------ sources
    def source_alive(self, source: str, now: float) -> None:
        self.source_seen[source] = now

    def sources_healthy(self, now: float) -> bool:
        return any(now - t <= SOURCE_LOST_AFTER for t in self.source_seen.values())

    # ------------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        plugs = {}
        for dev, p in self.plugs.items():
            plugs[dev] = {
                "name": p.name,
                "state": "on" if p.is_on else ("off" if p.state == State.OFF else "unknown"),
                "on_since_utc": p.on_since_wall,
                "gone_since_utc": p.gone_since_wall,
                "overrides": self.overrides.get(dev, {}),
            }
        for dev, o in self.overrides.items():  # keep overrides of not-yet-discovered plugs
            plugs.setdefault(dev, {"state": "unknown", "overrides": o})
        return {"plugs": plugs}

    def load_state(self, data: dict) -> None:
        for dev, rec in (data.get("plugs") or {}).items():
            if not isinstance(rec, dict):
                continue
            self.restored[dev] = rec
            o = rec.get("overrides")
            if isinstance(o, dict):
                clean = {}
                if isinstance(o.get("enabled"), bool):
                    clean["enabled"] = o["enabled"]
                d = o.get("delay_seconds")
                if isinstance(d, (int, float)) and 1 <= d <= 86400:
                    clean["delay_seconds"] = float(d)
                if clean:
                    self.overrides[dev] = clean
