# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Signal / decision log separation, files, levels and secret scrubbing (R12, R5.4)."""
import contextlib
import logging
import os
import secrets as pysecrets
import stat

import pytest

from innr_autooff import logging_util as lu

log = logging.getLogger("innr_autooff.test")


@contextlib.contextmanager
def installed(tmp_path, level="INFO"):
    """Install the handlers inside the test body (capsys' stream is only valid there)."""
    files = lu.setup_logging(level, tmp_path / "logs")
    assert files == ["signals.log", "decisions.log"]
    try:
        yield tmp_path / "logs"
    finally:
        lu.teardown_logging()


@pytest.fixture
def logs(tmp_path):
    files = lu.setup_logging("INFO", tmp_path / "logs")
    assert files == ["signals.log", "decisions.log"]
    yield tmp_path / "logs"
    lu.teardown_logging()


def read(p):
    return p.read_text(encoding="utf-8").splitlines()


def test_signals_and_decisions_go_to_separate_files(logs):
    lu.log_signal(log, "push", source="local", plug="Coffee", onoff=1)
    lu.log_decision(log, "timer_started", plug="Coffee", delay=1200, reason="push")
    log.info(lu.kv("startup", version="x"))  # operational line: in neither file
    sig, dec = read(logs / "signals.log"), read(logs / "decisions.log")
    assert len(sig) == 1 and "kind=signal event=push" in sig[0] and "source=local" in sig[0]
    assert len(dec) == 1 and "kind=decision event=timer_started" in dec[0]
    assert "kind=decision" not in sig[0] and "kind=signal" not in dec[0]
    for f in (sig + dec):
        assert "startup" not in f


def test_files_contain_debug_even_when_stdout_is_info(capsys, tmp_path):
    with installed(tmp_path) as logs:
        lu.log_signal(log, "push", "debug", source="local", plug="Coffee", attrs="Watts=407")
        lu.log_signal(log, "push", "info", source="local", plug="Coffee", onoff=1)
        sig = read(logs / "signals.log")
        err = capsys.readouterr().err
    assert len(sig) == 2 and "Watts=407" in sig[0]
    assert "onoff=1" in err and "Watts=407" not in err  # stdout honours the configured level


def test_stdout_gets_both_kinds_with_the_kind_tag(capsys, tmp_path):
    with installed(tmp_path):
        lu.log_signal(log, "rest_snapshot", plug="Coffee", snapshot=1, known=0)
        lu.log_decision(log, "rest_ignored", plug="Coffee", reason="within_push_grace")
        err = capsys.readouterr().err
    assert "INFO kind=signal event=rest_snapshot" in err and "INFO kind=decision event=rest_ignored" in err
    import re
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d INFO kind=signal", err.splitlines()[0])


def test_log_line_format_is_key_value_and_quotes_spaces(logs):
    lu.log_decision(log, "x", plug="Coffee machine", reason="a=b", empty="", none=None, n=3)
    line = read(logs / "decisions.log")[0]
    assert 'plug="Coffee machine"' in line and 'reason="a=b"' in line and 'empty=""' in line
    assert "none=" not in line and "n=3" in line


def test_secrets_are_scrubbed_in_every_sink(capsys, tmp_path):
    with installed(tmp_path) as logs:
        for _ in range(20):
            secret = pysecrets.token_urlsafe(24)
            lu.register_secret(secret)
            lu.log_signal(log, "cloud_error", "warning", reason=f"failed with {secret} inside")
            lu.log_decision(log, "x", reason=f"also {secret}")
            log.warning("event=ops text=%s", secret)
            err = capsys.readouterr().err
            assert secret not in err
            assert secret not in (logs / "signals.log").read_text()
            assert secret not in (logs / "decisions.log").read_text()
        assert "kind=signal" in (logs / "signals.log").read_text()  # the lines exist, only the secret is gone


def test_describe_error_is_scrubbed_single_line_and_capped():
    lu.register_secret("SUPER-SECRET-VALUE")
    e = RuntimeError("line one\nline two SUPER-SECRET-VALUE " + "x" * 500)
    d = lu.describe_error(e)
    assert d.startswith("RuntimeError: line one line two ***") and "\n" not in d
    assert len(d) <= len("RuntimeError: ") + lu.ERROR_TEXT_CAP
    assert lu.describe_error(KeyError()) == "KeyError"


def test_rotation_settings_and_private_directory(logs):
    import logging.handlers as h
    rot = [x for x in logging.getLogger().handlers if isinstance(x, h.RotatingFileHandler)]
    assert len(rot) == 2
    assert all(x.maxBytes == 2_000_000 and x.backupCount == 3 for x in rot)


def test_unwritable_log_dir_does_not_break_logging(capsys, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    files = lu.setup_logging("INFO", blocker / "logs")  # parent is a file: cannot create
    try:
        assert files == []
        lu.log_signal(log, "push", source="local")
        err = capsys.readouterr().err
        assert "event=log_files_unavailable" in err and "event=push" in err  # stdout still works
    finally:
        lu.teardown_logging()


def test_teardown_removes_our_handlers(tmp_path):
    lu.setup_logging("INFO", tmp_path / "logs")
    ours = list(lu._installed)
    assert len(ours) == 3 and all(h in logging.getLogger().handlers for h in ours)
    lu.teardown_logging()
    assert not any(h in logging.getLogger().handlers for h in ours) and lu._installed == []


def test_setup_twice_does_not_duplicate_lines(tmp_path):
    try:
        lu.setup_logging("INFO", tmp_path / "logs")
        lu.setup_logging("INFO", tmp_path / "logs")
        lu.log_signal(log, "push", source="local")
        assert len(read(tmp_path / "logs" / "signals.log")) == 1
    finally:
        lu.teardown_logging()


def test_log_event_skips_formatting_when_disabled():
    lg = logging.getLogger("innr_autooff.quiet")
    lg.setLevel(logging.ERROR)

    class Boom:
        def __str__(self):
            raise AssertionError("formatted although disabled")
    lu.log_event(lg, "debug", "x", "signal", v=Boom())  # must not raise


def test_kv_without_kind_is_unchanged():
    assert lu.kv("x", a="b c", n=1, skip=None) == 'event=x a="b c" n=1'
    assert lu.kv("x", "signal", a=1) == "kind=signal event=x a=1"
