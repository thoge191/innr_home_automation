# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""CLI: run | status | set-password | check-config | healthcheck"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config, load_credentials, require_client_profile, require_real_bridge_host
from .logging_util import setup_logging

DEFAULT_CONFIG = os.environ.get("INNR_CONFIG", "/config/config.toml")
HEALTH_FILE = Path(os.environ.get("INNR_HEALTH_FILE", "/tmp/innr-autooff.health"))
HEALTH_MAX_AGE = 120


def cmd_run(args) -> int:
    from .app import Service
    from .web.server import start_web

    cfg = load_config(args.config)
    require_real_bridge_host(cfg)
    require_client_profile(cfg)
    files = setup_logging(cfg.log_level, cfg.data_dir / "logs")
    logging.getLogger("innr_autooff").info("event=logging files=%s", ",".join(files) or "none")
    creds = load_credentials()
    svc = Service(cfg, creds)
    web_start = None
    if cfg.web.enabled:
        def web_start(s):
            return start_web(s, cfg.web, cfg.data_dir)
    asyncio.run(svc.run(web_start))
    return 0


def cmd_check_config(args) -> int:
    cfg = load_config(args.config)
    print(f"config OK: bridge {cfg.bridge_host}:{cfg.bridge_port}, region {cfg.region}, "
          f"{len(cfg.seeds)} seeded plug(s), web {'on port %d' % cfg.web.port if cfg.web.enabled else 'off'}")
    for check in (require_real_bridge_host, require_client_profile):
        try:
            check(cfg)
        except ConfigError as e:
            print(f"WARNING: {e}", file=sys.stderr)
    return 0


def cmd_set_password(args) -> int:
    from .web.auth import PasswordFile, check_password_policy

    cfg = load_config(args.config)
    pw1 = getpass.getpass("New Web UI password: ")
    problem = check_password_policy(pw1)
    if problem:
        print(f"refused: {problem}", file=sys.stderr)
        return 2
    if getpass.getpass("Repeat: ") != pw1:
        print("refused: passwords differ", file=sys.stderr)
        return 2
    PasswordFile(cfg.data_dir / "web_auth.json").set(pw1)
    from .web.audit import Audit
    Audit(cfg.data_dir / "audit.log").write("cli", "password_set")
    print("Web UI password set. Existing sessions end at the next service restart.")
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    p = cfg.data_dir / "state.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        print("no state file yet")
        return 1
    for dev, rec in sorted((data.get("plugs") or {}).items()):
        o = rec.get("overrides") or {}
        print(f"{rec.get('name', dev[-8:]):30} {rec.get('state', '?'):8} overrides={o}")
    print("health:", "ok" if _health_ok() else "NOT healthy")
    return 0


def _health_ok() -> bool:
    try:
        return time.time() - int(HEALTH_FILE.read_text().strip()) <= HEALTH_MAX_AGE
    except (OSError, ValueError):
        return False


def cmd_healthcheck(args) -> int:
    return 0 if _health_ok() else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="innr-autooff")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the service (default)")
    sub.add_parser("status", help="print plugs from the state file and health")
    sub.add_parser("set-password", help="set the Web UI password (interactive)")
    sub.add_parser("check-config", help="validate the configuration file")
    sub.add_parser("healthcheck", help="exit 0 when healthy (container health check)")
    args = ap.parse_args(argv)
    handlers = {None: cmd_run, "run": cmd_run, "status": cmd_status, "set-password": cmd_set_password,
                "check-config": cmd_check_config, "healthcheck": cmd_healthcheck}
    try:
        return handlers[args.cmd](args)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
