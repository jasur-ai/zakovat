"""Rejadagi turnirlar: vaqt tahlili, sozlama matni va eslatma rejasining kodi.

The webhook is serverless and its clock runs in UTC, while every curator thinks
in Tashkent time — so every clock in this feature goes through one zone here,
and one place (``ZAKOVAT_TZ``) can move it.

A plan is deliberately small: it only says *which chat*, *which card message*
and *when*. Everything else (package, rounds, teams, the chosen questions) lives
in the tournament card itself, which the bot pins in the group, so the plan fits
inside the statistics document next to the counters.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

try:                                      # the offset an owner may move
    TZ_OFFSET = float(os.getenv("ZAKOVAT_TZ", "5") or 5)
except ValueError:
    TZ_OFFSET = 5.0
ZONE = timezone(timedelta(hours=TZ_OFFSET), "Toshkent")

PLAN_PREFIX = "R|"          # a reminder hop, not a game or tournament strip
ARM_WINDOW = 15 * 60        # the hop chain only lives through the last quarter hour
HOP = 50                    # one hop waits at most this long (serverless limit)
MAX_LEAD = 30 * 24 * 3600   # a plan further away than a month is refused
MIN_LEAD = 30               # "start in 30 seconds" is just "start now"
LATE_GRACE = 2 * 3600       # a start that passed this long ago is no longer news

TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
STAMP_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[tT ](\d{1,2}):(\d{2})")
DATE_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?$")
DAY_WORDS = {
    "bugun": 0, "shu": 0, "hozir": 0,
    "ertaga": 1, "ertangi": 1,
    "indin": 2, "indini": 2, "indiniga": 2,
}
MONTHS = {
    "yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6,
    "iyul": 7, "avgust": 8, "sentabr": 9, "oktabr": 10, "noyabr": 11, "dekabr": 12,
}

TIME_WORDS = {"vaqt", "qachon", "soat", "boshlanish"}
TITLE_LIMIT = 40

SPEC_KEYS = {
    "vaqt": "when", "qachon": "when", "soat": "when", "time": "when", "boshlanish": "when",
    "jamoa": "teams", "komanda": "teams", "teams": "teams", "team": "teams",
    "tur": "rounds", "raund": "rounds", "rounds": "rounds",
    "savol": "per_round", "savollar": "per_round", "soni": "per_round",
    "paket": "package", "package": "package",
    "sarlavha": "title", "nom": "title", "title": "title",
    "vaqtson": "timer", "sekund": "timer", "timer": "timer",
    "liga": "section", "bolim": "section", "bo‘lim": "section", "section": "section",
}


def now() -> int:
    return int(time.time())


def moment(stamp: int) -> datetime:
    """A stored stamp in the zone curators read."""
    return datetime.fromtimestamp(int(stamp), tz=ZONE)


def when_text(stamp: int) -> str:
    """A planned start in the copy the bot uses everywhere: "bugun 19:00"."""
    if not stamp:
        return ""
    moment_ = moment(stamp)
    today = datetime.now(tz=ZONE).date()
    if moment_.date() == today:
        day = "bugun"
    elif moment_.date() == today + timedelta(days=1):
        day = "ertaga"
    elif moment_.year == today.year:
        day = f"{moment_.day:02d}.{moment_.month:02d}"
    else:
        day = f"{moment_.day:02d}.{moment_.month:02d}.{moment_.year}"
    return f"{day} {moment_.hour:02d}:{moment_.minute:02d}"


def clock_text(stamp: int) -> str:
    """The full stamp, for the panel: "28.11.2026 19:00"."""
    if not stamp:
        return ""
    return moment(stamp).strftime("%d.%m.%Y %H:%M")


def parse_when(value, reference: int = 0) -> tuple[int, str]:
    """Read a start time a curator wrote. Returns ``(stamp, "")`` or ``(0, why)``.

    Accepted: ``19:00``, ``19``, ``bugun 19:00``, ``ertaga 10:30``, ``28.11``,
    ``28.11 19:00``, ``28.11.2026 19:00``, ``+45`` (minutes from now) and a full
    ``1764200000`` stamp. A bare time means today, or tomorrow when it has
    already passed — nobody schedules a tournament in the past by accident.
    """
    text = " ".join(str(value or "").split()).strip().lower().replace(",", ".")
    if not text:
        return 0, "Vaqtni yozing, masalan: 19:00 yoki ertaga 10:30."
    base = int(reference or now())
    if text.isdigit() and len(text) >= 9:                 # a whole stamp
        stamp = int(text)
        return _check(stamp, base)

    matched = STAMP_RE.match(text)
    if matched:                                           # the panel's own picker
        year, month, day, hour, minute = (int(part) for part in matched.groups())
        try:
            target = datetime(year, month, day, hour, minute, tzinfo=ZONE)
        except ValueError:
            return 0, "Bunday sana va soat yo‘q (kun/oy/soatni tekshiring)."
        return _check(int(target.timestamp()), base)

    if text.startswith("+"):                              # "+45" = 45 minutes
        digits = text[1:].replace("min", "").replace("daqiqa", "").strip()
        if not digits.isdigit():
            return 0, "«+45» ko‘rinishida yozing (daqiqa hisobida)."
        return _check(base + int(digits) * 60, base)

    days = 0
    parts = text.split()
    if parts and parts[0] in DAY_WORDS:
        days = DAY_WORDS[parts.pop(0)]
        if parts and parts[0] in DAY_WORDS:               # "ertaga ertalab" is not a time
            parts.pop(0)
    kinds = [_classify(part, parts) for part in parts]
    day = month = year = 0
    hour = minute = -1
    for part, kind in zip(parts, kinds):
        if kind == "month":
            month = MONTHS[part]
        elif kind == "time":
            if hour >= 0:
                return 0, "Ikkita soat yozilmaydi — bittasini qoldiring."
            matched = TIME_RE.match(part)
            hour, minute = int(matched.group(1)), int(matched.group(2))
        elif kind == "date":
            if day:
                return 0, "Ikkita sana yozilmaydi — bittasini qoldiring."
            matched = DATE_RE.match(part)
            day, month = int(matched.group(1)), int(matched.group(2) or month)
            year = int(matched.group(3)) if matched.group(3) else 0
        elif kind == "hour":
            if hour >= 0:
                return 0, "Ikkita soat yozilmaydi — bittasini qoldiring."
            hour = int(part)
        else:
            return 0, f"«{part}» ni vaqt sifatida tushunmadim. Masalan: 19:00, ertaga 10:30."

    if hour < 0:                                          # a date without a clock
        hour = 19
    if minute < 0:                                        # "19" means 19:00
        minute = 0
    if hour > 23 or minute > 59:
        return 0, "Soat 0–23, daqiqa 0–59 bo‘lishi kerak."

    today = moment(base)
    if month and not day:
        day = 1
    if day:
        year = year or today.year
        try:
            target = datetime(year, month or today.month, day, hour, minute, tzinfo=ZONE)
        except ValueError:
            return 0, "Bunday sana yo‘q (kun/oyni tekshiring)."
        if target.year < today.year or (target.year == today.year
                                        and target.date() < today.date()):
            target = target.replace(year=target.year + 1)
        return _check(int(target.timestamp()), base)

    target = today.replace(hour=hour, minute=minute, second=0, microsecond=0) \
        + timedelta(days=days)
    if not days and int(target.timestamp()) < base + MIN_LEAD:
        target += timedelta(days=1)                       # "19:00" after 19:00 = tomorrow
    return _check(int(target.timestamp()), base)


def _classify(part: str, others: list[str]) -> str:
    """What one word of a time string is.

    ``28.11`` is a date and ``19.00`` is a clock, even though both match the same
    two-number shape: an impossible hour or month decides it, and when both
    readings are possible a colon always wins, while a bare ``5.11`` next to a
    real clock (``5.11 19:00``) is the date.
    """
    if part in MONTHS:
        return "month"
    clock = TIME_RE.match(part)
    date = DATE_RE.match(part)
    if clock and not date:
        return "time"
    if date and not clock:
        return "date"
    if clock and date:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            return "date"
        if not 1 <= int(date.group(2) or 0) <= 12:
            return "time"
        if ":" in part:
            return "time"
        if any(":" in other and TIME_RE.match(other) for other in others if other != part):
            return "date"
        return "time"
    if part.isdigit() and len(part) <= 2:
        return "hour"
    return "other"


def _check(stamp: int, base: int) -> tuple[int, str]:
    if stamp <= base + MIN_LEAD:
        return 0, "Vaqt o‘tib ketgan yoki juda yaqin — kamida bir daqiqa keyinroq qiling."
    if stamp - base > MAX_LEAD:
        return 0, "Reja bir oydan uzoqqa qo‘yilmaydi."
    return int(stamp), ""


def parse_spec(argument: str) -> dict:
    """Read `/turnir 959 vaqt=19:00 jamoa=6 tur=3 savol=10 sarlavha=Kuzgi`.

    Anything without ``=`` is kept as the package search text, exactly like
    before, so the old command keeps working.
    """
    spec: dict = {"package": "", "when": "", "error": ""}
    rest: list[str] = []
    tokens = str(argument or "").split()
    position = 0
    while position < len(tokens):
        token = tokens[position]
        position += 1
        if "=" not in token:
            rest.append(token)
            continue
        key, _, value = token.partition("=")
        field = SPEC_KEYS.get(key.strip().lower())
        if not field:
            rest.append(token)
            continue
        if field == "when":
            # "vaqt=ertaga 19:00" — keep the next words while they still read as
            # one time, and stop at the first word that does not.
            phrase = value.strip()
            while position < len(tokens) and "=" not in tokens[position]:
                trial = f"{phrase} {tokens[position]}".strip()
                if parse_when(trial)[1]:
                    break
                phrase = trial
                position += 1
            spec["when"] = f"{spec['when']} {phrase}".strip()
        elif field == "title":
            # "sarlavha=Kuzgi liga" — a name is several words, but a bare number
            # after it is the package, not part of the name.
            words = [value.strip()] if value.strip() else []
            while position < len(tokens) and "=" not in tokens[position] \
                    and not tokens[position].isdigit():
                words.append(tokens[position])
                position += 1
            spec["title"] = " ".join(words)[:TITLE_LIMIT]
        else:
            spec[field] = value.strip()
    # "vaqt 19:00" without an equals sign reads just as well
    words = list(rest)
    position = 0
    while position < len(words):
        if words[position].strip().lower() not in TIME_WORDS or position + 1 >= len(words):
            position += 1
            continue
        phrase = words[position + 1]
        taken = 2
        while position + taken < len(words):
            trial = f"{phrase} {words[position + taken]}".strip()
            if parse_when(trial)[1]:
                break
            phrase = trial
            taken += 1
        spec["when"] = f"{spec['when']} {phrase}".strip()
        del words[position:position + taken]
    for field in ("teams", "rounds", "per_round", "timer"):
        raw = str(spec.get(field) or "")
        if raw and not raw.isdigit():
            spec["error"] = f"«{raw}» — son bo‘lishi kerak ({field})."
            return spec
        if raw:
            spec[field] = int(raw)
    spec["text"] = " ".join(words).strip()
    if spec["when"]:
        stamp, problem = parse_when(spec["when"])
        if problem:
            spec["error"] = problem
        else:
            spec["start_at"] = stamp
    return spec


def plan_body(chat_id: int, message_id: int, start_at: int, title: str = "") -> str:
    """The whole reminder hop: which card in which chat, and when to announce it."""
    return PLAN_PREFIX + json.dumps(
        {"c": int(chat_id), "m": int(message_id or 0), "s": int(start_at),
         "t": str(title or "")[:40]},
        ensure_ascii=False, separators=(",", ":"))


def plan_data(body: str) -> dict:
    """Read a reminder hop back; `{}` when the body is not one."""
    text = str(body or "").lstrip()
    if not text.startswith(PLAN_PREFIX):
        return {}
    try:
        data = json.loads(text[len(PLAN_PREFIX):])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        return {"chat": int(data.get("c") or 0), "message": int(data.get("m") or 0),
                "start": int(data.get("s") or 0), "title": str(data.get("t") or "")[:40]}
    except (TypeError, ValueError):
        return {}


def hop_step(start_at: int, reference: int = 0) -> int:
    """How long the next hop may sleep: short near the deadline, capped always."""
    left = int(start_at) - int(reference or now())
    return max(1, min(HOP, left))


def due(start_at: int, reference: int = 0, window: int = ARM_WINDOW,
        late: int = LATE_GRACE) -> bool:
    """True when the hop chain should be running for this plan.

    A plan whose minute passed while every instance was cold is *still* due, for
    ``late`` seconds: a reminder ten minutes late beats one that never comes, and
    the card in the group is waiting for somebody to press «Boshlash». Past that
    it is no longer news — ``too_late`` says so and the plan is dropped.
    """
    if not int(start_at):
        return False
    left = int(start_at) - int(reference or now())
    return -int(late) <= left <= int(window)


def too_late(start_at: int, reference: int = 0, late: int = LATE_GRACE) -> bool:
    """True when announcing this plan would only confuse the group."""
    return bool(int(start_at)) and int(reference or now()) - int(start_at) > int(late)


def left_text(start_at: int, reference: int = 0) -> str:
    """"2 soat 15 daqiqa" — how long the group still has to get ready."""
    left = max(0, int(start_at) - int(reference or now()))
    hours, rest = divmod(left, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours} soat {minutes} daqiqa"
    if hours:
        return f"{hours} soat"
    if minutes:
        return f"{minutes} daqiqa"
    return f"{left} sekund"
