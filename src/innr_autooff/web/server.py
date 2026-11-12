# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Standard-library HTTP server for the Web UI (design 17.4).

Deny by default: every request is matched against ROUTES; no match is 404, a
wrong method 405, an unknown access level 403. Every POST except /login
checks the CSRF token before doing anything else.
"""
from __future__ import annotations

import http.cookies
import json
import logging
import re
import ssl
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from ..logging_util import kv, log_signal
from . import render
from .audit import Audit
from .auth import Lockout, PasswordFile, SessionStore, csrf_ok

log = logging.getLogger("innr_autooff.web")

COOKIE = "innr_session"
MAX_BODY = 16 * 1024
PUBLIC, AUTH = "public", "auth"
SECURITY_HEADERS = {
    # One same-origin script file only: no inline script, no eval, no other origins.
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
                                "img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
DEV = r"(?P<dev>[0-9a-f]{32})"
LOGNAME = r"(?P<name>signals|decisions)"
LOG_TAIL_LINES = 500
LOG_TAIL_BYTES = 512 * 1024


class Core:
    """What the UI may do. The Service adapter implements it; tests use a fake."""

    def snapshot(self) -> dict[str, Any]: ...
    def set_settings(self, dev: str, enabled: bool, minutes: int) -> None: ...
    def switch(self, dev: str, onoff: int) -> bool: ...


class ServiceCore(Core):
    def __init__(self, svc) -> None:
        self.svc = svc

    def snapshot(self):
        return self.svc.call(self.svc.snapshot)

    def set_settings(self, dev, enabled, minutes):
        self.svc.call(self.svc.set_plug_settings, dev, enabled, float(minutes))

    def switch(self, dev, onoff):
        return self.svc.call(self.svc.request_switch, dev, onoff, timeout=60)


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    access: str
    handler: str

    @property
    def regex(self) -> re.Pattern:
        return re.compile(self.pattern)


ROUTES: tuple[Route, ...] = (
    Route("GET", r"/health", PUBLIC, "h_health"),
    Route("GET", r"/static/app\.css", PUBLIC, "h_css"),
    Route("GET", r"/static/app\.js", PUBLIC, "h_js"),
    Route("GET", r"/api/state", AUTH, "h_api_state"),
    Route("GET", rf"/log/{LOGNAME}", AUTH, "h_log"),
    Route("GET", r"/login", PUBLIC, "h_login_get"),
    Route("POST", r"/login", PUBLIC, "h_login_post"),
    Route("POST", r"/logout", AUTH, "h_logout"),
    Route("GET", r"/", AUTH, "h_dashboard"),
    Route("GET", rf"/plug/{DEV}", AUTH, "h_plug"),
    Route("POST", rf"/plug/{DEV}/settings", AUTH, "h_settings"),
    Route("POST", rf"/plug/{DEV}/switch", AUTH, "h_switch"),
)
ALWAYS_AVAILABLE = {"h_health", "h_css", "h_js"}  # also while no password is set
POLL_HANDLERS = {"h_api_state"}  # background polling: never extends the session (admin_gui.md 5.1)


class WebApp:
    def __init__(self, core: Core, data_dir: Path, *, idle: float, maximum: float, tls: bool,
                 clock: Callable[[], float] = time.monotonic, routes: tuple[Route, ...] = ROUTES,
                 refresh: int = 1, log_dir: Path | None = None) -> None:
        self.core = core
        self.refresh = refresh
        self.log_dir = Path(log_dir) if log_dir else Path(data_dir) / "logs"
        self.passwords = PasswordFile(Path(data_dir) / "web_auth.json")
        self.sessions = SessionStore(idle, maximum, clock)
        self.lockout = Lockout(clock)
        self.audit = Audit(Path(data_dir) / "audit.log")
        self.tls = tls
        self.max_age = int(maximum)
        self.routes = routes


class Handler(BaseHTTPRequestHandler):
    server_version = "innr-autooff"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15
    app: WebApp  # set on the subclass created per server

    # ---------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):  # no query strings, no cookies
        log.debug(kv("http", client=self.client_address[0], line=(fmt % args).split("?")[0][:120]))

    def _send(self, status: int, body: str | bytes, *, ctype: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None, cache: str = "no-store") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        # A request body we did not read would be parsed as the next request on a
        # keep-alive connection (request smuggling). Close the connection instead.
        unread_body = self.command not in ("GET", "HEAD") and not getattr(self, "_body_read", False)
        if unread_body:
            self.close_connection = True
        self.send_response(status)
        if unread_body:
            self.send_header("Connection", "close")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self._send(303, "", headers={"Location": location, **(headers or {})})

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw or len(raw) > 4096:
            return None
        try:
            c = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        m = c.get(COOKIE)
        return m.value if m else None

    def _session_cookie(self, token: str, max_age: int) -> str:
        v = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"
        return v + "; Secure" if self.app.tls else v

    def _form(self) -> dict[str, str] | None:
        """Parsed form, or None after an error response was sent."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            self._send(415, render.message_page("Unsupported", "Unsupported content type."))
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(411, render.message_page("Length required", "Content-Length required."))
            return None
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._send(413, render.message_page("Too large", "Request too large."))
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        self._body_read = True
        try:
            parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            self._send(400, render.message_page("Bad request", "Malformed form."))
            return None
        return {k: v[0] for k, v in parsed.items()}

    # ---------------------------------------------------------------- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("GET")

    def _not_allowed(self):
        self._send(405, render.message_page("Not allowed", "Method not allowed."), headers={"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def _dispatch(self, method: str) -> None:
        self._body_read = False  # per request (the handler object serves a whole keep-alive connection)
        try:
            # take the path verbatim (urlsplit would read '//host/x' as a netloc)
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            matches = [(r, m) for r in self.app.routes if (m := r.regex.fullmatch(path))]
            if not matches:
                return self._send(404, render.message_page("Not found", "Page not found."))
            hit = next(((r, m) for r, m in matches if r.method == method), None)
            if hit is None:
                return self._not_allowed()
            route, match = hit
            if route.access not in (PUBLIC, AUTH):  # deny by default
                log.warning(kv("web_route_denied", path=path))
                return self._send(403, render.message_page("Forbidden", "Forbidden."))
            if route.handler not in ALWAYS_AVAILABLE and not self.app.passwords.exists():
                return self._send(503, render.not_configured_page())
            session = None
            if route.access == AUTH:
                poll = route.handler in POLL_HANDLERS
                session = self.app.sessions.get(self._cookie_token(), touch=not poll)
                if session is None:
                    if poll:  # the script redirects; never send a login page into a data request
                        return self._send(401, '{"error":"unauthenticated"}', ctype="application/json")
                    return self._redirect("/login")
            form: dict[str, str] = {}
            if method == "POST":
                f = self._form()
                if f is None:
                    return
                form = f
                if route.access == AUTH and not csrf_ok(session, form.get("csrf_token")):
                    self.app.audit.write("web", "csrf_rejected", ip=self.client_address[0], path=path)
                    return self._send(403, render.message_page("Forbidden", "CSRF token invalid or missing."))
            getattr(self, route.handler)(session=session, form=form, **match.groupdict())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception(kv("web_error"))
            try:
                self._send(500, render.message_page("Error", "Internal error."))
            except Exception:
                pass

    # ---------------------------------------------------------------- handlers
    def h_health(self, session, form):
        ok = False
        try:
            ok = bool(self.app.core.snapshot().get("healthy"))
        except Exception:
            ok = False
        self._send(200 if ok else 503, "ok" if ok else "unavailable", ctype="text/plain; charset=utf-8")

    def h_css(self, session, form):
        self._send(200, render.CSS, ctype="text/css; charset=utf-8", cache="no-cache")

    def h_js(self, session, form):
        self._send(200, render.JS, ctype="text/javascript; charset=utf-8", cache="no-cache")

    def h_log(self, session, form, name):
        """Tail of the signal / decision log as plain text (R12.6). `name` is restricted by the route regex."""
        text = tail_file(self.app.log_dir / f"{name}.log", LOG_TAIL_LINES) if self.app.log_dir else ""
        self._send(200, text or f"(no {name} logged yet)\n", ctype="text/plain; charset=utf-8")

    def h_api_state(self, session, form):
        body = json.dumps(render.state_json(self.app.core.snapshot()), separators=(",", ":"))
        self._send(200, body, ctype="application/json")

    def h_login_get(self, session, form):
        if self.app.sessions.get(self._cookie_token()):
            return self._redirect("/")
        self._send(200, render.login_page())

    def h_login_post(self, session, form):
        ip = self.client_address[0]
        wait = self.app.lockout.remaining(ip)
        if wait:
            self.app.audit.write("web", "login_locked", ip=ip)
            log_signal(log, "ui_login", "warning", client=ip, result="locked", wait=wait)
            return self._send(429, render.login_page(f"Too many attempts. Try again in {wait} s."),
                              headers={"Retry-After": str(wait)})
        if not self.app.passwords.verify(form.get("password", "")):
            secs = self.app.lockout.fail(ip)
            self.app.audit.write("web", "login_failure", ip=ip, lockout_seconds=secs)
            log_signal(log, "ui_login", "warning", client=ip, result="failure", lockout=secs)
            status, msg = (429, f"Invalid password. Locked for {secs} s.") if secs else (401, "Invalid password.")
            return self._send(status, render.login_page(msg))
        self.app.lockout.success(ip)
        self.app.sessions.destroy(self._cookie_token())  # no fixation
        token = self.app.sessions.create()
        self.app.audit.write("web", "login_success", ip=ip)
        log_signal(log, "ui_login", client=ip, result="success")
        self._redirect("/", headers={"Set-Cookie": self._session_cookie(token, self.app.max_age)})

    def h_logout(self, session, form):
        self.app.sessions.destroy(self._cookie_token())
        self.app.audit.write("web", "logout", ip=self.client_address[0])
        self._redirect("/login", headers={"Set-Cookie": self._session_cookie("", 0)})

    def _flash(self, session, kind: str, text: str) -> None:
        session.add_flash(kind, text, self.app.sessions.clock())

    def _take_flash(self, session):
        return session.current_flash(self.app.sessions.clock())

    def _plug(self, dev: str) -> dict | None:
        return next((p for p in self.app.core.snapshot().get("plugs", []) if p["dev"] == dev), None)

    def h_dashboard(self, session, form):
        self._send(200, render.dashboard(self.app.core.snapshot(), session.csrf, self._take_flash(session), self.app.refresh))

    def h_plug(self, session, form, dev):
        p = self._plug(dev)
        if p is None:
            return self._send(404, render.message_page("Not found", "Unknown plug."))
        self._send(200, render.plug_page(p, session.csrf, self._take_flash(session)))

    def h_settings(self, session, form, dev):
        p = self._plug(dev)
        if p is None:
            return self._send(404, render.message_page("Not found", "Unknown plug."))
        enabled = form.get("enabled") == "on"
        raw = form.get("minutes", "").strip()
        if not re.fullmatch(r"[0-9]{1,4}", raw) or not 1 <= int(raw) <= 1440:  # ASCII only (\d matches '１')
            self._flash(session, "error", "Minutes must be a whole number from 1 to 1440.")
            return self._redirect(f"/plug/{dev}")
        minutes = int(raw)
        self.app.core.set_settings(dev, enabled, minutes)
        self.app.audit.write("web", "settings_changed", ip=self.client_address[0], dev=dev,
                             name=p["name"], enabled=enabled, minutes=minutes)
        log_signal(log, "ui_settings", plug=p["name"], enabled=enabled, minutes=minutes,
                   client=self.client_address[0])
        self._flash(session, "ok", f"Saved: {p['name']}, auto-off "
                    + (f"after {minutes} min." if enabled else "disabled."))
        self._redirect("/")

    def h_switch(self, session, form, dev):
        p = self._plug(dev)
        if p is None or p.get("gone"):
            return self._send(404, render.message_page("Not found", "Unknown plug."))
        v = form.get("onoff")
        if v not in ("on", "off"):
            self._flash(session, "error", "Invalid switch request.")
            return self._redirect("/")
        ok = False
        try:
            ok = bool(self.app.core.switch(dev, 1 if v == "on" else 0))
        except Exception:
            ok = False
        self.app.audit.write("web", "manual_switch", ip=self.client_address[0], dev=dev, name=p["name"],
                             onoff=v, ok=ok)
        log_signal(log, "ui_switch", "info" if ok else "warning", plug=p["name"], onoff=v, ok=ok,
                   client=self.client_address[0])
        self._flash(session, "ok" if ok else "error",
                    f"{p['name']}: {v.upper()} " + ("sent." if ok else "failed (cloud not reachable?)."))
        self._redirect("/")


def tail_file(path: Path, lines: int) -> str:
    """Last `lines` lines of a text file, reading at most LOG_TAIL_BYTES from its end."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    parts = text.splitlines()
    if size > LOG_TAIL_BYTES and parts:
        parts = parts[1:]  # first line may be cut in the middle
    return "\n".join(parts[-lines:]) + ("\n" if parts else "")


class WebServer:
    def __init__(self, app: WebApp, bind: str, port: int, ssl_context: ssl.SSLContext | None = None) -> None:
        handler = type("BoundHandler", (Handler,), {"app": app})
        self.httpd = ThreadingHTTPServer((bind, port), handler)
        self.httpd.daemon_threads = True
        if ssl_context:
            self.httpd.socket = ssl_context.wrap_socket(self.httpd.socket, server_side=True)
        self.app = app
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)

    def start(self) -> "WebServer":
        self.thread.start()
        return self

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def start_web(svc, webcfg, data_dir: Path) -> WebServer:
    tls_ctx = None
    if webcfg.tls_cert and webcfg.tls_key:
        tls_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_ctx.load_cert_chain(webcfg.tls_cert, webcfg.tls_key)
    app = WebApp(ServiceCore(svc), data_dir, idle=webcfg.session_idle_seconds,
                 maximum=webcfg.session_max_seconds, tls=tls_ctx is not None,
                 refresh=webcfg.refresh_seconds, log_dir=Path(data_dir) / "logs")
    srv = WebServer(app, webcfg.bind, webcfg.port, tls_ctx).start()
    log.info(kv("web_ui_started", port=srv.port, tls=tls_ctx is not None,
                password_set=app.passwords.exists()))
    if tls_ctx is None:
        log.warning(kv("web_ui_plain_http", note="password and session cookie cross the LAN unencrypted"))
    if not app.passwords.exists():
        log.warning(kv("web_ui_not_configured", fix="docker exec -it innr-autooff innr-autooff set-password"))
    return srv
