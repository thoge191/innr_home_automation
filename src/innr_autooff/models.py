# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Shared data types. Nothing here holds secrets."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SOURCES = ("local", "cloud", "rest")


@dataclass(frozen=True)
class Observation:
    """A report 'plug X is ON/OFF' from one state source."""
    dev: str
    onoff: int  # 0 or 1
    source: str  # local | cloud | rest
    observed_at: float  # monotonic seconds; for 'rest' the instant the request was sent
    idx: int | None = None


@dataclass(frozen=True)
class PlugInfo:
    """What discovery knows about a plug. Contains NO device password."""
    dev_id: str
    name: str
    model: str
    gateway_id: str
    online: bool = True
    onoff: int | None = None
    power_w: float | None = None


@dataclass
class PlugSettings:
    enabled: bool
    delay_seconds: float


# ---- engine actions -------------------------------------------------------
@dataclass(frozen=True)
class SendOff:
    dev: str
    attempt: int


@dataclass(frozen=True)
class Persist:
    pass


@dataclass(frozen=True)
class VerifyState:
    """Ask the app for a fresh cloud snapshot (R2.11): the cloud accepted an OFF command
    but no OFF push arrived, so the plug may simply have been OFF already."""
    dev: str


@dataclass(frozen=True)
class Log:
    level: str
    event: str
    fields: dict[str, Any] = field(default_factory=dict)
    kind: str = "decision"  # engine output is always a decision (R12)


Action = SendOff | VerifyState | Persist | Log
