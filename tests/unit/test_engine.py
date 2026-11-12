# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Engine state machine (design 4.2) with a fake clock. Covers R1.6, R1.7, R2, R3, R10."""
import pytest

from innr_autooff.engine import GONE_KEEP_SECONDS, Engine, State
from innr_autooff.models import Log, Observation, Persist, PlugInfo, PlugSettings, SendOff

A = "a" * 32
B = "b" * 32
GW = "f" * 32
WALL0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.mono, self.wall = 1000.0, WALL0

    def adv(self, s):
        self.mono += s
        self.wall += s


def info(dev, onoff=0, name=None):
    return PlugInfo(dev, name or dev[:4], "Innr.SP 240", GW, True, onoff, None)


def make(delay=30.0, enabled=True, seeds=None, **kw):
    seeds = seeds if seeds is not None else {A: PlugSettings(enabled, delay), B: PlugSettings(True, 60.0)}
    return Engine(defaults=PlugSettings(True, 1800.0), seeds=seeds, **kw)


def sends(actions):
    return [a for a in actions if isinstance(a, SendOff)]


def events(actions):
    return [a.event for a in actions if isinstance(a, Log)]


def obs(dev, v, src="local", at=None, clock=None):
    return Observation(dev, v, src, at if at is not None else clock.mono)


@pytest.fixture
def c():
    return Clock()


def started(c, e=None, devs=(A,), onoff=0):
    e = e or make()
    e.sync_plugs({d: info(d, onoff) for d in devs}, c.mono, c.wall)
    return e


def run_until(e, c, seconds, step=1.0):
    out = []
    t = 0.0
    while t < seconds:
        c.adv(step)
        t += step
        out += e.tick(c.mono, c.wall)
    return out


# ---------------------------------------------------------------- basic timer
def test_on_then_expiry_sends_one_off(c):
    e = started(c)
    acts = e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    assert "timer_started" in events(acts)
    assert sends(run_until(e, c, 29)) == []
    out = run_until(e, c, 2)
    assert sends(out) == [SendOff(A, 1)]
    assert e.plugs[A].state == State.SWITCHING_OFF


def test_confirmation_by_off_observation(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    run_until(e, c, 31)
    acts = e.observe(obs(A, 0, clock=c), c.mono, c.wall)
    assert "command_confirmed" in events(acts)
    assert e.plugs[A].state == State.OFF and e.plugs[A].off_failed is False
    assert sends(run_until(e, c, 120)) == []


def test_manual_off_cancels_timer(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    c.adv(10)
    acts = e.observe(obs(A, 0, clock=c), c.mono, c.wall)
    assert "timer_cancelled" in events(acts)
    assert sends(run_until(e, c, 100)) == []


def test_repeated_on_does_not_restart_timer(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    c.adv(20)
    assert e.observe(obs(A, 1, "cloud", clock=c), c.mono, c.wall) == []
    out = run_until(e, c, 11)
    assert sends(out) == [SendOff(A, 1)]  # original deadline kept


def test_duplicate_event_from_two_sources_is_one_transition(c):
    e = started(c)
    a1 = e.observe(obs(A, 1, "local", clock=c), c.mono, c.wall)
    a2 = e.observe(obs(A, 1, "cloud", clock=c), c.mono, c.wall)
    assert events(a1).count("timer_started") == 1 and a2 == []


def test_on_off_on_starts_fresh_timer(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    c.adv(20)
    e.observe(obs(A, 0, clock=c), c.mono, c.wall)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    assert sends(run_until(e, c, 29)) == []
    assert sends(run_until(e, c, 2)) == [SendOff(A, 1)]


def test_independent_plugs(c):
    e = started(c, devs=(A, B))
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    e.observe(obs(B, 1, clock=c), c.mono, c.wall)
    out = run_until(e, c, 31)
    assert sends(out) == [SendOff(A, 1)]
    e.observe(obs(A, 0, clock=c), c.mono, c.wall)  # A confirmed OFF
    out = run_until(e, c, 30)
    assert sends(out) == [SendOff(B, 1)]
    assert e.plugs[A].state == State.OFF and e.plugs[B].state == State.SWITCHING_OFF


def test_disabled_plug_is_never_switched(c):
    e = started(c, make(enabled=False))
    acts = e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    assert "plug_on" in events(acts)
    assert sends(run_until(e, c, 3600, step=10)) == []


# ---------------------------------------------------------------- retries
def test_failing_commands_retry_with_growing_gaps_and_never_restart_the_timer(c):
    """R2.7 / R2.5: overdue stays overdue; no countdown ever comes back (replaces the flat 60 s retry)."""
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    t0 = c.mono
    sent_at = []
    for _ in range(3000):
        c.adv(1.0)
        for a in e.tick(c.mono, c.wall):
            if isinstance(a, SendOff):
                sent_at.append(int(c.mono - t0))
                e.command_result(A, False, c.mono, "ConnectionError", a.attempt)
        p = e.plugs[A]
        if c.mono - t0 >= 30:  # once the timer has expired the plug stays overdue: never a countdown again
            assert p.state == State.SWITCHING_OFF
            assert p.remaining(c.mono) is None and p.deadline is None
    gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
    assert sent_at[0] == 30 and gaps[:3] == [5, 5, 5]
    assert gaps[3:6] == [10, 10, 10] and gaps[6:9] == [20, 20, 20] and gaps[9:12] == [30, 30, 30]
    assert gaps[12:15] == [60, 60, 60] and set(gaps[15:]) == {120}
    assert e.plugs[A].off_failed is True


def test_failed_ack_retries_after_spacing(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    run_until(e, c, 31)
    acts = e.command_result(A, False, c.mono, "network down")
    assert "command_failed" in events(acts)
    assert sends(run_until(e, c, 5 - 1)) == []
    assert sends(run_until(e, c, 2)) == [SendOff(A, 2)]


def test_off_arriving_during_retry_wait_ends_switching(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    run_until(e, c, 31)
    e.command_result(A, False, c.mono)
    e.observe(obs(A, 0, "cloud", clock=c), c.mono, c.wall)
    assert e.plugs[A].state == State.OFF
    assert sends(run_until(e, c, 120)) == []


def test_manual_on_after_failure_clears_flag(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    for _ in range(80):
        c.adv(1.0)
        for a in e.tick(c.mono, c.wall):
            if isinstance(a, SendOff):
                e.command_result(A, False, c.mono, "down", a.attempt)
    assert e.plugs[A].off_failed
    e.observe(obs(A, 0, clock=c), c.mono, c.wall)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    p = e.plugs[A]
    assert not p.off_failed and p.attempt == 0 and p.state == State.ON


# ---------------------------------------------------------------- settings change
def test_delay_change_recomputes_from_on_since(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    c.adv(10)
    e.set_settings(A, c.mono, delay_seconds=15)
    assert sends(run_until(e, c, 4)) == []
    assert sends(run_until(e, c, 2)) == [SendOff(A, 1)]


def test_delay_shortened_below_elapsed_triggers_immediately(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    c.adv(20)
    e.set_settings(A, c.mono, delay_seconds=5)
    assert sends(e.tick(c.mono, c.wall)) == [SendOff(A, 1)]


def test_disable_cancels_and_enable_restarts_from_on_since(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    c.adv(5)
    acts = e.set_settings(A, c.mono, enabled=False)
    assert "timer_cancelled" in events(acts) and Persist() in acts
    assert sends(run_until(e, c, 60)) == []
    e.set_settings(A, c.mono, enabled=True)  # 65 s elapsed > 30 s delay
    assert sends(e.tick(c.mono, c.wall)) == [SendOff(A, 1)]


def test_invalid_delay_rejected(c):
    e = started(c)
    with pytest.raises(ValueError):
        e.set_settings(A, c.mono, delay_seconds=0)
    with pytest.raises(ValueError):
        e.set_settings(A, c.mono, delay_seconds=86401)


def test_override_beats_seed_beats_default():
    e = make(seeds={A: PlugSettings(False, 100.0)})
    assert e.effective_settings(A).enabled is False
    e.overrides[A] = {"enabled": True}
    s = e.effective_settings(A)
    assert s.enabled is True and s.delay_seconds == 100.0
    assert e.effective_settings(B).enabled is False  # new plug default: monitor-only (R10.3)
    assert e.effective_settings(B).delay_seconds == 1800.0


# ---------------------------------------------------------------- REST reconcile
def test_rest_observation_older_than_push_is_ignored(c):
    e = started(c)
    sent_at = c.mono
    c.adv(1)
    e.observe(obs(A, 1, "local", clock=c), c.mono, c.wall)  # push after REST request was sent
    acts = e.observe(Observation(A, 0, "rest", sent_at), c.mono, c.wall)
    assert events(acts) == ["rest_ignored"] and e.plugs[A].is_on


def test_rest_observation_catches_missed_on_after_two_sightings(c):
    e = started(c)
    c.adv(400)  # no push for a long time
    first = c.mono
    acts = e.observe(Observation(A, 1, "rest", c.mono), c.mono, c.wall)
    assert "rest_pending" in events(acts) and "timer_started" not in events(acts)
    assert not e.plugs[A].is_on
    c.adv(60)
    acts = e.observe(Observation(A, 1, "rest", c.mono), c.mono, c.wall)
    assert "reconcile_diff" in events(acts) and "timer_started" in events(acts)
    assert e.plugs[A].deadline == first + 30  # counted from the first sighting


# ---------------------------------------------------------------- startup / restart policy
def test_startup_off_plug():
    c = Clock()
    e = started(c, onoff=0)
    assert e.plugs[A].state == State.OFF


def test_startup_on_without_record_gets_full_timer():
    c = Clock()
    e = started(c, onoff=1)
    assert sends(run_until(e, c, 29)) == []
    assert sends(run_until(e, c, 2)) == [SendOff(A, 1)]


def test_startup_resume_keeps_original_on_since():
    c = Clock()
    e = make()
    e.load_state({"plugs": {A: {"state": "on", "on_since_utc": c.wall - 20}}})
    started(c, e, onoff=1)
    assert sends(run_until(e, c, 9)) == []
    assert sends(run_until(e, c, 2)) == [SendOff(A, 1)]


def test_startup_resume_expired_deadline_switches_immediately():
    c = Clock()
    e = make()
    e.load_state({"plugs": {A: {"state": "on", "on_since_utc": c.wall - 3600}}})
    started(c, e, onoff=1)
    assert sends(e.tick(c.mono, c.wall)) == [SendOff(A, 1)]


def test_startup_restart_policy_gives_fresh_timer():
    c = Clock()
    e = make(restart_policy="restart")
    e.load_state({"plugs": {A: {"state": "on", "on_since_utc": c.wall - 3600}}})
    started(c, e, onoff=1)
    assert sends(e.tick(c.mono, c.wall)) == []
    assert sends(run_until(e, c, 31)) == [SendOff(A, 1)]


def test_startup_record_in_future_is_ignored():
    c = Clock()
    e = make()
    e.load_state({"plugs": {A: {"state": "on", "on_since_utc": c.wall + 9999}}})
    started(c, e, onoff=1)
    assert sends(run_until(e, c, 29)) == []


def test_load_state_ignores_bad_overrides():
    e = make()
    e.load_state({"plugs": {A: {"overrides": {"enabled": "yes", "delay_seconds": -5}},
                            B: {"overrides": {"enabled": False, "delay_seconds": 120}}, "x": 3}})
    assert A not in e.overrides
    assert e.overrides[B] == {"enabled": False, "delay_seconds": 120.0}


def test_state_roundtrip(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    e.set_settings(A, c.mono, delay_seconds=99)
    st = e.to_state()
    e2 = make()
    e2.load_state(st)
    assert e2.overrides[A]["delay_seconds"] == 99.0
    assert e2.restored[A]["state"] == "on"


def test_monotonic_timing_ignores_wall_clock_jump(c):
    e = started(c)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    c.wall += 10_000  # wall clock jumps forward
    assert sends(e.tick(c.mono, c.wall)) == []


# ---------------------------------------------------------------- discovery
def test_new_plug_discovered_monitor_only(c):
    e = make(seeds={})
    acts = e.sync_plugs({A: info(A, 0)}, c.mono, c.wall)
    assert "plug_discovered" in events(acts)
    assert e.plugs[A].settings.enabled is False
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    assert sends(run_until(e, c, 4000, step=10)) == []


def test_new_plug_default_can_be_enabled(c):
    e = make(seeds={}, new_plug_auto_off=True)
    e.sync_plugs({A: info(A, 0)}, c.mono, c.wall)
    assert e.plugs[A].settings.enabled is True


def test_plug_missing_cancels_timer_and_returns(c):
    e = started(c, devs=(A, B))
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    acts = e.sync_plugs({B: info(B)}, c.mono, c.wall)
    assert "plug_missing" in events(acts)
    assert e.observe(obs(A, 0, clock=c), c.mono, c.wall) == []  # ignored while gone
    assert sends(run_until(e, c, 60)) == []
    acts = e.sync_plugs({A: info(A), B: info(B)}, c.mono, c.wall)
    assert "plug_returned" in events(acts)


def test_plug_forgotten_after_30_days(c):
    e = started(c, devs=(A, B))
    e.set_settings(A, c.mono, delay_seconds=50)
    e.sync_plugs({B: info(B)}, c.mono, c.wall)
    c.wall += GONE_KEEP_SECONDS + 1
    acts = e.sync_plugs({B: info(B)}, c.mono, c.wall)
    assert "plug_forgotten" in events(acts)
    assert A not in e.plugs and A not in e.overrides


def test_rename_in_cloud_is_applied(c):
    e = started(c)
    e.sync_plugs({A: info(A, name="Kitchen")}, c.mono, c.wall)
    assert e.plugs[A].name == "Kitchen"


def test_unknown_device_observation_ignored(c):
    e = started(c)
    assert e.observe(obs("c" * 32, 1, clock=c), c.mono, c.wall) == []


def test_sources_health(c):
    e = make()
    assert e.sources_healthy(c.mono) is False
    e.source_alive("local", c.mono)
    assert e.sources_healthy(c.mono)
    c.adv(121)
    assert e.sources_healthy(c.mono) is False


def test_expiry_while_stale_still_sends_off(c):
    """D7 / R2.5: bias to OFF when the state is uncertain."""
    e = started(c)
    e.source_alive("local", c.mono)
    e.observe(obs(A, 1, clock=c), c.mono, c.wall)
    out = run_until(e, c, 31)
    assert sends(out) == [SendOff(A, 1)]
