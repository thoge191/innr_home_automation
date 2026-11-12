# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Configuration (TOML) and secrets (files / env). Design section 6."""
from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import PlugSettings

DEV_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Documentation address (RFC 5737) used in config.example.toml. Starting with it unchanged is refused.
EXAMPLE_BRIDGE_HOST = "192.0.2.10"


class ConfigError(ValueError):
    pass


# How this service presents itself to the INNR cloud (R13). The cloud API is undocumented and only accepts
# requests that look like the official app, so these values must come from the operator's configuration;
# this project ships none of them.
CLIENT_STRING_KEYS = ("app_id", "user_agent", "package_name", "app_version", "web_version", "os_version",
                      "web_view_version", "device_model", "device_name", "origin")
CLIENT_KEYS = CLIENT_STRING_KEYS + ("build_number",)
_HEADER_SAFE = re.compile(r"^[\x20-\x7e]{1,300}$")  # printable ASCII only: no control characters, no header injection


@dataclass(frozen=True)
class ClientProfile:
    app_id: str
    user_agent: str
    package_name: str
    app_version: str
    web_version: str
    os_version: str
    web_view_version: str
    device_model: str
    device_name: str
    origin: str
    build_number: int


@dataclass
class WebConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8480
    session_idle_seconds: int = 28800
    session_max_seconds: int = 604800
    tls_cert: str = ""
    tls_key: str = ""
    refresh_seconds: int = 1


@dataclass
class Config:
    bridge_host: str = ""  # must be set: the LAN address of the INNR bridge
    bridge_port: int = 10000
    bridge_cert_sha256: str = ""
    region: str = "eu"
    cloud_state_feed: bool = True
    reconcile_seconds: float = 60.0
    defaults: PlugSettings = field(default_factory=lambda: PlugSettings(True, 1800.0))
    new_plug_auto_off: bool = False
    restart_policy: str = "resume"
    seeds: dict[str, PlugSettings] = field(default_factory=dict)
    seed_names: dict[str, str] = field(default_factory=dict)
    web: WebConfig = field(default_factory=WebConfig)
    client: ClientProfile | None = None
    data_dir: Path = Path("/data")
    log_level: str = "INFO"


def _get(d: dict, key: str, typ, default):
    v = d.get(key, default)
    if typ is float and isinstance(v, int) and not isinstance(v, bool):
        v = float(v)
    if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
        raise ConfigError(f"{key}: expected {typ.__name__}, got {type(v).__name__}")
    return v


def _delay(v: float, where: str) -> float:
    if not 1 <= v <= 86400:
        raise ConfigError(f"{where}: delay_seconds must be between 1 and 86400")
    return v


def _parse_client(raw) -> ClientProfile | None:
    """[cloud.client]: None when absent or entirely empty (the template), an error when half-filled or malformed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("cloud.client must be a table")
    unknown = sorted(set(raw) - set(CLIENT_KEYS))
    if unknown:
        raise ConfigError(f"cloud.client: unknown key(s): {', '.join(unknown)}")
    if all(raw.get(k) in (None, "", 0) for k in CLIENT_KEYS):
        return None
    problems = []
    values: dict = {}
    for k in CLIENT_STRING_KEYS:
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            problems.append(f"{k} is missing or empty")
        elif not _HEADER_SAFE.match(v.strip()):
            problems.append(f"{k} must be 1 to 300 printable ASCII characters")
        else:
            values[k] = v.strip()
    bn = raw.get("build_number")
    if isinstance(bn, bool) or not isinstance(bn, int) or not 1 <= bn <= 2**31 - 1:
        problems.append("build_number must be a positive whole number")
    else:
        values["build_number"] = bn
    if problems:
        raise ConfigError("cloud.client is incomplete or invalid: " + "; ".join(problems))
    return ClientProfile(**values)


def parse_config(data: dict, env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    c = Config()
    b = data.get("bridge", {})
    c.bridge_host = _get(b, "host", str, c.bridge_host).strip()
    c.bridge_port = _get(b, "port", int, c.bridge_port)
    c.bridge_cert_sha256 = _get(b, "cert_sha256", str, "").replace(":", "").lower()
    if c.bridge_cert_sha256 and not re.fullmatch(r"[0-9a-f]{64}", c.bridge_cert_sha256):
        raise ConfigError("bridge.cert_sha256 must be 64 hex characters")

    cl = data.get("cloud", {})
    c.region = _get(cl, "region", str, c.region)
    if c.region not in ("eu", "us"):
        raise ConfigError("cloud.region must be 'eu' or 'us'")
    c.cloud_state_feed = _get(cl, "state_feed", bool, c.cloud_state_feed)
    c.client = _parse_client(cl.get("client"))
    c.reconcile_seconds = _get(cl, "reconcile_seconds", float, c.reconcile_seconds)
    if not 15 <= c.reconcile_seconds <= 3600:
        raise ConfigError("cloud.reconcile_seconds must be between 15 and 3600")

    d = data.get("defaults", {})
    c.defaults = PlugSettings(_get(d, "auto_off_enabled", bool, True),
                              _delay(_get(d, "delay_seconds", float, 1800.0), "defaults"))
    c.new_plug_auto_off = _get(d, "new_plug_auto_off", bool, False)
    c.restart_policy = _get(d, "restart_policy", str, "resume")
    if c.restart_policy not in ("resume", "restart"):
        raise ConfigError("defaults.restart_policy must be 'resume' or 'restart'")

    for i, p in enumerate(data.get("plugs", []) or []):
        dev = _get(p, "dev_id", str, "")
        if not DEV_ID_RE.match(dev):
            raise ConfigError(f"plugs[{i}].dev_id must be 32 lower-case hex characters")
        enabled = _get(p, "auto_off_enabled", bool, c.defaults.enabled)
        delay = _delay(_get(p, "delay_seconds", float, c.defaults.delay_seconds), f"plugs[{i}]")
        c.seeds[dev] = PlugSettings(enabled, delay)
        if "name" in p:
            c.seed_names[dev] = _get(p, "name", str, "")

    w = data.get("web", {})
    c.web = WebConfig(
        enabled=_get(w, "enabled", bool, True),
        bind=_get(w, "bind", str, "0.0.0.0"),
        port=_get(w, "port", int, 8480),
        session_idle_seconds=_get(w, "session_idle_seconds", int, 28800),
        session_max_seconds=_get(w, "session_max_seconds", int, 604800),
        tls_cert=_get(w, "tls_cert", str, ""),
        tls_key=_get(w, "tls_key", str, ""),
        refresh_seconds=_get(w, "refresh_seconds", int, 1),
    )
    if not 1 <= c.web.refresh_seconds <= 60:
        raise ConfigError("web.refresh_seconds must be between 1 and 60")
    try:
        ipaddress.ip_address(c.web.bind)
    except ValueError as e:
        raise ConfigError("web.bind must be an IP address") from e
    if not 1 <= c.web.port <= 65535:
        raise ConfigError("web.port out of range")
    if not 60 <= c.web.session_idle_seconds <= c.web.session_max_seconds:
        raise ConfigError("web: need 60 <= session_idle_seconds <= session_max_seconds")
    if bool(c.web.tls_cert) != bool(c.web.tls_key):
        raise ConfigError("web: set both tls_cert and tls_key, or neither")

    c.data_dir = Path(env.get("INNR_DATA_DIR", "/data"))
    c.log_level = env.get("LOG_LEVEL", "INFO").upper()
    return c


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config file {p} is not valid TOML: {e}") from e
    cfg = parse_config(data, env)
    if not cfg.bridge_host:
        raise ConfigError("bridge.host is not set: enter the LAN address of your INNR bridge in config.toml")
    return cfg


def require_client_profile(cfg: Config) -> ClientProfile:
    """The service cannot talk to the INNR cloud without its client identity (R13.2)."""
    if cfg.client is None:
        raise ConfigError("cloud.client is not configured: fill in the [cloud.client] table of config.toml "
                          "(what each value means and where to get it: docs/CLOUD_CLIENT.md; this project ships none)")
    return cfg.client


def require_real_bridge_host(cfg: Config) -> None:
    """Refuse to run with the documentation placeholder from config.example.toml."""
    if cfg.bridge_host == EXAMPLE_BRIDGE_HOST:
        raise ConfigError(f"bridge.host is still the example value {EXAMPLE_BRIDGE_HOST}: "
                          "set it to the LAN address of your INNR bridge in config.toml")


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str

    def __repr__(self) -> str:  # never print the password
        return f"Credentials(email={self.email!r}, password=<masked>)"


def load_credentials(env: dict[str, str] | None = None) -> Credentials:
    """R5.1: e-mail from INNR_EMAIL, password from the file named by INNR_PASSWORD_FILE."""
    env = os.environ if env is None else env
    email = env.get("INNR_EMAIL", "").strip()
    if not email:
        raise ConfigError("INNR_EMAIL is not set")
    pw_file = env.get("INNR_PASSWORD_FILE", "/run/secrets/innr_password")
    try:
        password = Path(pw_file).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ConfigError(f"cannot read INNR_PASSWORD_FILE ({pw_file}): {e.strerror}") from e
    if not password:
        raise ConfigError("INNR_PASSWORD_FILE is empty")
    return Credentials(email, password)
