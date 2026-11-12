# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Password (scrypt), in-memory hashed sessions, CSRF derivation, progressive lockout."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_LEN, MAX_LEN = 12, 128
COMMON_PASSWORDS = frozenset({
    "password1234", "123456789012", "qwertyuiopas", "passwordpassword", "letmein12345", "adminadmin12",
    "administrator", "innrautooff1", "innr-autooff", "coffeemachine", "iloveyou1234", "welcome12345",
    "000000000000", "111111111111", "abcdefghijkl", "changeme1234", "smarthome123", "homeautomation",
})
CSRF_LABEL = b"innr-autooff-csrf-v1"


def check_password_policy(pw: str) -> str | None:
    """None if acceptable, else a short reason."""
    if len(pw) < MIN_LEN:
        return f"at least {MIN_LEN} characters required"
    if len(pw) > MAX_LEN:
        return f"at most {MAX_LEN} characters allowed"
    if pw.lower() in COMMON_PASSWORDS or len(set(pw)) < 4:
        return "too common or too simple"
    return None


def _scrypt(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                          maxmem=SCRYPT_MAXMEM, dklen=32)


class PasswordFile:
    """One operator password, stored as a scrypt hash in a 0600 JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._dummy_salt = secrets.token_bytes(16)

    def exists(self) -> bool:
        return self._load() is not None

    def _load(self) -> dict | None:
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not (isinstance(rec, dict) and rec.get("alg") == "scrypt" and isinstance(rec.get("salt"), str)
                and isinstance(rec.get("hash"), str)):
            return None
        return rec

    def set(self, pw: str) -> None:
        problem = check_password_policy(pw)
        if problem:
            raise ValueError(problem)
        salt = secrets.token_bytes(16)
        rec = {"alg": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
               "salt": salt.hex(), "hash": _scrypt(pw, salt).hex(), "set_at": int(time.time())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".web_auth.", dir=self.path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def verify(self, pw: str) -> bool:
        """Constant work whether or not a password is set (no oracle)."""
        pw = pw[:MAX_LEN * 4]
        rec = self._load()
        if rec is None:
            _scrypt(pw, self._dummy_salt)
            return False
        try:
            salt = bytes.fromhex(rec["salt"])
            expected = bytes.fromhex(rec["hash"])
        except ValueError:
            _scrypt(pw, self._dummy_salt)
            return False
        return hmac.compare_digest(_scrypt(pw, salt), expected)


FLASH_SECONDS = 5.0  # messages survive several 1 s dashboard reloads


@dataclass
class Session:
    token_hash: str
    csrf: str
    created: float
    last_seen: float
    flash: list[tuple[str, str, float]] = field(default_factory=list)  # (kind, text, expires_at)

    def add_flash(self, kind: str, text: str, now: float) -> None:
        self.flash.append((kind, text, now + FLASH_SECONDS))

    def current_flash(self, now: float) -> list[tuple[str, str]]:
        """Messages shown on every page view until they expire (not consumed by the first reload)."""
        self.flash = [f for f in self.flash if f[2] > now]
        return [(k, t) for k, t, _ in self.flash]


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    """Opaque random tokens; only their SHA-256 is kept. Idle + absolute timeout."""

    def __init__(self, idle: float, maximum: float, clock=time.monotonic) -> None:
        self.idle, self.max, self.clock = idle, maximum, clock
        self._s: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        csrf = hmac.new(token.encode(), CSRF_LABEL, hashlib.sha256).hexdigest()
        with self._lock:
            self._purge(now)
            self._s[_h(token)] = Session(_h(token), csrf, now, now)
        return token

    def get(self, token: str | None, touch: bool = True) -> Session | None:
        """touch=False for background polling: reading must not keep an idle session alive."""
        if not token or len(token) > 128:
            return None
        now = self.clock()
        with self._lock:
            s = self._s.get(_h(token))
            if s is None:
                return None
            if now - s.created >= self.max or now - s.last_seen >= self.idle:
                del self._s[s.token_hash]
                return None
            if touch:
                s.last_seen = now
            return s

    def destroy(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._s.pop(_h(token), None)

    def destroy_all(self) -> None:
        with self._lock:
            self._s.clear()

    def _purge(self, now: float) -> None:
        for k, s in list(self._s.items()):
            if now - s.created >= self.max or now - s.last_seen >= self.idle:
                del self._s[k]

    def __len__(self) -> int:
        return len(self._s)


def csrf_ok(session: Session | None, submitted: str | None) -> bool:
    return bool(session and submitted) and hmac.compare_digest(str(submitted), session.csrf)


def lockout_seconds(failures: int) -> int:
    if failures < 3:
        return 0
    if failures < 5:
        return 60
    if failures < 10:
        return 300
    return 900


class Lockout:
    """Per client IP, in memory, bounded (attacker-supplied keys)."""

    MAX_KEYS = 10_000

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._d: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._lock = threading.Lock()

    def remaining(self, key: str) -> int:
        with self._lock:
            n, until = self._d.get(key, (0, 0.0))
        return max(0, int(until - self.clock() + 0.999))

    def fail(self, key: str) -> int:
        with self._lock:
            n, _ = self._d.pop(key, (0, 0.0))
            n += 1
            secs = lockout_seconds(n)
            self._d[key] = (n, self.clock() + secs)
            while len(self._d) > self.MAX_KEYS:
                self._d.popitem(last=False)
        return secs

    def success(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)
