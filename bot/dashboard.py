"""The Mini App itself: one self-contained page, Telegram-native look.

``render(init_data)`` returns a single HTML document with its CSS and JS inline,
so the page has no dependency of its own (no font file, no image request, no
bundle). Everything it needs comes from two places:

* the Telegram WebApp SDK the client injects — theme, colour scheme, haptics,
  back button;
* ``POST /api/app`` on the same origin, which answers with the numbers and says
  whether this visitor is allowed to see them.

Themes follow Telegram's own palette. Light mode sits on a soft off-white page
with white cards, like Telegram's settings screen — not pure white. Dark mode
sits on a deep charcoal (``#15191d``): black, but not pitch black, because pure
``#000`` looks flat and dead next to Telegram's surfaces.

Nothing in the copy uses emoji: bullets, arrows and thin rules carry the
structure instead.
"""

from __future__ import annotations

import json

APP_TITLE = "Zakovat — panel"
SDK_URL = "https://telegram.org/js/telegram-web-app.js"

PAGE = """<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#15191d">
<title>__TITLE__</title>
<script src="__SDK__"></script>
<style>
:root{
  color-scheme: light dark;
  --bg:#eff1f4; --surface:#ffffff; --surface2:#f6f7f9; --text:#101317; --muted:#717c89;
  --line:#e3e6eb; --accent:#3390ec; --accent-soft:#e7f0fd; --good:#37a97a; --warn:#d99a30;
  --bad:#dd5b5b; --shadow:0 1px 2px rgba(16,19,23,.05),0 8px 24px rgba(16,19,23,.045);
  --radius:14px; --radius-sm:11px;
}
html[data-theme="dark"]{
  --bg:#15191d; --surface:#1d2329; --surface2:#222a31; --text:#e8eaed; --muted:#8d97a2;
  --line:#2b333b; --accent:#5eaaf5; --accent-soft:#21303e; --good:#4cb98a; --warn:#dcae5c;
  --bad:#e57373; --shadow:0 1px 2px rgba(0,0,0,.45),0 8px 24px rgba(0,0,0,.26);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0}
body{
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:15px;line-height:1.45;-webkit-font-smoothing:antialiased;
  padding-bottom:calc(28px + env(safe-area-inset-bottom));
}
button{font:inherit;color:inherit;border:0;background:none;cursor:pointer}

.hdr{
  position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;
  padding:calc(11px + env(safe-area-inset-top)) 16px 10px;
  background:color-mix(in srgb, var(--bg) 84%, transparent);
  backdrop-filter:saturate(180%) blur(18px);-webkit-backdrop-filter:saturate(180%) blur(18px);
  border-bottom:1px solid var(--line);
}
.ava{
  width:42px;height:42px;border-radius:50%;flex:0 0 auto;overflow:hidden;
  display:flex;align-items:center;justify-content:center;font-weight:600;font-size:16px;
  color:#fff;background:linear-gradient(155deg,var(--accent),#7a5cf0);
}
.ava img{width:100%;height:100%;object-fit:cover;display:block}
.who{min-width:0;flex:1 1 auto}
.who b{display:block;font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.who span{display:block;color:var(--muted);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.icon{
  width:38px;height:38px;border-radius:50%;flex:0 0 auto;display:grid;place-items:center;
  background:var(--surface);box-shadow:var(--shadow);color:var(--muted);transition:transform .12s;
}
.icon svg{width:18px;height:18px;display:block}
.icon:active{transform:scale(.93)}
.icon[data-busy="1"] svg{animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.wrap{padding:14px 16px 0;max-width:780px;margin:0 auto}
.tabs{
  display:flex;gap:4px;padding:4px;background:var(--surface2);border-radius:12px;
  border:1px solid var(--line);overflow-x:auto;scrollbar-width:none;
}
.tabs::-webkit-scrollbar{display:none}
.tabs button{
  flex:1 0 auto;padding:8px 13px;border-radius:9px;color:var(--muted);font-size:13.5px;
  font-weight:500;white-space:nowrap;transition:background .15s,color .15s;
}
.tabs button[aria-selected="true"]{
  background:var(--surface);color:var(--text);box-shadow:var(--shadow);font-weight:600;
}

h2{
  font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  margin:20px 2px 9px;font-weight:600;
}
.card{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:14px;margin-bottom:12px;
}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media (min-width:560px){
  .grid{grid-template-columns:repeat(3,1fr)}
  .grid-3{grid-template-columns:repeat(3,1fr)}
}
.kpi{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-sm);
  padding:12px;box-shadow:var(--shadow);min-width:0;
}
.kpi i{display:block;font-style:normal;font-size:11.5px;color:var(--muted);margin-bottom:5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi b{display:block;font-size:22px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kpi u{display:block;text-decoration:none;font-size:11.5px;color:var(--muted);margin-top:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.row{display:flex;align-items:center;gap:11px;padding:11px 2px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row .t{flex:1 1 auto;min-width:0}
.row .t b{display:block;font-weight:550;font-size:14.5px;overflow-wrap:anywhere}
.row .t span{display:block;color:var(--muted);font-size:12.5px;overflow-wrap:anywhere}
.row .v{flex:0 0 auto;text-align:right;font-variant-numeric:tabular-nums}
.row .v b{display:block;font-size:15px;font-weight:600}
.row .v span{display:block;color:var(--muted);font-size:11.5px}
.n{
  min-width:26px;height:26px;padding:0 6px;border-radius:8px;flex:0 0 auto;
  display:grid;place-items:center;background:var(--surface2);color:var(--muted);
  font-size:12px;font-weight:600;font-variant-numeric:tabular-nums;
}
.bar{height:7px;border-radius:99px;background:var(--surface2);overflow:hidden;margin-top:11px}
.bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent),#7a5cf0)}
.pill{
  display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:99px;
  background:var(--accent-soft);color:var(--accent);font-size:11.5px;font-weight:600;white-space:nowrap;
}
.pill[data-kind="good"]{background:color-mix(in srgb,var(--good) 17%,transparent);color:var(--good)}
.pill[data-kind="warn"]{background:color-mix(in srgb,var(--warn) 17%,transparent);color:var(--warn)}
.pill[data-kind="bad"]{background:color-mix(in srgb,var(--bad) 17%,transparent);color:var(--bad)}
.foot{margin:18px 2px 6px;color:var(--muted);font-size:12px;line-height:1.65}
.foot b{color:var(--text);font-weight:550}
.err{text-align:center;padding:46px 20px}
.err b{display:block;font-size:17px;font-weight:650;margin-bottom:7px}
.err p{color:var(--muted);margin:0 auto 18px;max-width:34ch;font-size:13.5px;overflow-wrap:anywhere}
.err button{
  background:var(--accent);color:#fff;padding:11px 22px;border-radius:12px;font-weight:600;font-size:14.5px;
}
.err small{
  display:block;color:var(--muted);font-size:12px;line-height:1.6;
  margin:-8px auto 20px;max-width:40ch;overflow-wrap:anywhere;
}
.empty{color:var(--muted);font-size:13.5px;padding:4px 2px;line-height:1.7;overflow-wrap:anywhere}
.empty b{color:var(--text);font-weight:600}
.sk{
  height:13px;border-radius:6px;margin:10px 0;
  background:linear-gradient(90deg,var(--surface2) 25%,var(--surface) 37%,var(--surface2) 63%);
  background-size:400% 100%;animation:sh 1.4s ease infinite;
}
@keyframes sh{0%{background-position:100% 50%}100%{background-position:0 50%}}
.note{color:var(--bad);font-size:12.5px;margin:2px 2px 12px}

/* ---- browser and composer ------------------------------------------ */
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:2px 0 4px}
.chips button{
  padding:7px 12px;border-radius:99px;background:var(--surface2);border:1px solid var(--line);
  color:var(--muted);font-size:12.5px;font-weight:550;white-space:nowrap;transition:all .15s;
}
.chips button[aria-pressed="true"]{
  background:var(--accent-soft);border-color:transparent;color:var(--accent);font-weight:650;
}
.field{margin:2px 0 11px}
.field i{
  display:block;font-style:normal;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);font-weight:600;margin-bottom:6px;
}
.field input,.field select{
  width:100%;padding:11px 12px;border-radius:var(--radius-sm);border:1px solid var(--line);
  background:var(--surface2);color:var(--text);font:inherit;font-size:14.5px;
}
.field input:focus,.field select:focus{outline:2px solid var(--accent);outline-offset:-1px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.q{display:flex;gap:10px;padding:11px 2px;border-bottom:1px solid var(--line);align-items:flex-start}
.q:last-child{border-bottom:0}
.q .body{flex:1 1 auto;min-width:0}
.q .body b{display:block;font-size:14px;font-weight:550;line-height:1.5;overflow-wrap:anywhere}
.q .body span{display:block;color:var(--muted);font-size:12.5px;margin-top:4px;overflow-wrap:anywhere}
.q .side{flex:0 0 auto;display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.pick{
  padding:7px 11px;border-radius:9px;background:var(--surface2);border:1px solid var(--line);
  color:var(--muted);font-size:12px;font-weight:600;white-space:nowrap;
}
.pick[aria-pressed="true"]{
  background:var(--accent);border-color:transparent;color:#fff;
}
.pager{display:flex;gap:8px;align-items:center;justify-content:center;margin-top:12px}
.pager button{
  padding:8px 14px;border-radius:10px;background:var(--surface2);border:1px solid var(--line);
  color:var(--text);font-size:13px;font-weight:600;
}
.pager button[disabled]{opacity:.4}
.pager span{color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.act{
  position:sticky;bottom:0;margin:14px -16px calc(-14px - env(safe-area-inset-bottom));
  padding:12px 16px calc(12px + env(safe-area-inset-bottom));
  background:color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter:saturate(180%) blur(18px);-webkit-backdrop-filter:saturate(180%) blur(18px);
  border-top:1px solid var(--line);
}
.act button{
  width:100%;padding:14px;border-radius:13px;background:var(--accent);color:#fff;
  font-size:15px;font-weight:650;box-shadow:var(--shadow);
}
.act button[disabled]{opacity:.55}
.act small{display:block;color:var(--muted);font-size:11.5px;text-align:center;margin-top:8px;line-height:1.5}
.ok{
  background:color-mix(in srgb,var(--good) 13%,transparent);border:1px solid transparent;
  border-radius:var(--radius-sm);padding:11px 12px;font-size:13.5px;line-height:1.6;margin-bottom:12px;
}
.crumb{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12.5px;margin:14px 2px 8px}
.crumb button{color:var(--accent);font-size:12.5px;font-weight:600}
h2 .cnt{color:var(--muted);font-weight:500;text-transform:none;letter-spacing:0}
</style>
</head>
<body>
<header class="hdr">
  <div class="ava" id="ava">Z</div>
  <div class="who">
    <b id="whoName">Zakovat</b>
    <span id="whoSub">yuklanmoqda…</span>
  </div>
  <button class="icon" id="refresh" title="Yangilash" aria-label="Yangilash">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>
    </svg>
  </button>
</header>

<main class="wrap" id="main">
  <div class="tabs" role="tablist" id="tabs"></div>
  <div id="view"></div>
</main>

<script>
(function(){
"use strict";
var API = __API__;
var INIT = __INIT__;
var TABS = [
  {id:"main", label:"Umumiy"},
  {id:"questions", label:"Savollar"},
  {id:"tour", label:"Turnir"},
  {id:"sections", label:"Bo‘limlar"},
  {id:"events", label:"O‘yinlar"},
  {id:"access", label:"Ruxsatlar", owner:true}
];
var tab = "main";
var data = null;
var busy = false;
var failed = "";
var themeTries = 0;
var proofSource = "";
var BROWSE_API = API.replace(/\/api\/app$/, "/api/browse");
var TOUR_API = API.replace(/\/api\/app$/, "/api/tour");
var browse = {turi:"paketlar", bolim:"", paket:0, sahifa:1, qidiruv:"",
              body:null, busy:false, error:""};
var plan = {guruh:0, paket:0, sarlavha:"", raund:1, savol_soni:10, sekund:60, jamoa:6,
            sana:"", vaqt:"19:00", savollar:[], busy:false, error:"", ok:"",
            plans:null, limits:null, guruhlar:null};

function tg(){
  return (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
}
function proof(){
  /*
   * Telegram never puts initData in the query string. Opening the app from a menu
   * button or a web_app button appends it to the address as a fragment:
   * #tgWebAppData=<urlencoded initData>. The SDK decodes that fragment and
   * exposes it as Telegram.WebApp.initData, which is what a server can verify.
   * The fragment itself is the fallback when the SDK has not arrived yet, and the
   * value the page was rendered with is the last resort (local preview).
   */
  var w = tg();
  if (w && w.initData){ proofSource = "sdk"; return w.initData; }
  var hash = window.location.hash || "";
  /* The fragment also carries tgWebAppVersion / tgWebAppPlatform, so the value is
     taken up to the next tgWebApp key. That works whether the client encoded the
     whole initData once more (the documented way) or left it plain. */
  var found = hash.match(/tgWebAppData=([^#]*?)(?:&tgWebApp[A-Za-z]+=|$)/);
  if (found && found[1]){
    var raw = found[1];
    try{ raw = decodeURIComponent(raw); }catch(e){}
    proofSource = "fragment";
    return raw;
  }
  if (INIT){ proofSource = "sahifa"; return INIT; }
  proofSource = "";
  return "";
}
function haptic(kind, style){
  var w = tg();
  try{
    if (!w || !w.HapticFeedback) return;
    if (kind === "impact") w.HapticFeedback.impactOccurred(style || "light");
    else w.HapticFeedback.notificationOccurred(style || "success");
  }catch(e){}
}
function num(value){
  var n = Number(value || 0);
  if (!isFinite(n)) n = 0;
  var rounded = Math.round(n * 10) / 10;
  var parts = String(rounded).split(".");
  parts[0] = parts[0].replace(/\\B(?=(\\d{3})+(?!\\d))/g, "\\u2009");
  return parts.join(",");
}
function esc(value){
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function ago(seconds){
  var n = Number(seconds || 0);
  if (!n) return "";
  var diff = Math.max(0, Math.floor(Date.now() / 1000) - n);
  if (diff < 60) return diff + " soniya oldin";
  if (diff < 3600) return Math.floor(diff / 60) + " daqiqa oldin";
  if (diff < 86400) return Math.floor(diff / 3600) + " soat oldin";
  return Math.floor(diff / 86400) + " kun oldin";
}
function until(seconds){
  var n = Number(seconds || 0);
  if (!n) return "";
  var diff = Math.floor(n - Date.now() / 1000);
  if (diff <= 0) return "vaqti keldi";
  if (diff < 60) return diff + " soniya keyin";
  if (diff < 3600) return Math.floor(diff / 60) + " daqiqa keyin";
  if (diff < 86400) return Math.floor(diff / 3600) + " soat keyin";
  return Math.floor(diff / 86400) + " kun keyin";
}
function stamp(value){
  var n = Number(value || 0);
  if (!n) return "—";
  var date = new Date(n > 1e12 ? n : n * 1000);
  function p(v){ return v < 10 ? "0" + v : String(v); }
  return p(date.getDate()) + "." + p(date.getMonth() + 1) + " · " +
    p(date.getHours()) + ":" + p(date.getMinutes());
}
function span(seconds){
  var n = Math.max(0, Number(seconds || 0));
  if (n < 60) return n + " s";
  if (n < 3600) return Math.floor(n / 60) + " daq";
  if (n < 86400) return Math.floor(n / 3600) + " soat";
  return Math.floor(n / 86400) + " kun";
}
function initials(name){
  var parts = String(name || "").trim().split(/\\s+/).filter(Boolean);
  if (!parts.length) return "Z";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
}

/* ---- theme ---------------------------------------------------------- */
function applyTheme(){
  var w = tg();
  var dark;
  if (w && w.colorScheme) dark = w.colorScheme === "dark";
  else dark = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  var background = dark ? "#15191d" : "#eff1f4";
  var tp = (w && w.themeParams) || {};
  if (tp.button_color) document.documentElement.style.setProperty("--accent", tp.button_color);
  try{
    if (w && w.setHeaderColor) w.setHeaderColor(background);
    if (w && w.setBackgroundColor) w.setBackgroundColor(background);
  }catch(e){}
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", background);
  return !!w;
}
function chaseTheme(onReady){
  /* In a plain browser the SDK arrives a moment late; pick it up when it does. */
  if (applyTheme()){
    if (onReady) onReady();
    return;
  }
  if (themeTries > 12) return;
  themeTries += 1;
  setTimeout(function(){ chaseTheme(onReady); }, 250);
}

/* ---- data ----------------------------------------------------------- */
function load(){
  if (busy) return;
  busy = true;
  failed = "";
  var button = document.getElementById("refresh");
  button.setAttribute("data-busy", "1");
  if (!data) paint();
  fetch(API, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({initData: proof(), manba: proofSource || "yo‘q"}),
    credentials: "same-origin"
  }).then(function(response){
    return response.json()["catch"](function(){
      return {ok:false, reason:"Javob o‘qilmadi (kod " + response.status + ")"};
    });
  }).then(function(body){
    busy = false;
    button.removeAttribute("data-busy");
    if (body && body.ok){
      data = body;
      failed = "";
      haptic("notify", "success");
    }else{
      failed = (body && body.reason) || "Ma‘lumot olinmadi.";
      haptic("notify", "error");
    }
    paint();
  })["catch"](function(error){
    busy = false;
    button.removeAttribute("data-busy");
    failed = "Server bilan bog‘lanish bo‘lmadi: " + error.message;
    haptic("notify", "error");
    paint();
  });
}

function post(address, payload, done){
  payload.initData = proof();
  payload.manba = proofSource || "yo‘q";
  return fetch(address, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
    credentials: "same-origin"
  }).then(function(response){
    return response.json()["catch"](function(){
      return {ok:false, reason:"Javob o‘qilmadi (kod " + response.status + ")"};
    });
  }).then(done)["catch"](function(error){
    done({ok:false, reason:"Server bilan bog‘lanish bo‘lmadi: " + error.message});
  });
}

function loadBrowse(){
  if (browse.busy) return;
  browse.busy = true;
  browse.error = "";
  post(BROWSE_API, {turi: browse.turi, bolim: browse.bolim, paket: browse.paket,
                    sahifa: browse.sahifa, qidiruv: browse.qidiruv}, function(body){
    browse.busy = false;
    if (body && body.ok){
      browse.body = body;
      browse.sahifa = Number(body.sahifa || browse.sahifa);
      if (body.ishlatilgan_indeks) usedIndex = body.ishlatilgan_indeks;
      haptic("notify", "success");
    }else{
      browse.error = (body && body.reason) || "Ro‘yxat olinmadi.";
      haptic("notify", "error");
    }
    paint();
  });
}

function sendTour(extra, quiet){
  if (plan.busy) return;
  plan.busy = true;
  plan.error = "";
  if (!quiet) plan.ok = "";
  var payload = {
    amal: (extra && extra.amal) || "reja",
    guruh: plan.guruh, paket: plan.paket, sarlavha: plan.sarlavha,
    raund: plan.raund, savol_soni: plan.savol_soni, sekund: plan.sekund,
    jamoa: plan.jamoa, savollar: plan.savollar,
    vaqt: plan.sana ? (plan.sana + "T" + (plan.vaqt || "19:00")) : ""
  };
  if (extra){
    for (var key in extra) if (extra.hasOwnProperty(key)) payload[key] = extra[key];
  }
  post(TOUR_API, payload, function(body){
    plan.busy = false;
    if (body && body.ok){
      plan.plans = body.rejalar || plan.plans;
      if (body.guruhlar) plan.guruhlar = body.guruhlar;
      if (body.chegaralar) plan.limits = body.chegaralar;
      if (body.amal === "shakllantirish"){
        plan.ok = body.xabar || "Turnir guruhga chiqarildi.";
        plan.guruh = 0;
        plan.savollar = [];
        load();                       // the counters and the plan list have moved
      }else if (body.xabar){
        plan.ok = body.xabar;
      }
      haptic("notify", "success");
    }else{
      plan.error = (body && body.reason) || "Amal bajarilmadi.";
      haptic("notify", "error");
    }
    paint();
  });
}

var usedIndex = null;

/* ---- pieces --------------------------------------------------------- */
function kpi(label, value, note){
  return '<div class="kpi"><i>' + esc(label) + '</i><b>' + esc(value) + '</b>' +
    (note ? '<u>' + esc(note) + '</u>' : '') + '</div>';
}
function pill(text, kind){
  return '<span class="pill"' + (kind ? ' data-kind="' + kind + '"' : '') + '>' + esc(text) + '</span>';
}
function row(index, title, sub, value, note){
  return '<div class="row"><div class="n">' + esc(index) + '</div><div class="t"><b>' +
    esc(title) + '</b>' + (sub ? '<span>' + esc(sub) + '</span>' : '') +
    '</div><div class="v"><b>' + esc(value) + '</b>' +
    (note ? '<span>' + esc(note) + '</span>' : '') + '</div></div>';
}
function chips(items, current, act){
  var html = '<div class="chips">';
  items.forEach(function(item){
    var on = String(item.value) === String(current);
    html += '<button data-act="' + act + '" data-val="' + esc(item.value) +
      '" aria-pressed="' + (on ? "true" : "false") + '">' + esc(item.label) + '</button>';
  });
  return html + '</div>';
}
function pager(page, pages, act){
  if (pages <= 1) return "";
  return '<div class="pager">' +
    '<button data-act="' + act + '" data-val="1"' + (page <= 1 ? " disabled" : "") + '>Boshi</button>' +
    '<button data-act="' + act + '" data-val="' + (page - 1) + '"' +
      (page <= 1 ? " disabled" : "") + '>‹ Orqaga</button>' +
    '<span>' + page + " / " + pages + '</span>' +
    '<button data-act="' + act + '" data-val="' + (page + 1) + '"' +
      (page >= pages ? " disabled" : "") + '>Oldinga ›</button>' +
    '<button data-act="' + act + '" data-val="' + pages + '"' +
      (page >= pages ? " disabled" : "") + '>Oxiri</button>' +
    '</div>';
}
function todayLocal(){
  var now = new Date();
  return now.getFullYear() + "-" + ("0" + (now.getMonth() + 1)).slice(-2) + "-" +
    ("0" + now.getDate()).slice(-2);
}
function fact(title, sub, value, kind){
  return '<div class="row"><div class="t"><b>' + esc(title) + '</b>' +
    (sub ? '<span>' + esc(sub) + '</span>' : '') + '</div><div class="v">' +
    (kind ? pill(value, kind) : '<b>' + esc(value) + '</b>') + '</div></div>';
}

/* ---- views ---------------------------------------------------------- */
function viewMain(){
  var c = data.counters || {};
  var accuracy = Number(data.accuracy || 0);
  var asked = Number(data.asked || 0);
  var html = '<h2>Asosiy ko‘rsatkichlar</h2><div class="grid">' +
    kpi("Boshlangan o‘yinlar", num(c.games_started)) +
    kpi("Yakunlangan", num(c.games_finished)) +
    kpi("So‘ralgan savollar", num(c.questions_asked)) +
    kpi("To‘g‘ri javoblar", num(c.answers_correct)) +
    kpi("Xato javoblar", num(c.answers_wrong)) +
    kpi("O‘tkazib yuborilgan", num(c.skips)) +
    '</div>';

  html += '<div class="card" style="margin-top:12px">' +
    fact("Javoblar aniqligi", num(asked) + " ta javob hisobga olindi", accuracy + "%") +
    '<div class="bar"><i style="width:' + Math.max(0, Math.min(100, accuracy)) + '%"></i></div>' +
    '</div>';

  html += '<h2>Odamlar</h2><div class="grid-3">' +
    kpi("Ishtirokchilar", num(data.users)) +
    kpi("Chatlar", num(data.chats)) +
    kpi("O‘ynalgan paketlar", num(data.packages_played)) +
    '</div>';

  html += '<h2>Harakatlar</h2><div class="grid-3">' +
    kpi("Avto-to‘xtatildi", num(c.games_auto_stopped)) +
    kpi("Jonli soat", num(c.clock_opened)) +
    kpi("Mini App", num(c.app_opened)) +
    '</div>';

  var top = data.top_packages || [];
  if (top.length){
    html += '<h2>Ko‘p o‘ynalgan paketlar</h2><div class="card">';
    top.forEach(function(item, index){
      html += row(index + 1, item.title || ("Paket " + item.id), "id " + item.id,
        num(item.plays) + "×", "o‘yin");
    });
    html += '</div>';
  }

  var archive = data.archive || {};
  var biggest = archive.biggest || {};
  var sources = archive.sources || [];
  html += '<h2>Arxiv</h2><div class="card">' +
    fact("Jami paketlar", sources.length + " ta manba", num(archive.total_packages || archive.packages)) +
    fact("Jami savollar", "o‘rtacha " + num(archive.average) + " ta paketda", num(archive.total_questions || archive.questions)) +
    fact("Bo‘limlar", num(archive.section_count) + " ta liga va turnirlar", "") +
    fact("Rasmlar", num(archive.cyrillic) + " ta paket kirillchada", num(archive.pictures)) +
    fact("Eng katta paket", esc(biggest.title || "—"), num(biggest.questions) + " savol");
  sources.forEach(function(item){
    html += fact(esc(item.name),
      item.stored === "release"
        ? (num(item.shards) + " shard · kerak bo‘lganda yuklanadi")
        : ("deploy bilan birga" + (item.exported_at ? " · " + esc(item.exported_at) : "")),
      num(item.packages) + " paket · " + num(item.questions) + " savol");
  });
  html += '</div>';

  var storage = data.storage || {};
  var instance = data.instance || {};
  html += '<h2>Tizim</h2><div class="card">' +
    fact("Saqlagich", esc(storage.label || "—"), esc(storage.backend || "—")) +
    fact("Holat", storage.synced ? "masofadagi nusxa o‘qildi" :
      (storage.remote_age ? "oxirgi o‘qish " + span(storage.remote_age) + " oldin" :
       "faqat shu nusxadagi hisoblar"),
      storage.dry_run ? "sinov rejimi" : "yozish ochiq", storage.dry_run ? "warn" : "good") +
    fact("Shu nusxada", "ishga tushganidan " + span(instance.uptime) + " oldin",
      num(instance.users) + " odam · " + num(instance.chats) + " chat") +
    fact("Server vaqti", stamp((data.server || {}).time), "") +
    '</div>';

  html += '<div class="foot">Raqamlar Telegramdagi pinlangan xabarda saqlanadi — ' +
    'server qayta ishga tushsa ham yo‘qolmaydi.<br>Yangilash uchun yuqoridagi aylanma ' +
    'tugma yetarli.</div>';
  return html;
}

function viewSections(){
  var sections = data.sections || [];
  if (!sections.length) return '<div class="empty">Arxivda bo‘lim topilmadi.</div>';
  var total = sections.reduce(function(sum, item){ return sum + Number(item.questions || 0); }, 0);
  var packages = sections.reduce(function(sum, item){ return sum + Number(item.packages || 0); }, 0);
  var html = '<h2>Bo‘limlar · ' + num(packages) + " paket · " + num(total) + " savol</h2>";
  sections.forEach(function(item){
    var share = total ? Math.round((Number(item.questions || 0) / total) * 1000) / 10 : 0;
    html += '<div class="card" style="padding:13px 14px">' +
      '<div class="row" style="padding:0;border:0"><div class="t"><b>' + esc(item.name) +
      '</b><span>' + num(item.packages) + " paket · " + num(item.questions) +
      ' savol</span></div><div class="v"><b>' + share + '%</b></div></div>' +
      '<div class="bar"><i style="width:' + Math.max(1.5, Math.min(100, share)) + '%"></i></div>' +
      '</div>';
  });
  return html;
}

function viewQuestions(){
  var html = "";
  var body = browse.body;
  var used = (data || {}).used || {};
  html += '<h2>Savollar bazasi <span class="cnt">· ' +
    num(((data || {}).archive || {}).questions || 0) +
    ' savol · indeksda ' + num(used.count || 0) + " ishlatilgan</span></h2>";
  if (used.count){
    html += '<div class="card" style="padding:12px 14px">' +
      '<div class="row" style="padding:0;border:0"><div class="t"><b>Ishlatilgan savollar indeksi</b>' +
      '<span>' + num(used.count) + " savol · " + num(used.packages) + " paket · jami " +
      num(used.uses) + " marta · chegara " + num(used.limit) + '</span></div>' +
      '<div class="v">' + pill(used.count >= used.limit ? "to‘lgan" : "yozilmoqda",
                               used.count >= used.limit ? "warn" : "good") + '</div></div>';
    if ((used.recent || []).length){
      html += '<div style="margin-top:9px">';
      (used.recent || []).slice(0, 5).forEach(function(item){
        html += '<div class="row" style="padding:7px 0"><div class="n">' +
          esc(String(item.id).slice(-4)) + '</div><div class="t"><b>' + esc("savol " + item.id) +
          '</b><span>' + esc(ago(item.t)) +
          (item.p ? " · paket " + esc(item.p) : "") + (item.m ? " · " + esc(item.m) : "") +
          (Number(item.n || 1) > 1 ? " · " + esc(item.n) + "-marta" : "") +
          '</span></div></div>';
      });
      html += '</div>';
    }
    html += '</div>';
  }

  if (browse.turi === "savollar" && body && body.paket){
    var pack = body.paket;
    html += '<div class="crumb"><button data-act="backpkgs">‹ Paketlar</button>' +
      '<span>' + esc(pack.bolim || "") + '</span></div>';
    html += '<div class="card">' +
      '<div class="row" style="padding:0;border:0"><div class="t"><b>' + esc(pack.title) +
      '</b><span>id ' + esc(pack.id) + " · " + num(pack.questions) + " savol · " +
      esc(pack.manba === "turnirlar" ? "turnirlar.uz bazasi" : "zakovatklubi.uz bazasi") +
      '</span></div><div class="v">' +
      '<button class="pick" data-act="choosepkg" data-val="' + esc(pack.id) + '"' +
      ' aria-pressed="' + (Number(plan.paket) === Number(pack.id) ? "true" : "false") + '">' +
      (Number(plan.paket) === Number(pack.id) ? "Turnirda" : "Turnirga") + '</button></div></div>';
    if (body.jami){
      html += '<div class="field" style="margin:12px 0 0"><i>Savollar ichidan qidirish</i>' +
        '<input id="browseQ" value="' + esc(body.qidiruv || "") + '" placeholder="so‘z yoki id"></div>';
    }
    html += '</div>';
    html += '<h2>' + num(body.jami) + ' savol <span class="cnt">· ' + num(body.ishlatilgan || 0) +
      " tasi ishlatilgan · sahifa " + body.sahifa + "/" + body.sahifalar + "</span></h2>";
    if (browse.error) html += '<div class="note">' + esc(browse.error) + '</div>';
    html += '<div class="card">';
    (body.savollar || []).forEach(function(item){
      var chosen = plan.savollar.indexOf(Number(item.id)) >= 0;
      html += '<div class="q"><div class="body"><b>' + esc(item.tartib || item.id) + ". " +
        esc(item.matn || "(matn yo‘q)") + '</b>' +
        '<span>Javob: ' + esc(item.javob || "—") +
        (item.rasm ? " · rasmi bor" : "") + '</span></div>' +
        '<div class="side">' +
        (item.used ? pill("ishlatildi", "warn") : "") +
        '<button class="pick" data-act="pick" data-val="' + esc(item.id) +
        '" aria-pressed="' + (chosen ? "true" : "false") + '">' +
        (chosen ? "Olingan" : "Tanlovga") + '</button>' +
        '</div></div>';
    });
    if (!(body.savollar || []).length){
      html += '<div class="empty">Bu sahifada savol yo‘q.</div>';
    }
    html += '</div>';
    html += pager(Number(body.sahifa || 1), Number(body.sahifalar || 1), "page");
    html += '<div class="foot">Tanlovga olingan savollar «Turnir» bo‘limida ko‘rinadi va ' +
      'shu turnirning savol to‘plamini tashkil qiladi. Bo‘sh qoldirilsa, bot paketdan ' +
      'o‘zi aralashtirib oladi.</div>';
    return html;
  }

  html += '<div class="card"><div class="field" style="margin:0"><i>Paketlar ichidan qidirish</i>' +
    '<input id="browseQ" value="' + esc(browse.qidiruv || "") + '" placeholder="liga, tur, sana yoki id"></div></div>';
  var sections = (data || {}).sections || [];
  if (sections.length){
    html += '<h2>Qaysi turkumdan <span class="cnt">· ' + sections.length + " bo‘lim</span></h2>";
    html += chips([{value:"", label:"Hammasi"}].concat(sections.map(function(item){
      return {value:item.name, label:item.name + " (" + item.packages + ")"};
    })), browse.bolim, "section");
  }
  html += '<h2>Paketlar <span class="cnt">' +
    (body ? "· " + num(body.jami) + " ta · sahifa " + body.sahifa + "/" + body.sahifalar : "") +
    "</span></h2>";
  if (browse.error) html += '<div class="note">' + esc(browse.error) + '</div>';
  if (browse.busy && !body){
    html += '<div class="card">' + '<div class="sk" style="width:80%"></div>'.repeat(4) + '</div>';
    return html;
  }
  html += '<div class="card">';
  ((body && body.paketlar) || []).forEach(function(item){
    var chosen = Number(plan.paket) === Number(item.id);
    html += '<div class="row"><div class="n">' + esc(String(item.id).slice(-4)) + '</div>' +
      '<div class="t"><b>' + esc(item.title) + '</b><span>id ' + esc(item.id) + " · " +
      num(item.questions) + " savol · " + esc(item.bolim || "") +
      (item.manba === "turnirlar" ? " · turnirlar.uz" : "") +
      (item.ishlatilgan ? " · " + esc(item.ishlatilgan) + " savoli ishlatilgan" : "") +
      '</span></div><div class="v">' +
      (chosen ? pill("tanlangan", "good") : "") +
      '<button class="pick" data-act="openpkg" data-val="' + esc(item.id) + '">Ochish ›</button>' +
      '</div></div>';
  });
  if (!body && !browse.error) html += '<div class="empty">Yuklanmoqda…</div>';
  else if (!((body && body.paketlar) || []).length){
    html += '<div class="empty">Bu bo‘limda paket topilmadi.</div>';
  }
  html += '</div>';
  html += pager(Number((body || {}).sahifa || 1), Number((body || {}).sahifalar || 1), "page");
  return html;
}

function viewTour(){
  var html = "";
  var limits = plan.limits || {};
  var groups = plan.guruhlar || (data || {}).groups || [];
  var rounds = limits.raund || [1, 2, 3, 5, 10];
  var perRound = limits.savol || [5, 10, 15, 25, 0];
  var timers = limits.sekund || [30, 45, 60, 90, 120];
  var teamRange = limits.jamoa || [3, 12];

  html += '<h2>Turnir shakllantirish <span class="cnt">· panel ichida</span></h2>';
  if (plan.ok) html += '<div class="ok">' + esc(plan.ok) + '</div>';
  if (plan.error) html += '<div class="note">' + esc(plan.error) + '</div>';

  /* --- where ------------------------------------------------------- */
  html += '<div class="card"><div class="field" style="margin:0"><i>Qaysi guruhga</i>';
  if (groups.length){
    html += '</div><div class="chips">';
    groups.forEach(function(item){
      var on = Number(plan.guruh) === Number(item.id);
      html += '<button data-act="group" data-val="' + esc(item.id) + '" aria-pressed="' +
        (on ? "true" : "false") + '">' + esc(item.title || ("guruh " + item.id)) + '</button>';
    });
    html += '</div>';
    html += '<div class="field" style="margin:10px 0 0"><i>Yoki guruh id si</i>' +
      '<input data-field="guruh" inputmode="numeric" value="' +
      (plan.guruh ? esc(plan.guruh) : "") + '" placeholder="-1001234567890"></div>';
  }else{
    html += '<input data-field="guruh" inputmode="numeric" value="' +
      (plan.guruh ? esc(plan.guruh) : "") + '" placeholder="-1001234567890"></div>';
    html += '<div class="empty" style="margin-top:9px">Bot hali biror guruhni eslab qolmagan. ' +
      'Guruhda bir marta <b>/turnir</b> yoki <b>/solo</b> buyrug‘i ishlansa, guruh shu ' +
      'ro‘yxatda paydo bo‘ladi. Id ni qo‘lda ham yozsa bo‘ladi.</div>';
  }
  html += '</div>';

  /* --- what -------------------------------------------------------- */
  html += '<div class="card">';
  html += '<div class="field"><i>Qaysi paketdan</i>' +
    '<input data-field="paket" inputmode="numeric" value="' + (plan.paket ? esc(plan.paket) : "") +
    '" placeholder="paket id si — «Savollar» bo‘limidan tanlang"></div>';
  html += '<div class="field"><i>Turnir sarlavhasi</i>' +
    '<input data-field="sarlavha" maxlength="40" value="' + esc(plan.sarlavha || "") +
    '" placeholder="bo‘sh qoldirilsa paket nomi oladi"></div>';
  html += '<i style="display:block;font-style:normal;font-size:11.5px;letter-spacing:.05em;' +
    'text-transform:uppercase;color:var(--muted);font-weight:600;margin:4px 0 6px">Necha tur (raund)</i>';
  html += chips(rounds.map(function(value){ return {value:value, label:value + " raund"}; }),
                plan.raund, "chip:raund");
  html += '<i style="display:block;font-style:normal;font-size:11.5px;letter-spacing:.05em;' +
    'text-transform:uppercase;color:var(--muted);font-weight:600;margin:12px 0 6px">' +
    'Har turda nechta savol</i>';
  html += chips(perRound.map(function(value){
    return {value:value, label:value === 0 ? "hammasi" : value + " savol"};
  }), plan.savol_soni, "chip:savol_soni");
  html += '<i style="display:block;font-style:normal;font-size:11.5px;letter-spacing:.05em;' +
    'text-transform:uppercase;color:var(--muted);font-weight:600;margin:12px 0 6px">' +
    'Har savolga vaqt</i>';
  html += chips(timers.map(function(value){ return {value:value, label:value + " s"}; }),
                plan.sekund, "chip:sekund");
  html += '<i style="display:block;font-style:normal;font-size:11.5px;letter-spacing:.05em;' +
    'text-transform:uppercase;color:var(--muted);font-weight:600;margin:12px 0 6px">' +
    'Nechta jamoa o‘ynashi mumkin</i>';
  html += chips([teamRange[0], 4, 6, 8, 10, teamRange[1]].filter(function(value, index, all){
    return all.indexOf(value) === index;
  }).map(function(value){ return {value:value, label:value + " jamoa"}; }), plan.jamoa, "chip:jamoa");
  html += '</div>';

  /* --- which questions --------------------------------------------- */
  html += '<div class="card">';
  html += '<div class="row" style="padding:0 0 9px;border:0"><div class="t">' +
    '<b>Qaysi savollar</b><span>' +
    (plan.savollar.length ? plan.savollar.length + " ta tanlangan — faqat shular o‘ynaladi"
                          : "tanlanmagan — bot paketdan o‘zi aralashtiradi") +
    '</span></div>' +
    (plan.savollar.length ? '<div class="v"><button class="pick" data-act="clearpick">' +
      "Tozalash</button></div>" : "") + '</div>';
  if (plan.savollar.length){
    html += '<div class="chips">';
    plan.savollar.forEach(function(id){
      html += '<button data-act="pick" data-val="' + esc(id) + '" aria-pressed="true">' +
        esc(id) + " \u00d7</button>";
    });
    html += '</div>';
  }
  html += '<div class="empty" style="margin-top:9px">Aniq savollarni tanlash uchun ' +
    '«Savollar» bo‘limini oching, paketni tanlang va kerakli savollardagi ' +
    '<b>Tanlovga</b> tugmasini bosing.</div>';
  html += '</div>';

  /* --- when -------------------------------------------------------- */
  html += '<div class="card">';
  html += '<div class="field"><i>Qaysi vaqtda</i></div>';
  html += '<div class="two">' +
    '<div class="field" style="margin:0"><i>Sana</i>' +
    '<input data-field="sana" type="date" value="' + esc(plan.sana || "") + '"></div>' +
    '<div class="field" style="margin:0"><i>Soat</i>' +
    '<input data-field="vaqt" type="time" value="' + esc(plan.vaqt || "19:00") + '"></div>' +
    '</div>';
  html += '<div class="chips" style="margin-top:11px">' +
    '<button data-act="quick" data-val="+30">30 daqiqadan so‘ng</button>' +
    '<button data-act="quick" data-val="+60">1 soatdan so‘ng</button>' +
    '<button data-act="quick" data-val="today19">Bugun 19:00</button>' +
    '<button data-act="quick" data-val="tomorrow10">Ertaga 10:00</button>' +
    '<button data-act="quick" data-val="">Rejasiz (hozir)</button>' +
    '</div>';
  html += '<div class="empty" style="margin-top:9px">Belgilangan paytda bot guruhga ' +
    '<b>eslatma</b> va tayyor <b>«Boshlash»</b> tugmasini yuboradi. O‘yin o‘zi boshlanmaydi — ' +
    'kurator yoki admin tugmani bosadi. Boshlashdan oldin kamida ' +
    '<b>' + esc((limits.jamoa || [3])[0]) + ' jamoa</b> va har biridan <b>2 kishi</b> ' +
    '«Men tayyorman» degan bo‘lishi kerak.</div>';
  html += '</div>';

  /* --- plans ------------------------------------------------------- */
  var plans = plan.plans || (data || {}).plans || [];
  html += '<h2>Rejadagi turnirlar <span class="cnt">· ' + plans.length + " ta</span></h2>";
  html += '<div class="card">';
  if (!plans.length){
    html += '<div class="empty">Hozircha reja yo‘q.</div>';
  }
  plans.forEach(function(item){
    var left = Number(item.left || 0);
    html += '<div class="row"><div class="t"><b>' + esc(item.title || "Turnir") + '</b>' +
      '<span>guruh ' + esc(item.chat) + " · karta " + esc(item.message) + " · " +
      esc(stamp(item.start)) + '</span></div>' +
      '<div class="v">' + pill(until(item.start), left > 0 ? "" : "good") +
      '<span><button class="pick" data-act="removeplan" data-val="' + esc(item.chat) +
      '" data-msg="' + esc(item.message) + '">Bekor</button></span></div></div>';
  });
  html += '</div>';

  html += '<div class="act"><button data-act="compose"' + (plan.busy ? " disabled" : "") + '>' +
    (plan.busy ? "Yuborilmoqda…" : "Turnirni guruhga chiqarish") + '</button>' +
    '<small>Karta guruhga yuboriladi va mahkamlanadi (pin). Savollar va e’lon qilingan ' +
    'javoblar guruhdagi hammaga ko‘rinadi.</small></div>';
  return html;
}

function viewEvents(){
  var events = data.events || [];
  var html = '<h2>Oxirgi harakatlar · ' + events.length + " ta yozuv</h2>";
  if (!events.length){
    return html + '<div class="card"><div class="empty">Hali harakat qayd etilmagan.</div></div>';
  }
  html += '<div class="card">';
  events.forEach(function(item){
    var at = Number(item.t || 0);
    html += '<div class="row"><div class="t"><b>' + esc(item.label || item.k || "Harakat") +
      '</b><span>' + esc(stamp(at)) + " · " + esc(ago(at)) +
      (item.x ? " · " + esc(item.x) : "") + '</span></div></div>';
  });
  html += '</div>';
  html += '<div class="foot">Tasma oxirgi ' + events.length +
    ' ta yozuvni saqlaydi; hammasi hisoblagichlarga qo‘shilib boradi.</div>';
  return html;
}

function viewAccess(){
  var list = data.access || [];
  var html = '<div class="card" style="margin-top:14px">' +
    fact("Bot egasi", "id " + esc(data.owner_id || "—"), "doimiy kirish", "good") +
    '</div>';
  html += '<h2>Ruxsat olganlar · ' + list.length + "</h2>";
  if (!list.length){
    html += '<div class="card"><div class="empty">Hozircha hech kimga ruxsat berilmagan.' +
      '<br><br>Qo‘shish uchun botga yozing:<br><b>/grant 123456789 Ism</b>' +
      '<br>Bekor qilish: <b>/revoke 123456789</b></div></div>';
    return html;
  }
  html += '<div class="card">';
  list.forEach(function(item, index){
    html += row(index + 1, item.name || ("id " + item.id), "id " + item.id,
      stamp(item.since), ago(item.since));
  });
  html += '</div>';
  html += '<div class="foot">Ruxsat olgan foydalanuvchi bot menyusida «Statistika» ' +
    'tugmasini ko‘radi va shu panelni ochadi. Ro‘yxat saqlagichda yozilgan.<br>' +
    'Bekor qilish: <b>/revoke</b> · ro‘yxat: <b>/access</b></div>';
  return html;
}

/* ---- paint ---------------------------------------------------------- */
function paint(){
  var name = document.getElementById("whoName");
  var sub = document.getElementById("whoSub");
  var ava = document.getElementById("ava");
  var tabsBox = document.getElementById("tabs");
  var view = document.getElementById("view");

  if (failed && !data){
    tabsBox.innerHTML = "";
    view.innerHTML = '<div class="err"><b>Kirish yo‘q</b><p>' + esc(failed) + '</p>' +
      '<small>' + esc(proofSource ? "initData manbasi: " + proofSource :
        "Telegram initData topilmadi. Sahifani bot menyusidagi «Statistika» " +
        "tugmasidan oching — oddiy brauzerda imzo bo‘lmaydi.") + '</small>' +
      '<button id="again">Qayta urinish</button></div>';
    var again = document.getElementById("again");
    if (again) again.addEventListener("click", load);
    name.textContent = "Zakovat";
    sub.textContent = proofSource ? "isbot tekshirilmadi (" + proofSource + ")" :
      "initData yo‘q";
    ava.innerHTML = "Z";
    return;
  }
  if (!data){
    tabsBox.innerHTML = "";
    var skeleton = "";
    for (var i = 0; i < 7; i++){
      skeleton += '<div class="sk" style="width:' + (55 + ((i * 9) % 45)) + '%"></div>';
    }
    view.innerHTML = '<div class="card">' + skeleton + '</div>';
    return;
  }

  var user = data.user || {};
  var displayName = user.name || "Foydalanuvchi";
  name.textContent = displayName;
  sub.textContent = (data.owner ? "Bot egasi · id " : "id ") + (user.id || "") +
    (user.username ? " · @" + user.username : "");
  ava.innerHTML = user.photo ? '<img src="' + esc(user.photo) + '" alt="">' : esc(initials(displayName));

  if (tab === "access" && !data.owner) tab = "main";
  tabsBox.innerHTML = TABS.filter(function(item){
    return !item.owner || data.owner;
  }).map(function(item){
    return '<button role="tab" data-tab="' + item.id + '" aria-selected="' +
      (item.id === tab ? "true" : "false") + '">' + item.label + '</button>';
  }).join("");
  Array.prototype.forEach.call(tabsBox.querySelectorAll("button"), function(button){
    button.addEventListener("click", function(){
      var next = button.getAttribute("data-tab");
      if (next === tab) return;
      tab = next;
      haptic("impact", "light");
      paint();
      window.scrollTo(0, 0);
    });
  });

  if (tab === "questions"){
    view.innerHTML = viewQuestions();
    if (!browse.body && !browse.busy) loadBrowse();
  }else if (tab === "tour"){
    view.innerHTML = viewTour();
    if (!plan.limits) sendTour({amal:"reja"}, true);
  }else if (tab === "sections") view.innerHTML = viewSections();
  else if (tab === "events") view.innerHTML = viewEvents();
  else if (tab === "access") view.innerHTML = viewAccess();
  else view.innerHTML = viewMain();

  if (failed){
    view.insertAdjacentHTML("beforeend", '<div class="note">' + esc(failed) + '</div>');
  }
}

/* ---- actions -------------------------------------------------------- */
function act(name, node){
  var value = node.getAttribute("data-val") || "";
  if (name === "section"){
    browse.bolim = (browse.bolim === value ? "" : value);
    browse.turi = "paketlar";
    browse.sahifa = 1;
    browse.body = null;
    haptic("impact", "light");
    loadBrowse();
  }else if (name === "page"){
    var page = Number(value || 1);
    if (page < 1 || page === browse.sahifa) return;
    browse.sahifa = page;
    browse.body = null;
    loadBrowse();
  }else if (name === "openpkg"){
    browse.paket = Number(value);
    browse.turi = "savollar";
    browse.sahifa = 1;
    browse.body = null;
    haptic("impact", "light");
    loadBrowse();
  }else if (name === "backpkgs"){
    browse.turi = "paketlar";
    browse.paket = 0;
    browse.sahifa = 1;
    browse.body = null;
    loadBrowse();
  }else if (name === "pick"){
    var id = Number(value);
    var at = plan.savollar.indexOf(id);
    if (at >= 0) plan.savollar.splice(at, 1); else plan.savollar.push(id);
    haptic("impact", "light");
    paint();
  }else if (name === "clearpick"){
    plan.savollar = [];
    paint();
  }else if (name === "choosepkg"){
    plan.paket = Number(value);
    var pack = browsePackage(Number(value));
    if (!plan.sarlavha && pack.title) plan.sarlavha = String(pack.title).slice(0, 40);
    plan.ok = "Paket turnirga tanlandi: " + (pack.title || value);
    haptic("notify", "success");
    paint();
  }else if (name === "group"){
    plan.guruh = Number(value);
    paint();
  }else if (name === "removeplan"){
    sendTour({amal:"bekor", guruh:Number(value), xabar:Number(node.getAttribute("data-msg") || 0)});
  }else if (name === "compose"){
    sendTour({amal:"shakllantirish"});
  }else if (name === "quick"){
    var now = new Date();
    if (value === ""){ plan.sana = ""; plan.vaqt = "19:00"; }
    else if (value.charAt(0) === "+"){
      now.setTime(now.getTime() + Number(value.slice(1)) * 60000);
      plan.sana = localDate(now);
      plan.vaqt = localTime(now);
    }else if (value === "today19"){ plan.sana = localDate(now); plan.vaqt = "19:00"; }
    else if (value === "tomorrow10"){
      now.setTime(now.getTime() + 86400000);
      plan.sana = localDate(now); plan.vaqt = "10:00";
    }
    paint();
  }else if (name.indexOf("chip:") === 0){
    var field = name.slice(5);
    plan[field] = Number(value);
    haptic("impact", "light");
    paint();
  }else{
    return;
  }
}

function browsePackage(id){
  /* The chosen package is described by the question view or by the list view. */
  var body = browse.body || {};
  if (body.paket && Number(body.paket.id) === Number(id)) return body.paket;
  var rows = body.paketlar || [];
  for (var index = 0; index < rows.length; index++){
    if (Number(rows[index].id) === Number(id)) return rows[index];
  }
  return {};
}

function localDate(date){
  return date.getFullYear() + "-" + ("0" + (date.getMonth() + 1)).slice(-2) + "-" +
    ("0" + date.getDate()).slice(-2);
}
function localTime(date){
  return ("0" + date.getHours()).slice(-2) + ":" + ("0" + date.getMinutes()).slice(-2);
}

function wireView(){
  var view = document.getElementById("view");
  view.addEventListener("click", function(event){
    var node = event.target;
    while (node && node !== view && !(node.getAttribute && node.getAttribute("data-act")))
      node = node.parentNode;
    if (!node || node === view) return;
    act(node.getAttribute("data-act"), node);
  });
  view.addEventListener("change", function(event){
    var node = event.target;
    if (!node || !node.getAttribute) return;
    var field = node.getAttribute("data-field");
    if (field){
      plan[field] = (field === "guruh" || field === "paket")
        ? Number(String(node.value || "").trim() || 0) : String(node.value || "");
      plan.ok = "";
      return;
    }
    if (node.id === "browseQ"){
      browse.qidiruv = String(node.value || "").trim();
      browse.sahifa = 1;
      browse.body = null;
      loadBrowse();
    }
  });
}

/* ---- boot ----------------------------------------------------------- */
function boot(){
  chaseTheme(function(){ if (!data && proof()) load(); });
  var w = tg();
  try{
    if (w){
      if (w.ready) w.ready();
      if (w.expand) w.expand();
      if (w.disableVerticalSwipes) w.disableVerticalSwipes();
      if (w.BackButton){
        if (w.BackButton.show) w.BackButton.show();
        if (w.BackButton.onClick) w.BackButton.onClick(function(){ if (w.close) w.close(); });
      }
      if (w.onEvent) w.onEvent("themeChanged", function(){ applyTheme(); paint(); });
      if (w.onEvent) w.onEvent("viewportChanged", function(){ applyTheme(); });
    }
  }catch(e){}
  document.getElementById("refresh").addEventListener("click", function(){
    haptic("impact", "medium");
    load();
  });
  wireView();
  document.addEventListener("visibilitychange", function(){
    if (!document.hidden){ applyTheme(); if (data) load(); }
  });
  load();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
})();
</script>
</body>
</html>
"""


def _literal(value: str) -> str:
    """A JS string literal that cannot close its own ``<script>`` element.

    ``initData`` arrives in the address bar, so anybody can put anything in it;
    ``json.dumps`` alone would still let a ``</script>`` end the block early and
    run whatever follows it. The three escapes below make that impossible while
    staying valid JavaScript.
    """
    return (
        json.dumps(value or "")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render(init_data: str = "", api: str = "/api/app") -> str:
    """Build the page; ``init_data`` is the visitor's signed proof, if any."""
    return (
        PAGE.replace("__TITLE__", APP_TITLE)
        .replace("__SDK__", SDK_URL)
        .replace("__API__", _literal(api))
        .replace("__INIT__", _literal(init_data))
    )
