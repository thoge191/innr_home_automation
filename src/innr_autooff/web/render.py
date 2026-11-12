# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Server-rendered HTML. Every dynamic value passes through e() (html.escape).

The dashboard is updated in place by one static script (JS below): it polls
/api/state and writes plain text into elements marked with data-f / data-s.
Buttons, links and forms are never replaced, so clicks are never lost.
Without JavaScript the page falls back to a <noscript> meta refresh.
The HTML page and the JSON endpoint share plug_view()/status_view(), so both
always show the same values.
"""
from __future__ import annotations

import html
from typing import Any


def e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#5d6575;--on:#1a7f37;--off:#6e7781;--warn:#b35900;--err:#c62828;--acc:#0b57d0}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;justify-content:space-between;padding:.75rem 1rem;background:#1d2330;color:#fff}
header a{color:#fff;text-decoration:none;font-weight:600}main{max-width:960px;margin:0 auto;padding:1rem}
.grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.card{background:var(--card);border-radius:10px;padding:1rem;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{font-size:1.1rem;margin:0 0 .25rem}.card h2 a{color:var(--fg)}.mut{color:var(--mut);font-size:.9rem}
.badge{display:inline-block;padding:.1rem .55rem;border-radius:999px;font-size:.85rem;font-weight:600;color:#fff;background:var(--off)}
.badge.on{background:var(--on)}.badge.warn{background:var(--warn)}.badge.err{background:var(--err)}
.row{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0}.strong{font-weight:700}.overdue{color:var(--err)}
button,.btn{font:inherit;padding:.45rem .9rem;border-radius:8px;border:1px solid #c7ccd6;background:#fff;color:var(--fg);cursor:pointer;text-decoration:none}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #7aa7ff;outline-offset:2px}
form.inline{display:inline}label{display:block;margin:.6rem 0 .2rem;font-weight:600}
input[type=number],input[type=password]{font:inherit;padding:.45rem;border:1px solid #c7ccd6;border-radius:8px;width:100%;max-width:18rem}
.check{display:flex;gap:.5rem;align-items:center;font-weight:600;margin:.6rem 0}
.flash{padding:.6rem .9rem;border-radius:8px;margin-bottom:1rem;background:#e8f0fe}.flash.error{background:#fdecea;color:var(--err)}
.status{display:flex;flex-wrap:wrap;gap:.4rem 1rem;font-size:.9rem;margin-bottom:1rem}
footer{max-width:960px;margin:0 auto;padding:1rem;color:var(--mut);font-size:.85rem}
"""

# Static, same-origin only (CSP script-src 'self'). No inline code, no eval, no innerHTML:
# server data is only ever written with textContent / className / hidden.
JS = """'use strict';
(function () {
  var root = document.querySelector('[data-dashboard]');
  if (!root || !window.fetch) { return; }
  var every = Math.max(1, parseInt(root.getAttribute('data-refresh') || '1', 10)) * 1000;
  var busy = false;
  function q(sel, base) { return (base || document).querySelector(sel); }
  function badge(el, b) { if (el && b) { el.className = 'badge' + (b[0] ? ' ' + b[0] : ''); el.textContent = b[1]; } }
  function text(el, t) { if (el) { el.textContent = t || ''; el.hidden = !t; } }
  function flag(el, on) { if (el) { el.hidden = !on; } }
  function apply(s) {
    var cards = document.querySelectorAll('[data-dev]');
    var known = {};
    for (var i = 0; i < cards.length; i++) { known[cards[i].getAttribute('data-dev')] = cards[i]; }
    var same = s.plugs.length === cards.length && s.plugs.every(function (p) { return known[p.dev]; });
    if (!same) { window.location.reload(); return; }  // plug added or removed: re-render once
    s.plugs.forEach(function (p) {
      var c = known[p.dev];
      var name = q('[data-f="name"]', c); if (name) { name.textContent = p.name; }
      badge(q('[data-f="state"]', c), p.badge);
      text(q('[data-f="power"]', c), p.power);
      text(q('[data-f="auto"]', c), p.auto);
      text(q('[data-f="overdue"]', c), p.overdue);
      text(q('[data-f="remaining"]', c), p.remaining);
      flag(q('[data-f="failed"]', c), p.failed);
      flag(q('[data-f="stale"]', c), p.stale);
      text(q('[data-f="last"]', c), p.last);
      flag(q('[data-f="buttons"]', c), !p.gone);
    });
    var st = s.status;
    badge(q('[data-s="local"]'), st.local);
    badge(q('[data-s="cloudfeed"]'), st.cloudfeed);
    badge(q('[data-s="login"]'), st.login);
    text(q('[data-s="sync"]'), st.sync);
    text(q('[data-s="error"]'), st.error);
    var up = q('[data-s="uptime"]'); if (up) { up.textContent = st.uptime; }
  }
  function poll() {
    if (busy || document.hidden) { return; }
    busy = true;
    fetch('/api/state', {credentials: 'same-origin', redirect: 'manual', cache: 'no-store',
                         headers: {'Accept': 'application/json'}})
      .then(function (r) {
        if (r.status === 401 || r.type === 'opaqueredirect') { window.location.assign('/login'); return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (s) { if (s) { apply(s); } })
      .catch(function () {})
      .then(function () { busy = false; });
  }
  window.setInterval(poll, every);
  document.addEventListener('visibilitychange', poll);
})();
"""


def page(title: str, body: str, *, refresh: int | None = None, nav: str = "", script: bool = False) -> str:
    # refresh: fallback for browsers without JavaScript only (inside <noscript>)
    meta = f'<noscript><meta http-equiv="refresh" content="{int(refresh)}"></noscript>' if refresh else ""
    js = '<script src="/static/app.js" defer></script>' if script else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
            f'<title>{e(title)} - INNR auto-off</title><link rel="stylesheet" href="/static/app.css">{js}</head>'
            f'<body><header><a href="/">INNR auto-off</a>{nav}</header><main>{body}</main></body></html>')


def nav_logout(csrf: str) -> str:
    return (f'<form class="inline" method="post" action="/logout">'
            f'<input type="hidden" name="csrf_token" value="{e(csrf)}"><button type="submit">Log out</button></form>')


def flashes(items: list[tuple[str, str]]) -> str:
    return "".join(f'<div class="flash {e(kind)}" role="status">{e(msg)}</div>' for kind, msg in items)


def fmt_duration(sec: int | None) -> str:
    if sec is None:
        return ""
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- view model (shared by HTML and JSON)
def _conn(v: bool | None, ok: str = "connected", bad: str = "disconnected") -> list[str]:
    if v is None:
        return ["warn", "unknown"]
    return ["on", ok] if v else ["err", bad]


def _state(p: dict) -> list[str]:
    if p.get("gone"):
        return ["warn", "not in INNR account"]
    if p.get("switching"):
        return ["warn", "OVERDUE"]
    return {"on": ["on", "ON"], "off": ["", "OFF"]}.get(p.get("state"), ["warn", "unknown"])


def overdue_text(p: dict) -> str:
    """What an overdue plug (timer expired, not yet OFF) is doing right now. Never a countdown."""
    if not p.get("switching"):
        return ""
    n = p.get("attempt") or 0
    phase = p.get("phase")
    if phase == "retry_wait":
        return f"OVERDUE: attempt {n} failed, next try in {p.get('next_in') or 0} s"
    if phase == "waiting_plug":
        return f"OVERDUE: attempt {n} accepted, waiting for the plug"
    if phase == "verifying":
        return "OVERDUE: checking the plug state in the cloud"
    return f"OVERDUE: sending OFF (attempt {n})"


def plug_view(p: dict) -> dict[str, Any]:
    rem = p.get("remaining")
    return {
        "dev": p["dev"],
        "name": p["name"],
        "badge": _state(p),
        "power": f'{round(p["power_w"])} W' if p.get("power_w") is not None else "",
        "auto": f'auto-off after {p["delay_minutes"]} min' if p["enabled"] else "auto-off disabled",
        "overdue": overdue_text(p),
        "remaining": f"switches off in {fmt_duration(rem)}" if rem is not None else "",
        "failed": bool(p.get("off_failed")),
        "stale": bool(p.get("stale")),
        "last": f'last: {p["last_result"]}' if p.get("last_result") else "",
        "gone": bool(p.get("gone")),
    }


def status_view(snap: dict) -> dict[str, Any]:
    age = snap.get("reconcile_age")
    return {
        "local": _conn(snap.get("local_connected")),
        "cloudfeed": _conn(snap.get("cloud_feed_connected")) if snap.get("cloud_feed_enabled") else None,
        "login": _conn(snap.get("cloud_login_ok"), "ok", "failed"),
        "sync": f"last sync: {fmt_duration(age)} ago" if age is not None else "last sync: never",
        "error": f'Cloud: {snap["cloud_error"]}' if snap.get("cloud_error") else "",
        "uptime": fmt_duration(snap.get("uptime")),
    }


def state_json(snap: dict) -> dict[str, Any]:
    return {"plugs": [plug_view(p) for p in snap.get("plugs") or []], "status": status_view(snap)}


# ---------------------------------------------------------------- HTML
def _badge(b: list[str] | None, attr: str) -> str:
    if b is None:
        return ""
    cls = f"badge {b[0]}".strip()
    return f'<span class="{e(cls)}" {attr}>{e(b[1])}</span>'


def _opt(tag: str, cls: str, attr: str, text: str) -> str:
    """Element that is always present (so the script can fill it) and hidden while empty."""
    hidden = "" if text else " hidden"
    return f'<{tag} class="{cls}" {attr}{hidden}>{e(text)}</{tag}>'


def switch_form(dev: str, csrf: str, onoff: str, label: str) -> str:
    return (f'<form class="inline" method="post" action="/plug/{e(dev)}/switch">'
            f'<input type="hidden" name="csrf_token" value="{e(csrf)}">'
            f'<input type="hidden" name="onoff" value="{e(onoff)}">'
            f'<button type="submit" aria-label="Switch {e(label)} {e(onoff).upper()}">{e(onoff).upper()}</button></form>')


def plug_card(p: dict, csrf: str) -> str:
    v = plug_view(p)
    dev = e(p["dev"])
    return (f'<section class="card" data-dev="{dev}">'
            f'<h2><a href="/plug/{dev}" data-f="name">{e(v["name"])}</a></h2>'
            f'<div class="mut">{e(p["model"])}</div>'
            f'<div class="row">{_badge(v["badge"], "data-f=\"state\"")} {_opt("span", "", "data-f=\"power\"", v["power"])}</div>'
            + _opt("div", "row mut", 'data-f="auto"', v["auto"])
            + _opt("div", "row strong overdue", 'data-f="overdue"', v["overdue"])
            + _opt("div", "row strong", 'data-f="remaining"', v["remaining"])
            + f'<div class="row" data-f="failed"{"" if v["failed"] else " hidden"}>'
              '<span class="badge err">auto-off failed, retrying</span></div>'
            + f'<div class="row" data-f="stale"{"" if v["stale"] else " hidden"}>'
              '<span class="badge warn">state may be outdated</span></div>'
            + _opt("div", "row mut", 'data-f="last"', v["last"])
            + f'<div class="row" data-f="buttons"{" hidden" if v["gone"] else ""}>'
            + switch_form(p["dev"], csrf, "on", p["name"]) + " " + switch_form(p["dev"], csrf, "off", p["name"])
            + f' <a class="btn" href="/plug/{dev}">Settings</a></div></section>')


def dashboard(snap: dict, csrf: str, flash: list[tuple[str, str]], refresh: int = 1) -> str:
    s = status_view(snap)
    status = ('<div class="status">'
              f'<span>Bridge (local): {_badge(s["local"], "data-s=\"local\"")}</span>'
              + (f'<span>Cloud feed: {_badge(s["cloudfeed"], "data-s=\"cloudfeed\"")}</span>' if s["cloudfeed"] else "")
              + f'<span>Cloud login: {_badge(s["login"], "data-s=\"login\"")}</span>'
              + f'<span class="mut" data-s="sync">{e(s["sync"])}</span>'
              + "</div>")
    err = _opt("div", "flash error", 'data-s="error"', s["error"])
    plugs = snap.get("plugs") or []
    cards = "".join(plug_card(p, csrf) for p in plugs) or '<p>No plugs found yet. Waiting for the INNR cloud.</p>'
    every = "every second" if refresh == 1 else f"every {int(refresh)} s"
    foot = (f'<footer>Version {e(snap.get("version"))}, up <span data-s="uptime">{e(s["uptime"])}</span>. '
            f'Updates {every}. Logs: <a href="/log/signals">signals</a>, <a href="/log/decisions">decisions</a>.</footer>')
    body = (f'<div data-dashboard data-refresh="{int(refresh)}">' + flashes(flash) + err + status
            + f'<div class="grid">{cards}</div></div>' + foot)
    return page("Plugs", body, refresh=refresh, nav=nav_logout(csrf), script=True)


def plug_page(p: dict, csrf: str, flash: list[tuple[str, str]]) -> str:
    minutes = max(1, min(1440, int(round(float(p["delay_minutes"])))))
    checked = " checked" if p["enabled"] else ""
    body = (flashes(flash) + f'<section class="card"><h2>{e(p["name"])}</h2><div class="mut">{e(p["model"])}</div>'
            f'<div class="row">{_badge(_state(p), "")}</div>'
            f'<form method="post" action="/plug/{e(p["dev"])}/settings">'
            f'<input type="hidden" name="csrf_token" value="{e(csrf)}">'
            f'<div class="check"><input type="checkbox" id="enabled" name="enabled" value="on"{checked}>'
            f'<label for="enabled" style="margin:0">Switch off automatically</label></div>'
            f'<label for="minutes">Minutes after switching on (1 to 1440)</label>'
            f'<input type="number" id="minutes" name="minutes" min="1" max="1440" step="1" required value="{minutes}">'
            f'<div class="row"><button class="primary" type="submit">Save</button> <a class="btn" href="/">Back</a></div>'
            f'</form><p class="mut">A change applies to a running timer at once: the plug switches off '
            f'at the new time counted from when it was switched on.</p></section>')
    return page(p["name"], body, nav=nav_logout(csrf))


def login_page(message: str = "", kind: str = "error") -> str:
    msg = f'<div class="flash {e(kind)}" role="alert">{e(message)}</div>' if message else ""
    body = (f'<section class="card" style="max-width:26rem;margin:2rem auto">{msg}<h2>Sign in</h2>'
            '<form method="post" action="/login"><label for="password">Password</label>'
            '<input type="password" id="password" name="password" autocomplete="current-password" required autofocus>'
            '<div class="row"><button class="primary" type="submit">Sign in</button></div></form></section>')
    return page("Sign in", body)


def not_configured_page() -> str:
    body = ('<section class="card"><h2>Web UI not configured</h2><p>No password has been set yet. '
            'Set it on the host with:</p><pre>docker exec -it innr-autooff innr-autooff set-password</pre>'
            '<p>The auto-off service itself is running independently of this page.</p></section>')
    return page("Not configured", body)


def message_page(title: str, text: str) -> str:
    return page(title, f'<section class="card"><h2>{e(title)}</h2><p>{e(text)}</p>'
                       '<p><a class="btn" href="/">Back</a></p></section>')
