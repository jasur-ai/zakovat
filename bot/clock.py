"""The Mini App clock face: a countdown that runs on the user's own device.

Telegram messages can only be rewritten by the server, so on a slow network a
text countdown looks frozen. This page is opened from the strip's "Jonli soat"
button and keeps counting by itself: an analogue dial with a sweeping hand,
digital seconds and the familiar bar, all driven by the deadline in the URL.

Everything is rendered locally — no request per second, no dependency on the
bot being reachable. One optional call to /api/now corrects a wrong device
clock; if it fails the page simply trusts the device.
"""
from __future__ import annotations

import html
import json
import time

STYLE = """
    :root { color-scheme: dark; --accent: #7c5cff; --ok: #35d07f; --warn: #ffb020; --bad: #ff5c5c; }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body { margin: 0; background: #0b0b11; color: #f2f2f7; overflow: hidden;
           font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           display: flex; align-items: center; justify-content: center; }
    main { width: 100%; max-width: 420px; padding: 18px; text-align: center; }
    .head { font-size: 12.5px; letter-spacing: .12em; text-transform: uppercase; opacity: .65; }
    .pack { font-size: 13px; opacity: .5; margin: 2px 0 10px; white-space: nowrap;
            overflow: hidden; text-overflow: ellipsis; }
    .dial { width: min(74vw, 300px); height: auto; display: block; margin: 0 auto; }
    .track { fill: none; stroke: #23232f; stroke-width: 10; }
    .arc { fill: none; stroke: var(--ok); stroke-width: 10; stroke-linecap: round;
           transform: rotate(-90deg); transform-origin: 110px 110px;
           transition: stroke .4s linear; }
    .tickmark { stroke: #2f2f3d; stroke-width: 2; }
    .tickmark.major { stroke: #4a4a5e; stroke-width: 3; }
    .hand { stroke: #f2f2f7; stroke-width: 4; stroke-linecap: round;
            transform-origin: 110px 110px; }
    .hub { fill: #f2f2f7; }
    .digits { font-size: 54px; font-weight: 750; letter-spacing: 2px;
              font-variant-numeric: tabular-nums; margin-top: 6px; }
    .bar { letter-spacing: 3px; color: var(--accent); font-size: 17px; min-height: 26px; }
    .note { font-size: 12.5px; opacity: .45; margin-top: 10px; }
    .done { display: none; margin-top: 10px; font-weight: 750; letter-spacing: .08em; color: var(--bad); }
    .done span { display: block; font-weight: 400; letter-spacing: 0; font-size: 13px;
                 color: #f2f2f7; opacity: .6; margin-top: 6px; }
    body.over .done { display: block; }
    body.over .digits { color: var(--bad); }
    body.over .bar { color: var(--bad); }
    body.pulse .digits { animation: pulse 1s ease-in-out infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
"""

SCRIPT = """
(function () {
  var data = __DATA__;
  var TOTAL = Math.max(1, data.tm || 60);
  var RADIUS = 96, CIRC = 2 * Math.PI * RADIUS;
  var arc = document.getElementById('arc');
  var hand = document.getElementById('hand');
  var digits = document.getElementById('digits');
  var bar = document.getElementById('bar');
  var note = document.getElementById('note');
  var marks = document.getElementById('marks');

  arc.style.strokeDasharray = CIRC.toFixed(2);
  for (var i = 0; i < 60; i++) {
    var angle = (i * 6 - 90) * Math.PI / 180;
    var outer = i % 5 === 0 ? 84 : 88;
    var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', (110 + Math.cos(angle) * outer).toFixed(2));
    line.setAttribute('y1', (110 + Math.sin(angle) * outer).toFixed(2));
    line.setAttribute('x2', (110 + Math.cos(angle) * 92).toFixed(2));
    line.setAttribute('y2', (110 + Math.sin(angle) * 92).toFixed(2));
    line.setAttribute('class', i % 5 === 0 ? 'tickmark major' : 'tickmark');
    marks.appendChild(line);
  }

  var opened = Date.now() / 1000;
  var skew = 0;                       // server clock minus device clock
  var settled = false;

  function remaining() {
    if (data.d) { return data.d - (Date.now() / 1000 + skew); }
    return data.l - (Date.now() / 1000 - opened);
  }

  function settle(serverNow, rtt) {
    skew = serverNow + rtt / 2 - Date.now() / 1000;
    settled = true;
    note.textContent = 'Server vaqti bilan tekislandi';
  }

  if (data.sync) {
    var startedAt = Date.now();
    fetch(data.sync, { cache: 'no-store' })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (payload && payload.now) { settle(Number(payload.now), (Date.now() - startedAt) / 1000); }
      })
      .catch(function () { note.textContent = 'Qurilma vaqti bilan ishlamoqda'; });
  }

  function paint() {
    var left = remaining();
    if (left < 0) { left = 0; }
    if (data.demo && left <= 0) {                        // the web page demo loops
      opened = Date.now() / 1000;
      left = TOTAL;
    }
    var shown = Math.ceil(left);
    var minutes = Math.floor(shown / 60), seconds = shown % 60;
    digits.textContent = (minutes < 10 ? '0' : '') + minutes + ':' + (seconds < 10 ? '0' : '') + seconds;

    var share = Math.max(0, Math.min(1, left / TOTAL));
    arc.style.strokeDashoffset = (CIRC * (1 - share)).toFixed(2);
    arc.style.stroke = share > 0.5 ? 'var(--ok)' : (share > 0.2 ? 'var(--warn)' : 'var(--bad)');
    hand.setAttribute('transform', 'rotate(' + (-((left % 60) / 60) * 360).toFixed(2) + ' 110 110)');

    var track = '';
    for (var i = 0; i < 10; i++) { track += i < Math.round(share * 10) ? '\\u25b0' : '\\u25b1'; }
    bar.textContent = track;

    var over = !data.demo && left <= 0;
    document.body.classList.toggle('over', over);
    document.body.classList.toggle('pulse', !over && left <= 10 && left > 0);
    if (over && !paint.notified) {
      paint.notified = true;
      try { if (navigator.vibrate) { navigator.vibrate([120, 60, 120]); } } catch (error) {}
    }
    if (!over) { paint.notified = false; }
    requestAnimationFrame(paint);
  }

  if (!settled && !data.sync) { note.textContent = 'Qurilma vaqti bilan ishlamoqda'; }
  requestAnimationFrame(paint);
})();
"""


def _int(value: object, default: int = 0, low: int = -10 ** 9, high: int = 10 ** 9) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def render(params: dict) -> str:
    """Build the clock page from the query string of a strip button."""
    demo = str(params.get("demo") or "") == "1"
    total = _int(params.get("tm"), 60, 5, 600)
    deadline = _int(params.get("d"), 0, 0, 10 ** 11)   # unix seconds
    left = _int(params.get("l"), 0, 0, 3600)
    number = _int(params.get("n"), 0, 0, 10 ** 6)
    count = _int(params.get("t"), 0, 0, 10 ** 6)
    round_no = _int(params.get("r"), 0, 0, 99)
    package = str(params.get("p") or "")[:90]

    head = "JONLI SOAT"
    if number and count:
        head = f"SAVOL {number} / {count}"
        if round_no:
            head += f" · {round_no}-RAUND"
    if demo and not deadline:
        deadline = int(time.time()) + total
        left = total

    payload = {
        # The demo counts from the moment the page opens, so it can loop; a real
        # game counts from the absolute deadline carried by the strip button.
        "d": 0 if demo else deadline,
        "l": total if demo else left,
        "tm": total,
        "demo": demo,
        "sync": "" if demo else "/api/now",
    }
    script = SCRIPT.replace("__DATA__", json.dumps(payload))
    title = html.escape(package or "Zakovat Agent")
    return f"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#0b0b11">
<title>Jonli soat — Zakovat Agent</title>
<style>{STYLE}</style>
</head>
<body>
<main>
  <div class="head">{html.escape(head)}</div>
  <div class="pack">{title}</div>
  <svg class="dial" viewBox="0 0 220 220" aria-hidden="true">
    <circle class="track" cx="110" cy="110" r="96"></circle>
    <circle class="arc" id="arc" cx="110" cy="110" r="96"></circle>
    <g id="marks"></g>
    <line class="hand" id="hand" x1="110" y1="110" x2="110" y2="34"></line>
    <circle class="hub" cx="110" cy="110" r="6"></circle>
  </svg>
  <div class="digits" id="digits">--:--</div>
  <div class="bar" id="bar"></div>
  <div class="done">VAQT TUGADI<span>Chatdagi «Vaqtni yangilash» tugmasini bosing</span></div>
  <div class="note" id="note">Soat qurilmangizda yuradi — tarmoq sekin bo‘lsa ham</div>
</main>
<script>{script}</script>
</body>
</html>
"""
