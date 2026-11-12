# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Device discovery from the cloud device list (R10). Pure functions."""
from __future__ import annotations

import re
from typing import Any

from .models import PlugInfo

PLUG_TYPE = "plug"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
DEV_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def clean_text(value: Any, limit: int = 64) -> str:
    """Untrusted cloud text -> printable, length-capped (R10.6). HTML escaping happens at render."""
    s = _CTRL.sub("", str(value or "")).strip()
    return s[:limit]


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def discover(devices: list[dict[str, Any]], seed_names: dict[str, str] | None = None) -> dict[str, PlugInfo]:
    """All devices of type 'plug' with a valid id and a gateway (parentId)."""
    seed_names = seed_names or {}
    out: dict[str, PlugInfo] = {}
    for d in devices:
        if not isinstance(d, dict) or d.get("type") != PLUG_TYPE:
            continue
        dev = d.get("id")
        gw = d.get("parentId")
        if not (isinstance(dev, str) and DEV_ID_RE.match(dev) and isinstance(gw, str) and DEV_ID_RE.match(gw)):
            continue
        props = d.get("properties") if isinstance(d.get("properties"), dict) else {}
        onoff = _num(props.get("OnOff"))
        out[dev] = PlugInfo(
            dev_id=dev,
            name=clean_text(seed_names.get(dev) or d.get("name") or dev[-8:]),
            model=clean_text(d.get("modelId")),
            gateway_id=gw,
            online=bool(d.get("online", True)),
            onoff=int(onoff) if onoff in (0.0, 1.0) else None,
            power_w=_num(props.get("Watts")),
        )
    return out
