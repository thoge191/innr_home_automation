# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""INNR / Arnoo cloud REST client (blocking; call through asyncio.to_thread).

Request format taken from the open-source smarthomesven/homey-innr-bridge app
and verified against the live account. Tokens and passwords stay in memory and
are registered with the log scrubber (R5.2, R5.4).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .config import ClientProfile
from .logging_util import register_secret

log = logging.getLogger(__name__)

COUNTRY = {"eu": "region:Netherlands", "us": "region:United States"}
MIN_LOGIN_INTERVAL = 60.0  # no login flapping (design section 8)


class ApiError(RuntimeError):
    def __init__(self, status: int, path: str, text: str = "") -> None:
        super().__init__(f"HTTP {status} from {path}")
        self.status = status
        self.text = text[:200]


class AuthError(ApiError):
    pass


class InnrApi:
    def __init__(self, email: str, password: str, region: str, terminal_id: str, profile: ClientProfile,
                 opener=urllib.request.urlopen, base_url: str | None = None, clock=time.monotonic) -> None:
        self.email = email
        self._password = password
        register_secret(password)
        self.region = region
        self.terminal_id = terminal_id
        self.profile = profile  # how we present ourselves to the cloud: operator configuration, never code (R13)
        self.base = base_url or f"https://prod-{region}-api.arnoo.com/v17"
        self._open = opener
        self._clock = clock
        self._lock = threading.Lock()
        self.user_id = ""
        self.terminal_index = ""
        self._access = ""
        self._refresh = ""
        self._last_login_attempt = -1e9
        self.last_login_ok: bool | None = None
        self.last_error = ""

    # ------------------------------------------------------------------ http
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        p = self.profile
        h = {
            "User-Agent": p.user_agent, "Origin": p.origin,
            "Referer": p.origin + "/", "appId": p.app_id, "locale": "en-US",
            "terminal": "app", "webVersion": p.web_version, "X-Requested-With": p.package_name,
            "Content-Type": "application/json",
        }
        h.update(extra or {})
        return h

    def _call(self, method: str, path: str, body: Any = None, extra: dict[str, str] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self._headers(extra))
        try:
            with self._open(req, timeout=20) as r:
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode(errors="replace")
            except Exception:
                pass
            raise ApiError(e.code, path.split("?")[0], text) from None

    # ------------------------------------------------------------------ auth
    def login(self, force: bool = False) -> None:
        with self._lock:
            now = self._clock()
            if not force and now - self._last_login_attempt < MIN_LOGIN_INTERVAL:
                raise AuthError(0, "/users/loginWithEmailPassword", "login rate limited")
            self._last_login_attempt = now
            try:
                r = self._call("POST", "/users/loginWithEmailPassword", {
                    "username": self.email,
                    "password": hashlib.md5(self._password.encode()).hexdigest(),  # protocol requirement
                    "countryKey": COUNTRY[self.region],
                    "nativeVersion": self.profile.app_version, "buildNumber": self.profile.build_number,
                    "osVersion": self.profile.os_version, "os": 2,
                    "packName": self.profile.package_name, "terminalModelName": self.profile.device_model,
                    "terminalName": self.profile.device_name, "terminalId": self.terminal_id,
                    "area": "GMT", "webVersion": self.profile.web_version,
                    "webViewVersion": self.profile.web_view_version,
                })
            except ApiError as e:
                self.last_login_ok, self.last_error = False, f"login failed: HTTP {e.status}"
                raise AuthError(e.status, "/users/loginWithEmailPassword", e.text) from None
            except OSError as e:
                self.last_login_ok, self.last_error = False, f"login failed: {type(e).__name__}"
                raise
            if not isinstance(r, dict) or not r.get("accessToken") or not r.get("id"):
                self.last_login_ok, self.last_error = False, "login failed: unexpected response"
                raise AuthError(0, "/users/loginWithEmailPassword", "unexpected response")
            self._access, self._refresh = r["accessToken"], r.get("refreshToken", "")
            register_secret(self._access)
            register_secret(self._refresh)
            self.user_id = str(r["id"])
            self.terminal_index = str(r.get("terminalIndex") or "")
            self.last_login_ok, self.last_error = True, ""
            log.info("event=cloud_login result=ok")

    def _refresh_token(self) -> bool:
        if not self._refresh:
            return False
        try:
            r = self._call("POST", "/users/refreshToken", {
                "refreshToken": self._refresh, "nativeVersion": self.profile.web_version, "webVersion": self.profile.web_version,
                "t": int(time.time() * 1000)})
        except (ApiError, OSError):
            return False
        if isinstance(r, dict) and r.get("accessToken"):
            self._access = r["accessToken"]
            register_secret(self._access)
            log.info("event=cloud_token_refreshed")
            return True
        return False

    def _authed_get(self, path: str, extra: dict[str, str] | None = None) -> Any:
        if not self._access:
            self.login()
        for attempt in (1, 2):
            try:
                return self._call("GET", path, extra={"token": self._access, "owner": self.user_id, **(extra or {})})
            except ApiError as e:
                if attempt == 1 and e.status in (401, 403):
                    if not self._refresh_token():
                        self.login(force=True)
                    continue
                raise
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ data
    def devices(self) -> tuple[str, list[dict]]:
        """(house_id, all devices of all houses). Device records include their passwords."""
        houses = self._authed_get("/houses?isDefault=true") or []
        house_id = ""
        devices: list[dict] = []
        for h in houses:
            if isinstance(h, dict) and h.get("deviceCount"):
                house_id = house_id or str(h["id"])
                for d in self._authed_get(f"/devices?houseId={h['id']}") or []:
                    if isinstance(d, dict):
                        if d.get("password"):
                            register_secret(str(d["password"]))
                        devices.append(d)
        return house_id, devices

    def mqtt_config(self, house_id: str) -> dict:
        cfg = self._authed_get("/commons/mqttConfig?allowApp=", extra={"houseId": house_id})
        if not isinstance(cfg, dict) or not cfg.get("host") or not cfg.get("password"):
            raise ApiError(0, "/commons/mqttConfig", "unexpected response")
        register_secret(str(cfg["password"]))
        return cfg

    def client_id(self, cfg: dict) -> str:
        idx = self.terminal_index or str(cfg.get("clientId", "")).split("-")[0]
        return f"{idx}-{self.user_id}"
