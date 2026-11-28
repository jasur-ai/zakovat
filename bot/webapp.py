"""The owner's Mini App: who may open it and what it is shown.

A Mini App cannot be trusted to say who its user is, so every request carries
``WebApp.initData`` — a signed payload from Telegram. The signature is checked
here with the bot token (HMAC-SHA256 over ``WebAppData``), exactly as the Bot API
documents it, and only then is the user id believed.

Visibility has two layers:

* the bot's menu button is set per chat, so only the owner and the users they
  granted see the Mini App at all;
* this module refuses everybody else anyway, because a URL can be shared.

The access list itself lives in the statistics document, so it survives a cold
start like the counters do.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl, urlencode

from bot import stats

APP_PATH = "/app"
INIT_DATA_TTL = int(os.getenv("ZAKOVAT_APP_TTL", "86400") or "86400")   # a day
EVENT_LABELS = {
    "game_start": "O‘yin boshlandi",
    "game_end": "O‘yin yakunlandi",
    "auto_stop": "Avto-to‘xtatildi",
    "stopped": "O‘yin to‘xtatildi",
    "skip": "Savol o‘tkazildi",
    "question": "Savol berildi",
    "correct": "To‘g‘ri javob",
    "wrong": "Noto‘g‘ri javob",
    "refresh": "Vaqt yangilandi",
    "command": "Buyruq",
    "clock_opened": "Jonli soat ochildi",
    "app_opened": "Mini App ochildi",
    "grant": "Ruxsat berildi",
    "revoke": "Ruxsat bekor qilindi",
    "probe": "Saqlash tekshiruvi",
    "tour_scheduled": "Turnir rejalashtirildi",
    "tour_composed": "Turnir guruhga chiqarildi",
    "tour_reminder": "Turnir eslatmasi yuborildi",
    "tour_cancelled": "Turnir rejasi bekor qilindi",
}


# A local preview must be able to show the panel without a real bot. The stand-in
# key is only ever used when the environment asks for a demo *and* the code is not
# running on Vercel, so production cannot end up in this branch by accident.
DEMO_TOKEN = "0:zakovat-local-preview"


def bot_token() -> str:
    return (os.getenv("BOT_TOKEN") or "").strip()


def demo_enabled() -> bool:
    """Local preview only: sign a proof for the owner without a real bot token."""
    return os.getenv("ZAKOVAT_APP_DEMO") == "1" and not os.getenv("VERCEL_ENV")


def signing_token() -> str:
    """The key initData is checked with — the real bot token, or the local one."""
    return bot_token() or (DEMO_TOKEN if demo_enabled() else "")


def demo_init_data() -> str:
    """A correctly signed proof for the owner, for looking at the page locally."""
    if not demo_enabled():
        return ""
    return forge_init_data({"id": int(stats.OWNER_ID), "first_name": "Ega",
                            "username": "zakovat"}, signing_token())


def app_url(base: str = "") -> str:
    """Absolute address of the Mini App, or empty when there is no public one."""
    base = (base or "").strip().rstrip("/")
    return f"{base}{APP_PATH}" if base else ""


# ------------------------------------------------------------------ signature
def _sign(data_check_string: str, token: str) -> str:
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def forge_init_data(user: dict, token: str | None = None, chat: dict | None = None,
                    start_param: str = "", auth_date: int | None = None) -> str:
    """Build a correctly signed initData string.

    Used by the tests and by the ``?appprobe=1`` diagnostic; the token never
    leaves the process.
    """
    fields: dict[str, str] = {
        "query_id": f"probe-{int(time.time())}",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
    }
    if chat:
        fields["chat_instance"] = "1"
        fields["chat"] = json.dumps(chat, ensure_ascii=False, separators=(",", ":"))
    if start_param:
        fields["start_param"] = start_param
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    fields["hash"] = _sign(check, token if token is not None else bot_token())
    return urlencode(fields)


def validate_init_data(init_data: str, token: str | None = None,
                       max_age: int | None = None) -> dict | None:
    """Return the verified payload, or None when the signature or age is wrong.

    The signed data is the newline-joined ``key=value`` list of every field
    except ``hash``, sorted by key, with URL-decoded values.
    """
    raw = (init_data or "").strip()
    if not raw or "=" not in raw:
        return None
    token = token if token is not None else signing_token()
    if not token:
        return None
    try:
        fields = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=False))
    except ValueError:
        return None
    signature = fields.pop("hash", "")
    if not signature or not fields:
        return None
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    if not hmac.compare_digest(_sign(check, token), signature):
        return None
    try:
        auth_date = int(fields.get("auth_date") or 0)
    except ValueError:
        return None
    limit = INIT_DATA_TTL if max_age is None else max_age
    if not auth_date or time.time() - auth_date > limit or auth_date - time.time() > 300:
        return None
    try:
        user = json.loads(fields.get("user") or "{}")
    except ValueError:
        return None
    if not isinstance(user, dict) or not int(user.get("id") or 0):
        return None
    try:
        chat = json.loads(fields.get("chat") or "null")
    except ValueError:
        chat = None
    return {"user": user, "chat": chat if isinstance(chat, dict) else None,
            "auth_date": auth_date, "start_param": fields.get("start_param", ""),
            "query_id": fields.get("query_id", "")}


# ------------------------------------------------------------------ access
def display_name(user: dict) -> str:
    parts = [str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]
    name = " ".join(part for part in parts if part)
    return name or str(user.get("username") or "").strip() or f"id {user.get('id')}"


def can_open(user_id) -> bool:
    """The owner and the ids they granted; nobody else, ever."""
    return stats.allowed(user_id)


def access_report() -> list[dict]:
    """The granted users as a list the UI can draw, newest first."""
    rows = []
    for key, value in stats.access_list().items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        rows.append({"id": number, "name": (value or {}).get("name") or "",
                     "since": int((value or {}).get("since") or 0)})
    rows.sort(key=lambda row: -row["since"])
    return rows


# ------------------------------------------------------------------ payload
async def payload(user: dict, archive=None, count: bool = True, flush=None) -> dict:
    """Everything the Mini App draws, for one verified user.

    The open is counted before the numbers are read, so the panel a visitor sees
    already includes their own visit. ``flush`` (webhook's ``bg_add``) is handed
    the store write when the batch window says one is due — the caller owns the
    background queue, this module does not import it.
    """
    user_id = int(user.get("id") or 0)
    if count and stats.record("app_opened", user_id=user_id, app_opened=1, log=False) and flush:
        flush(stats.sync)
    data = await stats.snapshot(archive)
    counters = data.get("counters") or {}
    top = []
    for package_id, amount in (data.get("top_packages") or []):
        title = ""
        try:
            record = archive.package(int(package_id)) if archive is not None else None
            title = str((record or {}).get("title") or "")[:90]
        except Exception:
            logging.getLogger(__name__).debug("package title failed", exc_info=True)
        top.append({"id": int(package_id), "plays": int(amount), "title": title})
    events = [{"t": int(item.get("t") or 0), "k": str(item.get("k") or ""),
               "x": str(item.get("x") or ""),
               "label": EVENT_LABELS.get(str(item.get("k") or ""), str(item.get("k") or ""))}
              for item in (data.get("events") or [])]
    sections = [{"name": row["name"], "packages": int(row["packages"]),
                 "questions": int(row["questions"])}
                for row in ((data.get("archive") or {}).get("sections") or [])]
    archive_block = data.get("archive") or {}
    # stats.archive_stats describes the local export; the totals cover both bases
    # (zakovatklubi.uz on disk, turnirlar.uz in the Release).
    sources = []
    total_packages = int(archive_block.get("packages") or 0)
    total_questions = int(archive_block.get("questions") or 0)
    if archive is not None and hasattr(archive, "source_counts"):
        labels = (("zakovatklubi", "zakovatklubi.uz"), ("turnirlar", "turnirlar.uz"))
        counts = archive.source_counts()
        for key, name in labels:
            entry = counts.get(key)
            if not entry:
                continue
            sources.append({"name": name, "packages": int(entry.get("packages") or 0),
                            "questions": int(entry.get("questions") or 0),
                            "shards": int(entry.get("shards") or 0),
                            "stored": str(entry.get("stored") or ""),
                            "exported_at": str(entry.get("exported_at") or "")})
        try:
            total_packages, total_questions = archive.counts()
        except Exception:                                        # noqa: BLE001
            logging.getLogger(__name__).debug("archive totals failed", exc_info=True)
    return {
        "ok": True,
        "owner": user_id == int(stats.OWNER_ID),
        "owner_id": int(stats.OWNER_ID),
        "user": {"id": user_id, "name": display_name(user),
                 "username": str(user.get("username") or ""),
                 "photo": str(user.get("photo_url") or "")},
        "counters": counters,
        "accuracy": data.get("accuracy", 0),
        "asked": data.get("asked", 0),
        "users": data.get("users", 0),
        "chats": data.get("chats", 0),
        "packages_played": data.get("packages_played", 0),
        "top_packages": top,
        "events": events,
        "sections": sections,
        "archive": {
            "packages": archive_block.get("packages", 0),
            "questions": archive_block.get("questions", 0),
            "section_count": archive_block.get("section_count", 0),
            "pictures": archive_block.get("pictures", 0),
            "cyrillic": archive_block.get("cyrillic", 0),
            "average": archive_block.get("average", 0),
            "biggest": archive_block.get("biggest", {"title": "", "questions": 0}),
            "exported_at": archive_block.get("exported_at", ""),
            "sources": sources,
            "total_packages": total_packages,
            "total_questions": total_questions,
        },
        "instance": data.get("instance", {}),
        "access": access_report(),
        "used": data.get("used") or {},
        "plans": data.get("plans") or [],
        "groups": [{"id": int(key), "title": str(value or "")}
                   for key, value in sorted((data.get("groups") or {}).items(),
                                            key=lambda kv: -int(kv[0]))],
        "storage": {"backend": data.get("backend", "lokal"), "label": stats.store_label(),
                    "synced": bool(data.get("synced")), "dry_run": bool(data.get("dry_run")),
                    "remote_age": data.get("remote_age")},
        "server": {"time": time.time()},
    }


# ------------------------------------------------------------------ browsing
BROWSE_PAGE = 12          # paketlar bir sahifada
QUESTION_PAGE = 20        # savollar bir sahifada
SNIPPET = 220             # savol matnidan qancha ko‘rsatiladi


def _used_marks(rows: list, used: set) -> int:
    """Mark the rows the index already knows, and say how many there were."""
    marked = 0
    for row in rows:
        row["used"] = int(row.get("id") or 0) in used
        marked += 1 if row["used"] else 0
    return marked


def browse(body: dict, archive) -> dict:
    """One step of the panel's browser: sections, packages, then questions.

    The panel is the admin's tool, so it shows answers too — that is the point of
    picking questions by hand. Every row also carries whether the used-question
    index already knows it, so a spent question is visible before it is chosen.
    """
    kind = str((body or {}).get("turi") or "bolimlar").strip().lower()
    used = stats.used_ids()
    if kind in ("paketlar", "packages"):
        return _browse_packages(body, archive, used)
    if kind in ("savollar", "questions"):
        return _browse_questions(body, archive, used)
    sections = [{"name": row["name"], "packages": int(row["packages"]),
                 "questions": int(row["questions"])}
                for row in stats.archive_sections(archive)]
    return {"ok": True, "turi": "bolimlar", "bolimlar": sections,
            "ishlatilgan": len(used), "sahifa": 1, "sahifalar": 1}


def _browse_packages(body: dict, archive, used: set) -> dict:
    section = str((body or {}).get("bolim") or "").strip()
    query = str((body or {}).get("qidiruv") or "").strip().casefold()
    only_used = bool((body or {}).get("ishlatilgan"))
    packages = getattr(archive, "packages", {}) or {}
    ids = sorted((pid for pid, info in packages.items()
                  if int((info or {}).get("questions") or 0) > 0), reverse=True)
    if section:
        ids = [pid for pid in ids
               if stats.section_of(str((packages[pid] or {}).get("title") or "")) == section]
    if query:
        ids = [pid for pid in ids
               if query in str((packages[pid] or {}).get("title") or "").casefold()]
    if only_used:
        known = {int(row.get("p") or 0) for row in stats.used_index()["items"]}
        ids = [pid for pid in ids if int(pid) in known]
    per_page = max(1, BROWSE_PAGE)
    pages = max(1, -(-len(ids) // per_page))
    page = min(max(1, int((body or {}).get("sahifa") or 1)), pages)
    start = (page - 1) * per_page
    rows = [{"id": int(pid),
             "title": str((packages[pid] or {}).get("title") or "(sarlavhasiz)"),
             "questions": int((packages[pid] or {}).get("questions") or 0),
             "bolim": stats.section_of(str((packages[pid] or {}).get("title") or "")),
             "manba": "turnirlar" if archive.is_remote(int(pid)) else "zakovatklubi"}
            for pid in ids[start:start + per_page]]
    spent = {}
    for item in stats.used_index()["items"]:
        package = int(item.get("p") or 0)
        if package:
            spent[package] = int(spent.get(package) or 0) + 1
    for row in rows:
        row["ishlatilgan"] = int(spent.get(row["id"]) or 0)
    return {"ok": True, "turi": "paketlar", "paketlar": rows, "bolim": section,
            "qidiruv": str((body or {}).get("qidiruv") or ""),
            "jami": len(ids), "sahifa": page, "sahifalar": pages,
            "ishlatilgan": len(used)}


def _browse_questions(body: dict, archive, used: set) -> dict:
    try:
        package_id = int((body or {}).get("paket") or 0)
    except (TypeError, ValueError):
        package_id = 0
    info = archive.package_info(package_id) if package_id else None
    if not info:
        return {"ok": False, "reason": "Paket topilmadi.", "turi": "savollar"}
    query = str((body or {}).get("qidiruv") or "").strip().casefold()
    questions = archive.questions(package_id) or []
    if not questions and int(info.get("questions") or 0) > 0:
        # a remote package whose shard could not be fetched: say so, do not lie
        return {"ok": False, "turi": "savollar",
                "reason": "Paketning bo‘lagi (shard) yuklanmadi — birozdan so‘ng "
                          "qayta urining. Bu paket turnirlar.uz bazasida saqlanadi."}
    if query:
        questions = [item for item in questions
                     if query in str(item.get("text") or "").casefold()
                     or query in str(item.get("answer") or "").casefold()]
    per_page = max(1, QUESTION_PAGE)
    pages = max(1, -(-len(questions) // per_page))
    page = min(max(1, int((body or {}).get("sahifa") or 1)), pages)
    start = (page - 1) * per_page
    rows = [{"id": int(item.get("id") or 0),
             "tartib": int(item.get("index") or number),
             "tur": int(item.get("round") or 0),
             "matn": " ".join(str(item.get("text") or "").split())[:SNIPPET],
             "javob": " ".join(str(item.get("answer") or "").split())[:160],
             "rasm": any(str(a).startswith("http") for a in (item.get("attachments") or []))}
            for number, item in enumerate(questions[start:start + per_page], start=start + 1)]
    marked = _used_marks(rows, used)
    return {"ok": True, "turi": "savollar", "savollar": rows,
            "paket": {"id": package_id, "title": str(info.get("title") or ""),
                      "questions": int(info.get("questions") or 0),
                      "bolim": stats.section_of(str(info.get("title") or "")),
                      "manba": "turnirlar" if archive.is_remote(package_id) else "zakovatklubi"},
            "qidiruv": str((body or {}).get("qidiruv") or ""),
            "jami": len(questions), "sahifa": page, "sahifalar": pages,
            "ishlatilgan": marked, "indeksda": len(used)}


def denied(reason: str = "") -> dict:
    return {"ok": False, "reason": reason or "Bu Mini App faqat bot egasi va u ruxsat "
                                             "bergan foydalanuvchilar uchun."}
