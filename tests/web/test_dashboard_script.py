# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
"""Runs the dashboard script (render.JS) in Node against a minimal fake DOM.

Proves: fields are updated in place, buttons/forms are never replaced (the bug
that swallowed clicks), a changed plug set triggers one reload, 401 and
redirects go to /login, hidden tabs do not poll. Skipped when node is absent.
"""
import json
import shutil
import subprocess

import pytest

from innr_autooff.web import render

HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1); };

function el(attrs) { return { attrs, textContent: '', hidden: false, className: '', getAttribute(k) { return this.attrs[k]; } }; }
function card(dev) {
  const c = el({'data-dev': dev});
  c.fields = {};
  for (const f of ['name','state','power','auto','overdue','remaining','failed','stale','last','buttons']) c.fields[f] = el({'data-f': f});
  c.button = el({type: 'submit'});           // the ON button inside data-f="buttons"
  c.querySelector = (sel) => { const m = sel.match(/data-f="([a-z]+)"/); return m ? c.fields[m[1]] : null; };
  return c;
}
const A = 'a'.repeat(32), B = 'b'.repeat(32);
let cards = [card(A), card(B)];
const root = el({'data-dashboard': '', 'data-refresh': '1'});
const status = {}; for (const s of ['local','cloudfeed','login','sync','error','uptime']) status[s] = el({'data-s': s});
const listeners = {};
const document = {
  hidden: false,
  querySelector(sel) {
    if (sel === '[data-dashboard]') return root;
    const m = sel.match(/data-s="([a-z]+)"/); return m ? status[m[1]] : null;
  },
  querySelectorAll(sel) { return sel === '[data-dev]' ? cards : []; },
  addEventListener(ev, fn) { listeners[ev] = fn; },
};
let reloads = 0, assigned = null, intervalFn = null, intervalMs = null, fetches = 0;
let nextResponse = null;
const window = {
  location: { reload() { reloads++; }, assign(u) { assigned = u; } },
  setInterval(fn, ms) { intervalFn = fn; intervalMs = ms; },
  fetch: true,
};
function fetch(url, opts) {
  fetches++;
  if (url !== '/api/state') fail('unexpected url ' + url);
  if (opts.credentials !== 'same-origin' || opts.redirect !== 'manual') fail('fetch options');
  return Promise.resolve(nextResponse);
}
window.fetch = fetch;
const resp = (status, body, type) => ({ status, ok: status === 200, type: type || 'basic', json: () => Promise.resolve(body) });
const tick = () => new Promise((r) => setImmediate(r));
const state = JSON.parse(process.argv[3]);

(async () => {
  vm.runInNewContext(code, { document, window, fetch, JSON, Math, parseInt });
  if (intervalMs !== 1000) fail('interval should be 1000 ms, got ' + intervalMs);
  const buttonBefore = cards[0].button, fieldsBefore = cards[0].fields.state;

  nextResponse = resp(200, state);
  intervalFn(); await tick(); await tick(); await tick();
  const fa = cards[0].fields;
  if (fa.state.textContent !== 'ON' || fa.state.className !== 'badge on') fail('state badge ' + fa.state.className + '/' + fa.state.textContent);
  if (fa.remaining.textContent !== 'switches off in 12:34' || fa.remaining.hidden) fail('remaining');
  if (fa.overdue.textContent !== '' || fa.overdue.hidden !== true) fail('overdue must be empty and hidden for a normal plug');
  if (fa.power.textContent !== '1200 W') fail('power');
  if (fa.failed.hidden !== false) fail('failed flag should be visible');
  if (fa.last.textContent !== 'last: <b>x</b>') fail('last must be plain text');
  if (fa.name.textContent !== state.plugs[0].name) fail('name');
  const fb = cards[1].fields;
  if (fb.state.className !== 'badge' || fb.state.textContent !== 'OFF') fail('B badge');
  if (!fb.remaining.hidden) fail('empty remaining must be hidden');
  // a plug that becomes overdue: the line appears, the countdown disappears, buttons stay the same elements
  const od = JSON.parse(JSON.stringify(state));
  od.plugs[0].overdue = 'OVERDUE: attempt 3 failed, next try in 5 s'; od.plugs[0].remaining = ''; od.plugs[0].badge = ['warn', 'OVERDUE'];
  nextResponse = resp(200, od); intervalFn(); await tick(); await tick(); await tick();
  if (fa.overdue.textContent !== od.plugs[0].overdue || fa.overdue.hidden) fail('overdue text not shown');
  if (!fa.remaining.hidden || fa.remaining.textContent !== '') fail('countdown must disappear while overdue');
  if (fa.state.textContent !== 'OVERDUE' || fa.state.className !== 'badge warn') fail('overdue badge');
  if (cards[0].button !== buttonBefore) fail('button replaced during overdue update');
  nextResponse = resp(200, state); intervalFn(); await tick(); await tick(); await tick();
  if (fa.overdue.hidden !== true) fail('overdue line must disappear again');
  if (status.local.className !== 'badge on' || status.sync.textContent !== 'last sync: 0:12 ago') fail('status');
  if (cards[0].button !== buttonBefore || cards[0].fields.state !== fieldsBefore) fail('elements were replaced');
  if (reloads !== 0 || assigned !== null) fail('no navigation expected');

  // busy guard: a second poll while one is in flight is skipped
  let resolveSlow; nextResponse = new Promise((r) => { resolveSlow = r; });
  const before = fetches; intervalFn(); intervalFn();
  if (fetches !== before + 1) fail('overlapping polls');
  resolveSlow(resp(200, state)); await tick(); await tick(); await tick();

  // hidden tab: no polling
  document.hidden = true; const f0 = fetches; intervalFn(); if (fetches !== f0) fail('polled while hidden'); document.hidden = false;

  // plug set changed (new plug discovered): exactly one reload
  const more = JSON.parse(JSON.stringify(state)); more.plugs.push({...state.plugs[1], dev: 'c'.repeat(32)});
  nextResponse = resp(200, more); intervalFn(); await tick(); await tick(); await tick();
  if (reloads !== 1) fail('expected one reload, got ' + reloads);

  // session gone: 401 -> login page
  nextResponse = resp(401, {}); intervalFn(); await tick(); await tick(); await tick();
  if (assigned !== '/login') fail('401 should go to /login');
  assigned = null;
  nextResponse = resp(0, null, 'opaqueredirect'); intervalFn(); await tick(); await tick(); await tick();
  if (assigned !== '/login') fail('redirect should go to /login');

  // server error: no navigation, nothing breaks
  assigned = null; const r0 = reloads;
  nextResponse = resp(500, null); intervalFn(); await tick(); await tick(); await tick();
  if (assigned !== null || reloads !== r0) fail('500 must not navigate');
  console.log('OK');
})().catch((e) => fail(String(e)));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_script_updates_in_place(tmp_path):
    A, B = "a" * 32, "b" * 32
    snap = {"plugs": [
        {"dev": A, "name": "Coffee <script>", "model": "m", "state": "on", "switching": False, "enabled": True,
         "delay_minutes": 30, "remaining": 754, "power_w": 1200.4, "off_failed": True, "last_result": "<b>x</b>",
         "gone": False, "stale": False},
        {"dev": B, "name": "Balcony", "model": "m", "state": "off", "switching": False, "enabled": False,
         "delay_minutes": 30, "remaining": None, "power_w": None, "off_failed": False, "last_result": "",
         "gone": False, "stale": False}],
        "local_connected": True, "cloud_feed_connected": True, "cloud_feed_enabled": True,
        "cloud_login_ok": True, "cloud_error": "", "reconcile_age": 12, "uptime": 70}
    (tmp_path / "app.js").write_text(render.JS)
    (tmp_path / "harness.js").write_text(HARNESS)
    r = subprocess.run(["node", str(tmp_path / "harness.js"), str(tmp_path / "app.js"),
                        json.dumps(render.state_json(snap))], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "OK", r.stderr + r.stdout
