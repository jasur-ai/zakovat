"""Statistics for the bot owner.

Every game event passes through the webhook, but serverless has no database, so
the counters live in one small JSON document:

* ``ZAKOVAT_STATS_URL`` — any endpoint that answers ``GET`` with a JSON document
  and accepts ``PUT`` with the new document (jsonblob.com, kvdb.io, your own
  function…). ``ZAKOVAT_STATS_TOKEN`` is sent as a bearer token when it is set.
* without a URL the counters stay inside the running instance and the panel says
  so. Nothing breaks, the numbers simply cover that one copy.

Writes are batched (``ZAKOVAT_STATS_FLUSH`` seconds) and every failure is
swallowed: statistics must never break a game. Two instances flushing at the
same moment can lose a few increments — acceptable for a dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter

import httpx

OWNER_ID = int(os.getenv("ZAKOVAT_OWNER_ID", "8004724563"))
STORE_URL = os.getenv("ZAKOVAT_STATS_URL", "").strip()
STORE_TOKEN = os.getenv("ZAKOVAT_STATS_TOKEN", "").strip()
FLUSH_SECONDS = max(5, int(os.getenv("ZAKOVAT_STATS_FLUSH", "20")))
DRY_RUN = False        # the self-test counts, but never writes to the store
EVENT_LIMIT = 30
MAP_LIMIT = 400

COUNTERS = (
    "games_started",        # o‘yin boshlandi
    "games_finished",       # o‘yin yakuniga yetdi
    "games_auto_stopped",   # ketma-ket javobsizlikdan to‘xtadi
    "games_stopped",        # /stop yoki tugma bilan to‘xtatildi
    "questions_asked",      # berilgan savollar
    "answers_correct",      # to‘g‘ri javoblar
    "answers_wrong",        # noto‘g‘ri taxminlar
    "unanswered",           # javobsiz qolgan (vaqt tugadi yoki o‘tkazildi)
    "skips",                # admin o‘tkazishlari
    "refreshes",            # «Vaqtni yangilash» bosishlari
    "clock_opened",         # jonli soat oynasi ochilishi
    "commands",             # buyruqlar soni
)

# Package titles are grouped into sections by keyword; the first match wins.
REGIONS = (
    "andijon", "buxoro", "farg‘ona", "farg'ona", "jizzax", "xorazm", "navoiy",
    "qashqadaryo", "samarqand", "sirdaryo", "surxondaryo", "toshkent", "namangan",
    "qoraqalpog‘iston", "qoraqalpog'iston", "viloyati", "shahri", "tumani",
)
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Professional va Birinchi liga", ("professional", "birinchi liga")),
    ("Ikkinchi liga", ("ikkinchi liga",)),
    ("Talabalar ligasi", ("talabalar",)),
    ("NZK ligasi", ("nzk",)),
    ("Ayollar ligasi", ("ayollar",)),
    ("O‘quvchilar ligasi", ("o‘quvchilar", "quvchilar")),
    ("Maktablar", ("maktab",)),
    ("Oliy ta’lim muassasalari", ("otm", "oliy ta’lim", "oliy talim", "universitet")),
    ("Intellektual Olimpiada", ("olimpiada",)),
    ("Intellektual marafon", ("marafon",)),
    ("Klub va to‘garaklar", ("klub", "to‘garak", "togarak")),
    ("Hududiy turlar", REGIONS),
)
OTHER_SECTION = "Boshqa paketlar"

# --- instance state -------------------------------------------------------
_pending: dict = {"c": Counter(), "u": Counter(), "h": Counter(), "p": Counter(), "e": []}
_instance: dict = {"c": Counter(), "u": set(), "h": set(), "p": set(), "started": time.time()}
_last_flush = 0.0
_remote: dict = {}
_remote_at = 0.0


def section_of(title: str) -> str:
    """Which section of the archive a package title belongs to."""
    low = (title or "").casefold()
    for name, keys in SECTIONS:
        if any(key in low for key in keys):
            return name
    return OTHER_SECTION


def _headers() -> dict:
    headers = {"content-type": "application/json"}
    if STORE_TOKEN:
        headers["authorization"] = f"Bearer {STORE_TOKEN}"
    return headers


def _normalise(doc: object) -> dict:
    """Accept whatever the store returned and shape it like our document."""
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            return {}
    if not isinstance(doc, dict):
        return {}
    result = {"c": Counter(), "u": Counter(), "h": Counter(), "p": Counter(), "e": []}
    for key, field in (("c", "c"), ("u", "u"), ("h", "h"), ("p", "p")):
        value = doc.get(key)
        if isinstance(value, dict):
            for name, amount in value.items():
                try:
                    result[field][str(name)] += int(amount)
                except (TypeError, ValueError):
                    continue
    events = doc.get("e")
    if isinstance(events, list):
        result["e"] = [event for event in events if isinstance(event, dict)][:EVENT_LIMIT]
    result["s"] = doc.get("s")
    return result


def record(kind: str, *, user_id: int | None = None, chat_id: int | None = None,
           package_id: int | None = None, detail: str = "", log: bool = True,
           **counts: int) -> bool:
    """Count one game event. Returns True when a flush to the store is due.

    ``log=False`` keeps the event out of the "last games" feed — per-question
    noise would push the interesting entries (games, stops) out of the ring.
    """
    global _last_flush
    if log:
        _pending["e"].append({"t": int(time.time()), "k": kind, "x": detail[:80]})
        _pending["e"] = _pending["e"][-EVENT_LIMIT:]
    for name, amount in counts.items():
        if name in COUNTERS:
            _pending["c"][name] += int(amount)
            _instance["c"][name] += int(amount)
    if user_id:
        _pending["u"][str(user_id)] += 1
        _instance["u"].add(int(user_id))
    if chat_id:
        _pending["h"][str(chat_id)] += 1
        _instance["h"].add(int(chat_id))
    if package_id:
        _pending["p"][str(package_id)] += 1
        _instance["p"].add(int(package_id))
    for field in ("u", "h", "p"):                     # keep the buffer bounded
        if len(_pending[field]) > MAP_LIMIT:
            _pending[field] = Counter(dict(_pending[field].most_common(MAP_LIMIT)))
    # Only report that a flush is due; `sync` owns the timestamp, so a whole
    # burst of events queues a single write.
    return bool(STORE_URL) and time.time() - _last_flush >= FLUSH_SECONDS


def archive_sections(archive) -> list[dict]:
    """Packages and questions per archive section (no storage needed)."""
    packages = archive.manifest.get("packages", []) or []
    by_section: dict[str, dict] = {}
    for package in packages:
        name = section_of(package.get("title") or "")
        entry = by_section.setdefault(name, {"name": name, "packages": 0, "questions": 0})
        entry["packages"] += 1
        try:
            entry["questions"] += int(package.get("questions") or 0)
        except (TypeError, ValueError):
            pass
    ordered = sorted(by_section.values(), key=lambda item: (-item["packages"], item["name"]))
    return ordered


_archive_cache: dict = {}


def archive_stats(archive) -> dict:
    """Everything the archive itself can tell: packages, questions, sections.

    Counting pictures walks every shard, so the result is cached per archive —
    the export is immutable between deployments.
    """
    manifest = getattr(archive, "manifest", {}) or {}
    key = (id(archive), manifest.get("exported_at"), len(manifest.get("packages") or []))
    if key in _archive_cache:
        return _archive_cache[key]
    packages = manifest.get("packages", []) or []
    sections = archive_sections(archive)
    pictures = 0
    cyrillic = 0
    biggest = {"title": "", "questions": 0}
    for package in packages:
        title = package.get("title") or ""
        if any("\u0400" <= ch <= "\u04ff" for ch in title):
            cyrillic += 1
        try:
            count = int(package.get("questions") or 0)
        except (TypeError, ValueError):
            count = 0
        if count > biggest["questions"]:
            biggest = {"title": title, "questions": count}
    try:
        for package in packages:
            for question in archive.questions(int(package["id"])):
                attachments = question.get("attachments") or []
                if any(str(item).startswith("http") for item in attachments):
                    pictures += 1
    except Exception:  # a shard may be missing in a slim checkout
        logging.getLogger(__name__).debug("picture count failed", exc_info=True)
    result = {
        "packages": int(manifest.get("package_count") or len(packages)),
        "questions": int(manifest.get("question_count") or 0),
        "sections": sections,
        "section_count": len(sections),
        "pictures": pictures,
        "cyrillic": cyrillic,
        "biggest": biggest,
        "average": round(sum(int(p.get("questions") or 0) for p in packages) / len(packages), 1)
        if packages else 0,
        "exported_at": manifest.get("exported_at") or "",
    }
    _archive_cache[key] = result
    return result


def _merge(target: dict, source: dict) -> dict:
    for field in ("c", "u", "h", "p"):
        for key, amount in (source.get(field) or {}).items():
            target[field][key] += amount
    events = list(source.get("e") or []) + list(target.get("e") or [])
    events.sort(key=lambda item: -int(item.get("t") or 0))
    target["e"] = events[:EVENT_LIMIT]
    return target


def _cap(counter: Counter) -> dict:
    """Keep the busiest keys only, so the document stays small."""
    return {key: int(amount) for key, amount in counter.most_common(MAP_LIMIT)}


async def sync(force: bool = False) -> bool:
    """Read the document, fold our pending deltas in and write it back."""
    global _remote, _remote_at, _last_flush
    if not STORE_URL or DRY_RUN:
        return False
    if not force and time.time() - _last_flush < FLUSH_SECONDS:
        return False
    _last_flush = time.time()
    deltas = {"c": Counter(_pending["c"]), "u": Counter(_pending["u"]),
              "h": Counter(_pending["h"]), "p": Counter(_pending["p"]),
              "e": list(_pending["e"])}
    document = _normalise(_remote) if _remote else {"c": Counter(), "u": Counter(),
                                                    "h": Counter(), "p": Counter(), "e": []}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
            response = await client.get(STORE_URL, headers=_headers())
            if response.status_code == 200 and response.text.strip():
                document = _normalise(_try_json(response.text))
            body = _merge(document, deltas)
            payload = {"v": 1, "s": int(time.time()), "c": dict(body["c"]),
                       "u": _cap(body["u"]), "h": _cap(body["h"]), "p": _cap(body["p"]),
                       "e": body["e"][:EVENT_LIMIT]}
            body_text = json.dumps(payload, ensure_ascii=False)
            written = await client.put(STORE_URL, content=body_text, headers=_headers())
            if written.status_code in (405, 501):
                # Some stores (a Google Apps Script, a plain function) take POST only.
                written = await client.post(STORE_URL, content=body_text, headers=_headers())
        if written.status_code >= 400:
            return False
    except Exception:
        logging.getLogger(__name__).debug("stats sync failed", exc_info=True)
        return False
    # Only drop what we actually sent: events recorded while we were writing stay.
    for field in ("c", "u", "h", "p"):
        for key, amount in deltas[field].items():
            _pending[field][key] -= amount
        _pending[field] = Counter({k: v for k, v in _pending[field].items() if v > 0})
    sent = {event["t"] for event in deltas["e"]}
    _pending["e"] = [event for event in _pending["e"] if event["t"] not in sent]
    _remote = payload
    _remote_at = time.time()
    return True


def _try_json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


async def probe() -> dict:
    """Check the configured store end to end: write a marker, read it back."""
    if not STORE_URL:
        return {"ok": False, "reason": "ZAKOVAT_STATS_URL berilmagan — hisob faqat shu nusxada",
                "rejim": "lokal"}
    token = f"probe-{int(time.time())}"
    document = {"v": 1, "s": int(time.time()), "c": {}, "u": {}, "h": {}, "p": {},
                "e": [{"t": int(time.time()), "k": "probe", "x": token}]}
    host = STORE_URL.split("//")[-1].split("/")[0]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            written = await client.put(STORE_URL, content=json.dumps(document, ensure_ascii=False),
                                       headers=_headers())
            if written.status_code in (405, 501):
                written = await client.post(STORE_URL,
                                            content=json.dumps(document, ensure_ascii=False),
                                            headers=_headers())
            read = await client.get(STORE_URL, headers=_headers())
        found = token in read.text
        return {"ok": written.status_code < 400 and found, "manzil": host,
                "yozish": written.status_code, "oqish": read.status_code,
                "qayta_oqildi": found,
                "izoh": ("saqlash ishlaydi: yozildi va qayta o‘qildi"
                         if found else "yozildi, lekin qayta o‘qishda topilmadi")}
    except Exception as exc:
        return {"ok": False, "manzil": host, "xato": f"{type(exc).__name__}: {exc}"}


async def snapshot(archive=None, force: bool = True) -> dict:
    """Everything the panel shows: stored totals, this copy's totals, archive."""
    global _remote, _remote_at
    stored = False
    if STORE_URL and not DRY_RUN:
        try:
            stored = await sync(force=force)
        except Exception:
            stored = False
        if not stored and not _remote:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
                    response = await client.get(STORE_URL, headers=_headers())
                if response.status_code == 200:
                    _remote = _normalise(_try_json(response.text))
                    _remote_at = time.time()
            except Exception:
                logging.getLogger(__name__).debug("stats read failed", exc_info=True)
    elif force:
        await asyncio.sleep(0)

    stored_doc = _normalise(_remote)
    totals: Counter = Counter(stored_doc.get("c") or {})
    users: Counter = Counter(stored_doc.get("u") or {})
    chats: Counter = Counter(stored_doc.get("h") or {})
    packages: Counter = Counter(stored_doc.get("p") or {})
    events = list(stored_doc.get("e") or [])
    for field, target in (("c", totals), ("u", users), ("h", chats), ("p", packages)):
        for key, amount in _pending[field].items():
            target[key] += amount
    events = list(_pending["e"]) + events
    events.sort(key=lambda item: -int(item.get("t") or 0))
    asked = totals["answers_correct"] + totals["answers_wrong"] + totals["unanswered"]
    return {
        "counters": {name: int(totals[name]) for name in COUNTERS},
        "users": len(users),
        "chats": len(chats),
        "packages_played": len(packages),
        "top_packages": packages.most_common(5),
        "events": events[:EVENT_LIMIT],
        "instance": {
            "counters": {name: int(_instance["c"][name]) for name in COUNTERS},
            "users": len(_instance["u"]),
            "chats": len(_instance["h"]),
            "packages": len(_instance["p"]),
            "uptime": int(time.time() - _instance["started"]),
        },
        "accuracy": round(totals["answers_correct"] * 100 / asked) if asked else 0,
        "asked": asked,
        "storage": STORE_URL or "",
        "stored": bool(STORE_URL),
        "dry_run": DRY_RUN,
        "synced": stored,
        "remote_age": int(time.time() - _remote_at) if _remote_at else None,
        "archive": archive_stats(archive) if archive is not None else {},
    }


def reset() -> None:
    """Test helper: drop everything this instance has counted."""
    global _last_flush, _remote, _remote_at
    _pending["c"] = Counter()
    _pending["u"] = Counter()
    _pending["h"] = Counter()
    _pending["p"] = Counter()
    _pending["e"] = []
    _instance["c"] = Counter()
    _instance["u"] = set()
    _instance["h"] = set()
    _instance["p"] = set()
    _instance["started"] = time.time()
    _last_flush = 0.0
    _remote = {}
    _remote_at = 0.0
