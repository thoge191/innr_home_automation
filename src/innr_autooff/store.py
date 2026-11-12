# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Atomic JSON state file (R3.1, R3.6). Holds no secrets."""
from __future__ import annotations

import json
import logging
import os
import secrets
import string
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
VERSION = 1
TERMINAL_ALPHABET = string.ascii_letters + string.digits + "_-"


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": VERSION}

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self.data
        except OSError as e:
            log.warning("event=state_unreadable error=%s", e.strerror)
            return self.data
        try:
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            bad = self.path.with_suffix(self.path.suffix + ".bad")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            log.warning("event=state_corrupt action=ignored moved_to=%s", bad.name)
            return self.data
        data.setdefault("version", VERSION)
        self.data = data
        return self.data

    def save(self, update: dict[str, Any] | None = None) -> None:
        if update:
            self.data.update(update)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def terminal_id(self) -> str:
        """R5.3: one persistent client identity towards the cloud."""
        tid = self.data.get("terminal_id")
        if not (isinstance(tid, str) and len(tid) == 21 and all(c in TERMINAL_ALPHABET for c in tid)):
            tid = "".join(secrets.choice(TERMINAL_ALPHABET) for _ in range(21))
            self.save({"terminal_id": tid})
        return tid
