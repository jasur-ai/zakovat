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
_blob_url = ""          # the public address of our blob, learned on first write


def backend() -> str:
    """Which storage this copy writes to: ``blob``, ``http`` or ``lokal``."""
    if STORE_URL:
        return "http"
    if BLOB_TOKEN:
        return "blob"
    return "lokal"


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


async def sync(force: bool = False, transport=None) -> bool:
    """Read the document, fold our pending deltas in and write it back."""
    global _remote, _remote_at, _last_flush
    if backend() == "lokal" or DRY_RUN:
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0),
                                     transport=transport) as client:
            if backend() == "blob":
                status, text = await blob_read(client)
            else:
                response = await client.get(STORE_URL, headers=_headers())
                status, text = response.status_code, response.text
            if status == 200 and text.strip():
                document = _normalise(_try_json(text))
            body = _merge(document, deltas)
            payload = {"v": 1, "s": int(time.time()), "c": dict(body["c"]),
                       "u": _cap(body["u"]), "h": _cap(body["h"]), "p": _cap(body["p"]),
                       "e": body["e"][:EVENT_LIMIT]}
            body_text = json.dumps(payload, ensure_ascii=False)
            if backend() == "blob":
                status, _ = await blob_write(client, body_text)
            else:
                written = await client.put(STORE_URL, content=body_text, headers=_headers())
                if written.status_code in (405, 501):
                    # Some stores (a Google Apps Script, a plain function) take POST only.
                    written = await client.post(STORE_URL, content=body_text,
                                                headers=_headers())
                status = written.status_code
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
    _remote = payload
    _remote_at = time.time()
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


def _try_json(text: str):
    try:
        return json.loads(text)
    except ValueError:
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
    host = "Vercel Blob" if backend() == "blob" else STORE_URL.split("//")[-1].split("/")[0]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0),
                                     follow_redirects=True, transport=transport) as client:
            # Keep whatever is already stored: a check must not erase history.
            if backend() == "blob":
                before_code, before_text = await blob_read(client)
            else:
                before = await client.get(STORE_URL, headers=_headers())
                before_code, before_text = before.status_code, before.text
            existing = _normalise(_try_json(before_text)) if before_code == 200 else {}
            document = {"v": 1, "s": int(time.time()),
                        "c": {str(k): int(v) for k, v in (existing.get("c") or {}).items()},
                        "u": _cap(Counter(existing.get("u") or {})),
                        "h": _cap(Counter(existing.get("h") or {})),
                        "p": _cap(Counter(existing.get("p") or {})),
                        "e": ([{"t": int(time.time()), "k": "probe", "x": marker}]
                              + list(existing.get("e") or []))[:EVENT_LIMIT]}
            body_text = json.dumps(document, ensure_ascii=False)
            if backend() == "blob":
                write_code, _ = await blob_write(client, body_text)
                read_code, read_text = await blob_read(client)
            else:
                written = await _put(client, STORE_URL, body_text)
                read = await client.get(STORE_URL, headers=_headers())
                write_code, read_code, read_text = written.status_code, read.status_code, read.text
        found = marker in read_text
        return {"ok": write_code < 400 and found, "manzil": host, "rejim": backend(),
                "yozish": write_code, "oqish": read_code,
                "qayta_o‘qildi": found, "saqlangan_hisoblar": document["c"],
                "izoh": ("saqlash ishlaydi: yozildi va qayta o‘qildi"
                         if found else "yozildi, lekin qayta o‘qishda topilmadi")}
    except Exception as exc:
        return {"ok": False, "manzil": host, "xato": f"{type(exc).__name__}: {exc}"}


async def snapshot(archive=None, force: bool = True) -> dict:
    """Everything the panel shows: stored totals, this copy's totals, archive."""
    global _remote, _remote_at
    stored = False
    if backend() != "lokal" and not DRY_RUN:
        try:
            stored = await sync(force=force)
        except Exception:
            stored = False
        if not stored and not _remote:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as client:
                    if backend() == "blob":
                        status, text = await blob_read(client)
                    else:
                        response = await client.get(STORE_URL, headers=_headers())
                        status, text = response.status_code, response.text
                if status == 200:
                    _remote = _normalise(_try_json(text))
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
        "storage": STORE_URL or ("Vercel Blob: " + BLOB_PATH if BLOB_TOKEN else ""),
        "stored": backend() != "lokal",
        "backend": backend(),
        "dry_run": DRY_RUN,
        "synced": stored,
        "remote_age": int(time.time() - _remote_at) if _remote_at else None,
        "archive": archive_stats(archive) if archive is not None else {},
    }


def reset() -> None:
    """Test helper: drop everything this instance has counted."""
    global _last_flush, _remote, _remote_at, _blob_url
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
    _blob_url = ""
