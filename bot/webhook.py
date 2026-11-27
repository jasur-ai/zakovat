"""Stateless Telegram webhook entry for Vercel serverless.

Serverless has no long-lived memory, so game state is carried inside the
question message as a compact signed-by-obscurity marker and returned by the
user via Telegram's ForceReply. Timeouts are resolved lazily on the next
interaction. No token or secret is stored in the repository.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os
import random
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from telegram import BotCommand, ForceReply, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import messages as msg
from . import settings as st
from .answers import AnswerChecker, normalize
from .archive import Archive
from .quiz import CORRECT, FOREIGN, IGNORED, WRONG, Session, build_playlist

MARKER = "\n\n#ZS "
TIMER = int(os.getenv("ZAKOVAT_TIMER_SECONDS", "60"))
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
    payload = {
        "m": session.mode,
        "p": session.package_id,
        "i": session.index,
        "c": session.correct,
        "w": session.wrong,
        "o": session.owner_id,
        "q": session.question["id"] if session.question else None,
        "d": deadline,
        "l": latest,
        "a": aliases or [],
        "r": session.rounds,
        "n": session.per_round,
        "t": session.timer_seconds,
        "s": session.seed,
        "k": session.control_message_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return MARKER + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unpack_state(text: str | None) -> dict | None:
    if not text or MARKER.strip() not in text:
        return None
    token = text.split(MARKER.strip(), 1)[1].strip().split()[0] if MARKER.strip() in text else ""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


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


def restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[Session, dict] | None:
    data = state_from(update)
    if not data or data.get("q") is None:
        return None
    archive: Archive = context.application.bot_data["archive"]
    question = archive.question(int(data["q"]))
    if not question:
        return None
    rounds = int(data.get("r") or 1)
    per_round = int(data.get("n") or 0)
    seed = int(data.get("s") or 0)
    package_id = int(data["p"])
    session = Session(
        chat_id=update.effective_chat.id,
        mode=data.get("m", "solo"),
        package_id=package_id,
        package_title=(archive.package(package_id) or {}).get("title", ""),
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
    session.control_message_id = data.get("k")
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


async def delete_control(bot, chat_id: int, session: Session) -> None:
    """Remove the countdown strip once its question is resolved."""
    if not session.control_message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(session.control_message_id))
    except (TelegramError, ValueError, TypeError):
        pass
    session.control_message_id = None


async def send_control(bot, chat_id: int, session: Session, deadline: int,
                       aliases: list[str] | None = None) -> None:
    """Countdown strip above every question: remaining seconds + in-game buttons."""
    remaining = max(0, deadline - int(time.time()))
    head = f"🎯 {session.number}/{session.total}-savol"
    if session.rounds > 1:
        head += f" · {session.current_round}-raund"
    text = (
        f"{head}\n{msg.timer_line(remaining, session.timer_seconds)}\n"
        + pack_state(session, deadline, aliases=aliases)
    )
    message = await bot.send_message(
        chat_id=chat_id, text=text[:4090], parse_mode=ParseMode.HTML,
        reply_markup=st.game_keyboard(), disable_web_page_preview=True,
    )
    session.control_message_id = getattr(message, "message_id", None)


async def send_question(bot, chat_id: int, session: Session, aliases: list[str] | None = None) -> None:
    question = session.question
    if not question:
        return
    timer = session.timer_seconds or TIMER
    deadline = int(time.time()) + timer
    await send_control(bot, chat_id, session, deadline, aliases)

    picture = has_picture(question)
    attachments = [a for a in (question.get("attachments") or []) if isinstance(a, str)]
    note = "" if picture else (attachments[0] if attachments else "")
    round_label = f"{session.current_round}-raund" if session.rounds > 1 else ""
    text = msg.question(
        session.number, session.total, session.package_title, question.get("text"),
        timer, session.score_text(), note, round_label, timer,
    )
    if picture:
        await send_photo_safely(bot, chat_id, question, caption="🖼 Savol rasmi")
    await bot.send_message(
        chat_id=chat_id,
        text=(text + pack_state(session, deadline, aliases=aliases))[:4090],
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="Javobingizni yozing..."),
        disable_web_page_preview=True,
    )


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
            await bot.send_photo(chat_id=chat_id, photo=pictures[0], caption="🖼 Javob rasmi")
        except TelegramError:
            await bot.send_message(chat_id=chat_id, text=f"🖼 {_esc(pictures[0])}",
                                   disable_web_page_preview=True)


async def next_question(bot, chat_id: int, session: Session, headline: str, aliases: list[str] | None = None) -> None:
    await delete_control(bot, chat_id, session)
    await reveal(bot, chat_id, session, headline)
    previous_round = session.current_round
    session.index += 1
    session.advance()
    if session.question is None:
        percent = round(session.correct * 100 / session.total) if session.total else 0
        await bot.send_message(
            chat_id=chat_id,
            text=msg.summary(session.package_title, session.total, session.correct, session.wrong,
                             percent, session.rounds),
            parse_mode=ParseMode.HTML,
        )
        return
    if session.rounds > 1 and session.current_round != previous_round:
        await bot.send_message(
            chat_id=chat_id,
            text=msg.round_banner(session.current_round, session.rounds, session.score_text()),
            parse_mode=ParseMode.HTML,
        )
    await send_question(bot, chat_id, session, aliases)


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
        text += f"\n\nℹ️ Bazada {available} savol bor — qolgan {total - available} savol takrorlanadi."
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
                            chat_id: int, user_id: int) -> None:
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
    label = "Yakka o‘yin" if config.mode == "solo" else "Guruh o‘yini"
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg.game_start(label, session.package_title, session.total, session.timer_seconds,
                            session.score_text(), session.rounds, session.per_round),
        parse_mode=ParseMode.HTML,
    )
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
            await query.edit_message_text("❌ O‘yin bekor qilindi. <code>/solo</code> bilan qayta boshlang.",
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
    await start_from_config(context, config, query.message.chat_id, query.from_user.id)


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
        if session.resolve_unanswered():
            await next_question(context.bot, chat_id, session,
                                "⏰ Vaqt tugadi. Ochko Qarshi tarafga qo‘shildi.", aliases)
        return

    result = session.submit(update.effective_user.id, update.message.text, checker_for(aliases))
    if result == CORRECT:
        await next_question(
            context.bot, chat_id, session,
            f"🎉 To‘g‘ri! {_esc(update.effective_user.first_name)} Bilimdonlar uchun ochko qozondi.", aliases,
        )
    elif result == FOREIGN:
        await update.message.reply_text("Bu yakka o‘yin — faqat boshlagan kishi javob yozadi.")
    elif result == WRONG and session.mode == "solo":
        text = "❌ Noto‘g‘ri. Yana urinib ko‘ring." + pack_state(session, deadline, latest=update.message.text,
                                                                 aliases=aliases)
        await update.message.reply_text(text[:4090], reply_markup=ForceReply(input_field_placeholder="Yangi javob..."))


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
        if remaining > 0 and not session.answered:
            await query.answer(f"⏳ {remaining} sekund qoldi")
            head = f"🎯 {session.number}/{session.total}-savol"
            if session.rounds > 1:
                head += f" · {session.current_round}-raund"
            text = (f"{head}\n{msg.timer_line(remaining, session.timer_seconds)}\n"
                    + pack_state(session, deadline, state.get("l"), aliases))
            try:
                await query.edit_message_text(text[:4090], parse_mode=ParseMode.HTML,
                                              reply_markup=st.game_keyboard())
            except TelegramError:
                pass
            return
        await query.answer("⌛ Vaqt tugadi")
        if session.resolve_unanswered():
            await next_question(context.bot, chat_id, session,
                                "⏰ Vaqt tugadi. Ochko Qarshi tarafga qo‘shildi.", aliases)
        return

    if action == "k":
        if not await is_admin(update, context, session):
            await query.answer("⛔ O‘tkazish faqat admin uchun", show_alert=True)
            return
        await query.answer("⏭ O‘tkazildi")
        if session.resolve_unanswered():
            await next_question(context.bot, chat_id, session,
                                "⏭ O‘tkazildi — hech kim javobni topmadi.", aliases)
        else:
            await query.answer("Bu savol allaqachon yechilgan.", show_alert=True)
        return

    if action == "c":
        await query.answer(f"{session.score_text()} · {session.number}/{session.total}-savol")
        return

    if action == "x":
        await query.answer("O‘yin to‘xtatildi")
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
    if not session.resolve_unanswered():
        await update.effective_message.reply_text("Bu savol allaqachon yechilgan.")
        return
    await next_question(context.bot, update.effective_chat.id, session,
                        "⏭ O‘tkazildi — hech kim javobni topmadi.", data.get("a") or [])


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("To‘xtatish uchun savol xabariga reply qiling.")
        return
    session, _ = restored
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
        await next_question(context.bot, update.effective_chat.id, session,
                            "✅ Javob qabul qilindi va o‘rganildi.", aliases)
    else:
        await update.effective_message.reply_text("Bu javob hali mos kelmadi.")


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
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("rules", cmd_rules),
        CommandHandler("packages", cmd_packages),
        CommandHandler("solo", cmd_solo),
        CommandHandler("quiz", cmd_solo),
        CommandHandler("duel", cmd_duel),
        CommandHandler("score", cmd_score),
        CommandHandler("skip", cmd_skip),
        CommandHandler("stop", cmd_stop),
        CommandHandler("accept", cmd_accept),
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        CallbackQueryHandler(on_settings_button, pattern="^" + st.PREFIX_CONFIG + r"\|"),
        CallbackQueryHandler(on_page_button, pattern="^" + st.PREFIX_PAGE + r"\|"),
        CallbackQueryHandler(on_game_button, pattern="^" + st.PREFIX_GAME + r"\|"),
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
        _webhook_ready = True
        _setup_error = ""
    except Exception as exc:  # telegram API errors must not break the site
        _setup_error = str(exc)


async def bootstrap_webhook() -> None:
    """Runs on any first HTTP request so a plain site visit registers the webhook."""
    if _webhook_ready or _setup_attempted or not os.getenv("BOT_TOKEN"):
        return
    application = get_application()
    await application.initialize()
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


@app.get("/api/index")
async def index(request: Request):
    """Vercel exposes api/index.py at this path; it doubles as a diagnostics page."""
    mode = request.query_params.get("test") or request.query_params.get("setup")
    if request.query_params.get("setup") == "1" or mode == "setup":
        return await setup(request)
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
    return JSONResponse({"ok": True})
