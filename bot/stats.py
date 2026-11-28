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
import secrets
import time
from collections import Counter

import httpx

OWNER_ID = int(os.getenv("ZAKOVAT_OWNER_ID", "8004724563"))
STORE_URL = os.getenv("ZAKOVAT_STATS_URL", "").strip()
STORE_TOKEN = os.getenv("ZAKOVAT_STATS_TOKEN", "").strip()
# Vercel Blob: connecting a store to the project injects this token, so nothing
# else has to be configured — the bot finds its own storage.
BLOB_TOKEN = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
BLOB_PATH = os.getenv("ZAKOVAT_STATS_BLOB_PATH", "zakovat-stats.json").strip()
BLOB_WRITE_URL = "https://blob.vercel-storage.com/"
# Telegram itself is the storage that needs no service at all: the bot keeps the
# document in a pinned message of one chat and reads it back through getChat.
TG_API = "https://api.telegram.org/bot"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STATS_CHAT = int(os.getenv("ZAKOVAT_STATS_CHAT", "0") or 0) or OWNER_ID
DOC_HEADER = ("Zakovat Agent — statistika hujjati. Bu xabarni o‘chirmang: "
              "bot hisoblarni shu yerda saqlaydi.")
MESSAGE_LIMIT = 4096
FLUSH_SECONDS = max(5, int(os.getenv("ZAKOVAT_STATS_FLUSH", "20")))
DRY_RUN = False        # the self-test counts, but never writes to the store
EVENT_LIMIT = 30
MAP_LIMIT = 400
ACCESS_LIMIT = 60           # nechta foydalanuvchini Mini App ga ruxsat berish mumkin
USED_LIMIT = 120              # «ishlatildi» indeksida saqlanadigan eng ko‘p savol
PLAN_LIMIT = 12               # nechta rejadagi turnir eslatib qo‘yiladi
GROUP_LIMIT = 12              # panel tanlay oladigan guruhlar soni

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
    "app_opened",           # Mini App oynasi ochilishi
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
_pending: dict = {"c": Counter(), "u": Counter(), "h": Counter(), "p": Counter(),
                  "e": [], "q": []}
_instance: dict = {"c": Counter(), "u": set(), "h": set(), "p": set(), "started": time.time()}
_last_flush = 0.0
_remote: dict = {}
_remote_at = 0.0
_blob_url = ""          # the public address of our blob, learned on first write
_tg_message_id = 0      # the pinned message that holds the document
_access: dict = {}      # user ids the owner let into the Mini App
_used: dict[str, dict] = {}    # savol id -> qachon, qaysi paketda ishlatilgan
_plans: dict[str, dict] = {}   # "chat:xabar" -> rejadagi turnir eslatmasi
_groups: dict[str, str] = {}   # bot yozgan guruhlar: id -> sarlavha


def backend() -> str:
    """Which storage this copy writes to.

    ``http`` when an address was configured, ``blob`` when Vercel injected a
    Blob token, ``telegram`` when only the bot token is there (the default on a
    fresh deploy — nothing has to be set up), ``lokal`` otherwise.
    """
    if STORE_URL:
        return "http"
    if BLOB_TOKEN:
        return "blob"
    if BOT_TOKEN:
        return "telegram"
    return "lokal"


def section_of(title: str) -> str:
    """Which section of the archive a package title belongs to."""
    low = (title or "").casefold()
    for name, keys in SECTIONS:
        if any(key in low for key in keys):
            return name
    return OTHER_SECTION


def store_label() -> str:
    """One line for the status page: where the numbers are kept."""
    kind = backend()
    if kind == "http":
        return STORE_URL.split("//")[-1].split("/")[0]
    if kind == "blob":
        return f"Vercel Blob · {BLOB_PATH}"
    if kind == "telegram":
        return f"Telegram · chat {STATS_CHAT} (pinlangan xabar)"
    return "faqat shu nusxa"


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
    result["a"] = _normalise_access(doc.get("a"))
    result["s"] = doc.get("s")
    result["q"] = _normalise_used(doc.get("q"))
    result["r"] = _normalise_plans(doc.get("r"))
    result["g"] = _normalise_groups(doc.get("g"))
    return result


def _normalise_access(value: object) -> dict:
    """Read the Mini App guest list: `{user_id: {"name":…, "since":…}}`.

    Stored compactly as `"Ism|minutes"` per user — the document is one Telegram
    message, and the fat shape alone would eat the room the used-question index
    needs. The older dict shape is still accepted.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for key, item in value.items():
        key = str(key)[:20]
        if not key.isdigit():
            continue
        if isinstance(item, str):
            name, _, minutes = item.rpartition("|")
            try:
                since = int(float(minutes or 0) * 60) if _ else 0
            except ValueError:
                name, since = item, 0
            out[key] = {"name": str(name)[:40], "since": since}
        elif isinstance(item, dict):
            try:
                out[key] = {"name": str(item.get("name") or "")[:40],
                            "since": int(item.get("since") or 0)}
            except (TypeError, ValueError):
                continue
    return out


def _pack_access(access: dict) -> dict:
    """Write the guest list back out in its compact shape."""
    return {str(key): f"{str((item or {}).get('name') or '')[:40]}|"
                      f"{int((item or {}).get('since') or 0) // 60}"
            for key, item in (access or {}).items()}


def _normalise_plans(value: object) -> dict:
    """Read the reminder list: `{"<chat>:<message>": [start, title]}`.

    Stored compactly as `chat.message.minutes.title|…`, because the whole
    document has to fit into one Telegram message.
    """
    out: dict[str, list] = {}
    if isinstance(value, str):
        for chunk in value.split("|"):
            parts = chunk.split(".", 3)
            if len(parts) < 3:
                continue
            chat, message, minutes = parts[0], parts[1], parts[2]
            title = parts[3] if len(parts) > 3 else ""
            if not chat.lstrip("-").isdigit() or not message.isdigit():
                continue
            try:
                start = int(float(minutes or 0) * 60)
            except ValueError:
                continue
            out[f"{chat}:{message}"] = [start, str(title)[:40]]
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(item, (list, tuple)) or len(item) < 1:
                continue
            try:
                out[str(key)[:24]] = [int((list(item) + [0, ""])[0] or 0),
                                      str((list(item) + [0, ""])[1] or "")[:40]]
            except (TypeError, ValueError):
                continue
    return dict(sorted(out.items(), key=lambda kv: kv[1][0])[:PLAN_LIMIT])


def _pack_plans(plans: dict) -> str:
    """Write the reminder list back out, soonest first."""
    rows = sorted((plans or {}).items(), key=lambda kv: int((kv[1] or [0])[0]))[:PLAN_LIMIT]
    return "|".join(
        f"{key.split(':')[0]}.{key.split(':')[1] if ':' in key else 0}."
        f"{int((item or [0])[0]) // 60}.{str((item or ['', ''])[1])[:40]}"
        for key, item in rows if ":" in key)


def _normalise_groups(value: object) -> dict:
    """The chats the panel may post a tournament into: `{chat_id: title}`."""
    if not isinstance(value, dict):
        return {}
    out = {str(key)[:20]: str(item or "")[:40] for key, item in value.items()
           if str(key).lstrip("-").isdigit()}
    return dict(list(out.items())[-GROUP_LIMIT:])


def _normalise_used(value: object) -> dict:
    """Read the used-question index: `{"<id>": <stamp>}`, newest first.

    The document is one Telegram message, so the stored form is the compact
    string `"id:minutes,id:minutes,…"`. A dict (an older or richer shape) is
    accepted too; anything unreadable is simply dropped.
    """
    out: dict[str, int] = {}
    if isinstance(value, str):
        for chunk in value.split(","):
            head, _, tail = chunk.partition(":")
            head = head.strip()
            if not head.isdigit():
                continue
            try:
                out[head[:12]] = int(float(tail.strip() or 0) * 60)
            except ValueError:
                out[head[:12]] = 0
    elif isinstance(value, dict):
        for key, item in value.items():
            key = str(key)[:12]
            if not key.isdigit():
                continue
            if isinstance(item, (list, tuple)):
                when = (list(item) + [0])[0]
            elif isinstance(item, dict):
                when = item.get("t")
            else:
                when = item
            try:
                out[key] = int(when or 0)
            except (TypeError, ValueError):
                continue
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:USED_LIMIT])


def _pack_used(index: dict) -> str:
    """Write the index back out as `id:minutes`, newest first, capped."""
    rows = sorted(((str(key), int(stamp or 0)) for key, stamp in (index or {}).items()),
                  key=lambda kv: -kv[1])[:USED_LIMIT]
    return ",".join(f"{key}:{stamp // 60}" for key, stamp in rows)


def _trim_used(value: object, keep: int) -> str:
    """The stored (compact) form of the index, cut down to `keep` newest rows."""
    index = _normalise_used(value) if not isinstance(value, str) else _normalise_used(value)
    if keep <= 0:
        return ""
    return _pack_used(dict(list(index.items())[:keep]))


def _pending_used() -> dict:
    """Pending uses folded into one index entry per question."""
    merged: dict[str, int] = {}
    for key, stamp, _package, _chat in _pending.get("q") or []:
        if int(stamp) >= int(merged.get(str(key)) or 0):
            merged[str(key)] = int(stamp)
    return merged


def _cap_used(index: dict) -> dict:
    """Keep the newest ``USED_LIMIT`` rows of the index.

    A whole playlist asked in one second shares one stamp, so equal stamps are
    ordered by insertion: the question asked last is the newest one.
    """
    rows = sorted(reversed(list(index.items())),
                  key=lambda kv: -int((kv[1] or {}).get("t") or 0))
    return dict(rows[:USED_LIMIT])


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
    # burst of events queues a single write. Any backend counts, not only an
    # address that was configured by hand.
    return backend() != "lokal" and time.time() - _last_flush >= FLUSH_SECONDS


def mark_used(questions, package_id: int = 0, chat_id: int = 0, mode: str = "",
              detail: str = "") -> int:
    """Index the questions a game asked, so "ishlatildi" can be shown later.

    Asking one again keeps the newest stamp and grows its use count. The index
    travels inside the statistics document, so it costs no extra storage call and
    a cold instance learns it back on the first read.
    """
    global _used
    stamp = int(time.time())
    added = 0
    for question in questions or []:
        try:
            question_id = int((question or {}).get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not question_id:
            continue
        key = str(question_id)
        before = _used.get(key) or {}
        _used[key] = {"t": stamp,
                      "p": int(package_id or (question or {}).get("package_id") or 0),
                      "h": int(chat_id or 0),
                      "m": str(mode or "")[:12],
                      "n": int(before.get("n") or 0) + 1,
                      "d": str(detail or "")[:40]}
        _pending["q"].append([key, stamp, _used[key]["p"], _used[key]["h"]])
        added += 1
    if added:
        del _pending["q"][:-USED_LIMIT]
        _used = _cap_used(_used)
    return added


def used_index(archive=None, limit: int = 0) -> dict:
    """The whole "ishlatildi" index, newest first, plus a short summary.

    With an archive the package is resolved for the rows actually shown, so a
    stored row (which carries only id and stamp) still reads well in the panel.
    """
    records = list(_cap_used(_used).items())
    packages: Counter = Counter()
    for _, item in records:
        package = int((item or {}).get("p") or 0)
        if package:
            packages[str(package)] += 1
    rows = records[:limit] if limit and limit > 0 else records
    items = []
    for key, item in rows:
        row = {"id": int(key), **(item or {})}
        if archive is not None and not row.get("p"):
            found = archive.question(int(key)) or {}
            row["p"] = int(found.get("package_id") or 0)
            row["x"] = str(found.get("text") or "")[:90]
        items.append(row)
    return {
        "count": len(records),
        "limit": USED_LIMIT,
        "packages": len(packages),
        "uses": sum(int((item or {}).get("n") or 1) for _, item in records),
        "top_packages": [(int(key), amount) for key, amount in packages.most_common(6)],
        "items": items,
    }


def _adopt_used(stored: dict) -> None:
    """Fold the stored index into this copy's memory, newest stamp wins."""
    global _used
    for key, stamp in (stored or {}).items():
        stamp = int(stamp or 0)
        current = _used.get(key)
        if current and int(current.get("t") or 0) >= stamp:
            continue
        _used[key] = {"t": stamp, "p": 0, "h": 0, "m": "", "n": 1}
    if len(_used) > USED_LIMIT:
        _used = _cap_used(_used)


def used_summary() -> dict:
    """The headline numbers of the "ishlatildi" index, plus a few fresh rows."""
    index = used_index()
    index["recent"] = index.pop("items")[:8]
    return index


def plan_key(chat_id: int, message_id: int) -> str:
    return f"{int(chat_id)}:{int(message_id)}"


def plan_add(chat_id: int, message_id: int, start_at: int, title: str = "") -> None:
    """Remember that this chat's card has to be announced at `start_at`."""
    if not chat_id or not message_id or not start_at:
        return
    _plans[plan_key(chat_id, message_id)] = [int(start_at), str(title or "")[:40]]
    while len(_plans) > PLAN_LIMIT:
        # the farthest plan goes: the ones about to fire are the point of the list
        furthest = max(_plans.items(), key=lambda kv: int((kv[1] or [0])[0]))
        _plans.pop(furthest[0], None)


def plan_drop(chat_id: int, message_id: int) -> None:
    """The reminder is no longer needed: it fired, or the card is gone."""
    _plans.pop(plan_key(chat_id, message_id), None)


def plans(now: int = 0) -> list[dict]:
    """Every registered reminder, soonest first."""
    moment = int(now or time.time())
    rows = [{"chat": int(key.split(":")[0]), "message": int(key.split(":")[1]),
             "start": int((item or [0])[0]), "title": str((item or ["", ""])[1]),
             "left": int((item or [0])[0]) - moment}
            for key, item in sorted(_plans.items(), key=lambda kv: int((kv[1] or [0])[0]))]
    return rows


def due_plans(now: int = 0, lead: int = 0) -> list[dict]:
    """Reminders whose time has come (or comes within `lead` seconds)."""
    return [row for row in plans(now) if -3600 <= row["left"] <= max(0, int(lead))]


def group_seen(chat_id: int, title: str = "") -> None:
    """Remember a group the bot works in, so the panel can offer it as a target."""
    if not chat_id or int(chat_id) >= 0:
        return
    key = str(int(chat_id))
    if title:
        _groups[key] = str(title)[:40]
    elif key not in _groups:
        _groups[key] = ""
    while len(_groups) > GROUP_LIMIT:
        _groups.pop(next(iter(_groups)), None)


def groups() -> dict:
    """The chats the composer can post into: `{chat_id: title}`."""
    return {int(key): value for key, value in _groups.items() if key.lstrip("-").isdigit()}


def used_ids() -> set[int]:
    """Every question id the index knows about."""
    return {int(key) for key in _used if key.isdigit()}


def is_used(question_id) -> bool:
    """True when this question has already been asked somewhere."""
    try:
        return str(int(question_id or 0)) in _used
    except (TypeError, ValueError):
        return False


def allowed(user_id: int | str | None) -> bool:
    """May this user open the Mini App? The owner always may, plus granted ids."""
    try:
        number = int(user_id or 0)
    except (TypeError, ValueError):
        return False
    if not number:
        return False
    return number == int(OWNER_ID) or str(number) in _access


def access_list() -> dict:
    """Everyone the owner let in: ``{user id: {"name": …, "since": …}}``."""
    return dict(_access)


async def set_access(user_id: int, name: str = "", grant: bool = True,
                     transport=None) -> tuple[bool, dict]:
    """Add or remove one user, writing the document out at once.

    The list lives in the same document as the counters, so it survives a cold
    start exactly like the numbers do.
    """
    global _access, _remote, _remote_at
    entry = str(int(user_id))
    access = dict(_access)
    if grant:
        access[entry] = {"name": (name or "").strip()[:40], "since": int(time.time())}
        if len(access) > ACCESS_LIMIT:
            # The oldest grant goes first, so a new one is never silently dropped.
            for key in sorted(access,
                              key=lambda item: int((access[item] or {}).get("since") or 0)):
                if len(access) <= ACCESS_LIMIT:
                    break
                access.pop(key, None)
    else:
        access.pop(entry, None)
    if backend() == "lokal":
        _access = access
        return True, dict(access)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                     transport=transport) as client:
            code, text = await store_read(client)
            document = _normalise(_try_json(text)) if code == 200 else {}
            document["a"] = access
            # a guest list write must not throw the rest of the document away:
            # the used-question index, the reminders and the groups ride along.
            payload = {"v": 1, "s": document.get("s") or int(time.time()),
                       "c": {str(k): int(v) for k, v in (document.get("c") or {}).items()},
                       "u": _cap(Counter(document.get("u") or {})),
                       "h": _cap(Counter(document.get("h") or {})),
                       "p": _cap(Counter(document.get("p") or {})),
                       "e": list(document.get("e") or [])[:EVENT_LIMIT],
                       "a": _pack_access(access),
                       "q": _pack_used(document.get("q") or {}),
                       "r": _pack_plans(document.get("r") or {}),
                       "g": _normalise_groups(document.get("g") or {})}
            code, _ = await store_write(client, json.dumps(payload, ensure_ascii=False))
    except Exception:
        logging.getLogger(__name__).debug("access write failed", exc_info=True)
        return False, dict(_access)
    _access = access
    if code < 400:
        _remote = payload
        _remote_at = time.time()
    return code < 400, dict(access)


async def load_access(transport=None) -> dict:
    """Refresh the list from the store (a cold instance starts knowing nobody)."""
    global _access, _remote, _remote_at
    if backend() == "lokal":
        return dict(_access)
    if _remote and time.time() - _remote_at < FLUSH_SECONDS:
        _access = dict(_remote.get("a") or {})
        return dict(_access)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0),
                                     transport=transport) as client:
            code, text = await store_read(client)
        if code == 200:
            _remote = _normalise(_try_json(text))
            _remote_at = time.time()
            _access = dict(_remote.get("a") or {})
    except Exception:
        logging.getLogger(__name__).debug("access read failed", exc_info=True)
    return dict(_access)


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
    index = dict(target.get("q") or {})
    for key, stamp in (source.get("q") or {}).items():
        if int(stamp or 0) >= int(index.get(key) or 0):
            index[key] = int(stamp or 0)
    target["q"] = _normalise_used(index)
    target["r"] = _normalise_plans({**(target.get("r") or {}), **(source.get("r") or {})})
    target["g"] = _normalise_groups({**(target.get("g") or {}), **(source.get("g") or {})})
    return target


def _cap(counter: Counter) -> dict:
    """Keep the busiest keys only, so the document stays small."""
    return {key: int(amount) for key, amount in counter.most_common(MAP_LIMIT)}


def _shrink(payload: dict, room: int) -> dict:
    """Trim maps and events until the document fits in one Telegram message.

    The counters are the point of the panel, so they are never dropped; the
    longest lists go first.
    """
    text = json.dumps(payload, ensure_ascii=False)
    for maps, events, used in ((60, 12, USED_LIMIT), (60, 12, 40), (30, 8, 40),
                               (30, 8, 0), (12, 5, 0), (5, 3, 0), (0, 0, 0)):
        if len(text) + len(DOC_HEADER) + 1 <= room:
            return payload
        trimmed = dict(payload)
        for field in ("u", "h", "p"):
            trimmed[field] = dict(Counter(payload.get(field) or {}).most_common(maps))
        trimmed["e"] = list(payload.get("e") or [])[:events]
        # the used-question index is the youngest part of the document, so a tight
        # store gives it up first — the newest stamps are the ones that stay.
        if "q" in trimmed:
            trimmed["q"] = _trim_used(trimmed.get("q"), used)
        if "g" in trimmed and used == 0:
            trimmed["g"] = dict(list((trimmed.get("g") or {}).items())[:4])
        payload = trimmed
        text = json.dumps(payload, ensure_ascii=False)
    return payload


async def sync(force: bool = False, transport=None) -> bool:
    """Read the document, fold our pending deltas in and write it back."""
    global _remote, _remote_at, _last_flush, _access
    if backend() == "lokal" or DRY_RUN:
        return False
    if not force and time.time() - _last_flush < FLUSH_SECONDS:
        return False
    _last_flush = time.time()
    deltas = {"c": Counter(_pending["c"]), "u": Counter(_pending["u"]),
              "h": Counter(_pending["h"]), "p": Counter(_pending["p"]),
              "e": list(_pending["e"]), "q": _pending_used(),
              "r": _normalise_plans(_plans), "g": dict(_groups)}
    document = _normalise(_remote) if _remote else {"c": Counter(), "u": Counter(),
                                                    "h": Counter(), "p": Counter(), "e": [],
                                                    "a": {}, "q": {}}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0),
                                     transport=transport) as client:
            status, text = await store_read(client)
            if status == 200 and text.strip():
                document = _normalise(_try_json(text))
            body = _merge(document, deltas)
            payload = {"v": 1, "s": int(time.time()), "c": dict(body["c"]),
                       "u": _cap(body["u"]), "h": _cap(body["h"]), "p": _cap(body["p"]),
                       "e": body["e"][:EVENT_LIMIT], "a": _pack_access(body.get("a") or {}),
                       "q": _pack_used(body.get("q") or {}),
                       "r": _pack_plans(body.get("r") or {}),
                       "g": _normalise_groups(body.get("g") or {})}
            payload = _shrink(payload, MESSAGE_LIMIT)
            body_text = json.dumps(payload, ensure_ascii=False)
            status, _ = await store_write(client, body_text)
        if status >= 400:
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
    sent_questions = set(deltas["q"])
    _pending["q"] = [row for row in _pending["q"] if str(row[0]) not in sent_questions]
    _remote = payload
    _remote_at = time.time()
    _access = dict(payload.get("a") or {})
    _plans.update(_normalise_plans(payload.get("r") or {}))
    _groups.update(payload.get("g") or {})
    return True


def _blob_headers(method: str) -> dict:
    headers = {"authorization": f"Bearer {BLOB_TOKEN}", "x-api-version": "7"}
    if method == "put":
        headers["content-type"] = "application/json"
        headers["x-content-type"] = "application/json"
        headers["x-add-random-suffix"] = "0"     # keep one stable address
    return headers


async def blob_write(client, payload: str) -> tuple[int, str]:
    """PUT the document into the connected Vercel Blob store."""
    global _blob_url
    written = await client.put(BLOB_WRITE_URL + BLOB_PATH, content=payload,
                               headers=_blob_headers("put"))
    if written.status_code < 400:
        try:
            _blob_url = str(written.json().get("url") or "") or _blob_url
        except ValueError:
            pass
    return written.status_code, written.text[:120]


async def blob_read(client) -> tuple[int, str]:
    """Read the document back: the stored address, or a lookup by prefix."""
    global _blob_url
    if _blob_url:
        response = await client.get(_blob_url, headers={"x-api-version": "7"})
        if response.status_code == 200:
            return 200, response.text
    listed = await client.get(BLOB_WRITE_URL, params={"prefix": BLOB_PATH},
                              headers=_blob_headers("get"))
    if listed.status_code >= 400:
        return listed.status_code, listed.text[:120]
    try:
        blobs = listed.json().get("blobs") or []
    except ValueError:
        return listed.status_code, listed.text[:120]
    match = next((item for item in blobs if item.get("pathname") == BLOB_PATH), None)
    url = str((match or {}).get("url") or "")
    if not url:
        return 404, "hujjat topilmadi"
    _blob_url = url
    response = await client.get(url, headers={"x-api-version": "7"})
    return response.status_code, response.text


async def _tg_call(client, method: str, **fields) -> dict:
    """One plain Bot API call — no PTB lifecycle, so it also works after the
    response was sent (that is when the statistics are flushed)."""
    try:
        response = await client.post(f"{TG_API}{BOT_TOKEN}/{method}", json=fields)
        return response.json()
    except Exception as exc:
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}


async def telegram_read(client) -> tuple[int, str]:
    """Read the document from the pinned message of the statistics chat."""
    global _tg_message_id
    answer = await _tg_call(client, "getChat", chat_id=STATS_CHAT)
    pinned = (answer.get("result") or {}).get("pinned_message") or {}
    text = pinned.get("text") or ""
    _tg_message_id = int(pinned.get("message_id") or 0)
    if not answer.get("ok") and not text:
        return 502, str(answer.get("description") or "")[:120]
    return (200, text) if text else (404, "")


async def telegram_write(client, payload: str) -> tuple[int, str]:
    """Put the document into that message, creating and pinning it if needed."""
    global _tg_message_id
    text = f"{DOC_HEADER}\n{payload}"
    if len(text) > MESSAGE_LIMIT:
        return 413, "hujjat xabarga sig‘madi"
    if _tg_message_id:
        answer = await _tg_call(client, "editMessageText", chat_id=STATS_CHAT,
                                message_id=_tg_message_id, text=text)
        if answer.get("ok") or "not modified" in str(answer.get("description", "")):
            return 200, "tahrirlandi"
        _tg_message_id = 0                     # the message is gone; send a new one
    sent = await _tg_call(client, "sendMessage", chat_id=STATS_CHAT, text=text,
                          disable_notification=True)
    message_id = int((sent.get("result") or {}).get("message_id") or 0)
    if not message_id:
        return 502, str(sent.get("description") or "")[:120]
    await _tg_call(client, "pinChatMessage", chat_id=STATS_CHAT,
                   message_id=message_id, disable_notification=True)
    _tg_message_id = message_id
    return 200, "yuborildi va pinlandi"


async def store_read(client) -> tuple[int, str]:
    kind = backend()
    if kind == "blob":
        return await blob_read(client)
    if kind == "telegram":
        return await telegram_read(client)
    response = await client.get(STORE_URL, headers=_headers())
    return response.status_code, response.text


async def store_write(client, payload: str) -> tuple[int, str]:
    kind = backend()
    if kind == "blob":
        return await blob_write(client, payload)
    if kind == "telegram":
        return await telegram_write(client, payload)
    written = await _put(client, STORE_URL, payload)
    return written.status_code, written.text[:120]


def _try_json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")                      # a header line may precede the JSON
    if start > 0:
        try:
            return json.loads(text[start:])
        except ValueError:
            pass
    return text


def _empty_document(marker: str) -> dict:
    return {"v": 1, "s": int(time.time()), "c": {}, "u": {}, "h": {}, "p": {},
            "e": [{"t": int(time.time()), "k": "probe", "x": marker}]}


async def _put(client, url: str, body: str, headers: dict | None = None):
    """Write a document, falling back to POST where PUT is not allowed."""
    headers = _headers() if headers is None else headers
    written = await client.put(url, content=body, headers=headers)
    if written.status_code in (405, 501):
        written = await client.post(url, content=body, headers=headers)
    return written


# --- free document stores -------------------------------------------------
# No sign-up and no key: the bot creates the document itself and reports where
# it lives. Each provider answers GET with the document and accepts PUT/POST.
# A plain python user agent is refused by several of these, so look like a
# browser and keep redirects in our own hands (a followed redirect hides the
# Location header that carries the new address).
_STORE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/plain, */*",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ZakovatBot/1.0"),
}


def _detail(response, extra: dict | None = None) -> dict:
    snippet = " ".join(response.text.split())[:90]
    info = {"kod": response.status_code, "javob": snippet}
    location = response.headers.get("location") or response.headers.get("Location")
    if location:
        info["location"] = str(location)[:160]
    if extra:
        info.update(extra)
    return info


async def _provision_jsonblob(client) -> tuple[str, dict]:
    response = await client.post("https://jsonblob.com/api/jsonBlob",
                                 content=json.dumps(_empty_document("init")),
                                 headers=_STORE_HEADERS, follow_redirects=False)
    location = (response.headers.get("location") or "").strip()
    url = ("https://jsonblob.com" + location) if location.startswith("/") else location
    return url, _detail(response)


KVDB_TTL = 7776000      # 90 days: the longest the free plan keeps an idle key


async def _provision_kvdb(client) -> tuple[str, dict]:
    """kvdb: a free key-value bucket, private from the moment it is created.

    The documented call is ``curl -d 'email=user@example.com' https://kvdb.io``,
    yet the service answers "email required" to some clients, so a handful of
    equivalent spellings are tried in order and every answer is reported. The
    ``secret_key`` variant makes later reads and writes need ``Authorization:
    Bearer <key>`` — exactly what ``ZAKOVAT_STATS_TOKEN`` already sends.
    """
    secret = secrets.token_urlsafe(18)
    private = {"email": STORE_EMAIL, "secret_key": secret, "default_ttl": str(KVDB_TTL)}
    variants: tuple[tuple[str, bool, dict], ...] = (
        ("forma+kalit", True, {"data": private}),        # private bucket first
        ("forma", False, {"data": {"email": STORE_EMAIL}}),
        ("xom", False, {"content": f"email={STORE_EMAIL}",
                        "headers": {"content-type": "application/x-www-form-urlencoded"}}),
        ("json", False, {"json": {"email": STORE_EMAIL}}),
        ("so‘rov", False, {"params": {"email": STORE_EMAIL}}),
        ("sarlavha", False, {"headers": {"email": STORE_EMAIL}}),
    )
    summary: list[str] = []
    last_code = 0
    for usul, keyed, kwargs in variants:
        try:
            response = await client.post("https://kvdb.io/", timeout=5.0,
                                         follow_redirects=True, **kwargs)
        except Exception as exc:
            summary.append(f"{usul}:xato {type(exc).__name__}")
            continue
        bucket = response.text.strip()
        last_code = response.status_code
        summary.append(f"{usul}:{response.status_code} {' '.join(response.text.split())[:34]}")
        if _usable_bucket(response.status_code, bucket):
            detail = {"kod": response.status_code, "usul": usul,
                      "javob": " | ".join(summary)[:400]}
            if keyed:
                detail["kalit"] = secret
            return f"https://kvdb.io/{bucket}/zakovat", detail
    return "", {"kod": last_code, "usul": "hammasi", "javob": " | ".join(summary)[:400]}


def _usable_bucket(status: int, bucket: str) -> bool:
    return status < 400 and bool(bucket) and len(bucket) <= 64 and " " not in bucket


async def _provision_extendsclass(client) -> tuple[str, dict]:
    response = await client.post("https://extendsclass.com/api/json-storage",
                                 content=json.dumps(_empty_document("init")),
                                 headers=_STORE_HEADERS, follow_redirects=False)
    detail = _detail(response)
    try:
        blob_id = str(response.json().get("id") or "")
    except Exception:
        return "", detail
    if not blob_id:
        return "", detail
    return f"https://extendsclass.com/api/json-storage/{blob_id}", detail


STORE_EMAIL = os.getenv("ZAKOVAT_STATS_EMAIL", "zakovat-agent@users.noreply.github.com")


async def _provision_pantry(client) -> tuple[str, dict]:
    """getpantry.cloud: a free JSON pantry, the id is the only credential."""
    response = await client.post(
        "https://getpantry.cloud/apiv1/pantry/create",
        content=json.dumps({"name": "Zakovat Agent statistika",
                            "description": "Bot statistikasi uchun JSON hujjat",
                            "contactEmail": STORE_EMAIL}),
        headers=_STORE_HEADERS, follow_redirects=False)
    detail = _detail(response)
    try:
        pantry_id = str(response.json().get("id") or "")
    except Exception:
        return "", detail
    if not pantry_id:
        return "", detail
    detail["pantry"] = pantry_id
    return f"https://api.getpantry.cloud/apiv1/pantry/{pantry_id}/basket/zakovat", detail


async def _provision_npoint(client) -> tuple[str, dict]:
    """npoint.io keeps a JSON document behind a short id and allows PUT."""
    response = await client.post("https://api.npoint.io/",
                                 content=json.dumps({"v": 1, "c": {}}),
                                 headers=_STORE_HEADERS, follow_redirects=False)
    detail = _detail(response)
    try:
        blob_id = str(response.json().get("id") or "")
    except Exception:
        return "", detail
    if not blob_id:
        return "", detail
    return f"https://api.npoint.io/{blob_id}", detail


PROVIDERS: tuple[tuple[str, object], ...] = (
    ("kvdb", _provision_kvdb),           # bepul, kalitsiz, GET/PUT xom matn
    ("npoint", _provision_npoint),
    ("jsonblob", _provision_jsonblob),   # datacentre manzillarini 403 bilan rad etadi
    ("extendsclass", _provision_extendsclass),
    ("pantry", _provision_pantry),       # captcha talab qiladi
)


async def create_store(transport=None) -> dict:
    """Create a free JSON document store, verify it and report its address.

    Serverless has no database and registering a service for a dashboard is one
    chore too many, so the bot provisions one and hands back the URL: put it in
    ``ZAKOVAT_STATS_URL`` and every instance shares the same numbers.
    """
    marker = f"init-{int(time.time())}"
    body = json.dumps(_empty_document(marker), ensure_ascii=False)
    plain = {"content-type": "application/json", "accept": "application/json, */*",
             "user-agent": _STORE_HEADERS["user-agent"]}
    attempts: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                     follow_redirects=True, transport=transport) as client:
            for name, provision in PROVIDERS:
                try:
                    url, detail = await provision(client)
                except Exception as exc:
                    attempts.append({"xizmat": name, "xato": f"{type(exc).__name__}: {exc}"})
                    continue
                if not url:
                    attempts.append({"xizmat": name, "bosqich": "yaratish",
                                     "kod": detail.get("kod"), "usul": detail.get("usul"),
                                     "javob": detail.get("javob", "")[:300]})
                    continue
                # a private bucket needs its key for the very first check
                auth = ({**plain, "authorization": f"Bearer {detail['kalit']}"}
                        if detail.get("kalit") else plain)
                try:
                    written = await _put(client, url, body, auth)
                    read = await client.get(url, headers=auth)
                except Exception as exc:
                    attempts.append({"xizmat": name, "manzil": url, "bosqich": "yozish",
                                     "xato": f"{type(exc).__name__}: {exc}"})
                    continue
                if written.status_code < 400 and marker in read.text:
                    report = {"ok": True, "xizmat": name, "manzil": url,
                              "yozish": written.status_code, "oqish": read.status_code,
                              "urinishlar": attempts,
                              "izoh": ("manzilni ZAKOVAT_STATS_URL ga, kalitni "
                                       "ZAKOVAT_STATS_TOKEN ga yozing")}
                    if detail.get("kalit"):
                        report["kalit"] = detail["kalit"]
                    return report
                attempts.append({"xizmat": name, "manzil": url, "bosqich": "tekshirish",
                                 "yozish": written.status_code, "oqish": read.status_code,
                                 "oqish_javobi": read.text[:200].strip()})
    except Exception as exc:
        return {"ok": False, "xato": f"{type(exc).__name__}: {exc}", "urinishlar": attempts}
    return {"ok": False, "urinishlar": attempts,
            "izoh": "hech bir bepul xizmat javob bermadi"}


async def probe(transport=None) -> dict:
    """Check the configured store end to end without losing what it holds."""
    if backend() == "lokal":
        return {"ok": False, "rejim": "lokal",
                "reason": ("saqlash ulanmagan — hisob faqat shu nusxada. Vercel'da Storage → "
                           "Blob do‘konini loyihaga ulang (BLOB_READ_WRITE_TOKEN paydo "
                           "bo‘ladi) yoki ZAKOVAT_STATS_URL bering")}
    marker = f"probe-{int(time.time())}"
    host = {"blob": "Vercel Blob", "telegram": f"Telegram · chat {STATS_CHAT}"}.get(
        backend(), STORE_URL.split("//")[-1].split("/")[0])
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                     follow_redirects=True, transport=transport) as client:
            # Keep whatever is already stored: a check must not erase history.
            before_code, before_text = await store_read(client)
            existing = _normalise(_try_json(before_text)) if before_code == 200 else {}
            document = {"v": 1, "s": int(time.time()),
                        "c": {str(k): int(v) for k, v in (existing.get("c") or {}).items()},
                        "u": _cap(Counter(existing.get("u") or {})),
                        "h": _cap(Counter(existing.get("h") or {})),
                        "p": _cap(Counter(existing.get("p") or {})),
                        "e": ([{"t": int(time.time()), "k": "probe", "x": marker}]
                              + list(existing.get("e") or []))[:EVENT_LIMIT],
                        "a": dict(existing.get("a") or {})}
            body_text = json.dumps(document, ensure_ascii=False)
            write_code, _ = await store_write(client, body_text)
            read_code, read_text = await store_read(client)
        found = marker in read_text
        if write_code >= 400:
            note = f"yozish ishlamadi ({write_code})"
        elif found:
            note = "saqlash ishlaydi: yozildi va qayta o‘qildi"
        else:
            note = "yozildi, lekin qayta o‘qishda topilmadi"
        return {"ok": write_code < 400 and found, "manzil": host, "rejim": backend(),
                "yozish": write_code, "oqish": read_code,
                "qayta_o‘qildi": found, "saqlangan_hisoblar": document["c"], "izoh": note}
    except Exception as exc:
        return {"ok": False, "manzil": host, "xato": f"{type(exc).__name__}: {exc}"}


async def snapshot(archive=None, force: bool = True) -> dict:
    """Everything the panel shows: stored totals, this copy's totals, archive."""
    global _remote, _remote_at, _access
    stored = False
    if backend() != "lokal" and not DRY_RUN:
        try:
            stored = await sync(force=force)
        except Exception:
            stored = False
        if not stored and not _remote:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
                    status, text = await store_read(client)
                if status == 200:
                    _remote = _normalise(_try_json(text))
                    _remote_at = time.time()
            except Exception:
                logging.getLogger(__name__).debug("stats read failed", exc_info=True)
    elif force:
        await asyncio.sleep(0)

    stored_doc = _normalise(_remote)
    if _remote:                     # nothing stored yet: keep what this copy knows
        _access = dict(stored_doc.get("a") or {})
        _adopt_used(stored_doc.get("q") or {})
        for key, item in (stored_doc.get("r") or {}).items():
            _plans.setdefault(key, item)
        _groups.update({key: value for key, value in (stored_doc.get("g") or {}).items()
                        if key not in _groups or not _groups[key]})
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
        "storage": STORE_URL or {"blob": "Vercel Blob: " + BLOB_PATH,
                                 "telegram": f"Telegram: chat {STATS_CHAT}"}.get(backend(), ""),
        "stored": backend() != "lokal",
        "backend": backend(),
        "access": dict(_access),
        "used": used_summary(),
        "plans": plans(),
        "groups": groups(),
        "dry_run": DRY_RUN,
        "synced": stored,
        "remote_age": int(time.time() - _remote_at) if _remote_at else None,
        "archive": archive_stats(archive) if archive is not None else {},
    }


def freeze() -> dict:
    """Copy the buffers, so a dry run can be undone without a trace."""
    return {"pending": {field: Counter(_pending[field]) for field in ("c", "u", "h", "p")},
            "events": list(_pending["e"]),
            "used": [list(row) for row in _pending.get("q") or []],
            "index": {key: dict(item) for key, item in _used.items()},
            "plans": {key: list(item) for key, item in _plans.items()},
            "groups": dict(_groups),
            "instance_c": Counter(_instance["c"]),
            "instance_u": set(_instance["u"]),
            "instance_h": set(_instance["h"]),
            "instance_p": set(_instance["p"])}


def restore(state: dict) -> None:
    """Put the buffers back exactly as they were before the dry run.

    The self-test counts a whole synthetic game. Those numbers must never reach
    the real store later, when a warm instance handles live traffic again.
    """
    for field in ("c", "u", "h", "p"):
        _pending[field] = Counter(state["pending"][field])
    _pending["e"] = list(state["events"])
    _pending["q"] = [list(row) for row in state.get("used") or []]
    _used.clear()
    _used.update({key: dict(item) for key, item in (state.get("index") or {}).items()})
    _plans.clear()
    _plans.update({key: list(item) for key, item in (state.get("plans") or {}).items()})
    _groups.clear()
    _groups.update(dict(state.get("groups") or {}))
    _instance["c"] = Counter(state["instance_c"])
    _instance["u"] = set(state["instance_u"])
    _instance["h"] = set(state["instance_h"])
    _instance["p"] = set(state["instance_p"])


def reset() -> None:
    """Test helper: drop everything this instance has counted."""
    global _last_flush, _remote, _remote_at, _blob_url, _tg_message_id, _access
    _pending["c"] = Counter()
    _pending["u"] = Counter()
    _pending["h"] = Counter()
    _pending["p"] = Counter()
    _pending["e"] = []
    _pending["q"] = []
    _used.clear()
    _plans.clear()
    _groups.clear()
    _instance["c"] = Counter()
    _instance["u"] = set()
    _instance["h"] = set()
    _instance["p"] = set()
    _instance["started"] = time.time()
    _last_flush = 0.0
    _remote = {}
    _remote_at = 0.0
    _blob_url = ""
    _tg_message_id = 0
    _access = {}
