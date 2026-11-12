# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Small append-only audit log (JSON lines, size-rotated). Never contains secrets."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

SECRET_HINTS = ("password", "token", "secret", "csrf", "cookie")


class Audit:
    def __init__(self, path: Path, max_bytes: int = 1_000_000, keep: int = 3) -> None:
        self.path = Path(path)
        self.max_bytes, self.keep = max_bytes, keep
        self._lock = threading.Lock()

    def write(self, actor: str, action: str, **detail) -> None:
        clean = {k: ("<redacted>" if any(h in k.lower() for h in SECRET_HINTS) else v) for k, v in detail.items()}
        line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "actor": actor, "action": action, **clean}, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # auditing must never break the service

    def _rotate(self) -> None:
        for i in range(self.keep - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
