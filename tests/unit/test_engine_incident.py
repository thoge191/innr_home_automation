# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Incident 2026-11-12 and the new retry / snapshot rules (spec R1.7, R1.10, R2.5 to R2.7, R2.11, R2.12).

Timeline of the incident (UTC, from the NAS log):
  09:56:26 ON (local)      timer 20 min
  10:16:27 timer expired   OFF sent
  10:16:28 OFF confirmed   by the local feed (1 s)
  10:16:45 REST says ON    -> engine started a NEW 20 min timer (the bug)
  10:18:46 timer (2 min)   3 acknowledged OFF commands to an already-OFF plug -> 'auto_off_failed' (the bug)
"""
import pytest

from innr_autooff.engine import (
    CONFIRM_TIMEOUT, COMMAND_TIMEOUT, REST_GRACE, REST_MIN_SPACING, REST_VERIFY_MIN_AFTER_ACK, VERIFY_TIMEOUT,
    Engine, State, retry_gap,
)
from innr_autooff.models import Log, Observation, PlugInfo, PlugSettings, SendOff, VerifyState

A = "a" * 32
GW = "f" * 32


class Clock:
    def __init__(self):
        self.mono, self.wall = 1000.0, 1_800_000_000.0

    def adv(self, s):
        self.mono += s
        self.wall += s


@pytest.fixture
def c():
    return Clock()


def make(delay=1200.0, **kw):
    e = Engine(defaults=PlugSettings(True, delay), seeds={A: PlugSettings(True, delay)}, **kw)
    return e


def boot(c, delay=1200.0, onoff=0):
    e = make(delay)
    e.sync_plugs({A: PlugInfo(A, "Coffee", "Innr.SP 240", GW, True, onoff, None)}, c.mono, c.wall)
    return e


def push(e, c, v, src="local"):
    return e.observe(Observation(A, v, src, c.mono), c.mono, c.wall)


def rest(e, c, v, taken_at=None):
    return e.observe(Observation(A, v, "rest", c.mono if taken_at is None else taken_at), c.mono, c.wall)


def names(actions):
    return [a.event for a in actions if isinstance(a, Log)]


def find(actions, event):
    return [a for a in actions if isinstance(a, Log) and a.event == event]


def sends(actions):
    return [a for a in actions if isinstance(a, SendOff)]


def step(e, c, seconds, cb=None):
    out = []
    for _ in range(int(seconds)):
        c.adv(1.0)
        acts = e.tick(c.mono, c.wall)
        out += acts
        if cb:
            for a in acts:
                cb(a)
    return out


# ================================================================== the incident, replayed
def test_incident_stale_snapshot_after_confirmed_off_is_ignored(c):
    e = boot(c, 1200)
    push(e, c, 1)                                    # 09:56:26 ON
    step(e, c, 1200)                                 # 10:16:27 timer expired, OFF sent
    assert e.plugs[A].state == State.SWITCHING_OFF
    c.adv(1)
    e.command_result(A, True, c.mono, "", 1)         # accepted
    acts = push(e, c, 0)                             # 10:16:28 local OFF
    assert "command_confirmed" in names(acts) and e.plugs[A].state == State.OFF
    c.adv(16)                                        # 10:16:44 the cloud still shows ON
    acts = rest(e, c, 1)
    assert names(acts) == ["rest_ignored"]
    assert find(acts, "rest_ignored")[0].fields["reason"] == "within_push_grace"
    p = e.plugs[A]
    assert p.state == State.OFF and p.deadline is None and "timer_started" not in names(acts)
    # the cloud keeps lagging for a while: still ignored (inside the grace window)
    for _ in range(2):
        c.adv(60)
        assert names(rest(e, c, 1)) == ["rest_ignored"]
    assert p.state == State.OFF and p.deadline is None
    assert sends(step(e, c, 2000)) == []             # no phantom timer, no phantom OFF command


def test_incident_command_to_already_off_plug_is_verified_not_failed(c):
    """What happened at 10:18:46: three acknowledged OFF commands, nothing to confirm."""
    e = boot(c, 120)
    push(e, c, 1)
    out = step(e, c, 120)
    assert [a.attempt for a in sends(out)] == [1]
    c.adv(1)
    e.command_result(A, True, c.mono, "", 1)         # the cloud accepts; the plug was already OFF: no push
    out = step(e, c, CONFIRM_TIMEOUT)
    assert any(isinstance(a, VerifyState) for a in out)
    assert "command_unconfirmed" in names(out) and "verify_ordered" in names(out)
    assert e.plugs[A].phase() == "verifying" and e.plugs[A].off_failed is False
    c.adv(2)                                         # the fresh snapshot arrives
    acts = rest(e, c, 0, taken_at=c.mono - 1)
    assert names(acts).count("assumed_off") == 1
    p = e.plugs[A]
    assert p.state == State.OFF and p.attempt == 0 and not p.off_failed
    assert "already OFF" in p.last_result
    assert sends(step(e, c, 600)) == []              # nothing more is sent


# ================================================================== snapshot trust (R1.7, R1.10)
def test_single_snapshot_never_starts_a_timer(c):
    e = boot(c, 600)
    c.adv(1000)
    acts = rest(e, c, 1)
    assert names(acts) == ["rest_pending"] and e.plugs[A].state == State.OFF
    assert sends(step(e, c, 5000)) == []


def test_two_matching_snapshots_accept_the_claim_and_count_from_the_first(c):
    e = boot(c, 600)
    c.adv(1000)
    first = c.mono
    rest(e, c, 1)
    c.adv(60)
    acts = rest(e, c, 1)
    assert names(acts) == ["rest_accepted", "reconcile_diff", "timer_started"]
    assert e.plugs[A].deadline == first + 600 and e.plugs[A].state == State.ON
    assert find(acts, "timer_started")[0].fields["counted_from"] == "first snapshot"


def test_second_snapshot_too_soon_does_not_count(c):
    e = boot(c, 600)
    c.adv(1000)
    rest(e, c, 1)
    c.adv(REST_MIN_SPACING - 5)  # e.g. an extra discovery reconcile
    acts = rest(e, c, 1)
    assert names(acts) == ["rest_pending"] and e.plugs[A].state == State.OFF
    c.adv(40)
    assert "rest_accepted" in names(rest(e, c, 1))


def test_pending_claim_is_discarded_by_push_or_agreeing_snapshot(c):
    e = boot(c, 600)
    c.adv(1000)
    rest(e, c, 1)
    c.adv(40)
    rest(e, c, 0)                                    # agrees with the known OFF: claim cleared
    assert e.plugs[A].rest_pending_first is None
    c.adv(40)
    assert names(rest(e, c, 1)) == ["rest_pending"]  # starts over
    c.adv(40)
    push(e, c, 0)                                    # a push clears it as well
    assert e.plugs[A].rest_pending_first is None


def test_snapshot_inside_grace_of_a_push_is_ignored_then_believed_after_grace(c):
    e = boot(c, 600)
    push(e, c, 0)
    c.adv(REST_GRACE - 1)
    assert names(rest(e, c, 1)) == ["rest_ignored"]
    c.adv(2)  # now 181 s after the push
    assert names(rest(e, c, 1)) == ["rest_pending"]
    c.adv(60)
    assert "rest_accepted" in names(rest(e, c, 1))


def test_stale_off_snapshot_cannot_cancel_a_running_timer(c):
    """Mirror image of the incident: the cloud lags a real ON; the timer must keep running."""
    e = boot(c, 600)
    push(e, c, 1)
    c.adv(17)
    assert names(rest(e, c, 0)) == ["rest_ignored"]
    p = e.plugs[A]
    assert p.state == State.ON and p.deadline is not None
    assert sends(step(e, c, 590)) == [SendOff(A, 1)]


def test_off_snapshot_accepted_only_after_two_sightings_and_no_recent_push(c):
    e = boot(c, 5000)
    push(e, c, 1)
    c.adv(REST_GRACE + 10)                           # the OFF push was missed by both feeds
    assert names(rest(e, c, 0)) == ["rest_pending"] and e.plugs[A].is_on
    c.adv(60)
    acts = rest(e, c, 0)
    assert "rest_accepted" in names(acts) and "timer_cancelled" in names(acts)
    assert e.plugs[A].state == State.OFF


def test_snapshot_taken_before_a_later_push_is_ignored(c):
    e = boot(c, 600)
    t_req = c.mono
    c.adv(1)
    push(e, c, 1)
    assert names(rest(e, c, 0, taken_at=t_req)) == ["rest_ignored"]


def test_unknown_state_accepts_a_snapshot_immediately(c):
    e = make()
    e.sync_plugs({A: PlugInfo(A, "Coffee", "m", GW, True, None, None)}, c.mono, c.wall)
    assert e.plugs[A].state == State.UNKNOWN
    acts = rest(e, c, 1)
    assert "observation" in names(acts) and e.plugs[A].state == State.ON


# ================================================================== escalating retries (R2.7)
def test_retry_gap_table():
    assert [retry_gap(n) for n in range(1, 19)] == [5, 5, 5, 10, 10, 10, 20, 20, 20, 30, 30, 30, 60, 60, 60, 120, 120, 120]
    assert retry_gap(0) == 5 and retry_gap(1000) == 120


def test_overdue_phases_and_no_countdown(c):
    e = boot(c, 30)
    push(e, c, 1)
    out = step(e, c, 30)
    p = e.plugs[A]
    assert sends(out) == [SendOff(A, 1)] and p.phase() == "sending" and p.remaining(c.mono) is None
    e.command_result(A, False, c.mono, "ConnectionError: boom", 1)
    assert p.phase() == "retry_wait" and p.next_in(c.mono) == 5 and p.attempt == 1
    assert "ConnectionError" in p.last_error
    step(e, c, 5)
    assert p.phase() == "sending" and p.attempt == 2
    e.command_result(A, True, c.mono, "", 2)
    assert p.phase() == "waiting_plug"


def test_first_failures_are_logged_and_flag_after_third(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    flagged = []
    for n in range(1, 4):
        acts = e.command_result(A, False, c.mono, "down", n)
        assert "command_failed" in names(acts)
        flagged += find(acts, "auto_off_failed")
        step(e, c, 5)
    assert len(flagged) == 1 and flagged[0].level == "error" and e.plugs[A].off_failed


def test_command_timeout_counts_as_failed_attempt(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    out = step(e, c, COMMAND_TIMEOUT + 1)
    assert "command_failed" in names(out)
    assert "no result from the command" in e.plugs[A].last_error
    assert sends(out) == [] and e.plugs[A].phase() == "retry_wait"


def test_late_result_of_an_older_attempt_is_ignored(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    e.command_result(A, False, c.mono, "down", 1)
    step(e, c, 5)                                    # attempt 2 in flight
    acts = e.command_result(A, True, c.mono, "", 1)  # late 'accepted' of attempt 1
    assert names(acts) == ["command_result_stale"]
    assert e.plugs[A].phase() == "sending" and e.plugs[A].acked_at is None
    acts = e.command_result(A, False, c.mono, "late failure", 1)
    assert names(acts) == ["command_result_stale"] and e.plugs[A].attempt == 2


def test_duplicate_result_for_the_same_attempt_is_ignored(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    e.command_result(A, False, c.mono, "down", 1)
    assert names(e.command_result(A, False, c.mono, "down again", 1)) == ["command_result_stale"]
    assert e.plugs[A].next_in(c.mono) == 5


def test_off_push_ends_overdue_in_every_phase(c):
    for phase_setup in ("sending", "retry_wait", "waiting_plug", "verifying"):
        c = Clock()
        e = boot(c, 30)
        push(e, c, 1)
        step(e, c, 30)
        if phase_setup == "retry_wait":
            e.command_result(A, False, c.mono, "x", 1)
        elif phase_setup in ("waiting_plug", "verifying"):
            e.command_result(A, True, c.mono, "", 1)
            if phase_setup == "verifying":
                step(e, c, CONFIRM_TIMEOUT)
        assert e.plugs[A].phase() == phase_setup
        acts = push(e, c, 0)
        p = e.plugs[A]
        assert "command_confirmed" in names(acts), phase_setup
        assert p.state == State.OFF and p.attempt == 0 and p.next_attempt_at is None and p.verify_by is None
        push(e, c, 1)
        step(e, c, 30)
        assert p.attempt == 1  # a new cycle starts at attempt 1


# ================================================================== verification (R2.11)
def accepted_unconfirmed(c, delay=30):
    e = boot(c, delay)
    push(e, c, 1)
    step(e, c, delay)
    e.command_result(A, True, c.mono, "", 1)
    out = step(e, c, CONFIRM_TIMEOUT)
    assert any(isinstance(a, VerifyState) for a in out)
    return e


def test_verification_snapshot_on_counts_as_failed_attempt(c):
    e = accepted_unconfirmed(c)
    c.adv(2)
    acts = rest(e, c, 1, taken_at=c.mono - 1)
    assert "command_failed" in names(acts) and "still reports the plug ON" in e.plugs[A].last_error
    assert e.plugs[A].phase() == "retry_wait" and e.plugs[A].state == State.SWITCHING_OFF
    out = step(e, c, 5)
    assert sends(out) == [SendOff(A, 2)]


def test_verification_snapshot_requested_too_early_is_not_believed(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    e.command_result(A, True, c.mono, "", 1)
    acked = e.plugs[A].acked_at
    out = step(e, c, CONFIRM_TIMEOUT)
    assert any(isinstance(a, VerifyState) for a in out)
    acts = rest(e, c, 0, taken_at=acked + REST_VERIFY_MIN_AFTER_ACK - 1)  # asked before the window
    assert "assumed_off" not in names(acts) and e.plugs[A].state == State.SWITCHING_OFF


def test_verification_without_answer_fails_the_attempt(c):
    e = accepted_unconfirmed(c)
    out = step(e, c, VERIFY_TIMEOUT + 1)
    assert "command_failed" in names(out) and "no fresh cloud snapshot" in e.plugs[A].last_error
    assert sends(out) == []


# ================================================================== settings while overdue (R2.12)
def test_disabling_auto_off_stops_the_retries(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    e.command_result(A, False, c.mono, "down", 1)
    acts = e.set_settings(A, c.mono, enabled=False)
    assert "overdue_cancelled" in names(acts)
    p = e.plugs[A]
    assert p.state == State.ON and p.deadline is None and p.attempt == 0 and not p.off_failed
    assert sends(step(e, c, 600)) == []


def test_shortening_the_delay_does_not_touch_an_overdue_plug(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    e.command_result(A, False, c.mono, "down", 1)
    e.set_settings(A, c.mono, delay_seconds=5)
    p = e.plugs[A]
    assert p.state == State.SWITCHING_OFF and p.attempt == 1 and p.next_in(c.mono) == 5


def test_plug_missing_while_overdue_clears_everything(c):
    e = boot(c, 30)
    push(e, c, 1)
    step(e, c, 30)
    e.command_result(A, False, c.mono, "down", 1)
    e.sync_plugs({}, c.mono, c.wall)
    p = e.plugs[A]
    assert p.state == State.ON and p.attempt == 0 and p.next_attempt_at is None and p.deadline is None
    assert sends(step(e, c, 600)) == []


# ================================================================== log contract (R12)
def test_every_engine_log_is_a_decision_with_a_plug_or_reason(c):
    e = boot(c, 30)
    acts = push(e, c, 1) + step(e, c, 30)
    logs = [a for a in acts if isinstance(a, Log)]
    assert logs and all(a.kind == "decision" for a in logs)
    sent = find(acts, "send_off")[0]
    assert sent.fields["reason"] == "timer expired" and sent.fields["attempt"] == 1
    assert find(acts, "timer_started")[0].fields["counted_from"] == "push"
