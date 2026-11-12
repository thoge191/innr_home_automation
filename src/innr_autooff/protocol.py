# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Bridge wire protocol: frame codec and notification decoding (R1.3).

Frame layout (verified against the real bridge):
    bytes 0-1  magic   1e ed
    bytes 2-3  version 00 01
    bytes 4-7  payload length, big-endian
    bytes 8..  JSON payload (UTF-8)
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Any

MAGIC = b"\x1e\xed"
VERSION = b"\x00\x01"
HEADER_LEN = 8
MAX_PAYLOAD = 1_000_000  # larger lengths mean we are misaligned


def encode_frame(obj: Any) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode()
    return MAGIC + VERSION + struct.pack(">I", len(body)) + body


class FrameParser:
    """Incremental parser: feed() arbitrary chunks, get complete frames back."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.warnings: list[str] = []

    def feed(self, data: bytes) -> list[bytes]:
        """Return the payloads of all complete frames in the buffered data."""
        self.buf.extend(data)
        payloads: list[bytes] = []
        while True:
            if len(self.buf) < HEADER_LEN:
                break
            if self.buf[:2] != MAGIC:
                idx = self.buf.find(MAGIC)
                if idx < 0:
                    skipped = len(self.buf) - 1  # keep last byte: magic may be split across reads
                    self.warnings.append(f"no magic found, discarded {skipped} bytes")
                    del self.buf[:skipped]
                    break
                self.warnings.append(f"resync: skipped {idx} bytes before magic")
                del self.buf[:idx]
                continue
            (length,) = struct.unpack(">I", self.buf[4:8])
            if length > MAX_PAYLOAD:
                self.warnings.append(f"implausible length {length}, resyncing")
                del self.buf[:2]
                continue
            if len(self.buf) < HEADER_LEN + length:
                break  # wait for more data
            payloads.append(bytes(self.buf[HEADER_LEN:HEADER_LEN + length]))
            del self.buf[:HEADER_LEN + length]
        return payloads

    def pop_warnings(self) -> list[str]:
        w, self.warnings = self.warnings, []
        return w


@dataclass(frozen=True)
class NotifEvent:
    """A decoded `setDevAttrNotif`: attribute update for one device."""
    dev: str
    attrs: dict[str, Any] = field(default_factory=dict)
    idx: int | None = None


def decode_notification(payload: bytes | str) -> NotifEvent | None:
    """Decode a notification payload; None if it is not a usable attribute update."""
    try:
        msg = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("method") != "setDevAttrNotif":
        return None
    p = msg.get("payload")
    if not isinstance(p, dict):
        return None
    dev, attrs = p.get("devId"), p.get("attr")
    if not isinstance(dev, str) or not isinstance(attrs, dict):
        return None
    idx = p.get("idx")
    return NotifEvent(dev=dev, attrs=attrs, idx=idx if isinstance(idx, int) else None)


def onoff_value(attrs: dict[str, Any]) -> int | None:
    """OnOff attribute as 0/1, or None when absent or not understood."""
    if "OnOff" not in attrs:
        return None
    try:
        v = int(float(attrs["OnOff"]))
    except (TypeError, ValueError):
        return None
    return v if v in (0, 1) else None


def watts_value(attrs: dict[str, Any]) -> float | None:
    if "Watts" not in attrs:
        return None
    try:
        return float(attrs["Watts"])
    except (TypeError, ValueError):
        return None
