# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Logging: secret masking (R5.4) and separate signal / decision streams (R12).

A *signal* is an input the service received; a *decision* is a choice it made.
Both are single-line key=value events carrying `kind=signal` / `kind=decision`.
Everything goes to stdout at the configured level; in addition each kind is
written to its own rotating file, always at DEBUG.
"""
from __future__ import annotations

import logging
import logging.handlers
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# A dict key containing any of these (case-insensitive) is masked by mask().
SECRET_KEY_HINTS = ("password", "passwd", "token", "secret", "aeskey")
MIN_SECRET_LEN = 6  # shorter values are not registered (would mangle ordinary text)
KINDS = ("signal", "decision")
LOG_FILE_BYTES = 2_000_000
LOG_FILE_KEEP = 3
ERROR_TEXT_CAP = 160

_secrets: set[str] = set()
_lock = threading.Lock()
_installed: list[logging.Handler] = []


def register_secret(value: Any) -> None:
    """Remember a secret value so it is replaced by *** in every log line."""
    if isinstance(value, str) and len(value) >= MIN_SECRET_LEN:
        with _lock:
            _secrets.add(value)


def scrub(text: str) -> str:
    with _lock:
        values = sorted(_secrets, key=len, reverse=True)
    for v in values:
        if v in text:
            text = text.replace(v, "***")
    return text


def mask(obj: Any) -> Any:
    """Return a copy of obj with values of secret-looking keys replaced."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(h in k.lower() for h in SECRET_KEY_HINTS) and v:
                out[k] = f"<masked len={len(str(v))}>"
            else:
                out[k] = mask(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [mask(x) for x in obj]
    return obj


def describe_error(exc: BaseException) -> str:
    """`ExceptionType: message` (scrubbed, single line, capped) for cloud errors (R12.5)."""
    text = " ".join(str(exc).split())
    text = scrub(text)[:ERROR_TEXT_CAP]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


class KindFilter(logging.Filter):
    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "kind", None) == self.kind


class IsoFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


def _formatter() -> logging.Formatter:
    return IsoFormatter("%(asctime)s %(levelname)s %(message)s")


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> list[str]:
    """Install handlers. Returns the names of the log files that are active."""
    teardown_logging()
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)  # handlers decide; files need DEBUG

    out = logging.StreamHandler()
    out.setLevel(getattr(logging, level.upper(), logging.INFO))
    out.setFormatter(_formatter())
    out.addFilter(SecretFilter())
    root.addHandler(out)
    _installed.append(out)

    active: list[str] = []
    if log_dir is not None:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            for kind in KINDS:
                path = Path(log_dir) / f"{kind}s.log"
                fh = logging.handlers.RotatingFileHandler(
                    path, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_KEEP, encoding="utf-8")
                fh.setLevel(logging.DEBUG)
                fh.setFormatter(_formatter())
                fh.addFilter(KindFilter(kind))
                fh.addFilter(SecretFilter())
                root.addHandler(fh)
                _installed.append(fh)
                active.append(path.name)
        except OSError as e:
            logging.getLogger("innr_autooff").warning(
                "event=log_files_unavailable reason=%s", e.strerror or type(e).__name__)
    return active


def teardown_logging() -> None:
    root = logging.getLogger()
    while _installed:
        h = _installed.pop()
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def _fmt(v: Any) -> str:
    s = str(v)
    return f'"{s}"' if (" " in s or "=" in s or not s) else s


def kv(event: str, kind: str | None = None, **fields: Any) -> str:
    parts = ([f"kind={kind}"] if kind else []) + [f"event={event}"]
    parts += [f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None]
    return " ".join(parts)


_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
           "error": logging.ERROR}


def log_event(logger: logging.Logger, level: str, event: str, kind: str | None = None, **fields: Any) -> None:
    lvl = _LEVELS[level.lower()]
    if logger.isEnabledFor(lvl):
        logger.log(lvl, kv(event, kind, **fields), extra={"kind": kind} if kind else None)


def log_signal(logger: logging.Logger, event: str, level: str = "info", **fields: Any) -> None:
    log_event(logger, level, event, "signal", **fields)


def log_decision(logger: logging.Logger, event: str, level: str = "info", **fields: Any) -> None:
    log_event(logger, level, event, "decision", **fields)
