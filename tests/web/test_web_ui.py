# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Web UI security and behaviour (design 17.6, R11)."""
import http.client
import json
import re
from urllib.parse import urlencode

import pytest

from innr_autooff.web import server as srv
from innr_autooff.web.auth import Lockout, PasswordFile, SessionStore, check_password_policy, lockout_seconds

PW = "correct horse battery"
A = "a" * 32
B = "b" * 32
DEVICE_PASSWORD = "DEVPW-should-never-appear-7Hq"
TOKEN = "TOKEN-should-never-appear-x9Z"

EXPECTED = {
    ("GET", "/health"): "public",
    ("GET", r"/static/app\.css"): "public",
    ("GET", r"/static/app\.js"): "public",
    ("GET", "/api/state"): "auth",
    ("GET", r"/log/(?P<name>signals|decisions)"): "auth",
    ("GET", "/login"): "public",
    ("POST", "/login"): "public",
    ("POST", "/logout"): "auth",
    ("GET", "/"): "auth",
    ("GET", r"/plug/(?P<dev>[0-9a-f]{32})"): "auth",
    ("POST", r"/plug/(?P<dev>[0-9a-f]{32})/settings"): "auth",
    ("POST", r"/plug/(?P<dev>[0-9a-f]{32})/switch"): "auth",
}
AUTH_GETS = ["/", f"/plug/{A}"]
AUTH_POSTS = ["/logout", f"/plug/{A}/settings", f"/plug/{A}/switch"]


class FakeCore:
    def __init__(self):
        self._secret_password = DEVICE_PASSWORD  # the core knows secrets; the UI must never show them
        self._token = TOKEN
        self.settings_calls = []
        self.switch_calls = []
        self.switch_ok = True
        self.plugs = [
            {"dev": A, "name": "Coffee <script>alert(1)</script> \"q\" 'x'", "model": "Innr.SP 240", "state": "on",
             "switching": False, "enabled": True, "delay_minutes": 30, "remaining": 754, "power_w": 1200.4,
             "off_failed": True, "last_result": "<b>x</b>", "gone": False, "stale": False, "online": True,
             "phase": None, "attempt": 0, "next_in": None, "last_error": ""},
            {"dev": B, "name": "Balcony", "model": "Innr.OSP 240", "state": "off", "switching": False,
             "enabled": False, "delay_minutes": 30, "remaining": None, "power_w": None, "off_failed": False,
             "last_result": "", "gone": False, "stale": False, "online": True},
        ]

    def snapshot(self):
        return {"plugs": [dict(p) for p in self.plugs], "version": "0.1.0", "local_connected": True,
                "cloud_feed_connected": False, "cloud_feed_enabled": True, "cloud_login_ok": True,
                "cloud_error": "", "reconcile_age": 12, "uptime": 3700, "healthy": True}

    def set_settings(self, dev, enabled, minutes):
        self.settings_calls.append((dev, enabled, minutes))

    def switch(self, dev, onoff):
        self.switch_calls.append((dev, onoff))
        return self.switch_ok


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    core, clock = FakeCore(), Clock()
    app = srv.WebApp(core, tmp_path, idle=600, maximum=3600, tls=False, clock=clock)
    app.passwords.set(PW)
    server = srv.WebServer(app, "127.0.0.1", 0).start()
    yield server, core, clock, tmp_path
    server.shutdown()


def req(server, method, path, body=None, cookie=None, headers=None, raw_body=None):
    c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    h = dict(headers or {})
    data = raw_body
    if body is not None:
        data = urlencode(body).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if cookie:
        h["Cookie"] = f"innr_session={cookie}"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    text = r.read().decode("utf-8", errors="replace")
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, text


def login(server, pw=PW):
    st, h, _ = req(server, "POST", "/login", {"password": pw})
    assert st == 303, st
    return re.search(r"innr_session=([^;]*)", h["set-cookie"]).group(1)


def csrf_of(server, cookie):
    _, _, text = req(server, "GET", "/", cookie=cookie)
    return re.search(r'name="csrf_token" value="([0-9a-f]{64})"', text).group(1)


# ---------------------------------------------------------------- route table / deny by default
def test_route_matrix_is_exactly_expected():
    found = {(r.method, r.pattern): r.access for r in srv.ROUTES}
    assert found == EXPECTED
    for r in srv.ROUTES:
        assert hasattr(srv.Handler, r.handler)


def test_route_without_access_level_is_denied(tmp_path):
    routes = srv.ROUTES + (srv.Route("GET", r"/secret", "", "h_dashboard"),)
    app = srv.WebApp(FakeCore(), tmp_path, idle=600, maximum=3600, tls=False, routes=routes)
    app.passwords.set(PW)
    s = srv.WebServer(app, "127.0.0.1", 0).start()
    try:
        assert req(s, "GET", "/secret")[0] == 403
    finally:
        s.shutdown()


@pytest.mark.parametrize("path", ["/admin", "/plug/xyz", f"/plug/{A}/delete", "/static/../etc/passwd",
                                  "/static/other.css", "/.env", "/plug/" + "A" * 32, "/%2e%2e/x"])
def test_unknown_paths_404(env, path):
    assert req(env[0], "GET", path)[0] == 404


def test_double_slash_paths_never_leave_the_site(env):
    # http.server collapses leading '//' (open-redirect guard); must never be treated as a host
    st, h, _ = req(env[0], "GET", "//evil.example/x")
    assert st == 404
    st, h, _ = req(env[0], "GET", "//")
    assert st == 303 and h["location"] == "/login"


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
def test_other_methods_405(env, method):
    assert req(env[0], method, "/")[0] == 405


def test_wrong_method_on_known_path_405(env):
    assert req(env[0], "GET", "/logout")[0] == 405
    assert req(env[0], "POST", "/", {"a": "b"})[0] == 405


# ---------------------------------------------------------------- authentication
@pytest.mark.parametrize("path", AUTH_GETS)
def test_unauthenticated_get_redirects_to_login(env, path):
    st, h, text = req(env[0], "GET", path)
    assert st == 303 and h["location"] == "/login" and "Coffee" not in text


@pytest.mark.parametrize("path", AUTH_POSTS)
def test_unauthenticated_post_redirects_and_changes_nothing(env, path):
    st, h, _ = req(env[0], "POST", path, {"csrf_token": "x", "onoff": "off", "minutes": "5"})
    assert st == 303 and h["location"] == "/login"
    assert env[1].settings_calls == [] and env[1].switch_calls == []


def test_login_success_and_cookie_flags(env):
    st, h, _ = req(env[0], "POST", "/login", {"password": PW})
    assert st == 303 and h["location"] == "/"
    ck = h["set-cookie"]
    assert "HttpOnly" in ck and "SameSite=Strict" in ck and "Path=/" in ck and "Secure" not in ck


def test_cookie_secure_when_tls(tmp_path):
    app = srv.WebApp(FakeCore(), tmp_path, idle=600, maximum=3600, tls=True)

    class H(srv.Handler):
        pass
    H.app = app
    h = H.__new__(H)
    assert "Secure" in h._session_cookie("t", 10)


def test_wrong_password_generic_message(env):
    st, _, text = req(env[0], "POST", "/login", {"password": "wrong-password-123"})
    assert st == 401 and "Invalid password." in text


def test_lockout_after_three_failures_blocks_even_correct_password(env):
    s = env[0]
    for _ in range(2):
        assert req(s, "POST", "/login", {"password": "nope-nope-nope"})[0] == 401
    st, h, _ = req(s, "POST", "/login", {"password": "nope-nope-nope"})
    assert st == 429
    st, h, _ = req(s, "POST", "/login", {"password": PW})
    assert st == 429 and int(h["retry-after"]) > 0
    env[2].t += 61
    assert req(s, "POST", "/login", {"password": PW})[0] == 303


def test_session_idle_and_absolute_expiry(env):
    s, _, clock, _ = env
    ck = login(s)
    clock.t += 599
    assert req(s, "GET", "/", cookie=ck)[0] == 200
    clock.t += 601
    assert req(s, "GET", "/", cookie=ck)[0] == 303  # idle expired
    ck = login(s)
    for _ in range(7):  # keep it active: 7 x 500 s = 3500 s < 3600 s absolute
        clock.t += 500
        assert req(s, "GET", "/", cookie=ck)[0] == 200
    clock.t += 100
    assert req(s, "GET", "/", cookie=ck)[0] == 303  # absolute 3600 s reached despite activity


def test_new_token_per_login_and_logout_invalidates(env):
    s = env[0]
    c1, c2 = login(s), login(s)
    assert c1 != c2
    csrf = csrf_of(s, c2)
    assert req(s, "POST", "/logout", {"csrf_token": csrf}, cookie=c2)[0] == 303
    assert req(s, "GET", "/", cookie=c2)[0] == 303
    assert req(s, "GET", "/", cookie=c1)[0] == 200


def test_forged_cookie_rejected(env):
    assert req(env[0], "GET", "/", cookie="x" * 43)[0] == 303
    assert req(env[0], "GET", "/", headers={"Cookie": "innr_session=" + "y" * 500})[0] == 303


def test_not_configured_refuses_everything_but_health_and_css(tmp_path):
    app = srv.WebApp(FakeCore(), tmp_path, idle=600, maximum=3600, tls=False)
    s = srv.WebServer(app, "127.0.0.1", 0).start()
    try:
        for m, p in [("GET", "/"), ("GET", "/login"), ("POST", "/login")]:
            st, _, text = req(s, m, p, {"password": "x"} if m == "POST" else None)
            assert st == 503 and "set-password" in text
        assert req(s, "GET", "/health")[0] == 200
        assert req(s, "GET", "/static/app.css")[0] == 200
        assert req(s, "GET", "/static/app.js")[0] == 200
    finally:
        s.shutdown()


# ---------------------------------------------------------------- CSRF
@pytest.mark.parametrize("path", AUTH_POSTS)
@pytest.mark.parametrize("token", [None, "", "0" * 64, "not-hex"])
def test_every_post_requires_valid_csrf(env, path, token):
    s, core = env[0], env[1]
    ck = login(s)
    body = {"onoff": "off", "minutes": "5", "enabled": "on"}
    if token is not None:
        body["csrf_token"] = token
    assert req(s, "POST", path, body, cookie=ck)[0] == 403
    assert core.settings_calls == [] and core.switch_calls == []
    assert req(s, "GET", "/", cookie=ck)[0] == 200  # session still valid (logout not executed)


def test_csrf_token_of_other_session_rejected(env):
    s, core = env[0], env[1]
    c1, c2 = login(s), login(s)
    csrf1 = csrf_of(s, c1)
    st, _, _ = req(s, "POST", f"/plug/{A}/switch", {"csrf_token": csrf1, "onoff": "off"}, cookie=c2)
    assert st == 403 and core.switch_calls == []


# ---------------------------------------------------------------- functions
def test_settings_change_and_switch(env):
    s, core = env[0], env[1]
    ck = login(s)
    csrf = csrf_of(s, ck)
    st, h, _ = req(s, "POST", f"/plug/{B}/settings", {"csrf_token": csrf, "enabled": "on", "minutes": "45"}, cookie=ck)
    assert st == 303 and core.settings_calls == [(B, True, 45)]
    st, _, _ = req(s, "POST", f"/plug/{B}/settings", {"csrf_token": csrf, "minutes": "10"}, cookie=ck)
    assert core.settings_calls[-1] == (B, False, 10)
    _, _, text = req(s, "GET", "/", cookie=ck)
    assert "Saved: Balcony" in text
    st, _, _ = req(s, "POST", f"/plug/{A}/switch", {"csrf_token": csrf, "onoff": "off"}, cookie=ck)
    assert st == 303 and core.switch_calls == [(A, 0)]
    core.switch_ok = False
    req(s, "POST", f"/plug/{A}/switch", {"csrf_token": csrf, "onoff": "on"}, cookie=ck)
    _, _, text = req(s, "GET", "/", cookie=ck)
    assert "failed" in text


@pytest.mark.parametrize("minutes", ["0", "1441", "-5", "abc", "1.5", "", "99999999", "１２"])
def test_invalid_minutes_rejected(env, minutes):
    s, core = env[0], env[1]
    ck = login(s)
    csrf = csrf_of(s, ck)
    st, h, _ = req(s, "POST", f"/plug/{A}/settings", {"csrf_token": csrf, "minutes": minutes}, cookie=ck)
    assert st == 303 and h["location"] == f"/plug/{A}"
    assert core.settings_calls == []


def test_invalid_switch_value_rejected(env):
    s, core = env[0], env[1]
    ck = login(s)
    req(s, "POST", f"/plug/{A}/switch", {"csrf_token": csrf_of(s, ck), "onoff": "toggle"}, cookie=ck)
    assert core.switch_calls == []


def test_unknown_plug_404(env):
    s = env[0]
    ck = login(s)
    c = "c" * 32
    assert req(s, "GET", f"/plug/{c}", cookie=ck)[0] == 404
    assert req(s, "POST", f"/plug/{c}/switch", {"csrf_token": csrf_of(s, ck), "onoff": "off"}, cookie=ck)[0] == 404


# ---------------------------------------------------------------- request limits
def test_body_too_large_413(env):
    s = env[0]
    st, _, _ = req(s, "POST", "/login", raw_body=b"password=" + b"x" * 20000,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert st == 413
    assert req(s, "GET", "/health")[0] == 200  # server still up


def test_wrong_content_type_415(env):
    st, _, _ = req(env[0], "POST", "/login", raw_body=b'{"password":"x"}', headers={"Content-Type": "application/json"})
    assert st == 415


@pytest.mark.parametrize("prefix", [
    "POST /login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n",      # 415, body unread
    f"POST /plug/{A}/switch HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n",  # 303, unauthenticated
    "POST /nowhere HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n",  # 404
])
def test_unread_body_cannot_smuggle_a_second_request(env, prefix):
    import socket
    smuggled = b"GET /static/app.css HTTP/1.1\r\nHost: x\r\n\r\n"
    raw = prefix.encode() + f"Content-Length: {len(smuggled)}\r\n\r\n".encode() + smuggled
    s = socket.create_connection(("127.0.0.1", env[0].port), timeout=5)
    s.sendall(raw)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    assert data.count(b"HTTP/1.1 ") == 1  # exactly one response, then the server closed
    assert b"Connection: close" in data


def test_too_many_fields_400(env):
    body = "&".join(f"f{i}=1" for i in range(50)).encode()
    st, _, _ = req(env[0], "POST", "/login", raw_body=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert st == 400


# ---------------------------------------------------------------- headers, escaping, secrets
def test_security_headers_on_every_response(env):
    s = env[0]
    ck = login(s)
    for m, p, c in [("GET", "/login", None), ("GET", "/", ck), ("GET", "/nope", None), ("GET", "/health", None),
                    ("GET", "/static/app.css", None), ("GET", "/static/app.js", None), ("GET", "/api/state", ck),
                    ("GET", f"/plug/{A}", ck), ("GET", "/log/signals", ck), ("GET", "/log/decisions", ck)]:
        _, h, _ = req(s, m, p, cookie=c)
        csp = h["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self';" in csp and "connect-src 'self';" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert "cache-control" in h
        assert h.get("server", "").startswith("innr-autooff")


def test_dynamic_pages_not_cached(env):
    s = env[0]
    ck = login(s)
    assert req(s, "GET", "/", cookie=ck)[1]["cache-control"] == "no-store"


def test_untrusted_names_are_escaped(env):
    s = env[0]
    ck = login(s)
    for path in ["/", f"/plug/{A}"]:
        _, _, text = req(s, "GET", path, cookie=ck)
        assert "<script>" not in text and "&lt;script&gt;" in text
        assert "<b>x</b>" not in text
    page = req(s, "GET", "/", cookie=ck)[2]
    assert page.count("<script") == 1 and '<script src="/static/app.js" defer></script>' in page


def test_no_page_contains_secrets(env):
    s = env[0]
    ck = login(s)
    for m, p in [("GET", "/"), ("GET", f"/plug/{A}"), ("GET", f"/plug/{B}"), ("GET", "/login"), ("GET", "/health")]:
        st, h, text = req(s, m, p, cookie=ck)
        blob = text + str(h)
        assert DEVICE_PASSWORD not in blob and TOKEN not in blob and PW not in blob


def test_dashboard_content(env):
    s = env[0]
    ck = login(s)
    _, _, text = req(s, "GET", "/", cookie=ck)
    assert "switches off in 12:34" in text and "1200 W" in text and "auto-off failed" in text
    # no full-page reload when JavaScript runs (that swallowed clicks); <noscript> fallback only
    assert '<noscript><meta http-equiv="refresh" content="1"></noscript>' in text
    assert text.count('http-equiv="refresh"') == 1
    assert 'data-refresh="1"' in text and '<script src="/static/app.js" defer></script>' in text
    assert "Updates every second" in text
    assert "auto-off disabled" in text


def test_plug_settings_page_does_not_auto_refresh(env):
    s = env[0]
    ck = login(s)
    _, _, text = req(s, "GET", f"/plug/{A}", cookie=ck)
    assert 'http-equiv="refresh"' not in text  # never reload a form while it is being edited


def test_refresh_interval_configurable(tmp_path):
    app = srv.WebApp(FakeCore(), tmp_path, idle=600, maximum=3600, tls=False, refresh=5)
    app.passwords.set(PW)
    s = srv.WebServer(app, "127.0.0.1", 0).start()
    try:
        ck = login(s)
        _, _, text = req(s, "GET", "/", cookie=ck)
        assert '<meta http-equiv="refresh" content="5">' in text and 'data-refresh="5"' in text and "every 5 s" in text
    finally:
        s.shutdown()


def test_flash_message_survives_fast_reloads_then_expires(env):
    s, _, clock, _ = env
    ck = login(s)
    csrf = csrf_of(s, ck)
    req(s, "POST", f"/plug/{B}/settings", {"csrf_token": csrf, "enabled": "on", "minutes": "45"}, cookie=ck)
    for _ in range(4):  # four 1 s reloads: message still visible
        clock.t += 1
        assert "Saved: Balcony" in req(s, "GET", "/", cookie=ck)[2]
    clock.t += 2
    assert "Saved: Balcony" not in req(s, "GET", "/", cookie=ck)[2]


def test_audit_log_written_without_secrets(env):
    s, _, _, tmp = env
    req(s, "POST", "/login", {"password": "nope-nope-nope"})
    ck = login(s)
    csrf = csrf_of(s, ck)
    req(s, "POST", f"/plug/{A}/switch", {"csrf_token": csrf, "onoff": "off"}, cookie=ck)
    log = (tmp / "audit.log").read_text()
    for action in ("login_failure", "login_success", "manual_switch"):
        assert action in log
    assert PW not in log and csrf not in log and ck not in log and "nope-nope" not in log


# ---------------------------------------------------------------- auth primitives
def test_password_file_roundtrip_and_mode(tmp_path):
    p = PasswordFile(tmp_path / "web_auth.json")
    assert not p.exists() and p.verify("anything-long") is False
    p.set(PW)
    assert p.exists() and p.verify(PW) and not p.verify(PW + "x")
    assert oct((tmp_path / "web_auth.json").stat().st_mode & 0o777) == "0o600"
    assert PW not in (tmp_path / "web_auth.json").read_text()


@pytest.mark.parametrize("pw,ok", [("short", False), ("a" * 129, False), ("password1234", False),
                                   ("aaaaaaaaaaaaaa", False), (PW, True)])
def test_password_policy(pw, ok):
    assert (check_password_policy(pw) is None) is ok


def test_lockout_table():
    assert [lockout_seconds(n) for n in (1, 2, 3, 4, 5, 9, 10, 50)] == [0, 0, 60, 60, 300, 300, 900, 900]
    clock = Clock()
    lo = Lockout(clock)
    for _ in range(3):
        lo.fail("ip")
    assert lo.remaining("ip") == 60
    lo.success("ip")
    assert lo.remaining("ip") == 0


def test_lockout_is_bounded():
    lo = Lockout(Clock())
    lo.MAX_KEYS = 100
    for i in range(500):
        lo.fail(f"k{i}")
    assert len(lo._d) == 100


def test_session_tokens_stored_hashed():
    st = SessionStore(60, 600, Clock())
    tok = st.create()
    assert tok not in st._s and all(len(k) == 64 for k in st._s)


# ---------------------------------------------------------------- live updates without page reloads
def test_api_state_requires_login_and_returns_401_not_a_page(env):
    st, h, text = req(env[0], "GET", "/api/state")
    assert st == 401 and h["content-type"].startswith("application/json")
    assert "Coffee" not in text and "Balcony" not in text


def test_api_state_content_matches_dashboard(env):
    s = env[0]
    ck = login(s)
    st, h, text = req(s, "GET", "/api/state", cookie=ck)
    assert st == 200 and h["content-type"] == "application/json" and h["cache-control"] == "no-store"
    data = json.loads(text)
    a = next(p for p in data["plugs"] if p["dev"] == A)
    assert a["badge"] == ["on", "ON"] and a["power"] == "1200 W" and a["remaining"] == "switches off in 12:34"
    assert a["failed"] is True and a["last"] == "last: <b>x</b>"  # raw text; the script writes it with textContent
    b = next(p for p in data["plugs"] if p["dev"] == B)
    assert b["badge"] == ["", "OFF"] and b["auto"] == "auto-off disabled" and b["remaining"] == ""
    assert data["status"]["local"] == ["on", "connected"] and data["status"]["sync"] == "last sync: 0:12 ago"
    for word in (DEVICE_PASSWORD, TOKEN, PW):
        assert word not in text


def test_dashboard_has_targets_for_every_live_field(env):
    s = env[0]
    ck = login(s)
    page = req(s, "GET", "/", cookie=ck)[2]
    for dev in (A, B):
        assert f'data-dev="{dev}"' in page
    for f in ("name", "state", "power", "auto", "overdue", "remaining", "failed", "stale", "last", "buttons"):
        assert page.count(f'data-f="{f}"') == 2, f
    for st in ("local", "cloudfeed", "login", "sync", "error", "uptime"):
        assert f'data-s="{st}"' in page, st
    assert "data-dashboard" in page


def test_polling_does_not_extend_the_session(env):
    s, _, clock, _ = env
    ck = login(s)
    clock.t += 500
    assert req(s, "GET", "/api/state", cookie=ck)[0] == 200
    clock.t += 101  # 601 s since the last real request, although polled in between
    assert req(s, "GET", "/api/state", cookie=ck)[0] == 401
    assert req(s, "GET", "/", cookie=ck)[0] == 303


def test_navigation_still_extends_the_session(env):
    s, _, clock, _ = env
    ck = login(s)
    clock.t += 500
    assert req(s, "GET", "/", cookie=ck)[0] == 200
    clock.t += 500
    assert req(s, "GET", "/api/state", cookie=ck)[0] == 200


def test_script_is_static_and_never_injects_html():
    from innr_autooff.web import render
    js = render.JS
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                "setTimeout('", 'setTimeout("', "http://", "https://"):
        assert bad not in js, bad
    assert "textContent" in js and "'/api/state'" in js


def test_no_inline_scripts_or_event_handlers_in_any_page(env):
    s = env[0]
    ck = login(s)
    for path in ["/", f"/plug/{A}", "/login"]:
        page = req(s, "GET", path, cookie=ck)[2]
        for m in re.finditer(r"<script\b[^>]*>", page):
            assert 'src="/static/app.js"' in m.group(0)
        assert "</script>" not in page.replace('<script src="/static/app.js" defer></script>', "")
        assert not re.search(r"\son[a-z]+\s*=", page)


def test_script_served_as_javascript(env):
    st, h, text = req(env[0], "GET", "/static/app.js")
    assert st == 200 and h["content-type"].startswith("text/javascript") and "use strict" in text


# ---------------------------------------------------------------- OVERDUE display (R2.5, R2.7)
def overdue_plug(phase, attempt=7, next_in=20, last="OFF attempt 6 failed: ConnectionError: boom"):
    p = dict(FakeCore().plugs[1])
    p.update(state="on", switching=True, enabled=True, remaining=None, phase=phase, attempt=attempt,
             next_in=next_in, off_failed=attempt >= 3, last_result=last, last_error="ConnectionError: boom")
    return p


@pytest.mark.parametrize("phase,expected", [
    ("retry_wait", "OVERDUE: attempt 7 failed, next try in 20 s"),
    ("sending", "OVERDUE: sending OFF (attempt 7)"),
    ("waiting_plug", "OVERDUE: attempt 7 accepted, waiting for the plug"),
    ("verifying", "OVERDUE: checking the plug state in the cloud"),
])
def test_overdue_text_per_phase_and_never_a_countdown(env, phase, expected):
    s, core = env[0], env[1]
    core.plugs[1] = overdue_plug(phase)
    ck = login(s)
    data = json.loads(req(s, "GET", "/api/state", cookie=ck)[2])
    b = next(p for p in data["plugs"] if p["dev"] == B)
    assert b["overdue"] == expected and b["remaining"] == "" and b["badge"] == ["warn", "OVERDUE"]
    assert "ConnectionError: boom" in b["last"]
    page = req(s, "GET", "/", cookie=ck)[2]
    assert expected in page and "switches off in" not in page.split(B)[1].split("</section>")[0]


def test_not_overdue_plug_has_empty_overdue_text(env):
    s = env[0]
    ck = login(s)
    data = json.loads(req(s, "GET", "/api/state", cookie=ck)[2])
    assert all(p["overdue"] == "" for p in data["plugs"])


# ---------------------------------------------------------------- log pages (R12.6)
def test_log_pages_require_login(env):
    for name in ("signals", "decisions"):
        st, h, text = req(env[0], "GET", f"/log/{name}")
        assert st == 303 and h["location"] == "/login"


def test_log_pages_show_tail_as_plain_text(env):
    s, _, _, tmp = env
    (tmp / "logs").mkdir()
    lines = [f"2026-11-12T10:00:{i % 60:02d}+00:00 INFO kind=signal event=push n={i}" for i in range(900)]
    (tmp / "logs" / "signals.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp / "logs" / "decisions.log").write_text("2026-11-12T10:00:00+00:00 INFO kind=decision event=timer_started <b>x</b>\n")
    ck = login(s)
    st, h, text = req(s, "GET", "/log/signals", cookie=ck)
    assert st == 200 and h["content-type"].startswith("text/plain") and h["x-content-type-options"] == "nosniff"
    got = text.splitlines()
    assert len(got) == 500 and got[-1].endswith("n=899") and got[0].endswith("n=400")
    st, h, text = req(s, "GET", "/log/decisions", cookie=ck)
    assert "event=timer_started" in text and "<b>x</b>" in text  # plain text: shown as is, never interpreted
    assert h["content-type"].startswith("text/plain")


def test_log_page_without_file_says_so(env):
    s = env[0]
    ck = login(s)
    st, _, text = req(s, "GET", "/log/signals", cookie=ck)
    assert st == 200 and "no signals logged yet" in text


@pytest.mark.parametrize("path", ["/log/other", "/log/signals/../x", "/log/", "/log", "/log/..%2fstate.json",
                                  "/log/signals.log", "/log/Signals"])
def test_log_route_cannot_reach_other_files(env, path):
    s = env[0]
    ck = login(s)
    assert req(s, "GET", path, cookie=ck)[0] == 404


def test_log_tail_reads_only_the_end_of_huge_files(tmp_path):
    from innr_autooff.web.server import LOG_TAIL_BYTES, tail_file
    f = tmp_path / "big.log"
    f.write_text("".join(f"line {i:08d}\n" for i in range(200_000)))  # about 2.6 MB
    assert f.stat().st_size > 4 * LOG_TAIL_BYTES
    out = tail_file(f, 500).splitlines()
    assert len(out) == 500 and out[-1] == "line 00199999" and out[0] == "line 00199500"
    assert tail_file(tmp_path / "missing.log", 10) == ""


def test_dashboard_links_to_the_logs(env):
    s = env[0]
    ck = login(s)
    page = req(s, "GET", "/", cookie=ck)[2]
    assert 'href="/log/signals"' in page and 'href="/log/decisions"' in page


def test_ui_actions_are_logged_as_signals_without_secrets(env, caplog):
    import logging
    s = env[0]
    with caplog.at_level(logging.INFO):
        ck = login(s)
        csrf = csrf_of(s, ck)
        req(s, "POST", f"/plug/{B}/settings", {"csrf_token": csrf, "enabled": "on", "minutes": "45"}, cookie=ck)
        req(s, "POST", f"/plug/{A}/switch", {"csrf_token": csrf, "onoff": "off"}, cookie=ck)
        req(s, "POST", "/login", {"password": "nope-nope-nope"})
    msgs = [r.getMessage() for r in caplog.records if getattr(r, "kind", None) == "signal"]
    joined = "\n".join(msgs)
    for ev in ("ui_login", "ui_settings", "ui_switch"):
        assert f"event={ev}" in joined, ev
    assert "result=success" in joined and "result=failure" in joined and "minutes=45" in joined
    assert PW not in joined and csrf not in joined and ck not in joined and "nope-nope" not in joined
