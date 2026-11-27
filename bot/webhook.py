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

from telegram import BotCommand, BotCommandScopeChat, ForceReply, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import clock as clock_page
from . import messages as msg
from . import settings as st
from . import stats
from .answers import AnswerChecker, normalize
from .archive import Archive
from .quiz import CORRECT, FOREIGN, IGNORED, WRONG, Session, build_playlist

MARKER = "\n\n#ZS2 "
LEGACY_MARKER = "\n\n#ZS "
TIMER = int(os.getenv("ZAKOVAT_TIMER_SECONDS", "60"))
TICK_SECONDS = max(2, int(os.getenv("ZAKOVAT_TICK_SECONDS", "5")))
IDLE_LIMIT = max(0, int(os.getenv("ZAKOVAT_IDLE_LIMIT", "3")))
TICKS_ENABLED = os.getenv("ZAKOVAT_LIVE_TIMER", "1") not in ("0", "false", "no")
TICK_MODE = os.getenv("ZAKOVAT_TICK_MODE", "bg")     # "bg" (after response) | "sleep"
MAX_TICK_HORIZON = 900          # a countdown never runs longer than this
PROBE_WAIT = 4                  # seconds used by the ?bgprobe= diagnostic

# Work queued while handling a request, run after the response has been sent.
_bg_tasks: "contextvars.ContextVar[list]" = contextvars.ContextVar("bg_tasks", default=None)  # type: ignore[assignment]
_bg_probe_log: list[str] = []

# The game state rides along inside the message, but nobody should see it:
# ZWNJ/ZWJ are kept verbatim by Telegram (real scripts depend on them) and are
# zero-width, so unlike a spoiler there is nothing on screen to tap open.
ZW0 = "\u200c"
ZW1 = "\u200d"
INVISIBLE = (ZW0, ZW1)
SENTINEL = 0xA5
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
    extra = ""
    if latest or aliases:
        raw = json.dumps({"l": latest, "a": aliases or []}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        extra = "|" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return core + extra


def encode_invisible(body: str) -> str:
    """Write the state as a run of zero-width characters (one bit each)."""
    data = body.encode("utf-8")
    payload = bytes([SENTINEL]) + data + bytes([sum(data) & 0xFF])
    return "".join(ZW1 if bit == "1" else ZW0 for byte in payload for bit in f"{byte:08b}")


def _decode_run(run: list[str]) -> str | None:
    """Turn one zero-width run back into text, checking sentinel and checksum."""
    if len(run) < 24 or len(run) % 8:
        return None
    payload = bytearray(
        int("".join("1" if c == ZW1 else "0" for c in run[i:i + 8]), 2)
        for i in range(0, len(run), 8)
    )
    if payload[0] != SENTINEL:
        return None
    data, checksum = bytes(payload[1:-1]), payload[-1]
    if not data or (sum(data) & 0xFF) != checksum:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def decode_invisible(text: str) -> str | None:
    """Read the zero-width state back out of a message, wherever it sits.

    Any run qualifies only with both the sentinel byte and the checksum, so text
    that happens to contain zero-width characters of its own is ignored. The
    longest valid run wins.
    """
    found: str | None = None
    longest = 0
    run: list[str] = []
    for char in str(text or "") + " ":          # the tail flushes the last run
        if char in INVISIBLE:
            run.append(char)
            continue
        if run:
            body = _decode_run(run)
            if body is not None and len(run) > longest:
                found, longest = body, len(run)
            run = []
    return found


def hide_state(body: str) -> str:
    """Invisible by default; ZAKOVAT_HIDDEN_STATE=spoiler keeps the old look."""
    if HIDDEN_MODE == "spoiler":
        return f'<span class="tg-spoiler">{html.escape(MARKER + body)}</span>'
    return encode_invisible(body)


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
            "x": bool(int(parts[15])), "l": None, "a": [],
        }
    except ValueError:
        return None
    if len(parts) > 16 and parts[16]:
        try:
            raw = base64.urlsafe_b64decode(parts[16] + "=" * (-len(parts[16]) % 4))
            extra = json.loads(raw.decode("utf-8"))
            payload["l"] = extra.get("l")
            payload["a"] = extra.get("a") or []
        except (ValueError, UnicodeDecodeError):
            pass
    return payload


def unpack_state(text: str | None) -> dict | None:
    """Read the state: invisible run first, then the older visible markers."""
    if not text:
        return None
    body = decode_invisible(text)
    if body is not None:
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


def close_question(session: Session, kind: str = "unanswered") -> bool:
    """Resolve the running question once (timeout or admin skip) and count it."""
    if not session.resolve_unanswered():
        return False
    note(kind, chat_id=session.chat_id, package_id=session.package_id,
         unanswered=1, skips=1 if kind == "skip" else 0,
         detail=f"{session.mode} · {session.package_id} · savol {session.number}")
    return True


def clock_url(session: Session, deadline: int, remaining: int) -> str:
    """Mini App clock for this question; empty when there is no public address."""
    base = public_base_url()
    if not base:
        return ""
    query = urlencode({
        "d": int(deadline),
        "l": max(0, int(remaining)),
        "tm": session.timer_seconds or TIMER,
        "n": session.number,
        "t": session.total,
        "r": session.current_round if session.rounds > 1 else 0,
        "p": (session.package_title or "")[:60],
    })
    return f"{base}/api/timer?{query}"


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
    """Rewrite the countdown strip, giving up the clock button if Telegram refuses it.

    An old client or a rejected Web App URL must never cost the player the
    countdown, so the same strip is offered once more without the Mini App row.
    """
    text = strip_text(session, remaining, deadline, aliases)
    plain = st.game_keyboard(clock=msg.clock(remaining))
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=int(session.control_message_id), text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=st.game_keyboard(clock=msg.clock(remaining),
                                          clock_url=clock_url(session, deadline, remaining)),
        )
    except TelegramError as exc:
        if not is_webapp_error(exc):
            raise
        logging.getLogger(__name__).warning("Mini App button refused: %s", exc)
        await bot.edit_message_text(
            chat_id=chat_id, message_id=int(session.control_message_id), text=text,
            parse_mode=ParseMode.HTML, reply_markup=plain,
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
                        wait: int | None = None, mode: str | None = None) -> None:
    """Keep the countdown moving: wait, repaint, resolve, chain the next hop.

    Serverless functions freeze between requests, so the timer is a chain of
    short hops. If a hop is ever lost the game still ends correctly — the
    deadline is also checked lazily on every reply and button press.
    """
    # Without delete rights a resolved strip cannot be removed, so a late tick
    # would repaint a question that is already closed. Those chats keep the
    # manual "Vaqtni yangilash" button instead.
    if not TICKS_ENABLED or not session.control_message_id or not session.can_delete:
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
            reply_markup=st.game_keyboard(clock=msg.clock(remaining),
                                          clock_url=clock_url(session, deadline, remaining)),
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        if not is_webapp_error(exc):
            raise
        logging.getLogger(__name__).warning("Mini App button refused: %s", exc)
        message = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=st.game_keyboard(clock=msg.clock(remaining)),
            disable_web_page_preview=True,
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
    found = archive.search(argument, limit=1)
    return (int(found[0]["id"]), found[0]["title"]) if found else None


async def render_settings(context: ContextTypes.DEFAULT_TYPE, config: st.GameConfig,
                          message=None, query=None) -> None:
    archive = _archive_of(context)
    info = archive.package_info(config.package_id) or {}
    available = int(info.get("questions") or 0)
    title = info.get("title") or "(sarlavhasiz)"
    per_round = config.per_round if config.per_round > 0 else available
    total = config.rounds * per_round
    text = msg.settings_panel(config.package_id, title, available, config.rounds,
                              config.per_round, config.timer, config.mode, total)
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
        await context.bot.send_message(chat_id, "Bu paketda savol yo‘q. /packages orqali boshqasini tanlang.")
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
    await update.effective_message.reply_text(msg.welcome(packages, questions), parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.help_text(), parse_mode=ParseMode.HTML)


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
    text = msg.packages_page(result, query)[:4000]
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


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    if int(time.time()) > deadline:
        if await close_strip(context.bot, chat_id, session) and close_question(session):
            await next_question(context.bot, chat_id, session,
                                "Vaqt tugadi. Ochko Qarshi tarafga.", aliases)
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
    session.control_message_id = query.message.message_id
    chat_id = query.message.chat_id
    aliases = list(state.get("a") or [])
    deadline = int(state.get("d", 0))
    remaining = deadline - int(time.time())

    if action == "f":
        note("refresh", user_id=getattr(query.from_user, "id", None), chat_id=chat_id,
             package_id=session.package_id, refreshes=1, log=False)
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
        await query.answer("O‘tkazildi")
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
    claimed = await close_strip(context.bot, update.effective_chat.id, session)
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


def is_owner(update: Update) -> bool:
    """Only the owner's Telegram id ever sees the statistics panel."""
    user = getattr(update, "effective_user", None)
    return bool(user) and int(getattr(user, "id", 0) or 0) == int(stats.OWNER_ID)


async def render_stats(context: ContextTypes.DEFAULT_TYPE, view: str = "main",
                       message=None, callback=None) -> None:
    """Build one statistics page and either send it or rewrite the panel."""
    data = await stats.snapshot(_archive_of(context))
    render = {"sections": msg.stats_sections, "events": msg.stats_events}.get(view, msg.stats_panel)
    text = render(data)[:4090]
    keyboard = st.stats_keyboard("main" if view == "main" else view)
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
        await update.effective_message.reply_text(msg.stats_denied())
        return
    view = (context.args[0].strip().lower() if context.args else "main")
    view = {"bo'limlar": "sections", "bolimlar": "sections", "b": "sections",
            "o'yinlar": "events", "oyinlar": "events", "e": "events"}.get(view, view)
    if view not in ("main", "sections", "events"):
        view = "main"
    await render_stats(context, view, message=update.effective_message)


async def on_stats_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if not data.startswith(st.PREFIX_STATS + "|"):
        return
    if not is_owner(update):
        await query.answer(msg.stats_denied(), show_alert=True)
        return
    view = {"r": "main", "b": "sections", "e": "events"}.get(data.split("|", 1)[1], "main")
    await query.answer()
    await render_stats(context, view, callback=query)


_recent_errors: list[str] = []


async def on_handler_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the last few handler errors so they can be shown on /api/index."""
    error = context.error
    entry = f"{time.strftime('%H:%M:%S')} {type(error).__name__}: {error}"
    _recent_errors.insert(0, entry)
    _recent_errors[:] = _recent_errors[:5]
    logging.getLogger(__name__).exception("Handler failed", exc_info=error)


BOT_COMMANDS = [
    ("start", "Tanishtiruv"),
    ("help", "Barcha buyruqlar"),
    ("rules", "O‘yin qoidalari"),
    ("packages", "Barcha paketlar (sahifalab)"),
    ("solo", "Yakka o‘yin"),
    ("duel", "Guruh o‘yini"),
    ("score", "Joriy hisob"),
    ("skip", "Savolni o‘tkazish (admin)"),
    ("accept", "Javobni qabul qilish (admin)"),
    ("stop", "O‘yinni to‘xtatish"),
]
OWNER_COMMANDS = BOT_COMMANDS + [("stats", "Statistika (faqat bot egasi)")]

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
    "stats": cmd_stats,
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
        note("command", user_id=getattr(getattr(update, "effective_user", None), "id", None),
             chat_id=getattr(getattr(update, "effective_chat", None), "id", None),
             commands=1, detail=f"/{name}", log=False)
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
        command_handler("stats", cmd_stats),
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        CallbackQueryHandler(on_settings_button, pattern="^" + st.PREFIX_CONFIG + r"\|"),
        CallbackQueryHandler(on_page_button, pattern="^" + st.PREFIX_PAGE + r"\|"),
        CallbackQueryHandler(on_game_button, pattern="^" + st.PREFIX_GAME + r"\|"),
        CallbackQueryHandler(on_stats_button, pattern="^" + st.PREFIX_STATS + r"\|"),
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


def archive_lines() -> list[str]:
    packages, questions = ARCHIVE.counts()
    loaded = len(ARCHIVE._load()) if hasattr(ARCHIVE, "_load") else 0
    root = ARCHIVE.root.resolve()
    return [
        f"Arxiv manifesti: {packages} paket, {questions} savol",
        f"Arxiv katalogi: {root} ({'bor' if root.exists() else 'YO‘Q — fayllar deploy bo‘lmagan'})",
        f"Yuklangan paketlar: {loaded}",
    ]


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
async def status():
    """Diagnostic page: shows only whether secrets exist, never their values."""
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
        f"Statistika: {stats.STORE_URL.split('//')[-1].split('/')[0] if stats.STORE_URL else 'faqat shu nusxa'}"
        f" · egasi {stats.OWNER_ID} · /stats · ?statsprobe=1",
        f"Jonli soat: /api/timer (Mini App) · server vaqti: /api/now",
        f"Yashirin holat: {HIDDEN_MODE} · fon ishi: "
        f"{_bg_probe_log[-1] if _bg_probe_log else 'tekshirilmagan'} · "
        f"diagnostika: ?tickprobe=1 · ?bgprobe=1",
        f"Shu nusxadagi taymer urinishlari: {_ticks_seen} · yangilangan: {_ticks_painted} · "
        f"vaqti tugagan: {_ticks_resolved} · to‘xtagan: {_ticks_stopped}",
    ]
    lines += archive_lines()
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
        report = await stats.probe()
        report["hisoblagichlar"] = dict(stats._pending["c"])
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
    return await status()


async def self_test() -> list[str]:
    """Run synthetic updates through the real handlers without touching Telegram."""
    from .selftest import run_self_test

    try:
        return await run_self_test(build_app)
    except Exception as exc:
        return [f"Sinov xatoligi: {type(exc).__name__}: {exc}"]


@app.get("/")
async def home():
    page = ROOT / "public" / "index.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Zakovat Agent</h1><p>Sahifa fayli topilmadi.</p>")


@app.get("/api/timer")
async def timer_page(request: Request):
    """Mini App clock face: counts down on the user's device, not on the server."""
    params = dict(request.query_params)
    bucket: list = []
    token = _bg_tasks.set(bucket)
    try:
        if str(params.get("demo") or "") != "1":
            note("clock_opened", clock_opened=1, log=False)
        response: Response = HTMLResponse(clock_page.render(params))
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
            response = JSONResponse({"ok": True})
    finally:
        _bg_tasks.reset(token)
    return with_background(response, bucket)
