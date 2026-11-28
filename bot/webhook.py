"""Stateless Telegram webhook entry for Vercel serverless.

Serverless has no long-lived memory, so game state is carried inside the
question message as a compact signed-by-obscurity marker and returned by the
user via Telegram's ForceReply. Timeouts are resolved lazily on the next
interaction. No token or secret is stored in the repository.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import html
import json
import logging
import os
import random
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from telegram import (
    BotCommand, BotCommandScopeChat, ForceReply, InputFile, MenuButtonDefault,
    MenuButtonWebApp, Update, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import dashboard
from . import hidden
from . import messages as msg
from . import schedule as sched
from . import settings as st
from . import stats
from . import tournament as tn
from . import webapp as wa
from .answers import AnswerChecker, normalize
from .archive import SHARDS_IN_MEMORY, Archive
from .quiz import CORRECT, FOREIGN, IGNORED, WRONG, Session, build_playlist

MARKER = "\n\n#ZS2 "
LEGACY_MARKER = "\n\n#ZS "
TIMER = int(os.getenv("ZAKOVAT_TIMER_SECONDS", "60"))
TICK_SECONDS = max(2, int(os.getenv("ZAKOVAT_TICK_SECONDS", "5")))
IDLE_LIMIT = max(0, int(os.getenv("ZAKOVAT_IDLE_LIMIT", "3")))
TICKS_ENABLED = os.getenv("ZAKOVAT_LIVE_TIMER", "1") not in ("0", "false", "no")
TICK_MODE = os.getenv("ZAKOVAT_TICK_MODE", "bg")     # "bg" (after response) | "sleep"
MAX_TICK_HORIZON = 900          # a countdown never runs longer than this
# A scheduled tournament is announced by a chain of short hops, and a chain costs
# function time, so it only lives through the last minutes before the start. The
# card in the group carries the whole plan, so nothing is lost when no hop runs.
REMINDER_ARM = max(60, int(os.getenv("ZAKOVAT_REMINDER_ARM", str(sched.ARM_WINDOW))))

# Javobni tekshirish oynasi: bot xulosani taklif qiladi, kurator tasdiqlaydi.
REVIEW_HOP = max(2, int(os.getenv("ZAKOVAT_REVIEW_HOP", "10")))   # bir qadam (sekund)
REVIEW_AUTO = max(30, int(os.getenv("ZAKOVAT_REVIEW_AUTO", str(tn.REVIEW_AUTO))))
GAME_CONFIRM_ACTION = st.GAME_CONFIRM.split("|", 1)[1]     # «TASDIQLASH»
GAME_REJECT_ACTION = st.GAME_REJECT.split("|", 1)[1]       # «RAD ETISH»
PROBE_WAIT = 4                  # seconds used by the ?bgprobe= diagnostic

# Work queued while handling a request, run after the response has been sent.
_bg_tasks: "contextvars.ContextVar[list]" = contextvars.ContextVar("bg_tasks", default=None)  # type: ignore[assignment]
_bg_probe_log: list[str] = []

# The game state rides along inside the message, but nobody should see it:
# zero-width characters are kept verbatim by Telegram (real scripts depend on
# them), so unlike a spoiler there is nothing on screen to tap open. Two
# densities live in `bot/hidden.py`: the short pipe state of an ordinary game
# and the compressed state of a tournament (rosters, selections, finals).
ZW0 = hidden.ZW0
ZW1 = hidden.ZW1
INVISIBLE = hidden.INVISIBLE
SENTINEL = hidden.SENTINEL_V2
HIDDEN_MODE = os.getenv("ZAKOVAT_HIDDEN_STATE", "invisible")   # or "spoiler"
OWNER_ID = stats.OWNER_ID       # the only chat that sees /stats
PER_PAGE = int(os.getenv("ZAKOVAT_PACKAGES_PER_PAGE", "12"))
ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = Archive(
    ROOT / os.getenv("ZAKOVAT_ARCHIVE_DIR", "data/all"),
    ROOT / "data" / "zakovat-959.flat.json",
)
IMAGES = ROOT / "data" / "images"
SECRET = os.getenv("WEBHOOK_SECRET", "")


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def pack_state(session: Session, deadline: int, latest: str | None = None, aliases: list[str] | None = None) -> str:
    """Compact game state: one short line of pipe separated numbers.

    Serverless has no memory, so the state travels inside the message. It is
    rendered invisibly (see `hide_state`), never as readable text.
    """
    fields = [
        "s" if session.mode == "solo" else "g",
        session.package_id,
        session.index,
        session.correct,
        session.wrong,
        session.owner_id,
        session.question["id"] if session.question else 0,
        deadline,
        session.rounds,
        session.per_round,
        session.timer_seconds,
        session.seed,
        session.control_message_id or 0,
        session.idle_streak,
        session.attempts,
        1 if session.can_delete else 0,
    ]
    core = "|".join(str(value) for value in fields)
    review = session_review_payload(session)
    if latest is None:
        # The last guess rides along: when the clock runs out, the person who has
        # to decide sees what was actually tried, not just "nobody answered".
        latest = session.latest_answer
    extra = ""
    if latest or aliases or review or not session.needs_confirm:
        payload: dict = {"l": latest, "a": aliases or []}
        if latest and session.latest_user:
            payload["lu"] = int(session.latest_user)
        if not session.needs_confirm:
            payload["cf"] = 0
        if review:
            payload["rv"] = review
            # The confirmation buttons are read in a private chat, so the state has
            # to say which group the game — and the result — belongs to.
            payload["h"] = int(session.chat_id or 0)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        extra = "|" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return core + extra


def session_review_payload(session: Session) -> list:
    """One parked answer, compact enough to travel inside a message.

    Empty when nothing is waiting: an ordinary game carries no review bytes at all.
    """
    if not session.reviewing():
        return []
    return [int(session.review_at), int(session.review_due), str(session.review_mark),
            str(session.review_text), int(session.review_user), str(session.review_name),
            int(session.review_noted),
            [[int(row[0]), int(row[1])] for row in (session.review_to or [])
             if isinstance(row, (list, tuple)) and len(row) == 2][:tn.REVIEW_TARGETS],
            int(session.review_msg or 0), str(session.review_kind or "answer")]


def encode_invisible(body: str) -> str:
    """Write the state as a run of zero-width characters (one bit each)."""
    return hidden.encode_legacy(body)


def _decode_run(run: list[str]) -> str | None:
    """Turn one zero-width run back into text, checking sentinel and checksum."""
    return hidden.decode_run(run)


def decode_invisible(text: str) -> str | None:
    """Read the zero-width state back out of a message, whichever density it is.

    A run only counts when its sentinel and checksum match, so text that happens
    to contain zero-width characters of its own is ignored. The longest valid
    run wins.
    """
    return hidden.decode(text)


def hide_state(body: str) -> str:
    """Invisible by default; ZAKOVAT_HIDDEN_STATE=spoiler keeps the old look.

    Short bodies keep the old one-bit density, so a game that is already running
    when a new version is deployed still decodes. A tournament state (teams,
    selections, finals) is compressed instead: it would not fit otherwise.
    """
    if HIDDEN_MODE == "spoiler":
        return f'<span class="tg-spoiler">{html.escape(MARKER + body)}</span>'
    return hidden.encode(body)


def utf16_length(text: str) -> int:
    """Telegram counts UTF-16 code units, not Python characters."""
    return len(text.encode("utf-16-le")) // 2


def with_state(visible: str, body: str) -> str:
    """Visible text plus the invisible state, inside Telegram's 4096 limit.

    The run is placed after the first line rather than at the very end, so a
    client that trims the tail of a message cannot take the state with it.
    """
    hidden = hide_state(body)
    budget = 4096 - utf16_length(hidden) - 2
    if budget <= 0:
        return visible[:4000]
    while utf16_length(visible) > budget:
        visible = visible[:max(0, len(visible) - 24)]
    head, _, tail = visible.partition("\n")
    return f"{head}{hidden}\n{tail}" if tail else f"{head}{hidden}"


def parse_body(body: str) -> dict | None:
    """Turn the compact state line into the payload dict handlers use."""
    parts = (body or "").strip().split("|")
    if len(parts) < 16:
        return None
    try:
        payload = {
            "m": "solo" if parts[0] == "s" else "group",
            "p": int(parts[1]), "i": int(parts[2]), "c": int(parts[3]), "w": int(parts[4]),
            "o": int(parts[5]), "q": int(parts[6]), "d": int(parts[7]), "r": int(parts[8]),
            "n": int(parts[9]), "t": int(parts[10]), "s": int(parts[11]),
            "k": int(parts[12]) or None, "z": int(parts[13]), "y": int(parts[14]),
            "x": bool(int(parts[15])), "l": None, "a": [], "lu": 0, "cf": 1, "rv": [],
            "h": 0,
        }
    except ValueError:
        return None
    if len(parts) > 16 and parts[16]:
        try:
            raw = base64.urlsafe_b64decode(parts[16] + "=" * (-len(parts[16]) % 4))
            extra = json.loads(raw.decode("utf-8"))
            payload["l"] = extra.get("l")
            payload["a"] = extra.get("a") or []
            payload["lu"] = int(extra.get("lu") or 0)
            payload["cf"] = 1 if extra.get("cf", 1) else 0
            payload["rv"] = extra.get("rv") or []
            payload["h"] = int(extra.get("h") or 0)
        except (ValueError, UnicodeDecodeError):
            pass
    return payload


def unpack_tournament(text: str | None) -> tn.Tournament | None:
    """Read a tournament state out of a message (card or countdown strip).

    The question texts are not in the state — they come from the archive, so
    ``tn.rebuild`` is called by whoever has one.
    """
    if not text:
        return None
    body = decode_invisible(text)
    if body is None:
        return None
    return tn.Tournament.from_body(body)


def unpack_state(text: str | None) -> dict | None:
    """Read the state: invisible run first, then the older visible markers."""
    if not text:
        return None
    body = decode_invisible(text)
    if body is not None:
        if str(body).startswith(tn.STATE_PREFIX):
            return None                      # a tournament, not an ordinary game
        return parse_body(body)
    token = MARKER.strip()
    legacy = LEGACY_MARKER.strip()
    if token in text:
        found = text.split(token, 1)[1].strip().split("\n", 1)[0].split("<", 1)[0].strip()
        return parse_body(found)
    if legacy in text:
        raw_token = text.split(legacy, 1)[1].strip().split()[0].split("<", 1)[0]
        try:
            raw = base64.urlsafe_b64decode(raw_token + "=" * (-len(raw_token) % 4))
            payload = json.loads(raw.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None
    return None


IMPORTANT_EVENTS = ("game_start", "game_end", "auto_stop", "stopped")


_reminders_seen = 0
_reminders_fired = 0
_armed: set[str] = set()        # reminder chains this instance already started


def note(kind: str, **fields) -> None:
    """Count one statistics event; the store write happens after the response.

    A game starting or ending is written out at once, everything else waits for
    the batch window so a busy chat does not produce a request per event.
    """
    try:
        due = stats.record(kind, **fields)
        if kind in IMPORTANT_EVENTS:
            bg_add(lambda: stats.sync(force=True))
        elif due:
            bg_add(stats.sync)
    except Exception:  # statistics must never break a game
        logging.getLogger(__name__).debug("stats record failed", exc_info=True)


def index_used(questions, *, package_id: int = 0, chat_id: int = 0, mode: str = "") -> None:
    """Put the questions just asked into the "ishlatildi" index.

    The index lives inside the statistics document, so a question asked in any
    chat is visible to the panel everywhere — and the next curator picking
    questions can see which ones are already spent.
    """
    try:
        if stats.mark_used(questions, package_id=package_id, chat_id=chat_id, mode=mode):
            bg_add(stats.sync)
    except Exception:  # the index must never break a game
        logging.getLogger(__name__).debug("used index failed", exc_info=True)


def close_question(session: Session, kind: str = "unanswered") -> bool:
    """Resolve the running question once (timeout or admin skip) and count it."""
    if not session.resolve_unanswered():
        return False
    note(kind, chat_id=session.chat_id, package_id=session.package_id,
         unanswered=1, skips=1 if kind == "skip" else 0,
         detail=f"{session.mode} · {session.package_id} · savol {session.number}")
    return True


def clock_url(session: Session, deadline: int, remaining: int) -> str:
    """The Mini App clock is gone; kept as a stub so old callers get "".

    It opened a second window inside Telegram, and while that window was open
    the question itself was not on screen. The countdown now lives in the strip
    text — digits plus a twenty-segment bar, repainted while the question runs.
    """
    return ""


def checker_for(aliases: list[str]):
    """Adapter with Session.submit()'s checker interface, backed by no files."""
    checker = AnswerChecker(os.devnull)
    current = list(aliases)

    class Adapter:
        def is_correct(self, question_id: int, expected: str, user_text: str) -> bool:
            checker.aliases = {str(question_id): current}
            return checker.is_correct(int(question_id), expected, user_text)

    return Adapter()


def state_from(update: Update) -> dict | None:
    """Marker can live in a text message, a photo caption or a callback's message."""
    message = update.effective_message
    callback = getattr(update, "callback_query", None)
    source = None
    if callback is not None and callback.message is not None:
        source = callback.message
    elif message is not None and message.reply_to_message is not None:
        source = message.reply_to_message
    if source is None:
        return None
    return unpack_state(source.text or source.caption)


def session_from_data(data: dict, chat_id: int, archive: Archive | None = None) -> Session | None:
    """Rebuild a Session from a marker payload (used by replies, buttons and ticks)."""
    archive = archive or ARCHIVE
    if not data or not data.get("q"):
        return None
    question = archive.question(int(data["q"]))
    if not question:
        return None
    rounds = int(data.get("r") or 1)
    per_round = int(data.get("n") or 0)
    seed = int(data.get("s") or 0)
    package_id = int(data["p"])
    session = Session(
        chat_id=chat_id,
        mode=data.get("m", "solo"),
        package_id=package_id,
        package_title=(archive.package_info(package_id) or {}).get("title", ""),
        questions=build_playlist(archive.questions(package_id), rounds, per_round, seed),
        owner_id=int(data["o"]),
        timer_seconds=int(data.get("t") or TIMER),
        rounds=rounds,
        per_round=per_round,
        seed=seed,
    )
    session.index = int(data.get("i", 0))
    session.correct = int(data.get("c", 0))
    session.wrong = int(data.get("w", 0))
    session.question = question
    session.answered = False
    session.deadline = int(data.get("d") or 0)
    session.attempts = int(data.get("y") or 0)
    session.idle_streak = int(data.get("z") or 0)
    session.idle_limit = IDLE_LIMIT
    session.can_delete = bool(data.get("x", True))
    session.control_message_id = data.get("k")
    session.latest_answer = data.get("l")
    session.latest_user = int(data.get("lu") or 0) or None
    session.needs_confirm = bool(data.get("cf", 1))
    review = data.get("rv") or []
    if len(review) >= 7:
        try:
            session.review_at = int(review[0] or 0)
            session.review_due = int(review[1] or 0)
            session.review_mark = "1" if str(review[2]) == "1" else "0"
            session.review_text = str(review[3] or "")
            session.review_user = int(review[4] or 0)
            session.review_name = str(review[5] or "")
            session.review_noted = int(review[6] or 0)
            if len(review) > 7:
                session.review_to = [[int(row[0]), int(row[1])] for row in (review[7] or [])
                                     if isinstance(row, (list, tuple)) and len(row) == 2]
            if len(review) > 8:
                session.review_msg = int(review[8] or 0)
            if len(review) > 9:
                session.review_kind = str(review[9] or "answer")
        except (TypeError, ValueError):
            session.clear_review()
    return session


def restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[Session, dict] | None:
    data = state_from(update)
    if not data:
        return None
    archive: Archive = context.application.bot_data["archive"]
    session = session_from_data(data, update.effective_chat.id, archive)
    if session is None:
        return None
    return session, data


def local_image(question_id: int) -> Path | None:
    """Generated picture shipped with the repo, used when the CDN is unreachable."""
    for suffix in (".png", ".jpg"):
        candidate = IMAGES / f"{question_id}{suffix}"
        if candidate.exists():
            return candidate
    return None


async def send_photo_safely(bot, chat_id: int, question: dict, caption: str = "") -> bool:
    """Send the question picture: original CDN first, generated local copy as backup."""
    attachments = [a for a in (question.get("attachments") or []) if isinstance(a, str) and a.startswith("http")]
    url = attachments[0] if attachments else ""
    if url:
        try:
            await bot.send_photo(chat_id=chat_id, photo=url, caption=caption[:1024] or None,
                                 parse_mode=ParseMode.HTML if caption else None)
            return True
        except TelegramError:
            pass
    path = local_image(int(question.get("id", 0)))
    if path:
        try:
            with path.open("rb") as handle:
                await bot.send_photo(chat_id=chat_id, photo=InputFile(handle),
                                     caption=caption[:1024] or None,
                                     parse_mode=ParseMode.HTML if caption else None)
            return True
        except (TelegramError, OSError):
            return False
    return False


def has_picture(question: dict) -> bool:
    if local_image(int(question.get("id", 0))):
        return True
    return any(isinstance(a, str) and a.startswith("http") for a in (question.get("attachments") or []))


def closed_strip_text(session: Session) -> str:
    """One line that replaces a finished countdown strip."""
    return f"Savol {session.number} / {session.total} — yakunlandi"


async def delete_control(bot, chat_id: int, session: Session) -> None:
    """Remove the countdown strip once its question is resolved.

    Where the bot has no delete rights (a group it merely joined) the strip is
    collapsed into one line instead, so no stale countdown is left behind.
    """
    if not session.control_message_id:
        return
    message_id = int(session.control_message_id)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (TelegramError, ValueError, TypeError):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=closed_strip_text(session), reply_markup=None)
        except (TelegramError, ValueError, TypeError):
            pass
    session.control_message_id = None


async def close_strip(bot, chat_id: int, session: Session) -> bool:
    """Close the countdown strip; True when this caller may resolve the question.

    A chat where no strip was ever sent (Telegram refused it) has nothing to
    claim, so the question is resolved normally.
    """
    if not session.control_message_id:
        return True
    return await claim_strip(bot, chat_id, session)


async def claim_strip(bot, chat_id: int, session: Session) -> bool:
    """Take ownership of the strip before resolving its question.

    The countdown keeps running on its own, so a timeout can be reached by the
    timer and by a button press at the same moment. Whoever closes the strip
    first resolves the question; everybody else stands down. Without delete
    rights the strip is collapsed into a fixed line — a second attempt then
    fails with "message is not modified", which is exactly the signal we need.
    """
    if not session.control_message_id:
        return False
    message_id = int(session.control_message_id)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        session.control_message_id = None
        return True
    except (TelegramError, ValueError, TypeError):
        pass
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    text=closed_strip_text(session), reply_markup=None)
        session.control_message_id = None
        return True
    except (TelegramError, ValueError, TypeError):
        session.control_message_id = None
        return False


def strip_text(session: Session, remaining: int, deadline: int,
               aliases: list[str] | None = None) -> str:
    """Countdown strip content: position, live remaining time and hidden state."""
    head = f"Savol {session.number} / {session.total}"
    if session.rounds > 1:
        head += f" · {session.current_round}-raund"
    visible = f"{head}\n{msg.countdown(remaining, session.timer_seconds)}"
    return with_state(visible, pack_state(session, deadline, aliases=aliases))


def is_webapp_error(exc: Exception) -> bool:
    """True when Telegram refused the Mini App button itself."""
    text = str(exc).lower()
    return "webapp" in text or "web app" in text


async def paint_strip(bot, chat_id: int, session: Session, remaining: int, deadline: int,
                      aliases: list[str] | None = None) -> None:
    """Rewrite the countdown strip: the digits and the bar move, the state stays hidden."""
    text = strip_text(session, remaining, deadline, aliases)
    await bot.edit_message_text(
        chat_id=chat_id, message_id=int(session.control_message_id), text=text,
        parse_mode=ParseMode.HTML, reply_markup=st.game_keyboard(clock=msg.clock(remaining)),
    )


async def detect_can_delete(bot, chat_id: int, chat_type: str) -> bool:
    """Groups only let the bot remove its own countdown strip with delete rights."""
    if chat_type == "private":
        return True
    try:
        bot_id = bot.id
    except RuntimeError:
        try:
            bot_id = (await bot.get_me()).id
        except TelegramError:
            return False
    try:
        member = await bot.get_chat_member(chat_id, bot_id)
    except TelegramError:
        return False
    if member.status == "creator":
        return True
    return bool(getattr(member, "can_delete_messages", False))


def bg_add(factory) -> bool:
    """Queue work for right after this request's response has been sent."""
    bucket = _bg_tasks.get()
    if bucket is None:
        return False
    bucket.append(factory)
    return True


def with_background(response: Response, bucket: list) -> Response:
    """Attach queued work to a response, unless the handler already did."""
    if bucket and response.background is None:
        response.background = BackgroundTask(run_background, bucket)
    return response


async def run_background(bucket: list) -> None:
    """Run the queued work once the client already has its answer."""
    token = _bg_tasks.set(None)
    try:
        for factory in bucket:
            try:
                await factory()
            except Exception:
                logging.getLogger(__name__).warning("background task failed", exc_info=True)
    finally:
        _bg_tasks.reset(token)


async def post_tick(body: str, chat_id: int, wait: int, mode: str | None = None) -> None:
    """Hand the next countdown hop over to a fresh invocation of ourselves."""
    base = public_base_url()
    if not base:
        return
    mode = mode or TICK_MODE
    payload = {"tick": body, "chat": chat_id, "wait": wait, "mode": mode}
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET} if SECRET else {}
    # In "bg" mode the other side answers at once. Should a platform buffer the
    # response instead, we still wait long enough for its whole hop, so the next
    # hop is never left running against a dropped connection.
    if mode == "bg":
        timeout = min(55.0, max(8.0, wait + 5.0))
    else:
        timeout = min(55.0, max(12.0, wait + 10.0))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0)) as client:
            await client.post(base + "/api/index", json=payload, headers=headers)
    except Exception:  # cosmetic feature — never break the game over it
        logging.getLogger(__name__).debug("tick scheduling failed", exc_info=True)


async def schedule_tick(session: Session, deadline: int, aliases: list[str] | None = None,
                        wait: int | None = None, mode: str | None = None,
                        need_delete: bool = True) -> None:
    """Keep the countdown moving: wait, repaint, resolve, chain the next hop.

    Serverless functions freeze between requests, so the timer is a chain of
    short hops. If a hop is ever lost the game still ends correctly — the
    deadline is also checked lazily on every reply and button press.
    """
    # Without delete rights a resolved strip cannot be removed, so a late tick
    # would repaint a question that is already closed. Those chats keep the
    # manual "Vaqtni yangilash" button instead.
    if not TICKS_ENABLED or not session.control_message_id:
        return
    if need_delete and not session.can_delete:
        return
    remaining = deadline - int(time.time())
    if remaining <= 0:
        return
    step = TICK_SECONDS if wait is None else wait
    step = max(1, min(int(step), remaining, 50))
    body = pack_state(session, deadline, aliases=aliases)
    chat_id = session.chat_id
    mode = mode or TICK_MODE
    if mode == "bg" and bg_add(lambda: tick_after_wait(body, chat_id, step)):
        return
    await post_tick(body, chat_id, step, mode)


async def tick_after_wait(body: str, chat_id: int, wait: int) -> None:
    """A countdown hop that runs after the response has gone out."""
    if wait:
        await asyncio.sleep(wait)
    try:
        await tick_work(body, chat_id)
    except Exception:
        logging.getLogger(__name__).warning("tick hop failed", exc_info=True)


async def send_control(bot, chat_id: int, session: Session, deadline: int,
                       aliases: list[str] | None = None) -> None:
    """Countdown strip above every question: live remaining time + buttons."""
    remaining = max(0, deadline - int(time.time()))
    text = strip_text(session, remaining, deadline, aliases)
    try:
        message = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=st.game_keyboard(clock=msg.clock(remaining)),
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        if not is_webapp_error(exc):
            raise
        logging.getLogger(__name__).warning("Keyboard refused: %s", exc)
        message = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=None, disable_web_page_preview=True,
        )
    session.control_message_id = getattr(message, "message_id", None)


async def send_question(bot, chat_id: int, session: Session, aliases: list[str] | None = None) -> None:
    question = session.question
    if not question:
        return
    timer = session.timer_seconds or TIMER
    deadline = int(time.time()) + timer
    session.deadline = deadline
    await send_control(bot, chat_id, session, deadline, aliases)
    note("question", chat_id=chat_id, package_id=session.package_id, questions_asked=1,
         log=False)
    index_used([question], package_id=session.package_id, chat_id=chat_id,
               mode=session.mode or "oyin")

    picture = has_picture(question)
    attachments = [a for a in (question.get("attachments") or []) if isinstance(a, str)]
    link = "" if picture else (attachments[0] if attachments else "")
    round_label = f"{session.current_round}-raund" if session.rounds > 1 else ""
    text = msg.question(
        session.number, session.total, session.package_title, question.get("text"),
        timer, session.score_text(), link, round_label,
    )
    if picture:
        await send_photo_safely(bot, chat_id, question, caption="Savol rasmi")
    await bot.send_message(
        chat_id=chat_id,
        text=with_state(text, pack_state(session, deadline, aliases=aliases)),
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="Javobingizni yozing..."),
        disable_web_page_preview=True,
    )
    await schedule_tick(session, deadline, aliases)


async def reveal(bot, chat_id: int, session: Session, headline: str) -> None:
    question = session.question
    if not question:
        return
    answer = re.sub(r"^\s*(?:to'?g'?ri\s+)?javob\s*[:.]\s*", "", str(question.get("answer", "")), flags=re.I)
    text = msg.reveal(headline, answer, question.get("comment") or "", "", session.score_text())
    await bot.send_message(chat_id=chat_id, text=text[:4090], parse_mode=ParseMode.HTML,
                           disable_web_page_preview=True)
    pictures = [a for a in (question.get("answer_attachments") or []) if isinstance(a, str) and a.startswith("http")]
    if pictures:
        try:
            await bot.send_photo(chat_id=chat_id, photo=pictures[0], caption="Javob rasmi")
        except TelegramError:
            await bot.send_message(chat_id=chat_id, text=f"Rasm: {_esc(pictures[0])}",
                                   disable_web_page_preview=True)


def _percent(session: Session) -> int:
    return round(session.correct * 100 / session.total) if session.total else 0


async def next_question(bot, chat_id: int, session: Session, headline: str, aliases: list[str] | None = None) -> None:
    await delete_control(bot, chat_id, session)
    await reveal(bot, chat_id, session, headline)
    previous_round = session.current_round
    idle_streak = session.idle_streak
    session.index += 1
    session.advance()

    if session.question is None:
        note("game_end", chat_id=chat_id, package_id=session.package_id, games_finished=1,
             detail=f"{session.package_id} · {session.score_text()}")
        await bot.send_message(
            chat_id=chat_id,
            text=msg.summary(session.package_title, session.total, session.correct, session.wrong,
                             _percent(session), session.rounds),
            parse_mode=ParseMode.HTML,
        )
        return

    if session.idle_limit_reached:
        session.auto_stopped = True
        session.finished = True
        note("auto_stop", chat_id=chat_id, package_id=session.package_id,
             games_finished=1, games_auto_stopped=1,
             detail=f"{session.package_id} · {session.score_text()}")
        await bot.send_message(
            chat_id=chat_id,
            text=msg.auto_stop(session.idle_streak, session.package_title, session.index,
                               session.correct, session.wrong, _percent(session), session.rounds),
            parse_mode=ParseMode.HTML,
        )
        return

    if idle_streak and session.idle_limit and idle_streak < session.idle_limit:
        await bot.send_message(
            chat_id=chat_id,
            text=msg.idle_warning(idle_streak, session.idle_limit),
            parse_mode=ParseMode.HTML,
        )

    if session.rounds > 1 and session.current_round != previous_round:
        await bot.send_message(
            chat_id=chat_id,
            text=msg.round_banner(session.current_round, session.rounds, session.score_text()),
            parse_mode=ParseMode.HTML,
        )
    await send_question(bot, chat_id, session, aliases)


async def handle_tick(payload: dict) -> JSONResponse:
    """Entry point of a countdown hop.

    "bg" mode answers at once and lets the hop run after the response has been
    sent — the way a serverless platform keeps such work alive — so the chain
    never depends on holding an HTTP connection open. "sleep" mode runs the hop
    inline and stays as the fallback.
    """
    body = str(payload.get("tick") or "")
    try:
        chat_id = int(payload.get("chat") or 0)
        wait = min(max(0, int(payload.get("wait") or 0)), 50)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "reason": "bad payload"})
    if str(payload.get("mode") or TICK_MODE) == "bg":
        if bg_add(lambda: tick_after_wait(body, chat_id, wait)):
            return JSONResponse({"ok": True, "scheduled": wait, "mode": "bg"},
                                background=BackgroundTask(run_background, _bg_tasks.get()))
    if wait:
        await asyncio.sleep(wait)
    return JSONResponse(await tick_work(body, chat_id))


async def tick_work(body: str, chat_id: int) -> dict:
    """One hop of the live countdown: repaint, resolve or chain again."""
    global _ticks_seen, _ticks_painted, _ticks_resolved, _ticks_stopped
    _ticks_seen += 1
    if str(body or "").lstrip().startswith(sched.PLAN_PREFIX):
        return await reminder_tick_work(body, chat_id)
    if str(body or "").lstrip().startswith(tn.STATE_PREFIX):
        return await tour_tick_work(body, chat_id)
    state = body if body.lstrip().startswith(MARKER.strip()) else MARKER + body
    data = unpack_state(state)
    if not data or not chat_id:
        return {"ok": False, "reason": "no state"}

    deadline = int(data.get("d") or 0)
    if deadline - int(time.time()) > MAX_TICK_HORIZON:
        return {"ok": False, "reason": "deadline too far ahead"}

    session = session_from_data(data, chat_id)
    if session is None:
        return {"ok": False, "reason": "unknown question"}
    if not session.control_message_id:
        return {"ok": True, "stopped": "no strip"}

    application = get_application()
    await application.initialize()
    try:
        bot = application.bot
        remaining = deadline - int(time.time())
        aliases = list(data.get("a") or [])

        if session.reviewing():
            # The question is closed but the point is not given yet: this hop
            # belongs to the confirmation window, not to the countdown.
            return await game_review_tick(bot, chat_id, session, aliases)

        if remaining > 0:
            try:
                await paint_strip(bot, chat_id, session, remaining, deadline, aliases)
            except TelegramError as exc:
                text = str(exc).lower()
                if "not modified" in text:
                    pass
                elif isinstance(exc, RetryAfter) or "flood" in text:
                    # Telegram asks for a pause: come back later instead of
                    # dropping the whole countdown chain.
                    pause = int(getattr(exc, "retry_after", 0) or 0) + TICK_SECONDS * 2
                    await schedule_tick(session, deadline, aliases, wait=min(50, pause))
                    return {"ok": True, "throttled": pause}
                else:
                    # The strip is gone: answered, skipped or stopped elsewhere.
                    _ticks_stopped += 1
                    return {"ok": True, "stopped": "strip closed"}
            _ticks_painted += 1
            await schedule_tick(session, deadline, aliases)
            return {"ok": True, "remaining": remaining}

        # Time is up. Whoever manages to close the strip resolves the question,
        # so a reply arriving at the same moment cannot produce a second reveal.
        if not await claim_strip(bot, chat_id, session):
            _ticks_stopped += 1
            return {"ok": True, "stopped": "already resolved"}
        _ticks_resolved += 1
        if close_question(session):
            await next_question(bot, chat_id, session, "Vaqt tugadi. Ochko Qarshi tarafga.", aliases)
        return {"ok": True, "resolved": True}
    finally:
        await application.shutdown()



def _archive_of(context: ContextTypes.DEFAULT_TYPE) -> Archive:
    return context.application.bot_data["archive"]


def _resolve_package(context: ContextTypes.DEFAULT_TYPE, argument: str):
    """Find a package without loading the 27 MB question shards."""
    archive = _archive_of(context)
    argument = (argument or "").strip()
    if argument.isdigit():
        info = archive.package_info(int(argument))
        return (int(argument), info.get("title", "")) if info and info.get("questions") else None
    if not argument:
        package_id = archive.random_package_id()
        return package_id, (archive.package_info(package_id) or {}).get("title", "")
    if argument.casefold().strip("!.?") in st.WHOLE_BASE_WORDS:
        # "/solo katta" — a random package from the whole base, turnirlar.uz included
        package_id = archive.random_package_id(remote=True)
        return package_id, (archive.package_info(package_id) or {}).get("title", "")
    found = archive.search(argument, limit=1)
    return (int(found[0]["id"]), found[0]["title"]) if found else None


APOSTROPHE_MARKS = "‘’ʼʻ′`´\u02bc"


def _title_words(title: str) -> list[str]:
    """The words of a package title, with every apostrophe shape made one.

    Uzbek writes ``o‘`` and people type ``o'``; both must find the same word, and
    Cyrillic titles (``Девятый тур``) must keep their words too.
    """
    text = str(title or "").casefold()
    for mark in APOSTROPHE_MARKS:
        text = text.replace(mark, "'")
    return [word for word in re.split(r"[^\w']+", text, flags=re.UNICODE) if word]


MIN_SWAP_WORD = 4       # a package choice has to be a word, not a "ha"


def _resolve_package_strict(context: ContextTypes.DEFAULT_TYPE, argument: str):
    """A package for the swap prompt: an id, or words its title really holds.

    ``_resolve_package`` is deliberately loose — it powers ``/solo olimpiada``,
    where a good guess is a kindness, and the archive search is a plain substring
    match, so a single letter finds something ("a" lands on a championship, "9" on
    the ninth tour of a student league). Moving the whole bank a group is about to
    play is not a guess, so three rules decide: every word is at least two
    letters, at least one is a real word (``MIN_SWAP_WORD``), and each one starts a
    word of the title it matched — "savol" finds "…savollari", "o'yin" finds
    "o'yini", and an answer typed for something else finds nothing. Refusing costs
    one line and leaves the question open; swapping wrongly costs the whole
    selection.
    """
    wanted = " ".join(str(argument or "").split())
    if not wanted:
        return None
    if wanted.isdigit():
        return _resolve_package(context, wanted)          # a number is exact
    phrase = wanted.casefold().strip("!.?,;:«»" + "".join(APOSTROPHE_MARKS) + '"')
    for mark in APOSTROPHE_MARKS:
        phrase = phrase.replace(mark, "'")
    words = [word for word in re.split(r"[^\w']+", phrase, flags=re.UNICODE) if word]
    if not words or any(len(word) < 2 for word in words):
        return None
    if max(len(word) for word in words) < MIN_SWAP_WORD:
        return None
    found = _resolve_package(context, wanted) or _resolve_package(context, " ".join(words))
    if not found:
        return None
    title = _title_words(found[1])
    if not all(any(candidate.startswith(word) for candidate in title) for word in words):
        return None
    return found


async def render_settings(context: ContextTypes.DEFAULT_TYPE, config: st.GameConfig,
                          message=None, query=None) -> None:
    archive = _archive_of(context)
    info = archive.package_info(config.package_id) or {}
    available = int(info.get("questions") or 0)
    title = info.get("title") or "(sarlavhasiz)"
    per_round = config.per_round if config.per_round > 0 else available
    total = config.rounds * per_round
    text = msg.settings_panel(config.package_id, title, available, config.rounds,
                              config.per_round, config.timer, config.mode, total,
                              confirm=bool(config.needs_confirm))
    if available and total > available:
        text += "\n\n" + msg.repeat_notice(available, total - available)
    keyboard = st.settings_keyboard(config)
    if query is not None:
        try:
            await query.edit_message_text(text[:4090], parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except TelegramError:
            pass
        return
    if message is not None:
        await message.reply_text(text[:4090], parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                 disable_web_page_preview=True)
        return
    await context.bot.send_message(config.chat_id if hasattr(config, "chat_id") else 0, text,
                                   parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def open_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, argument: str) -> None:
    resolved = _resolve_package(context, argument)
    if argument.strip() and not resolved:
        await update.effective_message.reply_text(
            "Paket topilmadi. <code>/packages</code> orqali qidiring yoki <code>/solo</code> deb yozing.",
            parse_mode=ParseMode.HTML,
        )
        return
    package_id = resolved[0] if resolved else _archive_of(context).random_package_id()
    config = st.GameConfig(package_id=package_id, mode=mode, seed=random.randrange(1, 999999))
    await render_settings(context, config, message=update.effective_message)


async def start_from_config(context: ContextTypes.DEFAULT_TYPE, config: st.GameConfig,
                            chat_id: int, user_id: int, chat=None) -> None:
    archive = _archive_of(context)
    questions = archive.questions(config.package_id)
    if not questions:
        info = archive.package_info(config.package_id) or {}
        text = (msg.remote_unavailable(info.get("title", ""))
                if archive.is_remote(config.package_id)
                else "Bu paketda savol yo‘q. /packages orqali boshqasini tanlang.")
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        return
    playlist = build_playlist(questions, config.rounds, config.per_round, config.seed)
    info = archive.package_info(config.package_id) or {}
    session = Session(
        chat_id=chat_id,
        mode=config.mode,
        package_id=config.package_id,
        package_title=info.get("title", ""),
        questions=playlist,
        owner_id=user_id,
        timer_seconds=config.timer,
        rounds=config.rounds,
        per_round=config.per_round or len(playlist),
        seed=config.seed,
    )
    session.begin()
    session.idle_limit = IDLE_LIMIT
    session.needs_confirm = bool(getattr(config, "needs_confirm", True))
    chat_type = getattr(chat, "type", "private") or "private"
    session.can_delete = await detect_can_delete(context.bot, chat_id, chat_type)
    label = "Yakka o‘yin" if config.mode == "solo" else "Guruh o‘yini"
    note("game_start", user_id=user_id, chat_id=chat_id, package_id=config.package_id,
         games_started=1,
         detail=f"{label} · {config.package_id} · {session.total} savol · {config.timer} s")
    text = msg.game_start(label, session.package_title, session.total, session.timer_seconds,
                          session.score_text(), session.rounds, session.per_round)
    if chat_type != "private" and not session.can_delete:
        text += "\n\n" + msg.no_delete_rights()
    await context.bot.send_message(chat_id=chat_id, text=text[:4090], parse_mode=ParseMode.HTML)
    await send_question(context.bot, chat_id, session)


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session | None = None) -> bool:
    """Private chat owners and group admins/creators may skip and accept answers."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    if getattr(chat, "type", "private") == "private":
        return True
    if session is not None and int(session.owner_id) == int(user.id):
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return False
    return member.status in ("creator", "administrator")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0].isdigit():
        await open_settings(update, context, "solo", context.args[0])
        return
    packages, questions = ARCHIVE.counts()
    await update.effective_message.reply_text(
        msg.welcome(packages, questions, ARCHIVE.welcome_sources()), parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — the whole list in groups; /help turnir — just one group."""
    section = " ".join(context.args or []).strip()
    await update.effective_message.reply_text(msg.help_text(section),
                                              parse_mode=ParseMode.HTML)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.rules(), parse_mode=ParseMode.HTML)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator"):
        await context.bot.send_message(chat.id, msg.group_hello(), parse_mode=ParseMode.HTML)


async def render_packages(context: ContextTypes.DEFAULT_TYPE, page: int, query: str,
                          message=None, callback=None) -> None:
    archive = _archive_of(context)
    result = archive.list_packages(query, page, PER_PAGE)
    text = msg.packages_page(result, query, is_remote=archive.is_remote)[:4000]
    keyboard = st.page_keyboard(result["page"], result["pages"], query)
    if callback is not None:
        try:
            await callback.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except TelegramError:
            pass
        return
    if message is not None:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                 disable_web_page_preview=True)
        return
    await context.bot.send_message(0, text)


async def cmd_packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args or [])
    page = 1
    parts = query.split()
    if len(parts) > 1 and parts[0].casefold() in {"sahifa", "page", "-p"} and parts[1].isdigit():
        page, query = int(parts[1]), " ".join(parts[2:])
    await render_packages(context, page, query, message=update.effective_message)


async def on_page_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    parsed = st.decode_page(update.callback_query.data)
    if not parsed:
        return
    page, query = parsed
    await update.callback_query.answer()
    await render_packages(context, page, query, callback=update.callback_query)


async def cmd_solo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await open_settings(update, context, "solo", " ".join(context.args or []))


async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await open_settings(update, context, "group", " ".join(context.args or []))


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parsed = st.decode(query.data)
    if not parsed:
        return
    config, action, value = parsed
    archive = _archive_of(context)

    if action == st.ACTION_CANCEL:
        await query.answer("Bekor qilindi")
        try:
            await query.edit_message_text("O‘yin bekor qilindi. <code>/solo</code> bilan qayta boshlang.",
                                          parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
        return

    if action == st.ACTION_CONFIRM:
        if value == "?":
            await query.answer(msg.game_confirm_note(config.needs_confirm)[:200],
                               show_alert=True)
            return
        config = config.replace(needs_confirm=value != "0")
        await query.answer(msg.game_confirm_note(config.needs_confirm)[:200], show_alert=True)
        await render_settings(context, config, query=query)
        return

    if action == st.ACTION_SHUFFLE:
        config = config.replace(package_id=archive.random_package_id(), seed=random.randrange(1, 999999))
    elif action in (st.ACTION_ROUNDS, st.ACTION_PER_ROUND, st.ACTION_TIMER):
        field = {st.ACTION_ROUNDS: "rounds", st.ACTION_PER_ROUND: "per_round", st.ACTION_TIMER: "timer"}[action]
        try:
            config = config.replace(**{field: int(value)})
        except ValueError:
            pass

    if action != st.ACTION_START:
        await query.answer()
        await render_settings(context, config, query=query)
        return

    await query.answer("O‘yin boshlandi!")
    await start_from_config(context, config, query.message.chat_id, query.from_user.id,
                            chat=query.message.chat)


async def game_review_targets(bot, session: Session, chat_id: int,
                              answerer: int = 0) -> list[int]:
    """Who confirms one answer of an ordinary game.

    Somebody other than the person who answered. A game in a private chat has
    nobody to ask — the player is alone, and a player confirming their own guess is
    not a check — so it keeps playing exactly as it did before, and ``/accept``
    still exists for the moments when a person wants to overrule the checker there.
    """
    if int(chat_id) > 0:
        return []
    wanted: list[int] = []
    owner = int(session.owner_id or 0)
    if owner and owner != int(answerer or 0):
        wanted.append(owner)
    if len(wanted) < tn.REVIEW_TARGETS:
        try:
            members = await bot.get_chat_administrators(int(chat_id))
        except (TelegramError, AttributeError, TypeError):
            members = []
        for member in members or []:
            person = getattr(member, "user", None)
            if person is None or getattr(person, "is_bot", False):
                continue
            number = int(getattr(person, "id", 0) or 0)
            if not number or number == int(answerer or 0) or number in wanted:
                continue
            wanted.append(number)
            if len(wanted) >= tn.REVIEW_TARGETS:
                break
    return wanted[:tn.REVIEW_TARGETS]


async def game_review_allowed(bot, chat_id: int, user_id: int, session: Session) -> bool:
    """May this person decide the parked answer?

    The button is usually pressed in a private chat, so the rights are read from
    the group the game belongs to — not from the chat the button sits in.
    """
    number = int(user_id or 0)
    if not number:
        return False
    if number == int(session.owner_id or 0):
        return True
    if any(int(row[0]) == number for row in (session.review_to or [])):
        return True                      # the bot handed this person the card
    if int(chat_id) > 0:
        return False                     # a private game: only its owner decides
    try:
        member = await bot.get_chat_member(int(chat_id), number)
    except TelegramError:
        return False
    return str(getattr(member, "status", "") or "") in ("creator", "administrator")


async def paint_review_strip(bot, chat_id: int, session: Session,
                             aliases: list[str] | None = None) -> None:
    """The group's own line while an answer is being checked.

    No answer and no verdict here: the card with those goes to the admins. The
    countdown is replaced by the confirmation clock, and this message becomes the
    strip, so the hops that keep the window alive have something to hold.
    """
    left = session.review_left(int(time.time()))
    body = with_state(msg.game_review_waiting(session.review_name or "—", session.number,
                                              session.total, left),
                      pack_state(session, session.review_due, aliases=aliases))
    if session.control_message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id,
                                        message_id=int(session.control_message_id),
                                        text=body, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=None)
            return
        except TelegramError:
            pass
    try:
        sent = await bot.send_message(chat_id=chat_id, text=body[:4090],
                                      parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
        session.control_message_id = int(getattr(sent, "message_id", 0) or 0) or None
    except TelegramError:
        session.control_message_id = None


async def game_review_open(bot, chat_id: int, session: Session, aliases: list[str] | None,
                           answer: str = "", user_id: int = 0, name: str = "",
                           correct: bool = False, kind: str = "answer") -> bool:
    """Park one answer and hand it to a person; True when the caller must stop.

    False means "carry on as before": confirmation is off, somebody is already
    checking, or there is simply nobody to ask. A game must never stall because a
    private message could not be delivered.
    """
    if not session.needs_confirm or session.reviewing() or session.question is None:
        return False
    if not str(answer or "").strip():
        return False            # nobody guessed: the rules decide this one, not a person
    targets = await game_review_targets(bot, session, chat_id, user_id)
    if not targets:
        return False
    now = int(time.time())
    session.park_review(now, now + REVIEW_AUTO, answer, user_id, name, correct, kind)
    left = session.review_left(now)
    body = with_state(msg.game_review_card(name or "—", session.review_text, session.number,
                                           session.total, left, session.review_mark == "1"),
                      pack_state(session, session.review_due, aliases=aliases))
    delivered = []
    for person in targets:
        try:
            sent = await bot.send_message(chat_id=int(person), text=body[:4090],
                                          parse_mode=ParseMode.HTML,
                                          disable_web_page_preview=True,
                                          reply_markup=st.game_review_keyboard())
        except TelegramError:
            continue
        message_id = int(getattr(sent, "message_id", 0) or 0)
        if message_id:
            delivered.append([int(person), message_id])
    if not delivered:
        session.clear_review()
        return False
    session.review_to = delivered
    # The cards went out before the list of copies existed, so each one still says
    # it is alone. They are repainted with the whole list: whichever admin decides,
    # every copy loses its buttons and nobody can score the question twice.
    body = with_state(msg.game_review_card(name or "—", session.review_text, session.number,
                                           session.total, left, session.review_mark == "1"),
                      pack_state(session, session.review_due, aliases=aliases))
    for person, message_id in delivered:
        try:
            await bot.edit_message_text(chat_id=int(person), message_id=int(message_id),
                                        text=body[:4090], parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True,
                                        reply_markup=st.game_review_keyboard())
        except TelegramError:
            continue
    note("review_opened", chat_id=chat_id, package_id=session.package_id,
         detail=f"{session.mode} · savol {session.number} · {len(delivered)} admin · "
                f"{'to‘g‘ri' if correct else 'noto‘g‘ri'} xulosa")
    await paint_review_strip(bot, chat_id, session, aliases)
    await schedule_tick(session, session.review_due, aliases, wait=REVIEW_HOP,
                        need_delete=False)
    return True


async def game_review_remind(bot, session: Session, aliases: list[str] | None = None) -> int:
    """A nudge that arrives: a fresh card, because an edit is easy to miss."""
    body = with_state(msg.game_review_nudge(session.number, session.review_left(int(time.time()))),
                      pack_state(session, session.review_due, aliases=aliases))
    reached = 0
    for person, _message in list(session.review_to or []):
        try:
            await bot.send_message(chat_id=int(person), text=body[:4090],
                                   parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True,
                                   reply_markup=st.game_review_keyboard())
            reached += 1
        except TelegramError:
            continue
    return reached


async def game_review_close(bot, chat_id: int, session: Session, decided: str,
                            targets: list | None = None, group_message: int = 0) -> None:
    """Take the buttons away from every copy of the card."""
    text = msg.game_review_closed(session.number, session.total, decided, session.score_text())
    rows = targets if targets is not None else [list(row) for row in (session.review_to or [])]
    for person, message_id in rows:
        try:
            await bot.edit_message_text(chat_id=int(person), message_id=int(message_id),
                                        text=text[:4090], parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=None)
        except TelegramError:
            continue
    fallback = int(group_message or session.review_msg or 0)
    if fallback and fallback != int(session.control_message_id or 0):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=fallback, text=text[:4090],
                                        parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=None)
        except TelegramError:
            pass
    session.review_to = []
    session.review_msg = 0


async def game_review_publish(bot, chat_id: int, session: Session,
                              aliases: list[str] | None, user_id: int = 0, name: str = "",
                              auto: bool = False, flip: bool = False) -> bool:
    """The verdict becomes a point, the group sees it and the game moves on."""
    if not session.reviewing():
        return False
    targets = [list(row) for row in (session.review_to or [])]
    group_message = int(session.review_msg or 0)
    answerer = session.review_name
    had_answer = bool(session.review_text)
    kind = session.review_kind or "answer"
    if not await close_strip(bot, chat_id, session):
        return False                      # somebody decided it a moment ago
    result = session.decide(user_id=user_id, auto=auto, flip=flip)
    correct = result == CORRECT
    decided = msg.game_review_decided("" if auto else str(name or ""), bool(auto),
                                      rejected=not correct and had_answer)
    if correct:
        headline = f"To‘g‘ri! {_esc(answerer or name)} Bilimdonlar uchun ochko qozondi."
        note("correct", user_id=session.history[-1].get("user_id"), chat_id=chat_id,
             package_id=session.package_id, answers_correct=1, log=False)
    elif kind == "skip":
        headline = "O‘tkazildi — hech kim javobni topmadi."
        note("unanswered", chat_id=chat_id, package_id=session.package_id, unanswered=1,
             skips=1, detail=f"{session.mode} · savol {session.number}", log=False)
    elif had_answer:
        headline = f"Noto‘g‘ri. {_esc(answerer)} javobi hisoblanmadi — ochko Qarshi tarafga."
        note("wrong", user_id=session.history[-1].get("user_id"), chat_id=chat_id,
             package_id=session.package_id, answers_wrong=1, log=False)
    else:
        headline = "Vaqt tugadi. Ochko Qarshi tarafga."
        note("unanswered", chat_id=chat_id, package_id=session.package_id, unanswered=1,
             detail=f"{session.mode} · savol {session.number}", log=False)
    note("review_auto" if auto else "review_confirmed", chat_id=chat_id,
         package_id=session.package_id, user_id=int(user_id or 0) or None,
         detail=f"{session.mode} · savol {session.number} · "
                f"{'to‘g‘ri' if correct else 'noto‘g‘ri'}" + (" · avtomatik" if auto else ""))
    await game_review_close(bot, chat_id, session, decided, targets, group_message)
    await next_question(bot, chat_id, session, f"{headline}\n{_esc(decided)}", aliases)
    return True


async def game_review_tick(bot, chat_id: int, session: Session,
                           aliases: list[str] | None) -> dict:
    """One hop of a confirmation window: remind, publish or keep waiting."""
    now = int(time.time())
    if session.review_nudge(now):
        session.review_noted = int(session.review_noted) + 1
        await game_review_remind(bot, session, aliases)
    if now >= int(session.review_due or 0):
        await game_review_publish(bot, chat_id, session, aliases, auto=True)
        return {"ok": True, "review": "auto"}
    await paint_review_strip(bot, chat_id, session, aliases)
    await schedule_tick(session, session.review_due, aliases, wait=REVIEW_HOP,
                        need_delete=False)
    return {"ok": True, "review": session.review_left(now)}


async def game_review_button(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             session: Session, state: dict, action: str) -> None:
    """«TASDIQLASH» and «RAD ETISH» under one parked answer.

    TASDIQLASH publishes the bot's verdict; RAD ETISH turns it over, which is how
    a person says "that spelling was right after all" without typing an alias.
    """
    query = update.callback_query
    group = int(state.get("h") or 0) or int(query.message.chat_id)
    user_id = int(getattr(query.from_user, "id", 0) or 0)
    session.chat_id = group
    if not session.reviewing():
        await query.answer("Tekshiruv oynasi yopilib bo‘lgan — javob hisoblangan.",
                           show_alert=True)
        return
    if not await game_review_allowed(context.bot, group, user_id, session):
        await query.answer(msg.admin_only("javobni tasdiqlash"), show_alert=True)
        return
    aliases = list(state.get("a") or [])
    name = str(getattr(query.from_user, "first_name", "") or "")
    reject = action == GAME_REJECT_ACTION
    await query.answer("Rad etildi — xulosa o‘zgartirildi" if reject else "Tasdiqlandi")
    await game_review_publish(context.bot, group, session, aliases, user_id=user_id,
                              name=name, flip=reject)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await tour_prompt_text(update, context):
        return
    if await tour_group_text(update, context):
        return
    restored = restore(update, context)
    if not restored:
        reply = msg.small_talk(update.message.text)
        if reply:
            await update.message.reply_text(reply, parse_mode=ParseMode.HTML)
        return
    session, data = restored
    aliases = list(data.get("a") or [])
    deadline = int(data.get("d", 0))
    chat_id = update.effective_chat.id

    user_id = int(getattr(update.effective_user, "id", 0) or 0)
    if session.reviewing():
        # An answer is already with an admin: a second guess must not change it,
        # and the window keeps its hops even if this chat's timer chain was lost.
        await update.message.reply_text(
            msg.game_review_waiting(session.review_name or "—", session.number, session.total,
                                    session.review_left(int(time.time()))),
            parse_mode=ParseMode.HTML)
        await schedule_tick(session, session.review_due, aliases, wait=REVIEW_HOP,
                            need_delete=False)
        return

    if int(time.time()) > deadline:
        if await game_review_open(context.bot, chat_id, session, aliases,
                                  str(session.latest_answer or ""),
                                  int(session.latest_user or 0), "", False, "timeout"):
            return
        if await close_strip(context.bot, chat_id, session) and close_question(session):
            await next_question(context.bot, chat_id, session,
                                "Vaqt tugadi. Ochko Qarshi tarafga.", aliases)
        return

    if session.needs_confirm and not session.answered:
        # Judged before it is scored: a right answer is proposed to a person and
        # only becomes a point when somebody confirms it.
        verdict = session.judge(user_id, update.message.text, checker_for(aliases))
        if verdict == CORRECT and await game_review_open(
                context.bot, chat_id, session, aliases, update.message.text, user_id,
                str(getattr(update.effective_user, "first_name", "") or ""), True, "answer"):
            return

    result = session.submit(update.effective_user.id, update.message.text, checker_for(aliases))
    if result == CORRECT:
        note("correct", user_id=update.effective_user.id, chat_id=chat_id,
             package_id=session.package_id, answers_correct=1, log=False)
        await next_question(
            context.bot, chat_id, session,
            f"To‘g‘ri! {_esc(update.effective_user.first_name)} Bilimdonlar uchun ochko qozondi.", aliases,
        )
    elif result == FOREIGN:
        await update.message.reply_text("Bu yakka o‘yin — faqat boshlagan kishi javob yozadi.")
    elif result == WRONG and session.mode != "solo" and session.control_message_id:
        # A duel does not answer a wrong guess out loud — the other side may still
        # steal. The guess itself is written into the strip, so that a question
        # nobody finishes still shows the person confirming what was tried.
        note("wrong", user_id=user_id, chat_id=chat_id, package_id=session.package_id,
             answers_wrong=1, log=False)
        session.latest_answer = update.message.text
        session.latest_user = user_id
        try:
            await paint_strip(context.bot, chat_id, session, max(0, deadline - int(time.time())),
                              deadline, aliases)
        except TelegramError:
            pass
    elif result == WRONG and session.mode == "solo":
        note("wrong", user_id=update.effective_user.id, chat_id=chat_id,
             package_id=session.package_id, answers_wrong=1, log=False)
        text = with_state("Noto‘g‘ri. Yana urinib ko‘ring.",
                          pack_state(session, deadline, latest=update.message.text, aliases=aliases))
        await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                        reply_markup=ForceReply(input_field_placeholder="Yangi javob..."))


async def on_game_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if not data.startswith(st.PREFIX_GAME + "|"):
        return
    action = data.split("|", 1)[1]
    restored = restore(update, context)
    if not restored:
        await query.answer("O‘yin holati topilmadi. /solo bilan qayta boshlang.", show_alert=True)
        return
    session, state = restored
    if action in (GAME_CONFIRM_ACTION, GAME_REJECT_ACTION):
        # Read before the strip is adopted: this button usually sits in a private
        # chat, and the game it decides lives in the group named by the state.
        await game_review_button(update, context, session, state, action)
        return
    session.control_message_id = query.message.message_id
    chat_id = query.message.chat_id
    aliases = list(state.get("a") or [])
    deadline = int(state.get("d", 0))
    remaining = deadline - int(time.time())

    if action == "f":
        note("refresh", user_id=getattr(query.from_user, "id", None), chat_id=chat_id,
             package_id=session.package_id, refreshes=1, log=False)
        if session.reviewing():
            left = session.review_left(int(time.time()))
            await query.answer(f"Javob tekshirilmoqda · {msg.clock(left)} keyin avtomatik")
            await paint_review_strip(context.bot, chat_id, session, aliases)
            await schedule_tick(session, session.review_due, aliases, wait=REVIEW_HOP,
                                need_delete=False)
            return
        if remaining > 0 and not session.answered:
            await query.answer(f"Qolgan vaqt: {msg.clock(remaining)}")
            session.control_message_id = query.message.message_id
            try:
                await paint_strip(context.bot, chat_id, session, remaining, deadline, aliases)
            except TelegramError:
                pass
            await schedule_tick(session, deadline, aliases)
            return
        await query.answer("Vaqt tugadi")
        if await close_strip(context.bot, chat_id, session) and close_question(session):
            await next_question(context.bot, chat_id, session,
                                "Vaqt tugadi. Ochko Qarshi tarafga.", aliases)
        return

    if action == "k":
        if not await is_admin(update, context, session):
            await query.answer("O‘tkazish faqat admin uchun", show_alert=True)
            return
        if session.reviewing():
            await query.answer("Javob admin tekshiruvida — o‘tkazib bo‘lmaydi.",
                               show_alert=True)
            return
        await query.answer("O‘tkazildi")
        if await game_review_open(context.bot, chat_id, session, aliases,
                                  str(session.latest_answer or ""),
                                  int(session.latest_user or 0), "", False, "skip"):
            return
        if await close_strip(context.bot, chat_id, session) and close_question(session, "skip"):
            await next_question(context.bot, chat_id, session,
                                "O‘tkazildi — hech kim javobni topmadi.", aliases)
        else:
            await query.answer("Bu savol allaqachon yechilgan.", show_alert=True)
        return

    if action == "c":
        await query.answer(f"{session.score_text()} · {session.number}/{session.total}-savol")
        return

    if action == "x":
        await query.answer("O‘yin to‘xtatildi")
        note("stopped", user_id=getattr(query.from_user, "id", None), chat_id=chat_id,
             package_id=session.package_id, games_stopped=1, games_finished=1,
             detail=f"{session.package_id} · {session.score_text()}")
        await delete_control(context.bot, chat_id, session)
        await context.bot.send_message(chat_id, session.summary(), parse_mode=ParseMode.HTML)
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("Hisobni ko‘rish uchun savol xabariga reply qiling.")
        return
    session, _ = restored
    await update.effective_message.reply_text(
        f"{session.score_text()} · Savol {session.number}/{session.total}"
        + (f" · {session.current_round}-raund" if session.rounds > 1 else "")
    )


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("O‘tkazish uchun savol xabariga reply qiling.")
        return
    session, data = restored
    if not await is_admin(update, context, session):
        await update.effective_message.reply_text(msg.admin_only("/skip"), parse_mode=ParseMode.HTML)
        return
    chat_id = update.effective_chat.id
    aliases = list(data.get("a") or [])
    if session.reviewing():
        await update.effective_message.reply_text(
            msg.game_review_nudge(session.number, session.review_left(int(time.time()))),
            parse_mode=ParseMode.HTML)
        return
    if await game_review_open(context.bot, chat_id, session, aliases,
                              str(session.latest_answer or ""), int(session.latest_user or 0),
                              "", False, "skip"):
        return
    claimed = await close_strip(context.bot, chat_id, session)
    if not claimed or not close_question(session, "skip"):
        await update.effective_message.reply_text("Bu savol allaqachon yechilgan.")
        return
    await next_question(context.bot, update.effective_chat.id, session,
                        "O‘tkazildi — hech kim javobni topmadi.", data.get("a") or [])


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("To‘xtatish uchun savol xabariga reply qiling.")
        return
    session, _ = restored
    note("stopped", user_id=getattr(update.effective_user, "id", None),
         chat_id=update.effective_chat.id, package_id=session.package_id,
         games_stopped=1, games_finished=1,
         detail=f"{session.package_id} · {session.score_text()}")
    await delete_control(context.bot, update.effective_chat.id, session)
    await update.effective_message.reply_text(session.summary())


async def cmd_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("Javobni qabul qilish uchun savol xabariga reply qiling.")
        return
    session, data = restored
    if not await is_admin(update, context, session):
        await update.effective_message.reply_text(msg.admin_only("/accept"), parse_mode=ParseMode.HTML)
        return
    latest = data.get("l")
    if not latest:
        await update.effective_message.reply_text("Avval noto‘g‘ri javob yozing, keyin unga reply qilib /accept bosing.")
        return
    aliases = list(data.get("a") or [])
    value = normalize(latest)
    if value and value not in aliases:
        aliases.append(value)
    if checker_for(aliases).is_correct(
        int(session.question["id"]), str(session.question.get("answer", "")), latest
    ):
        note("correct", user_id=getattr(update.effective_user, "id", None),
             chat_id=update.effective_chat.id, package_id=session.package_id,
             answers_correct=1, detail=f"{session.package_id} · qabul qilindi")
        await next_question(context.bot, update.effective_chat.id, session,
                            "Javob qabul qilindi va o‘rganildi.", aliases)
    else:
        await update.effective_message.reply_text("Bu javob hali mos kelmadi.")


# --------------------------------------------------------------------- turnir
# Ko‘p jamoali turnir: kurator savollarni tanlaydi, jamoalar ro‘yxatdan o‘tadi,
# har savolga har jamoa bitta yakuniy javob beradi va oxirida jadval chiqadi.
#
# Serverless xotirasiz: turnir holati ham xabar ichida ko‘rinmas belgilar bilan
# yuradi (roster, tanlov, ochkolar). Ikki nusxa saqlanadi — pinlangan karta va
# savol tasmasi. Karta pinlangan bo‘lsa, u **asosiy** manba hisoblanadi: /yakuniy
# javoblar shu yerga yoziladi, shuning uchun kechikkan taymer qadamida ham
# javoblar yo‘qolmaydi.
TOUR_DEFAULT_TITLE = "Zakovat turniri"
TOUR_PICK_PER_PAGE = 8          # savol tanlash oynasida bir sahifadagi savollar
TOUR_PIN_TTL = 20               # sekund: pinlangan kartani har xabarda qayta o‘qimaslik


def tour_bank(archive: Archive, tour: tn.Tournament) -> list[dict]:
    """Tanlovda qolgan savollar (include/exclude hisobga olingan holda)."""
    return tour.bank(archive.questions(tour.package_id))


def tour_bank_size(archive: Archive, tour: tn.Tournament) -> int:
    return len(tour_bank(archive, tour))


def tour_total(archive: Archive, tour: tn.Tournament) -> int:
    """Nechta savol o‘ynaladi: raundlar × har raunddagi savol."""
    bank = tour_bank_size(archive, tour)
    if not bank:
        return 0
    per_round = int(tour.per_round or 0) or bank
    wanted = max(1, int(tour.rounds or 1)) * per_round
    return max(1, min(wanted, bank))            # a question is never repeated


def tour_round_label(tour: tn.Tournament) -> str:
    return f"{tour.current_round}-raund" if int(tour.rounds or 1) > 1 else ""


QUESTION_PAGE = 10      # nechta savol ro‘yxatning bir sahifasida


def tour_question_rows(archive: Archive, tour: tn.Tournament) -> list[dict]:
    """The tournament's questions in play order, with what everybody may see.

    Before the game starts the list is the playlist the seed already fixes, so
    the group can read its questions in advance. An answer is only marked
    published once that question has actually been played.
    """
    pool = list(tour.questions) if tour.questions else tour.playlist(tour_bank(archive, tour))
    published = {int(row.get("id") or 0) for row in tour.history}
    rows = []
    for number, question in enumerate(pool, start=1):
        question_id = int(question.get("id") or 0)
        rows.append({"number": number, "id": question_id,
                     "text": str(question.get("text") or ""),
                     "answer": str(question.get("answer") or ""),
                     "published": question_id in published,
                     "used": stats.is_used(question_id)})
    return rows


async def tour_render_questions(bot, archive: Archive, tour: tn.Tournament, page: int = 1,
                                query=None, chat_id: int = 0) -> int:
    """Send (or rewrite) the public question list; returns the page shown."""
    rows = tour_question_rows(archive, tour)
    pages = max(1, -(-len(rows) // QUESTION_PAGE))
    page = max(1, min(int(page or 1), pages))
    shown = rows[(page - 1) * QUESTION_PAGE: page * QUESTION_PAGE]
    published = sum(1 for row in rows if row["published"])
    text = msg.tour_question_list(tour.title, shown, published, page, pages)[:4090]
    keyboard = st.question_list_keyboard(page, pages, shown)
    if query is not None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=keyboard)
            return page
        except TelegramError:
            pass
    await bot.send_message(chat_id=int(chat_id or tour.chat_id), text=text,
                           parse_mode=ParseMode.HTML, reply_markup=keyboard,
                           disable_web_page_preview=True)
    return page


def tour_teams_view(tour: tn.Tournament) -> list[dict]:
    """Roster, matn va klaviatura uchun oddiy lug‘atlar ko‘rinishida."""
    return [{"name": team.name, "captain": int(team.captain),
             "ready": int(team.ready_count), "size": int(team.size),
             "members": [{"id": int(person.user_id), "name": person.label(),
                          "username": person.username, "ready": bool(person.ready)}
                         for person in team.members]}
            for team in tour.teams]


def tour_standings_line(tour: tn.Tournament) -> str:
    """Bir qatorlik joriy hisob: «Bilimdonlar 4 · Zakovat 3»."""
    rows = tour.standings()
    if not rows:
        return ""
    parts = [f"{row['name']} {row['score']}" for row in rows[:6]]
    line = "HISOB: " + " · ".join(parts)
    if len(rows) > 6:
        line += f" · yana {len(rows) - 6} jamoa"
    return line


def tour_final_count(tour: tn.Tournament) -> int:
    return sum(1 for team in tour.teams if team.final)


def tour_card_text(archive: Archive, tour: tn.Tournament, curator: bool = False) -> str:
    """Pinlangan kartaning ko‘rinadigan matni — turnirning oynasi."""
    sizes = tn.sizes()
    teams = tour_teams_view(tour)
    info = archive.package_info(tour.package_id) or {}
    package_title = info.get("title") or f"paket {tour.package_id}"
    bank = tour_bank_size(archive, tour)
    if tour.phase == tn.PHASE_REGISTER:
        total = tour_total(archive, tour)
        per_round = int(tour.per_round or 0) or bank
        text = msg.tour_intro(tour.title, package_title, int(tour.rounds or 1), per_round,
                              int(tour.timer), total, sizes,
                              start_text=tn.when_text(tour.start_at),
                              team_limit=tour.team_limit)
        text += "\n" + msg.tour_teams(teams, tour.phase, sizes)
        problems = tour.missing(bank, int(time.time()))
        text += "\n\n" + (msg.tour_ready(total, tour.people, len(tour.teams)) if not problems
                            else msg.tour_missing(problems))
        if curator:
            text += "\n\nKurator: «Sozlamalar» — raund, savol soni, vaqt; " \
                    "«Savol tanlash» — aniq savollarni olish yoki chiqarish."
        return text
    if tour.phase == tn.PHASE_PLAY:
        text = msg.tour_card_play(tour.title, tour.number, tour.total, tour_round_label(tour),
                                  msg.tour_standings(tour.standings(), heading="JADVAL"),
                                  tour_final_count(tour), len(tour.teams), int(tour.timer))
        if tour.reviewing():
            text += "\n\n" + msg.tour_review_state(tour.review_left(int(time.time())),
                                                     tour_final_count(tour))
        elif tour.needs_confirm:
            text += "\n\n○ Javoblarni kurator tasdiqlaydi — ochko tasdiqdan keyin beriladi."
        return text
    return msg.tour_over(tour.title, tour.standings(), tour.asked, int(tour.rounds or 1))


def tour_card_keyboard(archive: Archive, tour: tn.Tournament, curator: bool):
    sizes = tn.sizes()
    if tour.phase == tn.PHASE_REGISTER:
        return st.tour_keyboard(tour_teams_view(tour), tour.phase, tour.ready_to_start(
            tour_bank_size(archive, tour), int(time.time())), curator, sizes,
            start_text=tn.when_text(int(tour.start_at)))
    if tour.phase == tn.PHASE_PLAY:
        return st.tour_play_keyboard(curator, msg.clock(
            max(0, int(tour.deadline) - int(time.time()))))
    return None


def tour_state_text(visible: str, tour: tn.Tournament) -> str:
    """Visible text plus the tournament state, shrunk until it fits one message.

    The roster survives every level; the directory, the question history and an
    over-long selection are given up first, so a full tournament still fits
    inside Telegram's 4096-character limit. The visible part is cut at a line
    boundary and only when the tag count stays balanced — a message cut through
    an HTML tag is rejected by Telegram, and losing the card would lose the
    tournament with it.
    """
    for level in range(tn.TRIM_LEVELS):
        body = tour.body(level)
        if utf16_length(hidden.encode(body)) + 24 > 4096:
            continue                        # this level cannot fit at all
        text = visible
        while True:
            candidate = with_state(text, body)
            if utf16_length(candidate) <= 4096 and hidden.decode(candidate) == body:
                return candidate
            if "\n" in text:
                head = text.rsplit("\n", 1)[0]     # cut whole lines: no half tags
                if head and head != text:
                    text = head
                    continue
            shorter = text[:max(0, len(text) - 80)]
            if not shorter or shorter == text:
                break
            text = shorter
    return with_state("", tour.body(tn.TRIM_LEVELS - 1))


async def tour_persist(bot, archive: Archive, tour: tn.Tournament,
                       merge_finals: bool = False) -> None:
    """Write the tournament back into its card — the copy that outlives a strip.

    The card is pinned when Telegram allows it, because a pinned message can be
    read back with getChat: that is how /yakuniy finds the tournament when
    nobody replied to the strip, and how a countdown hop picks up finals that
    were submitted after it was scheduled.

    ``merge_finals`` is for the paths where members race each other (a final
    answer, taking one back): the card is read once more and any answer another
    request wrote a moment ago is kept instead of overwritten.
    """
    curator = bool(tour.creator)
    if merge_finals and tour.card_message_id and tour.phase == tn.PHASE_PLAY:
        current = await tour_from_pin(bot, archive, tour.chat_id)
        # ``rev`` is no help here: two requests that read the same card end up
        # with the same number. The timestamps decide — a final written a moment
        # ago by the other request is newer than this copy's, and is kept.
        if tour.take_finals(current):
            tour.rev = max(int(tour.rev), int(getattr(current, "rev", 0) or 0) + 1)
    if tour.card_message_id is None:
        # The card is meant to be pinned: say so before it is written, so the
        # state inside it already knows where the tournament lives.
        tour.can_pin = True
    text = tour_card_text(archive, tour, curator)[:4000]
    keyboard = tour_card_keyboard(archive, tour, curator)
    body_text = tour_state_text(text, tour)
    if tour.card_message_id:
        try:
            await bot.edit_message_text(chat_id=tour.chat_id, message_id=int(tour.card_message_id),
                                        text=body_text, parse_mode=ParseMode.HTML,
                                        reply_markup=keyboard, disable_web_page_preview=True)
            return
        except TelegramError:
            tour.card_message_id = None      # karta o‘chirilgan — yangisini yuboramiz
    try:
        message = await bot.send_message(chat_id=tour.chat_id, text=body_text,
                                         parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                         disable_web_page_preview=True)
    except TelegramError:
        logging.getLogger(__name__).warning("tournament card rejected", exc_info=True)
        return
    tour.card_message_id = getattr(message, "message_id", None)
    _tour_probe.pop(int(tour.chat_id), None)      # the cached answer is stale now
    if tour.card_message_id:
        try:
            await bot.pin_chat_message(chat_id=tour.chat_id, message_id=int(tour.card_message_id),
                                       disable_notification=True)
            tour.can_pin = True
        except TelegramError:
            tour.can_pin = False
        # The state inside the card must carry its own message id, or the next
        # change would send a second card instead of rewriting this one.
        retry = tour_state_text(text, tour)
        try:
            await bot.edit_message_text(chat_id=tour.chat_id,
                                        message_id=int(tour.card_message_id), text=retry,
                                        parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                        disable_web_page_preview=True)
        except TelegramError:
            logging.getLogger(__name__).debug("tournament card could not be rewritten",
                                              exc_info=True)


def tour_attach(archive: Archive, tour: tn.Tournament) -> tn.Tournament:
    """Give a tournament read back from a message its question texts."""
    return tn.rebuild(tour, archive.questions(tour.package_id))


async def tour_from_pin(bot, archive: Archive, chat_id: int) -> tn.Tournament | None:
    """Read the tournament from the chat's pinned card, if there is one."""
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramError:
        return None
    pinned = getattr(chat, "pinned_message", None)
    if pinned is None:
        return None
    tour = unpack_tournament(getattr(pinned, "text", None) or getattr(pinned, "caption", None))
    if tour is None:
        return None
    tour.can_pin = True
    return tn.rebuild(tour, archive.questions(tour.package_id))


async def tour_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tn.Tournament | None:
    """Find the tournament this update belongs to.

    Reply to the strip or a button on the card is exact; without one the pinned
    card is read, so /yakuniy works even when nobody quoted anything. The newer
    of the two wins (``rev``), which keeps a stale copy from overwriting finals.
    """
    archive = _archive_of(context)
    chat_id = update.effective_chat.id if update.effective_chat else 0
    candidates: list[tn.Tournament] = []
    message = update.effective_message
    callback = getattr(update, "callback_query", None)
    source = None
    if callback is not None and callback.message is not None:
        source = callback.message
    elif message is not None and message.reply_to_message is not None:
        source = message.reply_to_message
    if chat_id:
        pinned = await tour_from_pin(context.bot, archive, chat_id)
        if pinned is not None:
            candidates.append(pinned)
    if source is not None:
        found = unpack_tournament(getattr(source, "text", None) or getattr(source, "caption", None))
        if found is not None:
            candidates.append(tn.rebuild(found, archive.questions(found.package_id)))
    if not candidates:
        return None
    playing = [item for item in candidates if item.phase == tn.PHASE_PLAY]
    pool = playing or candidates
    # Ties go to the pinned card (it is first in the list): every final answer is
    # written there at once, while a strip may still carry an older copy.
    return max(pool, key=lambda item: (int(item.rev), int(item.asked), int(item.index)))


async def tour_curator(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       tour: tn.Tournament | None) -> bool:
    """May this user run the tournament? Admin, owner, creator or appointed."""
    user = update.effective_user
    if not user:
        return False
    if is_owner(update):
        return True
    if tour is not None:
        if int(user.id) in (int(tour.creator),):
            return True
        if tour.is_curator(user.id):
            return True
    return await is_admin(update, context, None)


def tour_sender(user) -> str:
    first = getattr(user, "full_name", "") or getattr(user, "first_name", "") or ""
    username = getattr(user, "username", "") or ""
    return (first or username or f"id {getattr(user, 'id', 0)}").strip()


def tour_person(tour: tn.Tournament, user_id: int) -> str:
    """Who a confirmation came from, by name when the tournament remembers it."""
    number = int(user_id or 0)
    if not number:
        return ""
    for row in tour.directory or []:
        try:
            if int(row[0]) == number:
                return str(row[1] or row[2] or f"id {number}").strip()
        except (TypeError, ValueError, IndexError):
            continue
    return f"id {number}"


async def tour_curator_id(bot, chat_id: int, user_id: int,
                          tour: tn.Tournament | None = None) -> bool:
    """``tour_curator`` by id — for buttons pressed in a private chat.

    The confirmation list lives in the curator's own chat, so ``effective_chat``
    says nothing about the group: the rights have to be read from the group.
    """
    number = int(user_id or 0)
    if not number:
        return False
    if int(stats.OWNER_ID or 0) and number == int(stats.OWNER_ID):
        return True
    if tour is not None:
        if number == int(tour.creator or 0):
            return True
        if tour.is_curator(number):
            return True
    try:
        member = await bot.get_chat_member(int(chat_id), number)
    except TelegramError:
        return False
    return str(getattr(member, "status", "") or "") in ("creator", "administrator")


async def tour_delete(bot, chat_id: int, message_id: int | None, allowed: bool) -> bool:
    """Remove a message from the group when the bot is allowed to.

    Team answers and whispers must not stay readable by the other teams, so the
    bot deletes them; without that right the copy warns the group once.
    """
    if not message_id or not allowed:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        return True
    except TelegramError:
        return False


async def tour_private(bot, targets: list, text: str) -> tuple[int, int]:
    """Send one text to several users in private; returns (delivered, failed)."""
    delivered = failed = 0
    for person in targets:
        user_id = int(getattr(person, "user_id", 0) or (person.get("id") if isinstance(person, dict) else 0))
        if not user_id:
            continue
        try:
            await bot.send_message(chat_id=user_id, text=text[:4090], parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
            delivered += 1
        except TelegramError:
            failed += 1
    return delivered, failed


async def tour_team_note(bot, tour: tn.Tournament, team, text: str) -> tuple[int, int]:
    """One line to every member of a team — nobody else in the group sees it."""
    return await tour_private(bot, list(team.members), text)


def tour_strip_text(tour: tn.Tournament, remaining: int) -> str:
    """The tournament countdown strip: position, finals so far, live clock."""
    visible = msg.tour_strip(tour.number, tour.total, tour_round_label(tour),
                             tour_final_count(tour), len(tour.teams),
                             msg.countdown(remaining, int(tour.timer)))
    return tour_state_text(visible, tour)


async def send_tour_strip(bot, chat_id: int, tour: tn.Tournament, deadline: int) -> None:
    remaining = max(0, int(deadline) - int(time.time()))
    try:
        message = await bot.send_message(
            chat_id=chat_id, text=tour_strip_text(tour, remaining), parse_mode=ParseMode.HTML,
            reply_markup=st.question_strip_keyboard(False, msg.clock(remaining)),
            disable_web_page_preview=True)
        tour.control_message_id = getattr(message, "message_id", None)
    except TelegramError:
        tour.control_message_id = None


async def paint_tour_strip(bot, chat_id: int, tour: tn.Tournament, remaining: int) -> None:
    await bot.edit_message_text(
        chat_id=chat_id, message_id=int(tour.control_message_id),
        text=tour_strip_text(tour, remaining), parse_mode=ParseMode.HTML,
        reply_markup=st.question_strip_keyboard(False, msg.clock(remaining)),
        disable_web_page_preview=True)


async def tour_claim_strip(bot, chat_id: int, tour: tn.Tournament) -> bool:
    """Take ownership of the strip before resolving: the first caller wins."""
    if not tour.control_message_id:
        return True
    message_id = int(tour.control_message_id)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        tour.control_message_id = None
        return True
    except (TelegramError, ValueError, TypeError):
        pass
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    text=f"• Savol {tour.number} / {tour.total} — yakunlandi",
                                    reply_markup=None)
        tour.control_message_id = None
        return True
    except (TelegramError, ValueError, TypeError):
        tour.control_message_id = None
        return False


async def send_tour_question(bot, archive: Archive, chat_id: int,
                             tour: tn.Tournament) -> None:
    """One tournament question: strip with the clock, then the question itself."""
    question = tour.question
    if question is None:
        return
    timer = int(tour.timer or TIMER)
    deadline = int(time.time()) + timer
    tour.deadline = deadline
    tour.bump()
    await send_tour_strip(bot, chat_id, tour, deadline)
    await tour_persist(bot, archive, tour)      # the card now carries the strip id too
    note("question", chat_id=chat_id, package_id=tour.package_id, questions_asked=1,
         detail=f"turnir · savol {tour.number}/{tour.total}", log=False)
    index_used([question], package_id=tour.package_id, chat_id=chat_id, mode="turnir")

    picture = has_picture(question)
    attachments = [a for a in (question.get("attachments") or []) if isinstance(a, str)]
    link = "" if picture else (attachments[0] if attachments else "")
    info = archive.package_info(tour.package_id) or {}
    text = msg.tour_question(tour.number, tour.total, tour_round_label(tour),
                             info.get("title") or "", question.get("text") or "", timer, link,
                             tour_standings_line(tour))
    if picture:
        await send_photo_safely(bot, chat_id, question, caption="Savol rasmi")
    await bot.send_message(chat_id=chat_id, text=tour_state_text(text, tour)[:4096],
                           parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                           reply_markup=ForceReply(
                               input_field_placeholder="/yakuniy javobingiz"))
    await schedule_tour_tick(tour, deadline)


async def tour_review_targets(bot, tour: tn.Tournament, chat_id: int) -> list[int]:
    """Who may confirm: appointed curators, the creator, then the group's admins.

    The bot's owner is *not* added: a tournament in somebody else's group is not
    theirs to judge, and a message per question would only be noise.
    """
    wanted: list[int] = []
    for person in list(tour.curators or []) + [int(tour.creator or 0)]:
        number = int(person or 0)
        if number and number not in wanted:
            wanted.append(number)
    if len(wanted) < tn.REVIEW_TARGETS:
        try:
            members = await bot.get_chat_administrators(int(chat_id))
        except TelegramError:
            members = []
        for member in members or []:
            person = getattr(member, "user", None)
            if person is None or getattr(person, "is_bot", False):
                continue
            number = int(getattr(person, "id", 0) or 0)
            if number and number not in wanted:
                wanted.append(number)
            if len(wanted) >= tn.REVIEW_TARGETS:
                break
    return wanted[:tn.REVIEW_TARGETS]


def tour_review_text(bot_archive_answer: str, tour: tn.Tournament, rows: list) -> str:
    """The list itself, with the right answer the curator is judging against."""
    return msg.tour_review_list(tour.number, tour.total, bot_archive_answer, rows,
                                tour.review_left(int(time.time())), int(tour.review_noted))


async def tour_review_dm(bot, tour: tn.Tournament, answer: str, user_id: int):
    """One curator's private copy of the list; ``[who, message]`` when it lands."""
    rows = tour.review_rows()
    body = tour_state_text(tour_review_text(answer, tour, rows), tour)[:4096]
    try:
        sent = await bot.send_message(chat_id=int(user_id), text=body,
                                      parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True,
                                      reply_markup=st.review_keyboard(tour.chat_id, rows))
    except TelegramError:
        return None
    message_id = int(getattr(sent, "message_id", 0) or 0)
    return [int(user_id), message_id] if message_id else None


async def tour_review_open(bot, archive: Archive, chat_id: int, tour: tn.Tournament,
                           now: int = 0) -> bool:
    """Time is up: hold the points and hand the verdicts to a person.

    The group is told only that the question closed and how many answers there
    are — no answer and no verdict, because those are still a proposal. The list
    itself goes to the curators in private; when nobody can be reached it is
    posted in the group instead, so a game never stalls on a closed DM.
    """
    now = int(now or time.time())
    checker = checker_for([])
    rows = tour.propose(checker, now, now + REVIEW_AUTO)
    answer = str((tour.question or {}).get("answer") or "")
    answered = sum(1 for row in rows if str(row.get("answer") or "").strip())
    if not rows or not answered:
        # Not one team answered: there is no verdict to check and no point at
        # stake, so making a person press a button would only hold the game up.
        tour.clear_review()
        result = tour.close(checker, now)
        tour.bump()
        await tour_publish(bot, archive, chat_id, tour, result, now)
        return False
    await bot.send_message(
        chat_id=chat_id,
        text=tour_state_text(msg.tour_review_waiting(tour.number, tour.total, answered,
                                                     len(rows), REVIEW_AUTO), tour)[:4090],
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    delivered = []
    for person in await tour_review_targets(bot, tour, chat_id):
        row = await tour_review_dm(bot, tour, answer, person)
        if row:
            delivered.append(row)
    tour.review_to = delivered
    if delivered:
        # Every copy learns about the others: whichever curator decides, all the
        # lists lose their buttons, so a second press cannot score twice.
        await tour_review_render(bot, archive, tour, answer)
    if not delivered:
        # Nobody could be reached in private: the group carries the list instead.
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=tour_state_text(tour_review_text(answer, tour, tour.review_rows()),
                                     tour)[:4096],
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_markup=st.review_keyboard(chat_id, tour.review_rows()))
            tour.review_msg = int(getattr(sent, "message_id", 0) or 0)
        except TelegramError:
            tour.review_msg = 0
    tour.bump()
    await tour_persist(bot, archive, tour)
    note("review_opened", chat_id=chat_id, package_id=tour.package_id,
         detail=f"turnir · savol {tour.number} · {answered}/{len(rows)} javob · "
                f"{len(delivered)} kurator")
    await schedule_tour_review(tour, tour.review_due)
    return True


async def tour_review_render(bot, archive: Archive, tour: tn.Tournament,
                             answer: str = "") -> int:
    """Rewrite every open copy of the list — one mark changed, all of them show it.

    Two curators may be looking at the same list; whichever of them flips a
    verdict, the other must see the same thing before pressing «TASDIQLASH».
    """
    rows = tour.review_rows()
    answer = answer or str((tour.question or {}).get("answer") or "")
    body = tour_state_text(tour_review_text(answer, tour, rows), tour)[:4096]
    keyboard = st.review_keyboard(int(tour.chat_id), rows)
    edited = 0
    keep = []
    for person, message_id in list(tour.review_to or []):
        try:
            await bot.edit_message_text(chat_id=int(person), message_id=int(message_id),
                                        text=body, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=keyboard)
            keep.append([int(person), int(message_id)])
            edited += 1
        except TelegramError:
            fresh = await tour_review_dm(bot, tour, answer, int(person))
            if fresh:
                keep.append(fresh)
                edited += 1
    tour.review_to = keep[-tn.REVIEW_TARGETS:]
    if int(tour.review_msg or 0):
        try:
            await bot.edit_message_text(chat_id=int(tour.chat_id),
                                        message_id=int(tour.review_msg), text=body,
                                        parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=keyboard)
            edited += 1
        except TelegramError:
            tour.review_msg = 0
    return edited


async def tour_review_remind(bot, tour: tn.Tournament, answer: str = "") -> int:
    """A nudge that actually arrives: a new short message, not a silent edit."""
    rows = tour.review_rows()
    left = tour.review_left(int(time.time()))
    text = msg.tour_review_nudge(tour.number, left)
    keyboard = st.review_keyboard(int(tour.chat_id), rows, confirm="TASDIQLASH")
    reached = 0
    for person, _message in list(tour.review_to or []):
        try:
            await bot.send_message(chat_id=int(person), text=text[:4090],
                                   parse_mode=ParseMode.HTML, reply_markup=keyboard)
            reached += 1
        except TelegramError:
            continue
    if not tour.review_to and int(tour.review_msg or 0):
        try:
            await bot.edit_message_text(chat_id=int(tour.chat_id),
                                        message_id=int(tour.review_msg),
                                        text=tour_state_text(
                                            tour_review_text(answer, tour, rows) + "\n\n" + text,
                                            tour)[:4096],
                                        parse_mode=ParseMode.HTML, reply_markup=keyboard)
            reached += 1
        except TelegramError:
            pass
    return reached


async def tour_review_close(bot, tour: tn.Tournament, result: dict, auto: bool,
                            name: str, targets: list | None = None,
                            group_message: int = 0) -> None:
    """Take the buttons away: the list is decided, pressing more must do nothing.

    ``targets`` and ``group_message`` are the messages the list was sent to. They
    are passed in because confirming clears the review first — the messages do not
    disappear with it, and leaving live buttons behind is how a score gets changed
    after everybody has seen it.
    """
    decided = msg.tour_review_decided(name, auto)
    text = msg.tour_review_closed(tour.number, tour.total, list(result.get("winners") or []),
                                  decided)
    rows = targets if targets is not None else [list(row) for row in (tour.review_to or [])]
    fallback = int(group_message or tour.review_msg or 0)
    for person, message_id in rows:
        try:
            await bot.edit_message_text(chat_id=int(person), message_id=int(message_id),
                                        text=text[:4090], parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=None)
        except TelegramError:
            continue
    tour.review_to = []
    if fallback:
        try:
            await bot.edit_message_text(chat_id=int(tour.chat_id), message_id=fallback,
                                        text=text[:4090], parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True, reply_markup=None)
        except TelegramError:
            pass
    tour.review_msg = 0


async def tour_review_publish(bot, archive: Archive, chat_id: int, tour: tn.Tournament,
                              user_id: int = 0, auto: bool = False) -> None:
    """The marks become points, the group sees the result, the game moves on."""
    now = int(time.time())
    rows = tour.review_rows()
    changed = sum(1 for row in rows if row.get("changed"))
    confirmed = 0 if auto else int(user_id or 0)
    name = "" if auto else tour_person(tour, confirmed)
    targets = [list(row) for row in (tour.review_to or [])]
    group_message = int(tour.review_msg or 0)
    result = tour.confirm(confirmed, now, auto=bool(auto))
    tour.bump()
    await tour_review_close(bot, tour, result, bool(auto), name, targets, group_message)
    await tour_persist(bot, archive, tour)
    note("review_auto" if auto else "review_confirmed", chat_id=chat_id,
         package_id=tour.package_id, user_id=confirmed or None,
         detail=f"turnir · savol {tour.number} · {changed} o‘zgartirish"
                + (" · avtomatik" if auto else ""))
    await tour_publish(bot, archive, chat_id, tour, result, now)


async def tour_review_tick(bot, archive: Archive, chat_id: int, tour: tn.Tournament) -> dict:
    """One hop of the confirmation window: remind, publish or keep waiting."""
    now = int(time.time())
    if tour.review_nudge(now):
        tour.review_noted = int(tour.review_noted) + 1
        await tour_review_remind(bot, tour)
        tour.bump()
        await tour_persist(bot, archive, tour)
    if now >= int(tour.review_due or 0):
        await tour_review_publish(bot, archive, chat_id, tour, auto=True)
        return {"ok": True, "review": "auto"}
    await schedule_tour_review(tour, int(tour.review_due))
    return {"ok": True, "review": int(tour.review_due) - now}


async def tour_review_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                            query=None) -> tn.Tournament | None:
    """The tournament a confirmation button belongs to.

    The button may sit in a private chat, where the group's pinned card is not
    reachable through ``effective_chat``: the group id comes from the button and
    the card is read with it. The message the button hangs on is the backup.
    """
    archive = _archive_of(context)
    tour = await tour_from_pin(context.bot, archive, int(chat_id))
    if tour is not None:
        return tour
    message = getattr(query, "message", None)
    if message is None:
        return None
    found = unpack_tournament(getattr(message, "text", None) or getattr(message, "caption", None))
    if found is None:
        return None
    return tn.rebuild(found, archive.questions(found.package_id))


async def schedule_tour_review(tour: tn.Tournament, due: int) -> None:
    """Keep the confirmation window alive hop by hop.

    Unlike a countdown there is no strip to repaint, so nothing here depends on
    one — the window has to survive even in a group where the bot may not delete
    messages, and the hops are longer than a countdown's because a verdict does
    not need five-second precision.
    """
    if not TICKS_ENABLED:
        return
    remaining = int(due) - int(time.time())
    if remaining <= 0:
        return
    step = max(1, min(int(REVIEW_HOP), remaining, 50))
    body = tour.body()
    chat_id = tour.chat_id
    if TICK_MODE == "bg" and bg_add(lambda: tour_tick_after_wait(body, chat_id, step)):
        return
    await post_tick(body, chat_id, step)


async def tour_publish(bot, archive: Archive, chat_id: int, tour: tn.Tournament,
                       result: dict, now: int) -> None:
    """One closed question, published: result, then the next question or the end.

    Scoring happened before this — either by the checker alone or by a curator's
    marks — so everything here is only telling the group and moving the game on.
    """
    if not result.get("asked"):
        return
    question = archive.question(int(result.get("question_id") or 0)) or {}
    for team_name in result.get("winners") or []:
        note("correct", chat_id=chat_id, package_id=tour.package_id, answers_correct=1,
             detail=f"turnir · {team_name} · savol {tour.number}", log=False)
    if not result.get("winners"):
        note("unanswered", chat_id=chat_id, package_id=tour.package_id, unanswered=1,
             detail=f"turnir · savol {tour.number}", log=False)
    finals = [{"team": row.get("team"), "answer": row.get("answer"),
               "correct": bool(row.get("correct")), "changed": bool(row.get("changed"))}
              for row in result.get("finals") or []]
    decided = ""
    if int(result.get("confirmed_by") or 0):
        decided = msg.tour_review_decided(tour_person(tour, int(result["confirmed_by"])))
    elif result.get("auto"):
        decided = msg.tour_review_decided("", True)
    text = msg.tour_result(tour.number, tour.total, str(result.get("answer") or ""),
                           str(question.get("comment") or ""), list(result.get("winners") or []),
                           finals, tour_standings_line(tour), decided)
    await bot.send_message(chat_id=chat_id, text=text[:4090], parse_mode=ParseMode.HTML,
                           disable_web_page_preview=True)
    pictures = [a for a in (question.get("answer_attachments") or [])
                if isinstance(a, str) and a.startswith("http")]
    if pictures:
        try:
            await bot.send_photo(chat_id=chat_id, photo=pictures[0], caption="Javob rasmi")
        except TelegramError:
            pass

    previous_round = tour.current_round
    if tour.advance(now) is None:
        tour.phase = tn.PHASE_DONE
        tour.finished = True
        tour.bump()
        await tour_persist(bot, archive, tour)
        note("game_end", chat_id=chat_id, package_id=tour.package_id, games_finished=1,
             detail=f"turnir · {tour.title} · {tour.asked} savol · {tour.leader()}")
        await bot.send_message(chat_id=chat_id,
                               text=msg.tour_over(tour.title, tour.standings(), tour.asked,
                                                  int(tour.rounds or 1))[:4090],
                               parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    if int(tour.rounds or 1) > 1 and tour.current_round != previous_round:
        await bot.send_message(chat_id=chat_id,
                               text=msg.tour_round_banner(tour.current_round, int(tour.rounds or 1),
                                                          tour_standings_line(tour)),
                               parse_mode=ParseMode.HTML)
    await send_tour_question(bot, archive, chat_id, tour)


async def tour_resolve(bot, archive: Archive, chat_id: int, tour: tn.Tournament) -> None:
    """Close the open question — through a curator's confirmation when one is wanted.

    This is the single point where a tournament question ends, whether the clock
    ran out, the curator pressed «O‘tkazish» or a countdown hop arrived late.
    """
    if tour.reviewing():
        return                       # the list is already with the curators
    await tour_claim_strip(bot, chat_id, tour)
    now = int(time.time())
    if not tour.needs_confirm:
        result = tour.close(checker_for([]), now)
        tour.bump()
        await tour_publish(bot, archive, chat_id, tour, result, now)
        return
    await tour_review_open(bot, archive, chat_id, tour, now)


async def tour_start(bot, archive: Archive, chat_id: int, tour: tn.Tournament) -> bool:
    """Registration is over: build the playlist and open the first question."""
    bank = tour_bank(archive, tour)
    problems = tour.missing(len(bank), int(time.time()))
    if problems:
        return False
    chat_type = "supergroup"
    tour.can_delete = await detect_can_delete(bot, chat_id, chat_type)
    tour.open_mode = not tour.can_delete
    tour.start_at = 0                     # the wait is over
    tour_forget_plan(tour)
    tour.questions = tour.playlist(bank)
    tour.phase = tn.PHASE_PLAY
    tour.index = 0
    tour.bump()
    note("game_start", chat_id=chat_id, package_id=tour.package_id, games_started=1,
         detail=(f"turnir · {tour.title} · {len(tour.teams)} jamoa · {tour.people} kishi · "
                 f"{tour.total} savol"))
    first = tour.open_question(0)
    await bot.send_message(
        chat_id=chat_id,
        text=(msg.tour_started(tour.title, tour_teams_view(tour), tour.total, int(tour.timer),
                               not tour.open_mode) + msg.tour_started_private(tour.open_mode))[:4090],
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    if first is None:
        return False
    await send_tour_question(bot, archive, chat_id, tour)
    return True


async def tour_stop(bot, archive: Archive, chat_id: int, tour: tn.Tournament,
                    headline: str) -> None:
    """Stop the tournament and publish the table."""
    await tour_claim_strip(bot, chat_id, tour)
    if tour.reviewing():
        # Nobody should be left holding a list that can still change a finished game.
        result = {"winners": [row["team"] for row in tour.review_rows() if row.get("correct")]}
        await tour_review_close(bot, tour, result, False, "")
        tour.clear_review()
    tour.phase = tn.PHASE_DONE
    tour.finished = True
    tour.bump()
    tour_forget_plan(tour)
    note("stopped", chat_id=chat_id, package_id=tour.package_id, games_stopped=1,
         games_finished=1, detail=f"turnir · {tour.title} · {tour.leader()}")
    await tour_persist(bot, archive, tour)
    text = f"<b>{_esc(headline)}</b>\n\n" + msg.tour_stopped(
        tour.title, tour.asked, tour.total, tour.standings())
    await bot.send_message(chat_id=chat_id, text=text[:4090], parse_mode=ParseMode.HTML,
                           disable_web_page_preview=True)


async def schedule_tour_tick(tour: tn.Tournament, deadline: int) -> None:
    """Keep the tournament countdown moving, hop by hop, like the ordinary game."""
    if not TICKS_ENABLED or not tour.control_message_id or not tour.can_delete:
        return
    remaining = int(deadline) - int(time.time())
    if remaining <= 0:
        return
    step = max(1, min(int(TICK_SECONDS), remaining, 50))
    body = tour.body()
    chat_id = tour.chat_id
    if TICK_MODE == "bg" and bg_add(lambda: tour_tick_after_wait(body, chat_id, step)):
        return
    await post_tick(body, chat_id, step)


async def arm_reminder(chat_id: int, message_id: int, start_at: int, title: str = "",
                       wait: int = 0) -> bool:
    """Keep a scheduled tournament's reminder alive with short hops.

    Returns False when the start is still outside the arming window: nobody pays
    for a chain of hops three hours before the game, and the plan stays stored,
    so any later traffic opens the window again.
    """
    key = f"{int(chat_id)}:{int(message_id)}"
    if int(start_at) - int(time.time()) > REMINDER_ARM:
        _armed.discard(key)
        return False
    step = int(wait) if wait else sched.hop_step(int(start_at))
    body = sched.plan_body(chat_id, message_id, start_at, title)
    _armed.add(key)
    if TICK_MODE == "bg" and bg_add(lambda: reminder_after_wait(body, chat_id, step)):
        return True
    await post_tick(body, chat_id, step)
    return True


async def arm_due_reminders() -> int:
    """Lazy re-arming: whatever traffic arrives wakes the plans now due.

    A plan that ran out while every instance was cold is dropped here instead —
    the document holds twelve at most, and a stale one would otherwise sit in it
    forever and push out a real plan.
    """
    armed = 0
    dropped = 0
    try:
        rows = stats.plans()
    except Exception:
        return 0
    for row in rows:
        start_at = int(row.get("start") or 0)
        chat_id = int(row.get("chat") or 0)
        message_id = int(row.get("message") or 0)
        if sched.too_late(start_at):
            stats.plan_drop(chat_id, message_id)
            _armed.discard(f"{chat_id}:{message_id}")
            dropped += 1
            note("tour_plan_dropped", chat_id=chat_id,
                 detail=f"reja eskirgan · {start_at}")
            continue
        if not sched.due(start_at, window=REMINDER_ARM):
            continue
        key = f"{chat_id}:{message_id}"
        if key in _armed:
            continue
        try:
            if await arm_reminder(chat_id, message_id, start_at, str(row.get("title") or "")):
                armed += 1
        except Exception:
            logging.getLogger(__name__).debug("reminder arming failed", exc_info=True)
    if dropped:
        bg_add(lambda: stats.sync(force=True))
    return armed


async def reminder_after_wait(body: str, chat_id: int, wait: int) -> None:
    if wait:
        await asyncio.sleep(wait)
    try:
        await reminder_tick_work(body, chat_id)
    except Exception:
        logging.getLogger(__name__).warning("reminder hop failed", exc_info=True)


async def reminder_tick_work(body: str, chat_id: int) -> dict:
    """One hop of a scheduled start: wait, chain again, or announce the game."""
    global _reminders_seen, _reminders_fired
    _reminders_seen += 1
    plan = sched.plan_data(body)
    if not plan or not plan["chat"]:
        return {"ok": False, "reason": "no plan"}
    chat_id = int(plan["chat"] or chat_id or 0)
    message_id = int(plan["message"] or 0)
    start_at = int(plan["start"] or 0)
    key = f"{chat_id}:{message_id}"
    left = start_at - int(time.time())
    if left > REMINDER_ARM:
        _armed.discard(key)                    # somebody moved the start further away
        return {"ok": True, "stopped": "too early"}
    if left > 0:
        await arm_reminder(chat_id, message_id, start_at, plan["title"])
        return {"ok": True, "remaining": left}

    application = get_application()
    await application.initialize()
    try:
        bot = application.bot
        archive: Archive = application.bot_data.get("archive") or ARCHIVE
        tour = await tour_from_pin(bot, archive, chat_id)
        if tour is None:
            return await _reminder_done(chat_id, message_id, key, "no card")
        if tour.phase != tn.PHASE_REGISTER or int(tour.announced):
            return await _reminder_done(chat_id, message_id, key, "already started")
        sizes = tn.sizes()
        teams = tour_teams_view(tour)
        ready = tour.ready_to_start(tour_bank_size(archive, tour), int(time.time()))
        text = msg.tour_reminder(tour.title or plan["title"],
                                 tn.when_text(int(tour.start_at or start_at)), teams, sizes)
        sent = await bot.send_message(chat_id=chat_id, text=text[:4090],
                                      parse_mode=ParseMode.HTML,
                                      reply_markup=st.tour_keyboard(teams, tour.phase, ready,
                                                                    True, sizes))
        tour.announced = int(time.time())
        tour.card_message_id = getattr(sent, "message_id", tour.card_message_id)
        tour.bump()
        await tour_persist(bot, archive, tour)
        note("tour_reminder", chat_id=chat_id, package_id=tour.package_id,
             detail=f"turnir · {tour.title} · {len(tour.teams)} jamoa")
        _reminders_fired += 1
        return await _reminder_done(chat_id, message_id, key, "announced", announced=True)
    finally:
        await application.shutdown()


async def _reminder_done(chat_id: int, message_id: int, key: str, reason: str,
                         announced: bool = False) -> dict:
    """The chain ends here: forget the plan so it is never announced twice."""
    _armed.discard(key)
    stats.plan_drop(chat_id, message_id)
    bg_add(lambda: stats.sync(force=True))
    return {"ok": True, "announced": bool(announced), "stopped": reason}


async def tour_tick_after_wait(body: str, chat_id: int, wait: int) -> None:
    if wait:
        await asyncio.sleep(wait)
    try:
        await tour_tick_work(body, chat_id)
    except Exception:
        logging.getLogger(__name__).warning("tournament tick failed", exc_info=True)


async def tour_tick_work(body: str, chat_id: int) -> dict:
    """One hop of a tournament countdown: repaint, resolve or chain again."""
    global _ticks_seen, _ticks_painted, _ticks_resolved, _ticks_stopped
    _ticks_seen += 1
    tour = tn.Tournament.from_body(body)
    if tour is None or not chat_id:
        return {"ok": False, "reason": "no tournament"}
    application = get_application()
    await application.initialize()
    try:
        bot = application.bot
        archive: Archive = application.bot_data.get("archive") or ARCHIVE
        tour = tn.rebuild(tour, archive.questions(tour.package_id))
        # The pinned card knows about finals submitted after this hop was queued.
        pinned = await tour_from_pin(bot, archive, chat_id)
        if pinned is not None and pinned.question_id == tour.question_id \
                and int(pinned.rev) >= int(tour.rev):
            if pinned.control_message_id is None and tour.control_message_id:
                pinned.control_message_id = tour.control_message_id
            tour = pinned
        if tour.phase != tn.PHASE_PLAY:
            _ticks_stopped += 1
            return {"ok": True, "stopped": "not playing"}
        if tour.reviewing():
            # The question is closed and the verdicts are with a person: this hop
            # belongs to the confirmation window, not to the countdown any more.
            return await tour_review_tick(bot, archive, chat_id, tour)
        deadline = int(tour.deadline or 0)
        if deadline - int(time.time()) > MAX_TICK_HORIZON:
            return {"ok": False, "reason": "deadline too far ahead"}
        remaining = deadline - int(time.time())
        if remaining > 0:
            if not tour.control_message_id:
                _ticks_stopped += 1
                return {"ok": True, "stopped": "no strip"}
            try:
                await paint_tour_strip(bot, chat_id, tour, remaining)
            except TelegramError as exc:
                text = str(exc).lower()
                if "not modified" in text:
                    pass
                elif isinstance(exc, RetryAfter) or "flood" in text:
                    pause = int(getattr(exc, "retry_after", 0) or 0) + TICK_SECONDS * 2
                    await schedule_tour_tick(tour, deadline)
                    return {"ok": True, "throttled": pause}
                else:
                    _ticks_stopped += 1
                    return {"ok": True, "stopped": "strip closed"}
            _ticks_painted += 1
            await schedule_tour_tick(tour, deadline)
            return {"ok": True, "remaining": remaining}
        if not await tour_claim_strip(bot, chat_id, tour):
            _ticks_stopped += 1
            return {"ok": True, "stopped": "already resolved"}
        _ticks_resolved += 1
        await tour_resolve(bot, archive, chat_id, tour)
        if tour.reviewing():
            # The question is closed but nothing is published yet: the verdicts
            # are waiting for a person, and the window has its own hops now.
            return {"ok": True, "review": True, "left": tour.review_left(int(time.time()))}
        return {"ok": True, "resolved": True}
    finally:
        await application.shutdown()


def tour_pick_page(archive: Archive, tour: tn.Tournament) -> tuple[list[dict], int, int]:
    """One page of the curator's question picker."""
    pool = archive.questions(tour.package_id)
    needle = (tour.query or "").strip().casefold()
    if needle:
        pool = [item for item in pool if needle in str(item.get("text") or "").casefold()]
    pages = max(1, -(-len(pool) // TOUR_PICK_PER_PAGE))
    page = min(max(1, int(tour.page or 1)), pages)
    start = (page - 1) * TOUR_PICK_PER_PAGE
    chosen = {int(item) for item in tour.include}
    dropped = {int(item) for item in tour.exclude}
    rows = [{"id": int(item["id"]), "text": str(item.get("text") or ""),
             "included": int(item["id"]) in chosen, "excluded": int(item["id"]) in dropped}
            for item in pool[start:start + TOUR_PICK_PER_PAGE]]
    return rows, page, pages


async def tour_render_picker(context: ContextTypes.DEFAULT_TYPE, bot, tour: tn.Tournament,
                             query=None, message=None, allow_edit: bool = True) -> None:
    """Show (or rewrite) the include/exclude picker.

    ``allow_edit`` is False when the button that opened it sits on the pinned
    card: that message belongs to the tournament itself and must stay the card.
    """
    archive = _archive_of(context)
    rows, page, pages = tour_pick_page(archive, tour)
    tour.page = page
    info = archive.package_info(tour.package_id) or {}
    chosen = {int(item["id"]) for item in rows if item["included"]}
    dropped = {int(item["id"]) for item in rows if item["excluded"]}
    text = msg.tour_picker(rows, tour.query, page, pages, len(tour.include), len(tour.exclude),
                           tour_bank_size(archive, tour), info.get("title") or "")[:4000]
    keyboard = st.selection_keyboard(page, pages, rows, chosen, dropped, tour.query)
    body = tour_state_text(text, tour)
    if query is not None and allow_edit:
        try:
            await query.edit_message_text(text=body, parse_mode=ParseMode.HTML,
                                          reply_markup=keyboard,
                                          disable_web_page_preview=True)
            return
        except TelegramError:
            pass
    await bot.send_message(chat_id=tour.chat_id, text=body, parse_mode=ParseMode.HTML,
                           reply_markup=keyboard, disable_web_page_preview=True)


async def tour_render_curator_panel(context: ContextTypes.DEFAULT_TYPE, bot,
                                    tour: tn.Tournament, query=None, message=None,
                                    allow_edit: bool = True) -> None:
    archive = _archive_of(context)
    info = archive.package_info(tour.package_id) or {}
    bank = tour_bank_size(archive, tour)
    text = msg.tour_curator_panel(tour.title, tour.package_id, info.get("title") or "", bank,
                                  int(tour.rounds or 1), int(tour.per_round or 0),
                                  int(tour.timer), tour_total(archive, tour), int(tour.seed),
                                  len(tour.include), len(tour.exclude),
                                  len(tour.curators))[:4000]
    keyboard = st.curator_keyboard(int(tour.rounds or 1), int(tour.per_round or 0),
                                   int(tour.timer), int(tour.package_id), int(tour.seed), bank,
                                   tour_total(archive, tour), bool(tour.needs_confirm))
    body = tour_state_text(text, tour)
    if query is not None and allow_edit:
        try:
            await query.edit_message_text(text=body, parse_mode=ParseMode.HTML,
                                          reply_markup=keyboard,
                                          disable_web_page_preview=True)
            return
        except TelegramError:
            pass
    await bot.send_message(chat_id=tour.chat_id, text=body, parse_mode=ParseMode.HTML,
                           reply_markup=keyboard, disable_web_page_preview=True)


def clamp_choice(value: int, options, fallback: int) -> int:
    """The nearest setting the game actually offers, so a typed number is safe."""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return int(fallback)
    allowed = [int(item) for item in options]
    if wanted in allowed:
        return wanted
    return min(allowed, key=lambda item: (abs(item - wanted), item))


async def tour_new(bot, archive: Archive, chat_id: int, user_id: int, argument: str,
                   chat=None, spec: dict | None = None) -> tn.Tournament:
    """A fresh tournament: package resolved like /solo, structure at defaults.

    ``spec`` is a parsed composer line (``sched.parse_spec``), which is how one
    command can carry the package, the start time, the team cap and the shape of
    the game at once.
    """
    spec = spec if isinstance(spec, dict) else sched.parse_spec(argument)
    wanted = str(spec.get("package") or spec.get("text") or "").strip()
    package_id = 0
    title = TOUR_DEFAULT_TITLE
    if wanted:
        digits = wanted.split()[0]
        if digits.isdigit() and archive.package_info(int(digits)):
            package_id = int(digits)
        else:
            found = archive.search(wanted, limit=1)
            if found:
                package_id = int(found[0]["id"])
    if not package_id:
        package_id = archive.random_package_id()
    info = archive.package_info(package_id) or {}
    tour = tn.Tournament(chat_id=chat_id, title=(info.get("title") or title)[:40],
                         package_id=package_id, creator=int(user_id), timer=TIMER,
                         rounds=1, per_round=10, seed=random.randrange(1, 999999))
    if spec.get("title"):
        tour.title = str(spec["title"])[:40]
    if spec.get("rounds"):
        tour.rounds = clamp_choice(spec["rounds"], st.TOUR_ROUNDS, 1)
    if spec.get("per_round") is not None:
        tour.per_round = clamp_choice(spec["per_round"], st.TOUR_PER_ROUND, 10)
    if spec.get("timer"):
        tour.timer = clamp_choice(spec["timer"], st.TOUR_TIMERS, TIMER)
    if spec.get("teams"):
        tour.max_teams = max(tn.MIN_TEAMS, min(int(spec["teams"]), tn.MAX_TEAMS))
    tour.start_at = int(spec.get("start_at") or 0)
    tour.can_delete = await detect_can_delete(bot, chat_id,
                                              getattr(chat, "type", "supergroup") or "supergroup")
    return tour


async def tour_register_plan(bot, archive: Archive, tour: tn.Tournament) -> bool:
    """A tournament with a start time: store the reminder and arm it if it is near.

    The card pinned in the group stays the authority — it carries the package, the
    shape of the game and the teams — so the plan itself only has to say which
    message to announce and when.
    """
    if not tour.start_at or tour.phase != tn.PHASE_REGISTER or not tour.card_message_id:
        return False
    stats.plan_add(int(tour.chat_id), int(tour.card_message_id), int(tour.start_at),
                   str(tour.title or ""))
    stats.group_seen(int(tour.chat_id), "")
    bg_add(lambda: stats.sync(force=True))
    return await arm_reminder(int(tour.chat_id), int(tour.card_message_id),
                              int(tour.start_at), str(tour.title or ""))


def tour_forget_plan(tour: tn.Tournament) -> None:
    """The game started, stopped or moved: the reminder is no longer needed."""
    if not tour.card_message_id:
        return
    stats.plan_drop(int(tour.chat_id), int(tour.card_message_id))
    bg_add(lambda: stats.sync(force=True))


async def cmd_tour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/turnir — open the tournament card (admin, owner or an appointed curator)."""
    chat = update.effective_chat
    if chat is None or getattr(chat, "type", "private") == "private":
        await update.effective_message.reply_text(
            "Turnir faqat guruhda o‘tkaziladi. Botni guruhga qo‘shing va u yerda "
            "<code>/turnir</code> deb yozing.", parse_mode=ParseMode.HTML)
        return
    argument = " ".join(context.args or []).strip()
    if argument.casefold() in {"qoidalar", "rules"}:
        await update.effective_message.reply_text(msg.tour_rules_text(tn.sizes()),
                                                  parse_mode=ParseMode.HTML)
        return
    if argument.casefold() in {"yordam", "help", "?"}:
        await update.effective_message.reply_text(msg.tour_help(), parse_mode=ParseMode.HTML)
        return
    existing = await tour_state(update, context)
    if existing is not None and not argument:
        # Turnir allaqachon ochilgan: kartani yangilab, buyruqlarni eslatamiz.
        await tour_persist(context.bot, _archive_of(context), existing)
        await update.effective_message.reply_text(
            "Bu guruhda turnir allaqachon ochilgan — karta yangilandi.\n"
            "• Holat: <code>/jadval</code> · qoidalar: <code>/turnir qoidalar</code>\n"
            "• Yangi turnir: <code>/turnir 959</code> (paket bilan)",
            parse_mode=ParseMode.HTML)
        return
    if not await tour_curator(update, context, existing):
        await update.effective_message.reply_text(
            msg.tour_denied_action("turnir ochish"), parse_mode=ParseMode.HTML)
        return
    archive = _archive_of(context)
    spec = sched.parse_spec(argument)
    if spec.get("error"):
        await update.effective_message.reply_text(
            f"{spec['error']}\n\n{msg.tour_spec_help()}", parse_mode=ParseMode.HTML)
        return
    stats.group_seen(chat.id, getattr(chat, "title", "") or "")
    tour = await tour_new(context.bot, archive, chat.id, update.effective_user.id,
                          argument, chat, spec=spec)
    note("command", user_id=update.effective_user.id, chat_id=chat.id,
         detail=f"turnir · {tour.package_id}", log=False)
    await tour_persist(context.bot, archive, tour)
    await tour_register_plan(context.bot, archive, tour)
    lines = ["Turnir ochildi: ro‘yxat xabari yuborildi va mahkamlandi (pin)."]
    if tour.start_at:
        lines.append(f"• Boshlanish: <b>{msg.esc(tn.when_text(tour.start_at))}</b> — o‘sha paytda "
                     "bot shu guruhga eslatma va «Boshlash» tugmasini yuboradi")
    lines += [
        f"• Jamolar: ko‘pi bilan {tour.team_limit} ta · yaratish: «Yangi jamoa» tugmasi "
        "yoki <code>/jamoa nom</code>",
        "• Sozlamalar va savol tanlash: kartadagi tugmalar (faqat kurator)",
        "• Har savoldan keyin javoblar ro‘yxati kuratorga boradi va ochko "
        "tasdiqdan keyin beriladi (sozlamalarda o‘chiriladi)",
        "• Savollar va e’lon qilingan javoblar guruhdagi hammaga ko‘rinadi: "
        "<code>/savollar</code>",
        "• Qoidalar: <code>/turnir qoidalar</code>",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/jamoa <nom> — create a team; /jamoa alone shows your own team."""
    tour = await tour_state(update, context)
    if tour is None:
        await update.effective_message.reply_text(msg.tour_no_tournament(),
                                                  parse_mode=ParseMode.HTML)
        return
    archive = _archive_of(context)
    user = update.effective_user
    name = " ".join(context.args or []).strip()
    if not name:
        mine = tour.team_of(user.id)
        if mine is None:
            await update.effective_message.reply_text(msg.tour_not_registered())
            return
        ready = mine.ready_count
        await update.effective_message.reply_text(
            f"Sizning jamoangiz: <b>{msg.esc(mine.name)}</b> · {mine.size} kishi · "
            f"{ready} tayyor", parse_mode=ParseMode.HTML)
        return
    if tour.phase != tn.PHASE_REGISTER:
        await update.effective_message.reply_text(
            "Turnir boshlangan — yangi jamoa qo‘shib bo‘lmaydi.", parse_mode=ParseMode.HTML)
        return
    tour.see(user.id, tour_sender(user), getattr(user, "username", "") or "")
    team, problem = tour.add_team(name, user.id, tour_sender(user),
                                  getattr(user, "username", "") or "")
    if problem:
        await update.effective_message.reply_text(problem)
        return
    tour.clear_pending()
    tour.bump()
    await tour_persist(context.bot, archive, tour)
    await update.effective_message.reply_text(
        msg.tour_team_created(team.name, tour_sender(user), tn.sizes()),
        parse_mode=ParseMode.HTML)


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/qoshil <nom yoki raqam> — join a team."""
    tour = await tour_state(update, context)
    if tour is None:
        await update.effective_message.reply_text(msg.tour_no_tournament(),
                                                  parse_mode=ParseMode.HTML)
        return
    user = update.effective_user
    argument = " ".join(context.args or []).strip()
    position = -1
    if argument.isdigit():
        position = int(argument) - 1
    elif argument:
        team = tour.team(argument)
        position = tour.index_of(team)
    else:
        mine = tour.team_of(user.id)
        position = tour.index_of(mine)
    if position < 0:
        names = " · ".join(f"{index + 1}. {team.name}" for index, team in enumerate(tour.teams))
        await update.effective_message.reply_text(
            "Jamoa topilmadi. Ro‘yxat: " + (names or "(bo‘sh)") +
            "\nQo‘shilish: <code>/qoshil 2</code> yoki <code>/qoshil Zakovat</code>",
            parse_mode=ParseMode.HTML)
        return
    tour.see(user.id, tour_sender(user), getattr(user, "username", "") or "")
    team, problem = tour.join(position, user.id, tour_sender(user),
                              getattr(user, "username", "") or "", ready=False)
    if team is None:
        await update.effective_message.reply_text(problem or "Qo‘shilib bo‘lmadi.")
        return
    tour.bump()
    await tour_persist(context.bot, _archive_of(context), tour)
    if problem:
        await update.effective_message.reply_text(problem)
        return
    sizes = tn.sizes()
    await update.effective_message.reply_text(
        msg.tour_joined(team.name, tour_sender(user), team.size, team.ready_count, sizes),
        parse_mode=ParseMode.HTML)
    if team.size >= sizes["min_members"] and team.ready_count < sizes["min_ready"]:
        await update.effective_message.reply_text(
            f"Endi kamida {sizes['min_ready']} kishi «Men tayyorman» tugmasini bosishi "
            "yoki <code>/tayyor</code> deb yozishi kerak.", parse_mode=ParseMode.HTML)


async def cmd_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tayyor — «Men tayyorman»."""
    tour = await tour_state(update, context)
    if tour is None:
        await update.effective_message.reply_text(msg.tour_no_tournament(),
                                                  parse_mode=ParseMode.HTML)
        return
    user = update.effective_user
    team = tour.team_of(user.id)
    if team is None:
        await update.effective_message.reply_text(msg.tour_not_registered())
        return
    problem = team.set_ready(user.id, True)
    if problem:
        await update.effective_message.reply_text(problem)
        return
    tour.see(user.id, tour_sender(user), getattr(user, "username", "") or "")
    tour.bump()
    await tour_persist(context.bot, _archive_of(context), tour)
    sizes = tn.sizes()
    line = (f"<b>MEN TAYYORMAN</b> · {msg.esc(team.name)}\n"
            f"• Jamoada {team.size} kishi · {team.ready_count} tayyor")
    if team.size < sizes["min_members"]:
        line += f"\n• Yana {sizes['min_members'] - team.size} kishi kerak"
    elif team.ready_count < sizes["min_ready"]:
        line += f"\n• Yana {sizes['min_ready'] - team.ready_count} kishi tayyorligini aytsin"
    else:
        line += "\n• Jamoa tayyor"
    await update.effective_message.reply_text(line, parse_mode=ParseMode.HTML)


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chiqish — leave your team."""
    tour = await tour_state(update, context)
    if tour is None:
        await update.effective_message.reply_text(msg.tour_no_tournament(),
                                                  parse_mode=ParseMode.HTML)
        return
    user = update.effective_user
    team = tour.team_of(user.id)
    name = team.name if team is not None else ""
    result, problem = tour.leave(user.id)
    if problem and result is None and team is None:
        await update.effective_message.reply_text(problem)
        return
    tour.bump()
    await tour_persist(context.bot, _archive_of(context), tour)
    dropped = result is None
    await update.effective_message.reply_text(
        msg.tour_left(name, tour_sender(user), dropped), parse_mode=ParseMode.HTML)


async def tour_prompt_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Take the next plain message from the person a button asked.

    «Yangi jamoa», «Qidirish» and «Paketni almashtirish» used to answer with an
    instruction and wait for a command. Now the button asks, the card remembers
    who was asked, and the very next ordinary message that person writes in the
    group is the answer — the same code runs as behind ``/jamoa``, ``/tanlov``
    and ``/turnir``, so nothing about those flows changes. Everybody else's
    messages are left completely alone.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return False
    if getattr(chat, "type", "private") == "private":
        return False
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return False
    archive = _archive_of(context)
    if not await tour_active(context.bot, archive, int(chat.id)):
        return False
    tour = await tour_state(update, context)
    if tour is None:
        return False
    kind, asked = tour.pending_of()
    if not kind or int(asked) != int(user.id):
        return False

    if kind in (tn.PROMPT_TEAM, tn.PROMPT_SEARCH):
        tour.clear_pending()
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        context.args = [text]
        try:
            if kind == tn.PROMPT_TEAM:
                await cmd_team(update, context)          # what /jamoa <nom> does
            else:
                await cmd_selection(update, context)     # what /tanlov <so‘z> does
        finally:
            context.args = []
        return True

    if tour.phase != tn.PHASE_REGISTER:
        tour.clear_pending()
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await message.reply_text(msg.tour_package_locked(), parse_mode=ParseMode.HTML)
        return True
    resolved = _resolve_package_strict(context, text)
    if not resolved:
        # The question stays open: a typo should not cost another button press.
        await message.reply_text(msg.tour_package_unknown(text), parse_mode=ParseMode.HTML)
        return True
    package_id, title = resolved
    info = archive.package_info(int(package_id)) or {}
    previous = int(tour.package_id or 0)
    tour.package_id = int(package_id)
    tour.include = []
    tour.exclude = []
    tour.query = ""
    tour.page = 1
    tour.seed = random.randrange(1, 999999)
    tour.clear_pending()
    tour.bump()
    await tour_persist(context.bot, archive, tour)
    note("tour_package_swapped", user_id=int(user.id), chat_id=int(chat.id),
         package_id=int(package_id),
         detail=f"turnir · {tour.title} · tugma orqali")
    await message.reply_text(
        msg.tour_package_swapped(str(info.get("title") or title or package_id),
                                 int(package_id), int(info.get("questions") or 0),
                                 tour_sender(user), previous),
        parse_mode=ParseMode.HTML)
    await tour_render_curator_panel(context, context.bot, tour)
    return True


async def tour_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Keep a team's chat inside the team while a tournament question is open.

    Anything a registered member types in the group is removed from the group
    (when the bot may delete) and delivered privately to that member's own team,
    matched by id and username — the others see nothing at all. True when the
    message was handled, so the ordinary game logic does not touch it.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return False
    if getattr(chat, "type", "private") == "private":
        return False
    archive = _archive_of(context)
    if not await tour_active(context.bot, archive, chat.id):
        return False
    tour = await tour_state(update, context)
    if tour is None or tour.phase != tn.PHASE_PLAY:
        return False
    team = tour.team_of(int(user.id))
    if team is None:
        return False
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return False
    tour.see(int(user.id), tour_sender(user), getattr(user, "username", "") or "")
    tour.bump()
    removed = await tour_delete(context.bot, chat.id, message.message_id, tour.can_delete)
    delivered, failed = await tour_private(
        context.bot, [person for person in team.members if int(person.user_id) != int(user.id)],
        msg.tour_private_note(team.name, f"{tour_sender(user)}: {text}"))
    remaining = max(0, int(tour.deadline or 0) - int(time.time()))
    hint = ("Yakuniy javob uchun: tasmaga reply qilib <code>/yakuniy javob</code>."
            if remaining > 0 else msg.tour_final_deadline())
    await context.bot.send_message(
        chat_id=int(user.id),
        text=(msg.tour_private_note(team.name, text) + "\n\n" + hint +
              f"\nYuborildi: {delivered} a’zoga" + (f" · yetib bormadi: {failed}" if failed else "")
              + ("" if removed else "\nGuruhdan o‘chirib bo‘lmadi: botda huquq yo‘q.")),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await tour_persist(context.bot, archive, tour)
    return True


_tour_probe: dict[int, tuple[float, bool]] = {}
TOUR_PROBE_TTL = 45        # sekund: oddiy guruh xabari har safar getChat qilmasin


async def tour_active(bot, archive: Archive, chat_id: int) -> bool:
    """Is a tournament running in this chat? Cached — a chat without one must not
    pay a Telegram call for every message somebody types."""
    now = time.time()
    hit = _tour_probe.get(int(chat_id))
    if hit is not None and now - hit[0] < TOUR_PROBE_TTL:
        return hit[1]
    found = await tour_from_pin(bot, archive, chat_id)
    active = bool(found is not None and found.phase in (tn.PHASE_PLAY, tn.PHASE_REGISTER))
    _tour_probe[int(chat_id)] = (now, active)
    return active


async def cmd_final(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/yakuniy <javob> — the team's one final answer, editable until time is up."""
    message = update.effective_message
    user = update.effective_user
    chat_id = update.effective_chat.id
    tour = await tour_state(update, context)
    if tour is None:
        await message.reply_text(msg.tour_no_tournament(), parse_mode=ParseMode.HTML)
        return
    if tour.control_message_id is None:
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            tour.control_message_id = getattr(reply, "message_id", None)
    if tour.phase != tn.PHASE_PLAY or tour.question is None:
        await message.reply_text(msg.tour_final_closed())
        return
    text = " ".join(context.args or []).strip()
    if not text:
        mine = tour.team_of(int(user.id))
        state = (f"Sizning jamoangiz: {mine.name} · hozirgi yakuniy javob: "
                 f"{mine.final or '(yo‘q)'}") if mine is not None else msg.tour_not_registered()
        await message.reply_text(msg.final_usage() + "\n\n" + state, parse_mode=ParseMode.HTML)
        return
    now = int(time.time())
    if int(tour.deadline or 0) and now > int(tour.deadline or 0):
        await message.reply_text(msg.tour_final_deadline())
        await tour_delete(context.bot, update.effective_chat.id, message.message_id,
                          tour.can_delete)
        return
    team, problem = tour.submit_final(user.id, text, now)
    if team is None or problem:
        await message.reply_text(msg.tour_final_rejected(problem or "javob qabul qilinmadi"),
                                 parse_mode=ParseMode.HTML)
        return
    tour.see(user.id, tour_sender(user), getattr(user, "username", "") or "")
    tour.bump()
    remaining = max(0, int(tour.deadline) - now)
    changed = int(team.attempts) > 1
    # Javob guruhda qolmasligi kerak: imkon bo‘lsa xabar o‘chiriladi.
    removed = await tour_delete(context.bot, chat_id, message.message_id, tour.can_delete)
    if not removed and not tour.open_mode:
        tour.open_mode = True
    await tour_team_note(context.bot, tour, team,
                         msg.tour_final_set(team.name, team.final, tour_sender(user),
                                            remaining, changed))
    if tour.control_message_id and remaining > 0:
        try:
            await paint_tour_strip(context.bot, chat_id, tour, remaining)
        except TelegramError:
            pass
    await tour_persist(context.bot, _archive_of(context), tour, merge_finals=True)
    if not removed:
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg.answer_dropped(getattr(update.effective_chat, "title", "") or "guruh"),
            parse_mode=ParseMode.HTML)


async def cmd_final_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bekor — take the team's final answer back (while time is left)."""
    message = update.effective_message
    user = update.effective_user
    tour = await tour_state(update, context)
    if tour is None:
        await message.reply_text(msg.tour_no_tournament(), parse_mode=ParseMode.HTML)
        return
    if tour.phase != tn.PHASE_PLAY:
        await message.reply_text(msg.tour_final_closed())
        return
    team = tour.team_of(user.id)
    if team is None:
        await message.reply_text(msg.tour_not_registered())
        return
    if int(time.time()) > int(tour.deadline or 0):
        await message.reply_text(msg.tour_final_deadline())
        return
    had = bool(team.final)
    team.clear_final(int(time.time()))
    tour.bump()
    await tour_delete(context.bot, update.effective_chat.id, message.message_id, tour.can_delete)
    if had:
        await tour_team_note(context.bot, tour, team,
                             msg.final_cleared(team.name, tour_sender(user)))
    remaining = max(0, int(tour.deadline) - int(time.time()))
    if tour.control_message_id and remaining > 0:
        try:
            await paint_tour_strip(context.bot, update.effective_chat.id, tour, remaining)
        except TelegramError:
            pass
    await tour_persist(context.bot, _archive_of(context), tour, merge_finals=True)


def _whisper_match(person, needle: str) -> bool:
    """Does this member answer to ``needle`` — an id, a @username or a name?"""
    lowered = str(needle or "").strip().casefold().lstrip("@")
    if not lowered:
        return False
    if lowered.isdigit() and int(lowered) == int(getattr(person, "user_id", 0) or 0):
        return True
    username = str(getattr(person, "username", "") or "").casefold().lstrip("@")
    if username and username == lowered:
        return True
    name = str(getattr(person, "name", "") or "").strip().casefold()
    return bool(name) and name == lowered


def tour_whisper_targets(tour: "tn.Tournament | None", user_id: int, args: list,
                         curator: bool) -> tuple[list, str, str, str]:
    """Who a whisper may reach, and what they get.

    A whisper never leaves the tournament. The only people who can receive one
    are the sender's own teammates — plus, for a curator or an admin, any
    registered team. An id or a username that is not on the roster is refused
    instead of delivered, so nothing can be pushed to somebody outside the
    group, and the group itself never learns what was written.

    Returns ``(targets, body, problem, label)``. When the first word is not an
    address at all — ``/pichoq tez javob bering``, the usual case — the whole
    message goes to the sender's own team.
    """
    words = [str(item) for item in (args or []) if str(item).strip()]
    if not words:
        return [], "", "Matn yozilmadi: /pichoq salom", ""
    first = words[0].strip()
    rest = " ".join(words[1:]).strip()
    lowered = first.casefold().lstrip("@")
    mine = tour.team_of(int(user_id)) if tour is not None else None
    teams = list(getattr(tour, "teams", []) or [])

    def refuse(reason: str) -> tuple[list, str, str, str]:
        return [], "", reason, ""

    def whole(team) -> list:
        return [person for person in team.members if int(person.user_id) != int(user_id)]

    # 1) "jamoa" — the sender's own team, whatever follows is the message
    if lowered in {"jamoa", "jamoam", "team", "hamma", "biz", "everyone"}:
        if mine is None:
            return refuse("Siz biror jamoada emassiz. /jamoa Nom — jamoa yaratish, "
                          "/qoshil Nom — qo‘shilish." if not curator else
                          "Siz biror jamoada emassiz. Jamoa nomini yozing: "
                          "/pichoq JamoaNomi matn")
        if not rest:
            return refuse("Matn yozilmadi: /pichoq jamoa salom")
        return whole(mine), rest, "", mine.name

    # 2) a team by name — any team for a curator, yours for everybody else
    for team in teams:
        if str(team.name).strip().casefold() == lowered:
            if not curator and (mine is None or team.name != mine.name):
                return refuse("Boshqa jamoaga yoza olmaysiz: maxfiy xabar faqat o‘z "
                              "jamoangizga ketadi.")
            if not rest:
                return refuse(f"Matn yozilmadi: /pichoq {team.name} salom")
            return whole(team), rest, "", team.name

    # 3) one person — only from the roster, and only from your own team
    for team in teams:
        for person in team.members:
            if not _whisper_match(person, first):
                continue
            if int(person.user_id) == int(user_id):
                return refuse("Bu o‘zingiz. /pichoq matn — jamoangiz ko‘radi.")
            if not curator and (mine is None or team.name != mine.name):
                return refuse(f"{person.label()} boshqa jamoada ({team.name}): maxfiy "
                              "xabar faqat o‘z jamoangiz a’zolariga ketadi.")
            if not rest:
                return refuse("Matn yozilmadi: /pichoq " + first + " salom")
            return [person], rest, "", person.label()

    # 4) the first word was not an address: the whole line goes to the team
    if not (first.startswith("@") or lowered.isdigit()):
        if mine is None:
            return refuse("Siz biror jamoada emassiz. /jamoa Nom — jamoa yaratish, "
                          "/qoshil Nom — qo‘shilish." if not curator else
                          "Manzil topilmadi. Jamoa nomini yozing: /pichoq JamoaNomi matn")
        return whole(mine), " ".join(words).strip(), "", mine.name

    # 5) an address that is not on the roster — refused, never delivered
    if mine is None and not curator:
        return refuse("Avval jamoaga qo‘shiling: /jamoa Nom yoki /qoshil Nom")
    return refuse(f"{first} turnirda qatnashmaydi. /azolar — o‘z jamoangiz a’zolari.")


async def cmd_whisper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pichoq <matn|@a’zo|jamoa> — a message only the sender's own team sees.

    Two things are guaranteed here: the group never keeps a readable copy, and
    nobody outside the sender's team ever receives one. That is why the message
    is removed *before* anything is delivered, and why an address that is not on
    the roster is refused rather than looked up somewhere else.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    args = list(context.args or [])
    if not args:
        await message.reply_text(msg.whisper_usage(), parse_mode=ParseMode.HTML)
        return
    tour = await tour_state(update, context)
    if tour is None:
        await message.reply_text(msg.whisper_no_tournament(), parse_mode=ParseMode.HTML)
        return
    sender = tour_sender(user)
    title = getattr(chat, "title", "") or "guruh"
    tour.see(int(user.id), sender, getattr(user, "username", "") or "")

    # The group copy goes first: if it cannot be removed, nothing is delivered.
    removed = await tour_delete(context.bot, chat.id, message.message_id, bool(tour.can_delete))
    if not removed:
        if not tour.open_mode:
            tour.open_mode = True
            await context.bot.send_message(chat_id=chat.id, text=msg.whisper_dropped(title),
                                           parse_mode=ParseMode.HTML)
        tour.bump()
        await tour_persist(context.bot, _archive_of(context), tour)
        await context.bot.send_message(chat_id=int(user.id),
                                       text=msg.whisper_not_private(" ".join(args)),
                                       parse_mode=ParseMode.HTML)
        return

    curator = bool(await tour_curator(update, context, tour))
    targets, body, problem, label = tour_whisper_targets(tour, int(user.id), args, curator)
    tour.bump()
    await tour_persist(context.bot, _archive_of(context), tour)
    if not targets:
        # Refused in private: a reply in the group would quote the message back.
        await context.bot.send_message(
            chat_id=int(user.id), text=msg.whisper_outside(problem or "Manzil topilmadi."),
            parse_mode=ParseMode.HTML)
        return

    note = msg.whisper_received(sender, body, title)
    delivered, failed = await tour_private(context.bot, targets, note)
    if delivered:
        text = (msg.whisper_team_sent(label, delivered, failed, body) if len(targets) > 1
                else msg.whisper_sent(label, body))
    else:
        text = msg.whisper_failed("manzil bot bilan shaxsiy chatni boshlamagan (/start)")
    await context.bot.send_message(chat_id=int(user.id), text=text, parse_mode=ParseMode.HTML)


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/azolar — your own team with ids; a curator sees every team.

    The list is what makes /pichoq usable, so it shows exactly the people a
    whisper may reach: a member sees their own teammates, a curator or admin
    sees the whole tournament plus the rest of the group the bot has seen.
    """
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    tour = await tour_state(update, context)
    if tour is None:
        await message.reply_text(msg.tour_no_tournament(), parse_mode=ParseMode.HTML)
        return
    title = getattr(chat, "title", "") or "guruh"
    tour.see(int(user.id), tour_sender(user), getattr(user, "username", "") or "")
    curator = bool(await tour_curator(update, context, tour))
    mine = tour.team_of(int(user.id))
    teams = list(tour.teams) if curator else ([mine] if mine is not None else [])
    extra: list[dict] = []
    if curator:
        known = {int(person.user_id) for team in tour.teams for person in team.members}
        extra = [row for row in tour.directory_rows()
                 if int(row.get("id") or 0) not in known]
    tour.bump()
    await tour_persist(context.bot, _archive_of(context), tour)
    await message.reply_text(
        msg.members_list(teams, title, int(user.id), extra)[:4090], parse_mode=ParseMode.HTML)


async def cmd_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/jadval — the score table at any moment."""
    tour = await tour_state(update, context)
    if tour is None:
        await update.effective_message.reply_text(msg.tour_no_tournament(),
                                                  parse_mode=ParseMode.HTML)
        return
    heading = "YAKUNIY JADVAL" if tour.phase == tn.PHASE_DONE else "JADVAL"
    await update.effective_message.reply_text(
        msg.tour_standings(tour.standings(), tour.title, heading)[:4090],
        parse_mode=ParseMode.HTML)


async def cmd_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tanlov [so‘z] — search the package and open the include/exclude picker."""
    tour = await tour_state(update, context)
    if tour is None:
        await update.effective_message.reply_text(msg.tour_no_tournament(),
                                                  parse_mode=ParseMode.HTML)
        return
    if not await tour_curator(update, context, tour):
        await update.effective_message.reply_text(msg.tour_denied_action("savol tanlash"),
                                                  parse_mode=ParseMode.HTML)
        return
    query = " ".join(context.args or []).strip()
    if query:
        tour.query = query[:24]
        tour.page = 1
    rows, page, pages = tour_pick_page(_archive_of(context), tour)
    if query and not rows:
        info = _archive_of(context).package_info(tour.package_id) or {}
        await update.effective_message.reply_text(
            msg.tour_search_empty(query, info.get("title") or ""), parse_mode=ParseMode.HTML)
        return
    tour.bump()
    await tour_persist(context.bot, _archive_of(context), tour)
    await tour_render_picker(context, context.bot, tour, message=update.effective_message,
                             allow_edit=False)


async def _selection_ids(args: list) -> list[int]:
    ids: list[int] = []
    for chunk in args:
        for piece in str(chunk).replace(",", " ").split():
            if piece.isdigit():
                value = int(piece)
                if value and value not in ids:
                    ids.append(value)
    return ids


async def cmd_include(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/olish <id…> — put exact questions into the tournament."""
    await _selection_change(update, context, include=True)


async def cmd_exclude(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chiqarish <id…> — take exact questions out of the tournament."""
    await _selection_change(update, context, include=False)


async def _selection_change(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            include: bool) -> None:
    message = update.effective_message
    tour = await tour_state(update, context)
    if tour is None:
        await message.reply_text(msg.tour_no_tournament(), parse_mode=ParseMode.HTML)
        return
    if not await tour_curator(update, context, tour):
        await message.reply_text(msg.tour_denied_action("savol tanlash"),
                                 parse_mode=ParseMode.HTML)
        return
    archive = _archive_of(context)
    ids = await _selection_ids(context.args or [])
    if not ids:
        await message.reply_text(
            "Id ko‘rsatilmadi. Masalan: <code>/olish 12345 12346</code> yoki "
            "<code>/chiqarish 12347</code>", parse_mode=ParseMode.HTML)
        return
    known: list[int] = []
    unknown: list[int] = []
    for question_id in ids[:tn.SELECTION_LIMIT]:
        if archive.question(question_id) is None:
            unknown.append(question_id)
            continue
        known.append(question_id)
        if include:
            if question_id not in tour.include:
                tour.include.append(question_id)
            tour.exclude = [item for item in tour.exclude if item != question_id]
        else:
            if question_id not in tour.exclude:
                tour.exclude.append(question_id)
            tour.include = [item for item in tour.include if item != question_id]
    tour.include = tour.include[-tn.SELECTION_LIMIT:]
    tour.exclude = tour.exclude[-tn.SELECTION_LIMIT:]
    tour.bump()
    await tour_persist(context.bot, archive, tour)
    bank = tour_bank_size(archive, tour)
    text = msg.tour_selection_done(tour.include if include else tour.exclude,
                                   tour.exclude if include else tour.include, bank)
    if unknown:
        text += "\n• Topilmadi: " + ", ".join(str(item) for item in unknown[:20])
    await message.reply_text(text[:4090], parse_mode=ParseMode.HTML)


async def cmd_show_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/savollar — the tournament's questions, for everybody in the group."""
    message = update.effective_message
    tour = await tour_state(update, context)
    if tour is None:
        await message.reply_text(msg.tour_no_tournament(), parse_mode=ParseMode.HTML)
        return
    argument = " ".join(context.args or []).strip()
    page = int(argument) if argument.isdigit() else 1
    note("command", user_id=getattr(update.effective_user, "id", 0) or 0,
         chat_id=getattr(update.effective_chat, "id", 0) or 0,
         detail="savollar ro‘yxati", log=False)
    await tour_render_questions(context.bot, _archive_of(context), tour, page,
                                chat_id=getattr(update.effective_chat, "id", 0) or 0)


async def cmd_show_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/savol <id> — one question, open to everybody in the tournament chat.

    The answer follows the game: a curator sees it at once, anybody else sees it
    once the question has been played and its answer published. Nothing has to be
    asked in private any more.
    """
    message = update.effective_message
    tour = await tour_state(update, context)
    if tour is None:
        await message.reply_text(msg.tour_no_tournament(), parse_mode=ParseMode.HTML)
        return
    archive = _archive_of(context)
    argument = " ".join(context.args or []).strip()
    if not argument.isdigit():
        await message.reply_text("Masalan: <code>/savol 12345</code> · ro‘yxat: "
                                 "<code>/savollar</code>", parse_mode=ParseMode.HTML)
        return
    question_id = int(argument)
    question = archive.question(question_id)
    if question is None:
        await message.reply_text("Bunday savol topilmadi.")
        return
    private_chat = getattr(update.effective_chat, "type", "") == "private"
    curator = await tour_curator(update, context, tour)
    published = any(int(row.get("id") or 0) == question_id for row in tour.history)
    rows = tour_question_rows(archive, tour)
    number = next((row["number"] for row in rows if row["id"] == question_id), 0)
    show_answer = bool(private_chat or curator or published)
    page = max(1, (number + QUESTION_PAGE - 1) // QUESTION_PAGE) if number else 1
    await message.reply_text(msg.tour_question_card(question, show_answer,
                                                    used=stats.is_used(question_id),
                                                    number=number, total=len(rows)),
                             parse_mode=ParseMode.HTML,
                             reply_markup=None if private_chat
                             else st.question_card_keyboard(page))
    note("command", user_id=getattr(update.effective_user, "id", 0) or 0,
         chat_id=getattr(update.effective_chat, "id", 0) or 0,
         detail=f"savol {question_id}", log=False)


async def cmd_curator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kurator <id|@username> [olib] — appoint or remove a tournament curator."""
    message = update.effective_message
    args = list(context.args or [])
    tour = await tour_state(update, context)
    if not args:
        await message.reply_text(msg.tour_curator_usage(), parse_mode=ParseMode.HTML)
        return
    if not await is_admin(update, context, None):
        await message.reply_text(msg.admin_only("/kurator"), parse_mode=ParseMode.HTML)
        return
    revoke = any(item.casefold() in {"olib", "ochirish", "remove", "off"} for item in args[1:])
    target = args[0]
    user_id = 0
    name = target
    if target.lstrip("@").isdigit():
        user_id = int(target.lstrip("@"))
    elif tour is not None:
        user_id = tour.find(target)
    if not user_id:
        reply = getattr(message, "reply_to_message", None)
        if reply is not None and getattr(reply, "from_user", None) is not None:
            user_id = int(reply.from_user.id)
            name = tour_sender(reply.from_user)
    if not user_id:
        await message.reply_text(
            msg.whisper_failed(f"{target} topilmadi. Id bilan yozing: "
                               "<code>/kurator 123456789</code>"), parse_mode=ParseMode.HTML)
        return
    if tour is None:
        await message.reply_text(msg.tour_no_tournament(), parse_mode=ParseMode.HTML)
        return
    tour.appoint(user_id, grant=not revoke)
    tour.see(user_id, name, str(target).lstrip("@") if not target.isdigit() else "")
    tour.bump()
    await tour_persist(context.bot, _archive_of(context), tour)
    text = (msg.tour_curator_removed(name, len(tour.curators)) if revoke
            else msg.tour_curator_added(name, len(tour.curators)))
    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_tour_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/turnirstop — stop the tournament and print the table."""
    tour = await tour_state(update, context)
    if tour is None:
        await update.effective_message.reply_text(msg.tour_no_tournament(),
                                                  parse_mode=ParseMode.HTML)
        return
    if not await tour_curator(update, context, tour):
        await update.effective_message.reply_text(msg.tour_denied_action("to‘xtatish"),
                                                  parse_mode=ParseMode.HTML)
        return
    await tour_stop(context.bot, _archive_of(context), update.effective_chat.id, tour,
                    "TURNIR TO‘XTATILDI")


async def on_team_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registration card buttons: join, ready, new team, start, table, stop."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    tour = await tour_state(update, context)
    if tour is None:
        await query.answer("Turnir holati topilmadi. /turnir bilan qayta oching.",
                           show_alert=True)
        return
    archive = _archive_of(context)
    user = update.effective_user or query.from_user
    chat_id = update.effective_chat.id
    tour.see(int(getattr(user, "id", 0) or 0), tour_sender(user),
             getattr(user, "username", "") or "")
    pressed = getattr(getattr(query, "message", None), "message_id", None)
    # The pinned card is the roster; a list or a panel drawn into it would replace
    # the teams, so those open in their own message and rewrite that one instead.
    editable = bool(pressed) and pressed != tour.card_message_id

    if action == st.TEAM_JOIN:
        position = int(value) if str(value).lstrip("-").isdigit() else -1
        team, problem = tour.join(position, int(user.id), tour_sender(user),
                                  getattr(user, "username", "") or "", ready=False)
        if team is None:
            await query.answer(problem or "Qo‘shilib bo‘lmadi.", show_alert=True)
            return
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        if problem:
            await query.answer(problem, show_alert=True)
            return
        await query.answer(f"{team.name}: {team.size} kishi · {team.ready_count} tayyor")
        return

    if action == st.TEAM_READY:
        team = tour.team_of(int(user.id))
        if team is None:
            await query.answer(msg.tour_not_registered(), show_alert=True)
            return
        position = tour.index_of(team)
        problem = team.set_ready(int(user.id), True)
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        if problem:
            await query.answer(problem, show_alert=True)
            return
        sizes = tn.sizes()
        await query.answer(f"Men tayyorman · {team.name} ({team.ready_count}/{sizes['min_ready']})")
        if team.is_ready:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg.tour_joined(team.name, tour_sender(user), team.size,
                                     team.ready_count, sizes),
                parse_mode=ParseMode.HTML)
        return

    if action == st.TEAM_NEW:
        tour.ask(tn.PROMPT_TEAM, int(user.id))
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await query.answer(msg.tour_prompt_ask(tn.PROMPT_TEAM), show_alert=True)
        return

    if action == st.TEAM_LEAVE:
        team = tour.team_of(int(user.id))
        name = team.name if team is not None else ""
        result, problem = tour.leave(int(user.id))
        if team is None:
            await query.answer(problem or msg.tour_not_registered(), show_alert=True)
            return
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await query.answer(msg.tour_left(name, tour_sender(user), result is None)[:200])
        return

    if action == st.TEAM_REFRESH:
        if tour.phase == tn.PHASE_PLAY:
            if tour.control_message_id is None:
                tour.control_message_id = getattr(getattr(query, "message", None),
                                                  "message_id", None)
            remaining = int(tour.deadline or 0) - int(time.time())
            if remaining > 0 and tour.control_message_id:
                try:
                    await paint_tour_strip(context.bot, chat_id, tour, remaining)
                except TelegramError:
                    pass
                await query.answer(f"Qolgan vaqt: {msg.clock(remaining)}")
                await schedule_tour_tick(tour, int(tour.deadline or 0))
                return
            await query.answer("Vaqt tugadi")
            if await tour_claim_strip(context.bot, chat_id, tour):
                await tour_resolve(context.bot, archive, chat_id, tour)
            return
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await query.answer("Yangilandi")
        return

    if action == st.TEAM_SKIP:
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("o‘tkazish"), show_alert=True)
            return
        if tour.phase != tn.PHASE_PLAY:
            await query.answer("Hozir savol ochiq emas.", show_alert=True)
            return
        tour.control_message_id = getattr(getattr(query, "message", None), "message_id",
                                          tour.control_message_id)
        await query.answer("Savol yopildi")
        if await tour_claim_strip(context.bot, chat_id, tour):
            note("skip", chat_id=chat_id, package_id=tour.package_id, skips=1,
                 detail=f"turnir · savol {tour.number}", log=False)
            await tour_resolve(context.bot, archive, chat_id, tour)
        return

    if action == st.TEAM_LIST:
        await query.answer()
        await tour_render_questions(context.bot, archive, tour, 1, query=query if editable
                                    else None, chat_id=chat_id)
        return

    if action == st.TEAM_LIST_PAGE:
        page = int(value) if str(value).isdigit() else 1
        await query.answer()
        await tour_render_questions(context.bot, archive, tour, page, query=query if editable
                                    else None, chat_id=chat_id)
        return

    if action == st.TEAM_QUESTION:
        question_id = int(value) if str(value).isdigit() else 0
        question = archive.question(question_id) if question_id else None
        if question is None:
            await query.answer("Savol topilmadi.", show_alert=True)
            return
        rows = tour_question_rows(archive, tour)
        number = next((row["number"] for row in rows if row["id"] == question_id), 0)
        curator = await tour_curator(update, context, tour)
        show_answer = bool(curator or any(row["id"] == question_id and row["published"]
                                          for row in rows))
        await query.answer()
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg.tour_question_card(question, show_answer,
                                        used=stats.is_used(question_id), number=number,
                                        total=len(rows))[:4090],
            parse_mode=ParseMode.HTML,
            reply_markup=st.question_card_keyboard(
                max(1, (number + QUESTION_PAGE - 1) // QUESTION_PAGE) if number else 1))
        return

    if action == st.TEAM_MANAGE:
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("boshqaruv"), show_alert=True)
            return
        await query.answer()
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg.tour_manage_text(tour, tn.when_text(int(tour.start_at)),
                                      tour.ready_to_start(tour_bank_size(archive, tour),
                                                          int(time.time())))[:4090],
            parse_mode=ParseMode.HTML,
            reply_markup=st.manage_keyboard(tour.phase, tour.ready_to_start(
                tour_bank_size(archive, tour), int(time.time())),
                tn.when_text(int(tour.start_at))))
        return

    if action == st.TEAM_PLAN:
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("rejni ko‘rish"), show_alert=True)
            return
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text=msg.tour_plan_text(tour)[:4090],
                                       parse_mode=ParseMode.HTML)
        return

    if action == st.TEAM_TABLE:
        heading = "YAKUNIY JADVAL" if tour.phase == tn.PHASE_DONE else "JADVAL"
        text = msg.tour_standings(tour.standings(), tour.title, heading)[:4000]
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        return

    if action == st.TEAM_REVEAL:
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("javoblarni ko‘rish"), show_alert=True)
            return
        finals = tour_final_count(tour)
        lines = [f"Yakuniy javob bergan: {finals} / {len(tour.teams)} jamoa"]
        for team in tour.teams:
            mark = "bergan" if team.final else "hali bermagan"
            lines.append(f"• {team.name} — {mark}")
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines),
                                       parse_mode=ParseMode.HTML)
        return

    if action == "ku":
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("kurator tayinlash"), show_alert=True)
            return
        await query.answer()
        names = [person for person in (tour.directory or []) if len(person) >= 2]
        await context.bot.send_message(chat_id=chat_id,
                                       text=msg.tour_curator_picker(tour, names)[:4090],
                                       parse_mode=ParseMode.HTML)
        return

    if action == st.TEAM_PICK:
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("savol tanlash"), show_alert=True)
            return
        await query.answer()
        await tour_render_picker(context, context.bot, tour, query=query, allow_edit=editable)
        return

    if action == st.TEAM_START:
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("boshlash"), show_alert=True)
            return
        bank = tour_bank_size(archive, tour)
        problems = tour.missing(bank, int(time.time()))
        if problems:
            await query.answer("Shartlar bajarilmagan", show_alert=True)
            await context.bot.send_message(chat_id=chat_id,
                                           text=msg.tour_missing(problems)[:4090],
                                           parse_mode=ParseMode.HTML)
            return
        await query.answer("Turnir boshlandi!")
        started = await tour_start(context.bot, archive, chat_id, tour)
        if not started:
            await context.bot.send_message(chat_id=chat_id,
                                           text=msg.tour_missing(
                                               tour.missing(bank, int(time.time())))[:4090],
                                           parse_mode=ParseMode.HTML)
        return

    if action == st.TEAM_STOP:
        if not await tour_curator(update, context, tour):
            await query.answer(msg.tour_denied_action("to‘xtatish"), show_alert=True)
            return
        await query.answer("Turnir to‘xtatildi")
        await tour_stop(context.bot, archive, chat_id, tour, "TURNIR TO‘XTATILDI")
        return

    await query.answer()


async def on_review_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«TO‘G‘RI/NOTO‘G‘RI» and «TASDIQLASH» on a confirmation list.

    The list is usually read in the curator's private chat, so the group id rides
    inside the button and the rights are checked against that group — pressing a
    button in a DM must not become a way to score somebody else's tournament.
    """
    query = update.callback_query
    parts = (query.data or "").split("|")
    if len(parts) < 3 or parts[0] != st.PREFIX_TOUR \
            or parts[1] not in (st.REVIEW_FLIP, st.REVIEW_CONFIRM):
        return
    try:
        chat_id = int(parts[2])
    except ValueError:
        await query.answer("Ro‘yxat buzilgan — kartadan qayta oching.", show_alert=True)
        return
    action = parts[1]
    raw = parts[3] if len(parts) > 3 else ""
    position = int(raw) if raw.lstrip("-").isdigit() else -1
    tour = await tour_review_state(context, chat_id, query)
    if tour is None or int(tour.chat_id or 0) != int(chat_id):
        # Callback data can be forged by a modified client, so the group the
        # button names and the group the list belongs to have to be the same one.
        await query.answer("Turnir holati topilmadi. /turnir bilan qayta oching.",
                           show_alert=True)
        return
    user_id = int(getattr(update.effective_user, "id", 0) or 0)
    if not await tour_curator_id(context.bot, chat_id, user_id, tour):
        await query.answer(msg.tour_denied_action("javobni tasdiqlash"), show_alert=True)
        return
    archive = _archive_of(context)
    if not tour.reviewing():
        await query.answer("Tekshiruv oynasi yopilib bo‘lgan — natija guruhda.",
                           show_alert=True)
        return
    if tour.pending_of()[0]:
        tour.clear_pending()

    if action == st.REVIEW_FLIP:
        rows = tour.review_rows()
        if not 0 <= position < len(rows) or not str(rows[position].get("answer") or "").strip():
            await query.answer("Bu jamoaning javobi yo‘q — belgilash ham yo‘q.",
                               show_alert=True)
            return
        mark = tour.flip(position)
        tour.bump()
        await tour_review_render(context.bot, archive, tour)
        await tour_persist(context.bot, archive, tour)
        await schedule_tour_review(tour, int(tour.review_due))
        await query.answer(msg.tour_review_flip(str(rows[position].get("team") or ""),
                                                bool(mark)))
        return

    await query.answer("Tasdiqlandi — natija guruhga yozildi")
    await tour_review_publish(context.bot, archive, chat_id, tour, user_id=user_id)


async def on_curator_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Curator panel and picker buttons: rounds, per-round, timer, include, exclude."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    tour = await tour_state(update, context)
    if tour is None:
        await query.answer("Turnir holati topilmadi. /turnir bilan qayta oching.",
                           show_alert=True)
        return
    if not await tour_curator(update, context, tour):
        await query.answer(msg.tour_denied_action("sozlash"), show_alert=True)
        return
    archive = _archive_of(context)
    curator_id = int(getattr(update.effective_user, "id", 0) or 0)
    if tour.pending_of()[0]:
        tour.clear_pending()          # any button answers "never mind"
    pressed = getattr(getattr(query, "message", None), "message_id", None)
    # The pinned card is rewritten by tour_persist alone; a panel drawn into it
    # would replace the roster, so panels open in their own message instead.
    editable = bool(pressed) and pressed != tour.card_message_id

    if action == "c":
        await query.answer()
        await tour_persist(context.bot, archive, tour)
        await tour_render_curator_panel(context, context.bot, tour, query=query,
                                        allow_edit=editable)
        return

    if action in {"r", "q", "t"} and value.isdigit():
        number = int(value)
        if action == "r":
            tour.rounds = number
        elif action == "q":
            tour.per_round = number
        else:
            tour.timer = number
        tour.bump()
        await query.answer("Saqlandi")
        await tour_persist(context.bot, archive, tour)
        await tour_render_curator_panel(context, context.bot, tour, query=query, allow_edit=editable)
        return

    if action == st.CONFIRM_TOGGLE and value in ("0", "1"):
        tour.needs_confirm = value == "1"
        tour.bump()
        await query.answer(msg.tour_confirm_note(tour.needs_confirm)[:200], show_alert=True)
        await tour_persist(context.bot, archive, tour)
        await tour_render_curator_panel(context, context.bot, tour, query=query,
                                        allow_edit=editable)
        return

    if action == "s":
        tour.seed = int(value) if value.isdigit() else random.randrange(1, 999999)
        tour.bump()
        await query.answer(f"Urug‘: {tour.seed}")
        await tour_persist(context.bot, archive, tour)
        await tour_render_curator_panel(context, context.bot, tour, query=query, allow_edit=editable)
        return

    if action == "pk":
        if tour.phase != tn.PHASE_REGISTER:
            await query.answer(msg.tour_package_locked(), show_alert=True)
            return
        tour.ask(tn.PROMPT_PACKAGE, curator_id)
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await query.answer(msg.tour_prompt_ask(tn.PROMPT_PACKAGE), show_alert=True)
        return

    if action == "pg" and value.isdigit():
        tour.page = int(value)
        await query.answer()
        await tour_persist(context.bot, archive, tour)
        await tour_render_picker(context, context.bot, tour, query=query, allow_edit=editable)
        return

    if action in {"i", "e", "u"} and value.isdigit():
        question_id = int(value)
        if action == "i":
            if question_id not in tour.include:
                tour.include.append(question_id)
            tour.exclude = [item for item in tour.exclude if item != question_id]
        elif action == "e":
            if question_id not in tour.exclude:
                tour.exclude.append(question_id)
            tour.include = [item for item in tour.include if item != question_id]
        else:
            tour.include = [item for item in tour.include if item != question_id]
            tour.exclude = [item for item in tour.exclude if item != question_id]
        tour.include = tour.include[-tn.SELECTION_LIMIT:]
        tour.exclude = tour.exclude[-tn.SELECTION_LIMIT:]
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await query.answer(f"{question_id}: " + ("olindi" if action == "i" else
                                                 "chiqarildi" if action == "e" else "tozalandi"))
        await tour_render_picker(context, context.bot, tour, query=query, allow_edit=editable)
        return

    if action in {"ai", "ae", "ac"}:
        rows, page, pages = tour_pick_page(archive, tour)
        ids = [int(row["id"]) for row in rows]
        if action == "ai":
            for question_id in ids:
                if question_id not in tour.include:
                    tour.include.append(question_id)
                tour.exclude = [item for item in tour.exclude if item != question_id]
        elif action == "ae":
            for question_id in ids:
                if question_id not in tour.exclude:
                    tour.exclude.append(question_id)
                tour.include = [item for item in tour.include if item != question_id]
        else:
            tour.include = []
            tour.exclude = []
        tour.include = tour.include[-tn.SELECTION_LIMIT:]
        tour.exclude = tour.exclude[-tn.SELECTION_LIMIT:]
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await query.answer("Tanlov yangilandi")
        await tour_render_picker(context, context.bot, tour, query=query, allow_edit=editable)
        return

    if action == "qs":
        tour.ask(tn.PROMPT_SEARCH, curator_id)
        tour.bump()
        await tour_persist(context.bot, archive, tour)
        await query.answer(msg.tour_prompt_ask(tn.PROMPT_SEARCH), show_alert=True)
        return

    await query.answer()


def is_owner(update: Update) -> bool:
    """Only the owner's Telegram id ever sees the statistics panel."""
    user = getattr(update, "effective_user", None)
    return bool(user) and int(getattr(user, "id", 0) or 0) == int(stats.OWNER_ID)


USED_PAGE = 12          # nechta ishlatilgan savol bir sahifada


async def render_stats(context: ContextTypes.DEFAULT_TYPE, view: str = "main",
                       message=None, callback=None, page: int = 1) -> None:
    """Build one statistics page and either send it or rewrite the panel."""
    archive = _archive_of(context)
    data = await stats.snapshot(archive)
    pages = 1
    if view == "used":
        index = stats.used_index(archive)
        rows = index["items"]
        pages = max(1, -(-len(rows) // USED_PAGE))
        page = max(1, min(int(page or 1), pages))
        shown = rows[(page - 1) * USED_PAGE: page * USED_PAGE]
        for row in shown:                       # a stored row knows only its id
            if not row.get("x"):
                found = archive.question(int(row["id"])) or {}
                row["x"] = str(found.get("text") or "")[:60]
                row["p"] = int(row.get("p") or found.get("package_id") or 0)
        data = {**data, "used": {**index, "items": shown}}
        text = msg.stats_used(data, shown, page, pages)[:4090]
    elif view == "plans":
        text = msg.stats_plans(data)[:4090]
    elif view == "access":
        rows = [{"id": row["id"], "name": row["name"],
                 "label": sched.clock_text(row["since"]) if row.get("since") else ""}
                for row in wa.access_report()]
        text = msg.stats_access({**data, "access": rows})[:4090]
    else:
        render = {"sections": msg.stats_sections,
                  "events": msg.stats_events}.get(view, msg.stats_panel)
        text = render(data)[:4090]
    keyboard = st.stats_keyboard("main" if view == "main" else view, app_menu_url(),
                                 page=page, pages=pages)
    if callback is not None:
        try:
            await callback.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except TelegramError:
            pass
        return
    if message is not None:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                 disable_web_page_preview=True)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.effective_message.reply_text(
            msg.stats_denied(getattr(update.effective_user, "id", None)),
            parse_mode=ParseMode.HTML)
        return
    view = (context.args[0].strip().lower() if context.args else "main")
    view = {"bo'limlar": "sections", "bolimlar": "sections", "b": "sections",
            "o'yinlar": "events", "oyinlar": "events", "e": "events",
            "ishlatilgan": "used", "savollar": "used", "q": "used",
            "reja": "plans", "rejalar": "plans", "turnirlar": "plans", "p": "plans",
            "ruxsat": "access", "ruxsatlar": "access", "a": "access"}.get(view, view)
    if view not in ("main", "sections", "events", "used", "plans", "access"):
        view = "main"
    await render_stats(context, view, message=update.effective_message)


async def on_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if not data.startswith(st.PREFIX_STATS + "|"):
        return
    if not is_owner(update):
        await query.answer(msg.stats_denied(getattr(update.effective_user, "id", None)),
                           show_alert=True)
        return
    tail = data.split("|", 1)[1]
    view = {"r": "main", "b": "sections", "e": "events", "q": "used", "qp": "used",
            "p": "plans", "a": "access"}.get(tail.split("|")[0], "main")
    page = 1
    if tail.startswith("qp|"):
        try:
            page = max(1, int(tail.split("|", 1)[1]))
        except (TypeError, ValueError):
            page = 1
    await query.answer()
    await render_stats(context, view, callback=query, page=page)


APP_MENU_TEXT = "Statistika"


def app_menu_url() -> str:
    """Absolute address of the Mini App, or empty when there is no public one."""
    return wa.app_url(public_base_url())


async def apply_menu_button(bot, chat_id, granted: bool) -> str:
    """Point one private chat's menu button at the Mini App, or back to default.

    The menu button is what makes the Mini App visible in Telegram: it is set per
    chat, so somebody who was never granted has no button to tap. The server side
    checks the signature anyway, because an address can be shared.
    """
    url = app_menu_url()
    if not url:
        return "o‘rnatilmadi (ochiq manzil yo‘q)"
    try:
        if granted:
            await bot.set_chat_menu_button(
                chat_id=int(chat_id),
                menu_button=MenuButtonWebApp(text=APP_MENU_TEXT, web_app=WebAppInfo(url=url)))
        else:
            await bot.set_chat_menu_button(chat_id=int(chat_id), menu_button=MenuButtonDefault())
        return "o‘rnatildi" if granted else "oddiy holatga qaytdi"
    except TelegramError as exc:
        return f"xato: {str(exc)[:90]}"


async def apply_menu_buttons(client=None, ids=None) -> dict:
    """Set the menu button for the owner and everybody granted, with plain HTTP.

    Used by ``?appprobe=1&menu=1``, which runs outside an update handler where a
    PTB application may already have been shut down.
    """
    url = app_menu_url()
    await stats.load_access()
    targets = list(ids) if ids is not None else (
        [int(stats.OWNER_ID)] + [int(key) for key in stats.access_list() if str(key).isdigit()])
    report: dict = {"manzil": url}
    if not url:
        report["izoh"] = "ochiq manzil yo‘q (WEBHOOK_URL / VERCEL_PROJECT_PRODUCTION_URL)"
        return report
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0))
    try:
        for target in targets[:stats.ACCESS_LIMIT + 1]:
            answer = await stats._tg_call(
                client, "setChatMenuButton", chat_id=int(target),
                menu_button={"type": "web_app", "text": APP_MENU_TEXT,
                             "web_app": {"url": url}})
            report[str(target)] = ("ok" if answer.get("ok")
                                   else str(answer.get("description") or answer)[:90])
    finally:
        if own_client:
            await client.aclose()
    report["jami"] = len(targets)
    return report


async def chat_display_name(bot, chat_id: int) -> str:
    """Ask Telegram how to call a user the owner only knows by id."""
    try:
        chat = await bot.get_chat(int(chat_id))
    except TelegramError:
        return ""
    return str(getattr(chat, "full_name", "") or "").strip()[:40]


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: who may open the Mini App."""
    if not is_owner(update):
        await update.effective_message.reply_text(
            msg.stats_denied(getattr(update.effective_user, "id", None)),
            parse_mode=ParseMode.HTML)
        return
    await stats.load_access()
    await update.effective_message.reply_text(
        msg.access_list(wa.access_report(), app_menu_url()),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: give one user id access to the Mini App."""
    if not is_owner(update):
        await update.effective_message.reply_text(
            msg.stats_denied(getattr(update.effective_user, "id", None)),
            parse_mode=ParseMode.HTML)
        return
    args = list(context.args or [])
    raw = (args[0] if args else "").strip()
    name = " ".join(str(part) for part in args[1:]).strip()[:40]
    if not raw.lstrip("-").isdigit():
        await update.effective_message.reply_text(msg.access_usage(), parse_mode=ParseMode.HTML)
        return
    target = int(raw)
    if target == int(stats.OWNER_ID):
        await update.effective_message.reply_text(
            "Siz bot egasisiz — kirish har doim ochiq. Ro‘yxat: /access",
            parse_mode=ParseMode.HTML)
        return
    if not name:
        name = await chat_display_name(context.bot, target)
    saved, _ = await stats.set_access(target, name, True)
    menu = await apply_menu_button(context.bot, target, True)
    note("grant", user_id=int(stats.OWNER_ID),
         chat_id=getattr(update.effective_chat, "id", None),
         detail=f"{target} {name}".strip()[:80])
    await update.effective_message.reply_text(
        msg.access_changed(target, name, True, saved, menu),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: take the Mini App away from one user id."""
    if not is_owner(update):
        await update.effective_message.reply_text(
            msg.stats_denied(getattr(update.effective_user, "id", None)),
            parse_mode=ParseMode.HTML)
        return
    args = list(context.args or [])
    raw = (args[0] if args else "").strip()
    if not raw.lstrip("-").isdigit():
        await update.effective_message.reply_text(msg.access_usage(), parse_mode=ParseMode.HTML)
        return
    target = int(raw)
    if target == int(stats.OWNER_ID):
        await update.effective_message.reply_text(
            "Eganing o‘z kirishini olib bo‘lmaydi.", parse_mode=ParseMode.HTML)
        return
    await stats.load_access()
    entry = stats.access_list().get(str(target)) or {}
    name = str(entry.get("name") or "")
    saved, _ = await stats.set_access(target, name, False)
    menu = await apply_menu_button(context.bot, target, False)
    note("revoke", user_id=int(stats.OWNER_ID),
         chat_id=getattr(update.effective_chat, "id", None),
         detail=f"{target} {name}".strip()[:80])
    await update.effective_message.reply_text(
        msg.access_changed(target, name, False, saved, menu),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


_recent_errors: list[str] = []


async def on_handler_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the last few handler errors so they can be shown on /api/index."""
    error = context.error
    entry = f"{time.strftime('%H:%M:%S')} {type(error).__name__}: {error}"
    _recent_errors.insert(0, entry)
    _recent_errors[:] = _recent_errors[:5]
    logging.getLogger(__name__).exception("Handler failed", exc_info=error)


# The menu is short on purpose: 15 rows a player really types, in the order they
# are needed. Curator and owner commands still work, they are just not in
# everybody's menu — /help lists them in groups.
BOT_COMMANDS = [
    ("start", "Tanishtiruv"),
    ("help", "Buyruqlar ro‘yxati"),
    ("rules", "Qoidalar"),
    ("solo", "Yakka o‘yin"),
    ("duel", "Guruh o‘yini"),
    ("packages", "Paketlar (sahifalab)"),
    ("score", "Joriy hisob"),
    ("stop", "O‘yinni to‘xtatish"),
    ("turnir", "Turnir ochish (admin/kurator)"),
    ("jamoa", "Jamoa yaratish: /jamoa nom"),
    ("qoshil", "Jamoaga qo‘shilish: /qoshil nom"),
    ("tayyor", "Men tayyorman"),
    ("yakuniy", "Yakuniy javob: /yakuniy javob"),
    ("pichoq", "Maxfiy xabar: /pichoq matn"),
    ("jadval", "Ochkolar jadvali"),
    ("savollar", "Turnir savollari (hammaga ochiq)"),
]
# Everything the bot understands, menu or not: /help is written from these groups.
ALL_COMMANDS = BOT_COMMANDS + [
    ("chiqish", "Jamoadan chiqish"),
    ("bekor", "Yakuniy javobni bekor qilish"),
    ("azolar", "Jamoangiz a’zolari (id va username)"),
    ("tanlov", "Savol tanlash: /tanlov so‘z"),
    ("olish", "Savolni tanlovga olish: /olish 12345"),
    ("chiqarish", "Savolni tanlovdan chiqarish: /chiqarish 12345"),
    ("savol", "Bitta savolni ko‘rish: /savol 12345"),
    ("kurator", "Kurator tayinlash: /kurator 123456789"),
    ("turnirstop", "Turnirni to‘xtatish va jadval"),
    ("skip", "Savolni o‘tkazish (admin)"),
    ("accept", "Javobni qabul qilish (admin)"),
]
OWNER_COMMANDS = BOT_COMMANDS + [
    ("stats", "Statistika"),
    ("access", "Mini App ruxsatlari"),
    ("grant", "Ruxsat berish: /grant 123456789 Ism"),
    ("revoke", "Ruxsatni olish: /revoke 123456789"),
    ("tanlov", "Savol tanlash"),
    ("kurator", "Kurator tayinlash"),
    ("turnirstop", "Turnirni to‘xtatish"),
    ("azolar", "A’zolar ro‘yxati"),
]

COMMANDS = {
    "start": cmd_start,
    "help": cmd_help,
    "rules": cmd_rules,
    "packages": cmd_packages,
    "solo": cmd_solo,
    "quiz": cmd_solo,
    "duel": cmd_duel,
    "score": cmd_score,
    "skip": cmd_skip,
    "stop": cmd_stop,
    "accept": cmd_accept,
    "turnir": cmd_tour,
    "tournament": cmd_tour,
    "jamoa": cmd_team,
    "team": cmd_team,
    "qoshil": cmd_join,
    "join": cmd_join,
    "tayyor": cmd_ready,
    "chiqish": cmd_leave,
    "yakuniy": cmd_final,
    "final": cmd_final,
    "bekor": cmd_final_cancel,
    "pichoq": cmd_whisper,
    "whisper": cmd_whisper,
    "azolar": cmd_members,
    "jadval": cmd_table,
    "tanlov": cmd_selection,
    "olish": cmd_include,
    "chiqarish": cmd_exclude,
    "savol": cmd_show_question,
    "savollar": cmd_show_questions,
    "kurator": cmd_curator,
    "turnirstop": cmd_tour_stop,
    "stats": cmd_stats,
    "access": cmd_access,
    "grant": cmd_grant,
    "revoke": cmd_revoke,
}


async def on_command_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /commands that arrive without a bot_command entity.

    PTB's CommandHandler only matches when Telegram attaches the entity, so a
    message from an unusual client would otherwise fall through to small talk
    and stay unanswered.
    """
    text = (update.effective_message.text or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    name = parts[0][1:].split("@")[0].lower()
    callback = COMMANDS.get(name)
    if callback is None:
        await update.effective_message.reply_text(msg.help_text(), parse_mode=ParseMode.HTML)
        return
    context.args = parts[1:]
    await callback(update, context)


def command_handler(name: str, callback):
    """CommandHandler that also counts the command in the statistics."""

    async def counted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = getattr(update, "effective_chat", None)
        note("command", user_id=getattr(getattr(update, "effective_user", None), "id", None),
             chat_id=getattr(chat, "id", None), commands=1, detail=f"/{name}", log=False)
        # The panel can only offer a group it has seen, and seeing one is free:
        # it is a line of memory, written out with the next statistics flush.
        if chat is not None and getattr(chat, "type", "private") != "private":
            stats.group_seen(int(chat.id), str(getattr(chat, "title", "") or ""))
        await callback(update, context)

    return CommandHandler(name, counted)


def build_app(bot=None) -> Application:
    builder = ApplicationBuilder().updater(None)
    if bot is not None:
        builder = builder.bot(bot)
    else:
        token = os.getenv("BOT_TOKEN", "")
        if not token:
            raise RuntimeError("BOT_TOKEN environment variable is not set in Vercel")
        builder = builder.token(token)
    application = builder.build()
    application.bot_data["archive"] = ARCHIVE
    application.add_error_handler(on_handler_error)
    application.add_handlers([
        command_handler("start", cmd_start),
        command_handler("help", cmd_help),
        command_handler("rules", cmd_rules),
        command_handler("packages", cmd_packages),
        command_handler("solo", cmd_solo),
        command_handler("quiz", cmd_solo),
        command_handler("duel", cmd_duel),
        command_handler("score", cmd_score),
        command_handler("skip", cmd_skip),
        command_handler("stop", cmd_stop),
        command_handler("accept", cmd_accept),
        command_handler("turnir", cmd_tour),
        command_handler("tournament", cmd_tour),
        command_handler("jamoa", cmd_team),
        command_handler("team", cmd_team),
        command_handler("qoshil", cmd_join),
        command_handler("join", cmd_join),
        command_handler("tayyor", cmd_ready),
        command_handler("chiqish", cmd_leave),
        command_handler("yakuniy", cmd_final),
        command_handler("final", cmd_final),
        command_handler("bekor", cmd_final_cancel),
        command_handler("pichoq", cmd_whisper),
        command_handler("whisper", cmd_whisper),
        command_handler("azolar", cmd_members),
        command_handler("jadval", cmd_table),
        command_handler("tanlov", cmd_selection),
        command_handler("olish", cmd_include),
        command_handler("chiqarish", cmd_exclude),
        command_handler("savol", cmd_show_question),
        command_handler("savollar", cmd_show_questions),
        command_handler("kurator", cmd_curator),
        command_handler("turnirstop", cmd_tour_stop),
        command_handler("stats", cmd_stats),
        command_handler("access", cmd_access),
        command_handler("grant", cmd_grant),
        command_handler("revoke", cmd_revoke),
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        CallbackQueryHandler(on_settings_button, pattern="^" + st.PREFIX_CONFIG + r"\|"),
        CallbackQueryHandler(on_page_button, pattern="^" + st.PREFIX_PAGE + r"\|"),
        CallbackQueryHandler(on_game_button, pattern="^" + st.PREFIX_GAME + r"\|"),
        CallbackQueryHandler(on_stats_button, pattern="^" + st.PREFIX_STATS + r"\|"),
        CallbackQueryHandler(on_team_button, pattern="^" + st.PREFIX_TEAM + r"\|"),
        CallbackQueryHandler(on_review_button,
                             pattern="^" + st.PREFIX_TOUR + r"\|r[vc]\|"),
        CallbackQueryHandler(on_curator_button, pattern="^" + st.PREFIX_TOUR + r"\|"),
        MessageHandler(filters.Regex(r"^/"), on_command_fallback),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
    ])
    return application


def build_telegram_app() -> Application:
    """Real Telegram application used by the deployed webhook."""
    return build_app()



_application: Application | None = None
_webhook_ready = False
_setup_error = ""
_setup_attempted = False
_updates_seen = 0
_updates_rejected = 0
_ticks_seen = 0
_ticks_painted = 0
_ticks_resolved = 0
_ticks_stopped = 0


def get_application() -> Application:
    global _application
    if _application is None:
        _application = build_telegram_app()
    return _application


def public_base_url() -> str:
    """A URL Telegram and users can actually reach.

    VERCEL_URL is the generated deployment URL, which Vercel's Standard
    Deployment Protection guards with a login page — Telegram gets a 401 there
    and no update is ever delivered. VERCEL_PROJECT_PRODUCTION_URL is the public
    production domain and is always set, so it is preferred.
    """
    for name in ("WEBHOOK_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_BRANCH_URL", "VERCEL_URL"):
        value = (os.getenv(name) or "").strip()
        if value:
            if not value.startswith("http"):
                value = "https://" + value
            return value.rstrip("/")
    return ""


async def ensure_webhook(application: Application, force: bool = False, drop_pending: bool = False) -> None:
    """Register the webhook on Vercel's public production domain."""
    global _webhook_ready, _setup_error, _setup_attempted
    if (_webhook_ready or _setup_attempted) and not force:
        return
    _setup_attempted = True
    if not os.getenv("BOT_TOKEN"):
        _setup_error = "BOT_TOKEN environment variable is not set"
        return
    base = public_base_url()
    if not base:
        _setup_error = "WEBHOOK_URL / VERCEL_PROJECT_PRODUCTION_URL / VERCEL_URL is not set"
        return
    try:
        await application.bot.set_webhook(
            url=base + "/api/index",
            secret_token=SECRET or None,
            allowed_updates=["message", "my_chat_member", "callback_query"],
            drop_pending_updates=drop_pending,
        )
        await application.bot.set_my_commands([BotCommand(name, title) for name, title in BOT_COMMANDS])
        try:
            # /stats is listed only in the owner's own command menu.
            await application.bot.set_my_commands(
                [BotCommand(name, title) for name, title in OWNER_COMMANDS],
                scope=BotCommandScopeChat(chat_id=int(stats.OWNER_ID)),
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("owner command scope failed: %s", exc)
        try:
            # The owner always sees the Mini App in their chat menu. Granted users
            # are handled by /grant and by ?appprobe=1&menu=1.
            await apply_menu_button(application.bot, int(stats.OWNER_ID), True)
        except Exception as exc:
            logging.getLogger(__name__).debug("owner menu button failed: %s", exc)
        _webhook_ready = True
        _setup_error = ""
    except Exception as exc:  # telegram API errors must not break the site
        _setup_error = str(exc)


def _set_setup_error(exc: Exception) -> None:
    global _setup_error
    _setup_error = f"{type(exc).__name__}: {exc}"[:300]


async def bootstrap_webhook() -> None:
    """Runs on any first HTTP request so a plain site visit registers the webhook."""
    if _webhook_ready or _setup_attempted or not os.getenv("BOT_TOKEN"):
        return
    application = get_application()
    try:
        await application.initialize()
    except Exception as exc:          # Telegram unreachable must not break the page
        _set_setup_error(exc)
        return
    try:
        await ensure_webhook(application)
    finally:
        await application.shutdown()


app = FastAPI()


@app.middleware("http")
async def bootstrap(request: Request, call_next):
    if request.method == "GET":
        await bootstrap_webhook()
    return await call_next(request)


def status_page(title: str, lines: list[str], status_code: int = 200) -> HTMLResponse:
    body = "<br>".join(msg.esc(line) for line in lines)
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><title>{msg.esc(title)}</title>"
        f"<h1>{msg.esc(title)}</h1><pre>{body}</pre>",
        status_code=status_code,
    )


def archive_lines(shard_probe: bool = False) -> list[str]:
    packages, questions = ARCHIVE.counts()
    loaded = len(ARCHIVE._load()) if hasattr(ARCHIVE, "_load") else 0
    root = ARCHIVE.root.resolve()
    lines = [
        f"Arxiv manifesti: {packages} paket, {questions} savol",
        f"Arxiv katalogi: {root} ({'bor' if root.exists() else 'YO‘Q — fayllar deploy bo‘lmagan'})",
        f"Yuklangan paketlar: {loaded}",
    ]
    for name, entry in ARCHIVE.source_counts().items():
        detail = f"{entry.get('packages', 0)} paket, {entry.get('questions', 0)} savol"
        if entry.get("shards"):
            detail += f", {entry['shards']} shard · saqlanishi: {entry.get('stored')}"
        lines.append(f"Manba {name}: {detail}")
    if ARCHIVE.remote_available():
        lines.append(f"Katta baza: {ARCHIVE.remote_base or '(manzil yo‘q)'} · "
                     f"kesh: {ARCHIVE.cache_dir} · xotirada: {SHARDS_IN_MEMORY} shard")
        if shard_probe:
            lines.append(remote_probe_line())
    return lines


def remote_probe_line() -> str:
    """Fetch one shard for real: ?shardprobe=1 proves the big base is reachable."""
    started = time.time()
    package_id = min(ARCHIVE.remote_packages)
    rows = ARCHIVE.questions(package_id)
    cost = int((time.time() - started) * 1000)
    if not rows:
        return f"Shard tekshiruvi: YUKLANMADI ({cost} ms) · paket {package_id}"
    head = str(rows[0].get("text") or "")[:40].replace("\n", " ")
    return (f"Shard tekshiruvi: {len(rows)} savol, {cost} ms · paket {package_id} · "
            f"{ARCHIVE.package_info(package_id).get('title', '')[:40]} · «{head}…»")


@app.get("/api/setup")
async def setup(request: Request):
    """Idempotent manual bootstrap: open once after deploying.

    Add ?drop=1 to also discard updates Telegram still holds in its queue — a
    stuck update is delivered in order and can block every newer message.
    """
    drop = request.query_params.get("drop") == "1"
    try:
        application = get_application()
        await application.initialize()
        try:
            await ensure_webhook(application, force=True, drop_pending=drop)
            info = await application.bot.get_webhook_info()
        finally:
            await application.shutdown()
        if _setup_error:
            return status_page("Xatolik", [_setup_error], 500)
        lines = [
            f"Webhook o‘rnatildi: {info.url}",
            f"{info.pending_update_count} kutilayotgan xabar",
            f"Oxirgi xatolik: {info.last_error_message or 'yo‘q'}",
        ]
        if drop:
            lines.append("Telegram navbati tozalandi — endi yangi xabar yuboring.")
        return status_page("Webhook", lines)
    except Exception as exc:
        return status_page("Xatolik", [str(exc)], 500)


@app.get("/api/status")
async def status(request: Request):
    return await status_report(request)


async def status_report(request: Request | None = None):
    """Diagnostic page: shows only whether secrets exist, never their values.

    ``/api/index`` falls through here with its own request, so the shard probe
    (``?shardprobe=1``) works on both paths. Called without a request — from a
    test or another handler — it simply reports no probe.
    """
    lines = [
        f"BOT_TOKEN: {'set' if os.getenv('BOT_TOKEN') else 'MISSING'}",
        f"WEBHOOK_SECRET: {'set' if SECRET else 'MISSING'}",
        f"VERCEL_ENV: {os.getenv('VERCEL_ENV') or '(lokal)'}",
        f"VERCEL_PROJECT_PRODUCTION_URL: {os.getenv('VERCEL_PROJECT_PRODUCTION_URL') or '(yo‘q)'}",
        f"VERCEL_URL (himoyalangan bo‘lishi mumkin): {os.getenv('VERCEL_URL') or '(yo‘q)'}",
        f"Webhook uchun tanlangan manzil: {public_base_url() or '(yo‘q)'}",
        f"So‘nggi o‘rnatish xatoligi: {_setup_error or 'yo‘q'}",
        f"Jonli taymer: {'yoqilgan' if TICKS_ENABLED else 'o‘chirilgan'} · "
        f"qadam {TICK_SECONDS} s · usul {TICK_MODE} · "
        f"javobsizlik chegarasi {IDLE_LIMIT or 'o‘chirilgan'}",
        f"Statistika: {stats.store_label()}"
        f" · egasi {stats.OWNER_ID} · /stats · ?statsprobe=1",
        f"Soat savol tasmasida ko‘rinadi · server vaqti: /api/now",
        f"Mini App panel: {app_menu_url() or '(manzil yo‘q)'} · "
        f"ruxsatlar: {len(stats.access_list())} ta · egasi {stats.OWNER_ID} · "
        f"/access /grant /revoke · ?appprobe=1",
        f"Yashirin holat: {HIDDEN_MODE} · fon ishi: "
        f"{_bg_probe_log[-1] if _bg_probe_log else 'tekshirilmagan'} · "
        f"diagnostika: ?tickprobe=1 · ?bgprobe=1",
        f"Shu nusxadagi taymer urinishlari: {_ticks_seen} · yangilangan: {_ticks_painted} · "
        f"vaqti tugagan: {_ticks_resolved} · to‘xtagan: {_ticks_stopped}",
        f"Shu nusxadagi eslatmalar: qurollangan {len(_armed)} · ko‘rilgan {_reminders_seen} · "
        f"e’lon qilingan {_reminders_fired} · oyna {REMINDER_ARM} s",
        f"Xotirada: reja {len(stats.plans())} ta · guruh {len(stats.groups())} ta · "
        f"indeksda {int(stats.used_index().get('count') or 0)} savol · "
        f"/api/browse /api/tour · ?appprobe=1",
    ]
    probe = request is not None and request.query_params.get("shardprobe") == "1"
    lines += archive_lines(probe)
    if os.getenv("BOT_TOKEN"):
        try:
            application = get_application()
            await application.initialize()
            try:
                info = await application.bot.get_webhook_info()
            finally:
                await application.shutdown()
            lines.append(f"Telegram webhook: {info.url or '(o‘rnatilmagan)'}")
            lines.append(f"Kutilayotgan xabarlar: {info.pending_update_count}")
            lines.append(
                f"Shu nusxada qabul qilingan update: {_updates_seen} · rad etilgan (403): {_updates_rejected}"
            )
            if info.last_error_message:
                lines.append(f"Telegram oxirgi xatoligi: {info.last_error_message}")
        except Exception as exc:
            lines.append(f"Telegram xatoligi: {exc}")
    if _recent_errors:
        lines.append("So‘nggi handler xatolari:")
        lines += [f"  {entry}" for entry in _recent_errors]
    return status_page("Bot holati", lines)


async def tick_probe() -> dict:
    """Self-invoke the tick endpoint once, so the live timer can be verified live.

    The payload is deliberately invalid (chat 0), so nothing is sent to Telegram:
    it only proves that the function can reach itself through the public domain.
    """
    base = public_base_url()
    if not base:
        return {"ok": False, "reason": "ochiq manzil yo‘q (WEBHOOK_URL / VERCEL_*)"}
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET} if SECRET else {}
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            response = await client.post(base + "/api/index",
                                         json={"tick": "#ZS2 probe", "chat": 0, "wait": 0,
                                               "mode": "sleep"},
                                         headers=headers)
        body = response.text[:200]
        return {"ok": response.status_code == 200, "status": response.status_code,
                "ms": round((time.time() - started) * 1000), "javob": body,
                "kod": hidden_state_report()}
    except Exception as exc:
        return {"ok": False, "xato": f"{type(exc).__name__}: {exc}",
                "ms": round((time.time() - started) * 1000)}


def hidden_state_report() -> dict:
    """Round-trip the invisible state so the encoding can be checked live."""
    body = "s|959|0|0|0|777|23300|1795749465|3|10|60|976953|0|0|0|1"
    hidden = hide_state(body)
    parsed = unpack_state("Savol 1 / 30\n" + hidden)
    return {
        "usul": HIDDEN_MODE,
        "belgilar": len(hidden),
        "korinar_eni": 0 if HIDDEN_MODE == "invisible" else len(MARKER.strip() + body),
        "aylanma": bool(parsed and parsed.get("p") == 959 and parsed.get("q") == 23300),
    }


async def bg_step(stamp: str) -> JSONResponse:
    """Queue PROBE_WAIT seconds of work and answer at once (probe internals)."""
    started = time.time()

    async def work() -> None:
        await asyncio.sleep(PROBE_WAIT)
        _bg_probe_log.append(f"{stamp} + {time.time() - started:.1f} s")
        del _bg_probe_log[:-5]

    queued = bg_add(work)
    body = {"ok": True, "navbatga_olindi": queued, "belgi": stamp}
    if queued:
        return JSONResponse(body, background=BackgroundTask(run_background, _bg_tasks.get()))
    return JSONResponse(body)


async def tg_storage_probe() -> dict:
    """Force the Telegram store and check it end to end.

    Telegram is the storage that needs no service at all: the document lives in
    a pinned message of one chat, written with editMessageText and read back
    through getChat. This keeps the message (it *is* the store) and reports the
    whole round trip.
    """
    marker = f"tg-{int(time.time())}"
    result: dict = {"chat": stats.STATS_CHAT, "marker": marker}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=4.0)) as client:
            code, text = await stats.telegram_read(client)
            result["oqish"] = code
            result["pinlangan"] = code == 200 and bool(text)
            existing = stats._normalise(stats._try_json(text)) if code == 200 else {}
            document = {"v": 1, "s": int(time.time()),
                        "c": {str(k): int(v) for k, v in (existing.get("c") or {}).items()},
                        "u": {}, "h": {}, "p": {},
                        "e": ([{"t": int(time.time()), "k": "probe", "x": marker}]
                              + list(existing.get("e") or []))[:stats.EVENT_LIMIT],
                        "a": dict(existing.get("a") or {})}
            write_code, how = await stats.telegram_write(
                client, json.dumps(document, ensure_ascii=False))
            result["yozish"] = write_code
            result["usul"] = how
            code, text = await stats.telegram_read(client)
            result["qayta_oqildi"] = marker in text
            result["ok"] = write_code < 400 and marker in text
            result["hisoblar_saqlandi"] = document["c"]
    except Exception as exc:
        result["ok"] = False
        result["xato"] = f"{type(exc).__name__}: {exc}"
    return result


async def bg_probe() -> JSONResponse:
    """Does this platform still run work after the response was sent?

    Asks itself to queue a few seconds of work, measures how long that request
    took to come back, then looks whether the work finished anyway. The live
    countdown depends on the answer: a fast reply plus finished work means "bg"
    mode is real; a slow reply means the platform buffers the response and
    "sleep" mode is the safer choice.
    """
    base = public_base_url()
    if not base:
        return JSONResponse({"ok": False, "reason": "ochiq manzil yo‘q (WEBHOOK_URL / VERCEL_*)"})
    stamp = time.strftime("%H:%M:%S")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            started = time.time()
            first = await client.get(f"{base}/api/index?bgstep=1&stamp={stamp}")
            inner_ms = round((time.time() - started) * 1000)
            await asyncio.sleep(PROBE_WAIT + 3)
            second = await client.get(base + "/api/index?bglog=1")
    except Exception as exc:
        return JSONResponse({"ok": False, "xato": f"{type(exc).__name__}: {exc}"})

    try:
        log = list((second.json() or {}).get("log") or [])
    except ValueError:
        log = []
    finished = any(stamp in line for line in log)
    if finished and inner_ms < PROBE_WAIT * 600:
        verdict = ("javob darhol yuborildi va ish keyin bajarildi — "
                   "ZAKOVAT_TICK_MODE=bg ishlaydi")
    elif finished:
        verdict = ("ish bajarildi, lekin javob shundan keyin yuborildi — "
                   "ZAKOVAT_TICK_MODE=sleep ishonchliroq")
    else:
        verdict = ("aniqlanmadi: so‘rov boshqa nusxaga tushgan bo‘lishi mumkin, "
                   "qayta urinib ko‘ring")
    return JSONResponse({"ok": finished, "belgi": stamp, "ichki_javob_ms": inner_ms,
                         "fon_ishi": "bajarildi" if finished else "ko‘rinmadi",
                         "kutildi": PROBE_WAIT, "kundalik": log[-5:], "xulosa": verdict})


@app.get("/api/index")
async def index(request: Request):
    """Vercel exposes api/index.py at this path; it doubles as a diagnostics page."""
    mode = request.query_params.get("test") or request.query_params.get("setup")
    if request.query_params.get("setup") == "1" or mode == "setup":
        return await setup(request)
    if request.query_params.get("tickprobe") == "1":
        return JSONResponse(await tick_probe())
    if request.query_params.get("bgprobe") == "1":
        return await bg_probe()
    if request.query_params.get("statsprobe") == "1":
        # create=1 provisions a free JSON document store, tg=1 tries Telegram
        if request.query_params.get("tg") == "1":
            report = await tg_storage_probe()
        elif request.query_params.get("create") == "1":
            report = await stats.create_store()
        else:
            report = await stats.probe()
        report["hisoblagichlar"] = dict(stats._pending["c"])
        report["rejim_hozir"] = stats.backend()
        return JSONResponse(report)
    if request.query_params.get("appprobe") == "1":
        report = await app_probe(menu=request.query_params.get("menu") == "1")
        report["hisoblagichlar"] = dict(stats._pending["c"])
        report["rejim_hozir"] = stats.backend()
        return JSONResponse(report)
    if request.query_params.get("bgstep") == "1":
        bucket: list = []
        token = _bg_tasks.set(bucket)
        try:
            return await bg_step(request.query_params.get("stamp") or time.strftime("%H:%M:%S"))
        finally:
            _bg_tasks.reset(token)
    if request.query_params.get("bglog") == "1":
        return JSONResponse({"ok": True, "log": _bg_probe_log[-5:]})
    if mode == "1":
        return status_page("O‘z-o‘zini sinash", await self_test())
    return await status_report(request)


def review_probe() -> dict:
    """Prove the confirmation step end to end, without Telegram and without a game.

    A throwaway tournament is built in memory, one team answers right and one
    wrong, the verdicts are proposed, one of them is turned over by hand and the
    list is confirmed — then the same thing is left alone to publish itself. If
    any of that breaks, the deployed function is not the code that was tested.
    """
    now = int(time.time())
    report: dict = {"ok": False, "oyna": int(REVIEW_AUTO), "qadam": int(REVIEW_HOP),
                    "eslatmalar": list(tn.REVIEW_REMIND),
                    "kuratorlar": int(tn.REVIEW_TARGETS)}

    class Checker:
        def is_correct(self, question_id, expected, given):
            return str(given or "").strip().casefold() == str(expected or "").strip().casefold()

    def build(qid: int, answers: dict) -> tn.Tournament:
        tour = tn.Tournament(chat_id=-1, title="probe", package_id=0, creator=1, rounds=1,
                             per_round=1, timer=60, seed=1)
        for index, name in enumerate(("Bilimdon", "Zakovat", "Iqtidor")):
            tour.add_team(name, 100 + index, f"Kapitan{index}")
        tour.phase = tn.PHASE_PLAY
        tour.questions = [{"id": qid, "text": "probe", "answer": "Parnas"}]
        tour.index = 0
        tour.question_id = qid
        tour.deadline = now + 60
        for index, name in enumerate(("Bilimdon", "Zakovat", "Iqtidor")):
            if answers.get(name):
                tour.team(name).submit(100 + index, answers[name], now)
        return tour

    try:
        tour = build(1, {"Bilimdon": "Parnas", "Zakovat": "Olimp"})
        rows = tour.propose(Checker(), now, now + REVIEW_AUTO)
        report["qatorlar"] = len(rows)
        report["xulosa"] = tour.review_marks
        report["flip"] = bool(tour.flip(1)) and tour.review_edit == "110"
        result = tour.confirm(7, now)
        report["tasdiq"] = (sorted(result["winners"]) == ["Bilimdon", "Zakovat"]
                            and result["confirmed_by"] == 7 and not result["auto"]
                            and not tour.reviewing())
        cold = build(2, {"Bilimdon": "Parnas"})
        cold.propose(Checker(), now, now + REVIEW_AUTO)
        carried = tn.Tournament.from_body(cold.body())
        carried.questions = cold.questions
        report["holat"] = (carried.reviewing() and carried.review_marks == "100"
                           and carried.review_left(now) > 0)
        auto = tn.Tournament.from_body(build(3, {"Iqtidor": "Parnas"}).body())
        auto.questions = [{"id": 3, "text": "probe", "answer": "Parnas"}]
        auto.propose(Checker(), now, now + 1)
        late = auto.confirm(0, now + REVIEW_AUTO + 1, auto=True)
        report["avtomatik"] = bool(late["auto"]) and late["winners"] == ["Iqtidor"]
        off = build(4, {"Bilimdon": "Parnas"})
        off.needs_confirm = False
        report["ochiq"] = off.close(Checker(), now)["winners"] == ["Bilimdon"]
        report["ok"] = all(bool(report[key]) for key in
                           ("flip", "tasdiq", "holat", "avtomatik", "ochiq"))
    except Exception as exc:  # the probe must never break the app
        report["xato"] = f"{type(exc).__name__}: {exc}"
    return report


def browse_probe() -> dict:
    """The panel's browser, end to end: three shapes and one local package.

    A local package on purpose — a remote one would drag a shard down from the
    release, and a diagnostic page has no business doing that.
    """
    try:
        package_id = int(ARCHIVE.random_package_id(remote=False) or 0)
        sections = wa.browse({"turi": "bolimlar"}, ARCHIVE)
        packages = wa.browse({"turi": "paketlar", "sahifa": 2}, ARCHIVE)
        questions = wa.browse({"turi": "savollar", "paket": package_id}, ARCHIVE)
        search = wa.browse({"turi": "paketlar",
                            "qidiruv": str(ARCHIVE.package_info(package_id).get("title") or "")
                            .split()[0] if ARCHIVE.package_info(package_id).get("title") else ""},
                           ARCHIVE)
        return {"ok": bool(sections.get("ok")) and bool(packages.get("ok"))
                       and bool(questions.get("ok")) and bool(search.get("ok")),
                "bo‘limlar": len(sections.get("bolimlar") or []),
                "paketlar": int(packages.get("jami") or 0),
                "sahifalar": int(packages.get("sahifalar") or 0),
                "paket": package_id,
                "savollar": int(questions.get("jami") or 0),
                "qidiruv": int(search.get("jami") or 0)}
    except Exception as exc:
        return {"ok": False, "xato": f"{type(exc).__name__}: {exc}"[:180]}


async def composer_probe(owner: dict) -> dict:
    """The composer's gate: the plan list works and every bad input is refused.

    Nothing here reaches Telegram — the refusals all happen before the bot is
    started, and a real card is only posted by a curator pressing the button.
    """
    try:
        package_id = int(ARCHIVE.random_package_id(remote=False) or 0)
        plans = await panel_tour(owner, {"amal": "reja"})
        refused = {
            "guruh": await panel_tour(owner, {"amal": "shakllantirish", "guruh": owner["id"],
                                              "paket": package_id}),
            "paket": await panel_tour(owner, {"amal": "shakllantirish", "guruh": -1001234567890,
                                              "paket": 1}),
            "vaqt": await panel_tour(owner, {"amal": "shakllantirish", "guruh": -1001234567890,
                                             "paket": package_id, "vaqt": "kecha 10:00"}),
            "bekor": await panel_tour(owner, {"amal": "bekor"}),
        }
        limits = plans.get("chegaralar") or {}
        return {"ok": bool(plans.get("ok")) and all(not row.get("ok") for row in refused.values()),
                "rejalar": len(plans.get("rejalar") or []),
                "guruhlar": len(plans.get("guruhlar") or []),
                "chegaralar": sorted(limits),
                "ishlatilgan": int((plans.get("ishlatilgan") or {}).get("count") or 0),
                "rad_javoblari": {name: str(row.get("reason") or "")[:48]
                                  for name, row in refused.items()}}
    except Exception as exc:
        return {"ok": False, "xato": f"{type(exc).__name__}: {exc}"[:180]}


async def app_probe(menu: bool = False) -> dict:
    """Prove the Mini App gate end to end, without leaving a trace in the numbers.

    The signature is checked four ways (valid, tampered, expired, wrong token),
    then the access gate is checked for the owner and for a stranger, then the
    dashboard payload is built once with counting switched off. Everything runs
    inside ``stats.freeze()`` so the probe cannot move a single real counter.
    """
    token = wa.signing_token()
    report: dict = {"ok": False, "manzil": app_menu_url(), "sahifa": wa.APP_PATH,
                    "demo": wa.demo_enabled()}
    if not token:
        report["izoh"] = "BOT_TOKEN yo‘q — imzoni tekshirib bo‘lmaydi"
        return report
    owner = {"id": int(stats.OWNER_ID), "first_name": "Ega", "username": "zakovat_owner"}
    stranger = {"id": 424242, "first_name": "Begona"}
    good = wa.forge_init_data(owner, token)
    checks: dict = {}
    checks["imzo_qabul_qilindi"] = bool(wa.validate_init_data(good, token))
    broken = good[:-1] + ("0" if good[-1] != "0" else "1")
    checks["buzilgan_imzo_rad_etildi"] = wa.validate_init_data(broken, token) is None
    stale = wa.forge_init_data(owner, token,
                               auth_date=int(time.time()) - wa.INIT_DATA_TTL - 60)
    checks["eskirgan_rad_etildi"] = wa.validate_init_data(stale, token) is None
    checks["begona_token_rad_etildi"] = wa.validate_init_data(
        wa.forge_init_data(owner, "999:not-the-token"), token) is None
    outside = wa.validate_init_data(wa.forge_init_data(stranger, token), token) or {}
    checks["begona_id_ko‘rindi"] = int((outside.get("user") or {}).get("id") or 0) == 424242

    state = stats.freeze()
    browse: dict = {"ok": False, "izoh": "tekshirilmadi"}
    composer: dict = {"ok": False, "izoh": "tekshirilmadi"}
    try:
        await stats.load_access()
        checks["ega_kira_oladi"] = wa.can_open(owner["id"])
        checks["begona_kira_olmaydi"] = not wa.can_open(stranger["id"])
        body = await wa.payload(owner, ARCHIVE, count=False)
        panel = {"ok": bool(body.get("ok")), "ega": bool(body.get("owner")),
                 "hisoblagichlar": len(body.get("counters") or {}),
                 "bo‘limlar": len(body.get("sections") or []),
                 "yozuvlar": len(body.get("events") or []),
                 "ruxsatlar": len(body.get("access") or []),
                 "ishlatilgan": int((body.get("used") or {}).get("count") or 0),
                 "rejalar": len(body.get("plans") or []),
                 "guruhlar": len(body.get("groups") or [])}
        checks["javobni_tasdiqlash"] = bool(review_probe().get("ok"))
        browse = browse_probe()
        composer = await composer_probe(owner)
    except Exception as exc:
        panel = {"ok": False, "xato": f"{type(exc).__name__}: {exc}"}
    finally:
        stats.restore(state)
    report["tekshiruvlar"] = checks
    report["panel"] = panel
    report["brauzer"] = browse
    report["kompozitor"] = composer
    if menu:
        report["menyu"] = await apply_menu_buttons()
    report["ok"] = (bool(panel.get("ok")) and bool(browse.get("ok"))
                    and bool(composer.get("ok"))
                    and all(value is True for value in checks.values()))
    report["izoh"] = ("imzo tekshiriladi, faqat ega va /grant olganlar ko‘radi; "
                      "brauzer va kompozitor ham shu darvozadan o‘tadi"
                      if report["ok"] else "biror tekshiruv ishlamadi — pastga qarang")
    return report


async def self_test() -> list[str]:
    """Run synthetic updates through the real handlers without touching Telegram."""
    from .selftest import run_self_test

    try:
        return await run_self_test(build_app)
    except Exception as exc:
        return [f"Sinov xatoligi: {type(exc).__name__}: {exc}"]


@app.get("/")
async def home():
    if wa.demo_enabled():
        # Local preview only (refused whenever VERCEL_ENV is set): opening the
        # site shows the Mini App straight away, so there is no address to type.
        return HTMLResponse(dashboard.render(wa.demo_init_data(), api="/api/app"))
    page = ROOT / "public" / "index.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Zakovat Agent</h1><p>Sahifa fayli topilmadi.</p>")


@app.get("/app")
async def app_page(request: Request):
    """The owner's Mini App: one self-contained page, Telegram-native look.

    The page carries no numbers of its own; it asks ``/api/app`` with the signed
    initData Telegram hands it, and that endpoint decides who sees what.
    """
    init_data = (request.query_params.get("initData")
                 or request.query_params.get("initDataUnsafe") or wa.demo_init_data())
    response = HTMLResponse(dashboard.render(init_data, api="/api/app"))
    response.headers["Cache-Control"] = "no-store"
    return response


async def panel_request(request: Request) -> tuple[dict, dict]:
    """Read one panel call: its body, and who is asking.

    Every panel endpoint goes through here, so the gate is written once: a valid
    Telegram signature *and* a place on the owner's guest list. The answer is
    ``({"user": …, "body": …}, {})`` when allowed, or ``({}, refusal)``.
    """
    body: dict = {}
    if request.method == "POST":
        try:
            posted = await request.json()
        except Exception:
            posted = None
        if isinstance(posted, dict):
            body = posted
    init_data = str(body.get("initData") or body.get("init_data") or "")
    source = str(body.get("manba") or "")
    if not init_data:
        init_data = str(request.query_params.get("initData")
                        or request.query_params.get("auth") or "")
    verified = wa.validate_init_data(init_data)
    if not verified:
        # Saying *why* matters: an empty proof means the page was opened outside
        # Telegram (or the client never handed over #tgWebAppData), a wrong one
        # means the signature or its age failed. Both are refusals, not errors.
        reason = ("initData bo‘sh: sahifa Telegram ichida ochilmagan. Bot menyusidagi "
                  "«Statistika» tugmasidan oching." if not init_data else
                  "Telegram imzosi tekshirilmadi: initData yaroqsiz yoki eskirgan "
                  "(ZAKOVAT_APP_TTL).")
        answer = wa.denied(reason)
        answer["manba"] = source or "yo‘q"
        answer["uzunlik"] = len(init_data)
        return {}, answer
    user = verified.get("user") or {}
    user_id = int(user.get("id") or 0)
    await stats.load_access()
    if not wa.can_open(user_id):
        note("app_denied", user_id=user_id, chat_id=(verified.get("chat") or {}).get("id"),
             detail="mini app urinishi")
        answer = wa.denied()
        answer["manba"] = source or "yo‘q"
        answer["sizning_id"] = user_id
        answer["yechim"] = f"Bot egasiga shu id ni yuboring, u /grant {user_id} Ism deb yozadi."
        return {}, answer
    return {"user": user, "chat": verified.get("chat") or {}, "body": body}, {}


async def app_payload(request: Request) -> dict:
    """Verify who is asking, then answer with the dashboard (or a refusal)."""
    allowed, refusal = await panel_request(request)
    if refusal:
        return refusal
    return await wa.payload(allowed["user"], ARCHIVE, flush=bg_add)


# --------------------------------------------------------------- panel: browse
@app.post("/api/browse")
@app.get("/api/browse")
async def api_browse(request: Request):
    """Sections, packages and questions for the panel's browser."""
    bucket: list = []
    token = _bg_tasks.set(bucket)
    try:
        allowed, refusal = await panel_request(request)
        if refusal:
            answer: dict = refusal
        else:
            try:
                answer = wa.browse(allowed.get("body") or {}, ARCHIVE)
            except Exception as exc:                     # a shard may be unreachable
                logging.getLogger(__name__).warning("browse failed", exc_info=True)
                answer = {"ok": False, "reason": f"Ko‘rib chiqib bo‘lmadi: {exc}"[:200]}
            answer["ishlatilgan_indeks"] = stats.used_index()
            answer["ishlatilgan_indeks"].pop("items", None)
        response: Response = JSONResponse(answer)
        response.headers["Cache-Control"] = "no-store"
    finally:
        _bg_tasks.reset(token)
    return with_background(response, bucket)


# ---------------------------------------------------------------- panel: plans
@app.post("/api/tour")
async def api_tour(request: Request):
    """Compose a tournament in the panel and let the bot post it into a group."""
    bucket: list = []
    token = _bg_tasks.set(bucket)
    try:
        allowed, refusal = await panel_request(request)
        if refusal:
            answer = refusal
        else:
            answer = await panel_tour(allowed["user"], allowed.get("body") or {})
        response: Response = JSONResponse(answer)
        response.headers["Cache-Control"] = "no-store"
    finally:
        _bg_tasks.reset(token)
    return with_background(response, bucket)


async def panel_tour(user: dict, body: dict) -> dict:
    """One panel action about tournaments: list, compose, or cancel a plan."""
    action = str(body.get("amal") or "reja").strip().lower()
    if action in ("bekor", "cancel"):
        return await panel_cancel(user, body)
    if action in ("shakllantirish", "compose", "yuborish"):
        return await panel_compose(user, body)
    return {"ok": True, "amal": "reja", "rejalar": stats.plans(),
            "guruhlar": [{"id": int(key), "title": value}
                         for key, value in sorted(stats.groups().items(),
                                                  key=lambda kv: -int(kv[0]))],
            "chegaralar": {"jamoa": [tn.MIN_TEAMS, tn.MAX_TEAMS],
                           "raund": list(st.TOUR_ROUNDS),
                           "savol": list(st.TOUR_PER_ROUND),
                           "sekund": list(st.TOUR_TIMERS),
                           "eslatma": REMINDER_ARM},
            "ishlatilgan": _used_summary()}


def _used_summary() -> dict:
    """The used-question index without its rows: the panel only shows totals here."""
    index = stats.used_index()
    index.pop("items", None)
    return index


async def panel_cancel(user: dict, body: dict) -> dict:
    """Take a scheduled tournament's plan back (the card itself stays)."""
    try:
        chat_id = int(body.get("guruh") or 0)
        message_id = int(body.get("xabar") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Qaysi reja ekanini tushunmadim."}
    if not chat_id or not message_id:
        return {"ok": False, "reason": "Guruh va karta xabari id si kerak."}
    stats.plan_drop(chat_id, message_id)
    bg_add(lambda: stats.sync(force=True))
    note("tour_cancelled", user_id=int(user.get("id") or 0), chat_id=chat_id,
         detail=f"reja bekor · karta {message_id}")
    return {"ok": True, "amal": "bekor", "rejalar": stats.plans(),
            "xabar": "Eslatma bekor qilindi. Karta guruhda qoldi — «Boshlash» "
                     "tugmasi istalgan paytda ishlaydi."}


async def panel_compose(user: dict, body: dict) -> dict:
    """Build the tournament the panel described and post its card into the group.

    Nothing starts by itself: the card is pinned, the plan is written into the
    statistics document, and at the chosen minute the bot announces it with a
    «Boshlash» button that a curator or admin has to press.
    """
    user_id = int(user.get("id") or 0)
    try:
        chat_id = int(body.get("guruh") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if chat_id >= 0:
        return {"ok": False, "reason": "Guruh tanlanmagan: manfiy id kerak "
                                       "(guruh id si «Guruhlar» ro‘yxatida)."}
    try:
        package_id = int(body.get("paket") or 0)
    except (TypeError, ValueError):
        package_id = 0
    if not ARCHIVE.package_info(package_id):
        return {"ok": False, "reason": "Paket topilmadi — avval «Savollar» bo‘limidan "
                                       "paketni tanlang."}
    when = str(body.get("vaqt") or "").strip()
    start_at = 0
    if when:
        start_at, problem = sched.parse_when(when)
        if problem:
            return {"ok": False, "reason": problem}
    picked: list[int] = []
    for item in (body.get("savollar") or []):
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value and value not in picked:
            picked.append(value)
    picked = picked[:tn.SELECTION_LIMIT]
    info = ARCHIVE.package_info(package_id) or {}
    title = str(body.get("sarlavha") or info.get("title") or TOUR_DEFAULT_TITLE)[:40]

    application = get_application()
    await application.initialize()
    try:
        bot = application.bot
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramError as exc:
            return {"ok": False, "reason": f"Guruhga kirib bo‘lmadi: {str(exc)[:120]}"}
        status = str(getattr(member, "status", "") or "")
        admin = status in ("creator", "administrator") or bool(
            getattr(member, "can_delete_messages", False))
        if not admin and user_id != int(stats.OWNER_ID):
            return {"ok": False, "reason": f"Siz bu guruh admini emassiz (id {user_id}). "
                                           "Turnirni guruh adminlari chiqaradi."}
        tour = tn.Tournament(chat_id=chat_id, title=title, package_id=package_id,
                             creator=user_id,
                             timer=clamp_choice(int(body.get("sekund") or 0) or TIMER,
                                                st.TOUR_TIMERS, TIMER),
                             rounds=clamp_choice(int(body.get("raund") or 0) or 1,
                                                 st.TOUR_ROUNDS, 1),
                             per_round=clamp_choice(int(body.get("savol_soni") or 0) or 10,
                                                    st.TOUR_PER_ROUND, 10),
                             seed=random.randrange(1, 999999))
        tour.include = picked
        tour.max_teams = max(tn.MIN_TEAMS, min(int(body.get("jamoa") or tn.MAX_TEAMS),
                                               tn.MAX_TEAMS))
        tour.start_at = start_at
        tour.curators = [user_id]
        tour.can_delete = await detect_can_delete(bot, chat_id, "supergroup")
        tour.open_mode = not tour.can_delete
        bank = tour_bank_size(ARCHIVE, tour)
        if bank <= 0:
            return {"ok": False, "reason": "Tanlangan savollar bu paketda topilmadi — "
                                           "tanlovni tekshiring."}
        stats.group_seen(chat_id, str(getattr(getattr(member, "chat", None), "title", "")
                                      or body.get("guruh_nomi") or ""))
        await tour_persist(bot, ARCHIVE, tour)
        note("tour_composed", user_id=user_id, chat_id=chat_id, package_id=package_id,
             detail=(f"panel · {title} · {tour.rounds}×{tour.per_round} · {tour.team_limit} "
                     f"jamoa · {len(picked)} tanlov" +
                     (f" · {sched.when_text(start_at)}" if start_at else "")))
        armed = await tour_register_plan(bot, ARCHIVE, tour) if start_at else False
        return {"ok": True, "amal": "shakllantirish",
                "guruh": chat_id, "paket": package_id, "sarlavha": title,
                "karta": int(tour.card_message_id or 0),
                "savollar": bank, "tanlov": len(picked),
                "jamoa": tour.team_limit, "raund": int(tour.rounds),
                "har_raundda": int(tour.per_round), "sekund": int(tour.timer),
                "boshlanish": start_at,
                "boshlanish_matn": sched.when_text(start_at) if start_at else "",
                "eslatma": bool(armed),
                "rejalar": stats.plans(),
                "xabar": (f"Turnir «{title}» guruhga chiqarildi va mahkamlandi. "
                          + (f"Boshlanish: {sched.when_text(start_at)} — bot eslatma va "
                             "«Boshlash» tugmasini yuboradi." if start_at else
                             "«Boshlash» tugmasi hozir ishlaydi."))}
    except TelegramError as exc:
        return {"ok": False, "reason": f"Telegram xatosi: {str(exc)[:160]}"}
    finally:
        await application.shutdown()


@app.post("/api/app")
@app.get("/api/app")
async def api_app(request: Request):
    """Numbers for the Mini App, only for a verified and allowed Telegram user."""
    bucket: list = []
    token = _bg_tasks.set(bucket)
    try:
        response: Response = JSONResponse(await app_payload(request))
        response.headers["Cache-Control"] = "no-store"
    finally:
        _bg_tasks.reset(token)
    return with_background(response, bucket)


@app.get("/api/now")
async def api_now():
    """Server clock, so the Mini App can correct a device with a wrong time."""
    return JSONResponse({"now": time.time()})


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "archive": ARCHIVE.counts()[1]})


@app.post("/api/webhook")
@app.post("/api/index")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    global _updates_seen, _updates_rejected
    if SECRET and not secrets.compare_digest(x_telegram_bot_api_secret_token or "", SECRET):
        _updates_rejected += 1
        note = f"{time.strftime('%H:%M:%S')} 403: webhook secret mos kelmadi"
        if note not in _recent_errors:
            _recent_errors.insert(0, note)
            _recent_errors[:] = _recent_errors[:5]
        return Response(status_code=403)
    data = await request.json()
    # Anything the handlers queue here runs after the response has been sent, so
    # the countdown keeps ticking without keeping Telegram waiting for it.
    bucket: list = []
    token = _bg_tasks.set(bucket)
    try:
        if isinstance(data, dict) and "tick" in data and "update_id" not in data:
            response: Response = await handle_tick(data)
        else:
            update = Update.de_json(data, get_application().bot)
            if not update:
                return Response(status_code=400)
            _updates_seen += 1
            application = get_application()
            await application.initialize()
            try:
                await application.process_update(update)
            finally:
                await application.shutdown()
            bg_add(arm_due_reminders)      # any traffic wakes a scheduled start
            response = JSONResponse({"ok": True})
    finally:
        _bg_tasks.reset(token)
    return with_background(response, bucket)
