"""Telegram bot wired to the local Zakovat archive (long-polling variant).

Run with BOT_TOKEN in the environment (never in a file):
    python3 run_bot.py

The polling bot keeps game state in memory and uses the job queue, so the
countdown strip under every question really ticks. The serverless webhook in
bot/webhook.py offers the same game with a refresh-on-demand countdown.
"""
from __future__ import annotations

import html
import os
import random
import re
import time
from pathlib import Path

from telegram import BotCommand, ForceReply, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

from . import messages as msg
from . import settings as st
from .answers import AnswerChecker
from .archive import Archive
from .quiz import CORRECT, FOREIGN, IGNORED, WRONG, Session, build_playlist

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / os.getenv("ZAKOVAT_ARCHIVE_DIR", "data/all")
ALIAS_FILE = os.getenv("ZAKOVAT_ALIAS_FILE", "data/answer_aliases.json")
DEFAULT_TIMER = int(os.getenv("ZAKOVAT_TIMER_SECONDS", "60"))
PER_PAGE = int(os.getenv("ZAKOVAT_PACKAGES_PER_PAGE", "12"))
IMAGES = ROOT / "data" / "images"
TICK_SECONDS = max(2, int(os.getenv("ZAKOVAT_TICK_SECONDS", "6")))
IDLE_LIMIT = max(0, int(os.getenv("ZAKOVAT_IDLE_LIMIT", "3")))

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


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _sessions(context: ContextTypes.DEFAULT_TYPE) -> dict[int, Session]:
    return context.application.bot_data.setdefault("sessions", {})


def _archive(context: ContextTypes.DEFAULT_TYPE) -> Archive:
    if "archive" not in context.application.bot_data:
        context.application.bot_data["archive"] = Archive(ARCHIVE_DIR)
    return context.application.bot_data["archive"]


def _checker(context: ContextTypes.DEFAULT_TYPE) -> AnswerChecker:
    if "checker" not in context.application.bot_data:
        context.application.bot_data["checker"] = AnswerChecker(ALIAS_FILE)
    return context.application.bot_data["checker"]


def _current(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Session | None:
    return _sessions(context).get(chat_id)


def _jobs(context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    """Cancel every timer job belonging to this session."""
    for name in (session.job_name, f"tick-{session.chat_id}"):
        if not name or context.job_queue is None:
            continue
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    session.job_name = None


async def _delete_control(context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    if not session.control_message_id:
        return
    try:
        await context.bot.delete_message(chat_id=session.chat_id, message_id=session.control_message_id)
    except TelegramError:
        pass
    session.control_message_id = None


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session | None = None) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    if chat.type == "private":
        return True
    if session is not None and int(session.owner_id) == int(user.id):
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return False
    return member.status in ("creator", "administrator")


def _resolve_package(context: ContextTypes.DEFAULT_TYPE, argument: str) -> tuple[int, str] | None:
    archive = _archive(context)
    argument = (argument or "").strip()
    if not argument:
        package_id = archive.random_package_id()
        return package_id, (archive.package_info(package_id) or {}).get("title", "")
    if argument.isdigit():
        info = archive.package_info(int(argument))
        return (int(argument), info.get("title", "")) if info and info.get("questions") else None
    matches = archive.search(argument, limit=1)
    if matches:
        return int(matches[0]["id"]), matches[0]["title"]
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0].isdigit():
        await open_settings(update, context, "solo", context.args[0])
        return
    packages, questions = _archive(context).counts()
    await update.effective_message.reply_text(msg.welcome(packages, questions), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.help_text(), parse_mode=ParseMode.HTML)


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    result = _archive(context).list_packages(query, page, PER_PAGE)
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


async def packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


def _panel_text(context: ContextTypes.DEFAULT_TYPE, config: st.GameConfig) -> str:
    archive = _archive(context)
    info = archive.package_info(config.package_id) or {}
    available = int(info.get("questions") or 0)
    title = info.get("title") or "(sarlavhasiz)"
    per_round = config.per_round if config.per_round > 0 else available
    total = config.rounds * per_round
    text = msg.settings_panel(config.package_id, title, available, config.rounds,
                              config.per_round, config.timer, config.mode, total)
    if available and total > available:
        text += f"\n\nBazada {available} savol bor — qolgan {total - available} savol takrorlanadi."
    return text


async def open_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, argument: str) -> None:
    resolved = _resolve_package(context, argument)
    if argument.strip() and not resolved:
        await update.effective_message.reply_text(
            "Paket topilmadi. <code>/packages</code> orqali qidiring.", parse_mode=ParseMode.HTML,
        )
        return
    package_id = resolved[0] if resolved else _archive(context).random_package_id()
    config = st.GameConfig(package_id=package_id, mode=mode, seed=random.randrange(1, 999999))
    await update.effective_message.reply_text(
        _panel_text(context, config), parse_mode=ParseMode.HTML,
        reply_markup=st.settings_keyboard(config), disable_web_page_preview=True,
    )


async def on_settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parsed = st.decode(query.data)
    if not parsed:
        return
    config, action, value = parsed
    archive = _archive(context)

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
        try:
            await query.edit_message_text(_panel_text(context, config), parse_mode=ParseMode.HTML,
                                          reply_markup=st.settings_keyboard(config))
        except TelegramError:
            pass
        return

    await query.answer("O‘yin boshlandi")
    await start_from_config(context, config, query.message.chat_id, query.from_user.id)


async def start_from_config(context: ContextTypes.DEFAULT_TYPE, config: st.GameConfig,
                            chat_id: int, user_id: int) -> None:
    archive = _archive(context)
    questions = archive.questions(config.package_id)
    if not questions:
        await context.bot.send_message(chat_id, "Bu paketda savol yo‘q. /packages orqali boshqasini tanlang.")
        return

    old = _current(context, chat_id)
    if old:
        _jobs(context, old)
        await _delete_control(context, old)

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
    session.idle_limit = IDLE_LIMIT
    _sessions(context)[chat_id] = session
    label = "Yakka o‘yin" if config.mode == "solo" else "Guruh o‘yini"
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg.game_start(label, session.package_title, session.total, session.timer_seconds,
                            session.score_text(), session.rounds, session.per_round),
        parse_mode=ParseMode.HTML,
    )
    session.begin()
    await _send_question(chat_id, context, session)


async def solo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await open_settings(update, context, "solo", " ".join(context.args or []))


async def duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await open_settings(update, context, "group", " ".join(context.args or []))


def _local_image(question_id: int) -> Path | None:
    for suffix in (".png", ".jpg"):
        candidate = IMAGES / f"{question_id}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _has_picture(question: dict) -> bool:
    if _local_image(int(question.get("id", 0))):
        return True
    return any(isinstance(a, str) and a.startswith("http") for a in (question.get("attachments") or []))


async def _send_picture(context: ContextTypes.DEFAULT_TYPE, chat_id: int, question: dict) -> bool:
    urls = [a for a in (question.get("attachments") or []) if isinstance(a, str) and a.startswith("http")]
    if urls:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=urls[0], caption="Savol rasmi")
            return True
        except TelegramError:
            pass
    path = _local_image(int(question.get("id", 0)))
    if path:
        try:
            with path.open("rb") as handle:
                await context.bot.send_photo(chat_id=chat_id, photo=InputFile(handle), caption="Savol rasmi")
            return True
        except (TelegramError, OSError):
            return False
    return False


def _strip_text(session: Session, remaining: int) -> str:
    head = f"Savol {session.number} / {session.total}"
    if session.rounds > 1:
        head += f" · {session.current_round}-raund"
    return f"{head}\n{msg.timer_line(remaining, session.timer_seconds)}"


def _percent(session: Session) -> int:
    return round(session.correct * 100 / session.total) if session.total else 0


async def _send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    question = session.question
    if question is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg.summary(session.package_title, session.total, session.correct, session.wrong,
                             _percent(session), session.rounds),
            parse_mode=ParseMode.HTML,
        )
        _sessions(context).pop(chat_id, None)
        return

    if session.idle_limit_reached:
        # Three questions in a row without a single guess: the game stops itself.
        session.auto_stopped = True
        session.finished = True
        _jobs(context, session)
        await _delete_control(context, session)
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg.auto_stop(session.idle_streak, session.package_title, session.index,
                               session.correct, session.wrong, _percent(session), session.rounds),
            parse_mode=ParseMode.HTML,
        )
        _sessions(context).pop(chat_id, None)
        return

    if session.idle_streak and session.idle_limit and session.idle_streak < session.idle_limit:
        await context.bot.send_message(
            chat_id=chat_id, text=msg.idle_warning(session.idle_streak, session.idle_limit),
            parse_mode=ParseMode.HTML,
        )

    timer = session.timer_seconds
    strip = await context.bot.send_message(
        chat_id=chat_id, text=_strip_text(session, timer), parse_mode=ParseMode.HTML,
        reply_markup=st.game_keyboard(), disable_web_page_preview=True,
    )
    session.control_message_id = strip.message_id

    picture = _has_picture(question)
    attachments = [a for a in (question.get("attachments") or []) if isinstance(a, str)]
    note = "" if picture else (attachments[0] if attachments else "")
    round_label = f"{session.current_round}-raund" if session.rounds > 1 else ""
    text = msg.question(
        session.number, session.total, session.package_title, question.get("text"),
        timer, session.score_text(), note, round_label,
    )
    if picture:
        await _send_picture(context, chat_id, question)
    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="Javobingizni yozing..."),
        disable_web_page_preview=True,
    )

    if context.job_queue is None:
        return
    session.deadline = int(time.time()) + timer
    session.job_name = f"timeout-{chat_id}-{session.number}"
    context.job_queue.run_once(
        _on_timeout, when=timer,
        data={"chat_id": chat_id, "question_id": question["id"], "issue": session.number},
        name=session.job_name,
    )
    context.job_queue.run_repeating(
        _on_tick, interval=TICK_SECONDS, first=TICK_SECONDS,
        data={"chat_id": chat_id, "question_id": question["id"], "issue": session.number},
        name=f"tick-{chat_id}",
    )


async def _on_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Live countdown: rewrite the strip under the current question."""
    data = (context.job.data or {}) if context.job else {}
    session = _current(context, data.get("chat_id"))
    if not session or session.question is None or session.answered or not session.control_message_id:
        return
    if int(session.question["id"]) != int(data.get("question_id")) or session.number != data.get("issue"):
        return
    remaining = max(0, int(session.deadline) - int(time.time()))
    try:
        await context.bot.edit_message_text(
            chat_id=session.chat_id, message_id=session.control_message_id,
            text=_strip_text(session, remaining), parse_mode=ParseMode.HTML,
            reply_markup=st.game_keyboard(),
        )
    except TelegramError:
        pass


async def _reveal(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: Session, headline: str) -> None:
    question = session.question
    if not question:
        return
    answer = re.sub(r"^\s*(?:to'?g'?ri\s+)?javob\s*[:.]\s*", "", str(question.get("answer", "")), flags=re.I)
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg.reveal(headline, answer, question.get("comment") or "", "", session.score_text()),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )
    pictures = [a for a in (question.get("answer_attachments") or []) if isinstance(a, str) and a.startswith("http")]
    if pictures:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=pictures[0], caption="Javob rasmi")
        except TelegramError:
            await context.bot.send_message(chat_id=chat_id, text=f"Rasm: {_esc(pictures[0])}",
                                           disable_web_page_preview=True)


async def _advance(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: Session, headline: str) -> None:
    _jobs(context, session)
    await _delete_control(context, session)
    await _reveal(chat_id, context, session, headline)
    previous_round = session.current_round
    session.index += 1
    session.advance()
    if session.question is not None and session.rounds > 1 and session.current_round != previous_round:
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg.round_banner(session.current_round, session.rounds, session.score_text()),
            parse_mode=ParseMode.HTML,
        )
    await _send_question(chat_id, context, session)


async def _on_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    data = job.data or {}
    session = _current(context, data.get("chat_id"))
    if not session or session.question is None or session.answered:
        return
    if int(session.question["id"]) != int(data.get("question_id")) or session.number != data.get("issue"):
        return
    if session.resolve_unanswered():
        await _advance(data["chat_id"], context, session, "Vaqt tugadi. Ochko Qarshi tarafga.")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    session = _current(context, update.effective_chat.id)
    if not session or session.question is None or session.answered:
        reply = msg.small_talk(update.message.text)
        if reply:
            await update.message.reply_text(reply, parse_mode=ParseMode.HTML)
        return
    result = session.submit(update.effective_user.id, update.message.text, _checker(context))
    if result == FOREIGN:
        await update.message.reply_text("Bu yakka o‘yin — javobni faqat o‘yinni boshlagan kishi yozadi.")
    elif result == WRONG and session.mode == "solo":
        await update.message.reply_text("Noto‘g‘ri. Yana urinib ko‘ring yoki /skip qiling.")
    elif result == CORRECT:
        await _advance(
            update.effective_chat.id, context, session,
            f"To‘g‘ri! {_esc(update.effective_user.first_name)} Bilimdonlar uchun ochko qozondi.",
        )


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _current(context, update.effective_chat.id)
    if not session:
        await update.effective_message.reply_text("Hozirda o‘yin yo‘q. /solo yoki /duel bilan boshlang.")
        return
    await update.effective_message.reply_text(
        f"{_esc(session.score_text())}\nSavol: {session.number}/{session.total}"
        + (f"\nRaund: {session.current_round}/{session.rounds}" if session.rounds > 1 else "")
    )


async def cmd_skip(context: ContextTypes.DEFAULT_TYPE, session: Session, chat_id: int) -> None:
    if session.resolve_unanswered():
        await _advance(chat_id, context, session, "O‘tkazildi — hech kim javobni topmadi.")


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _current(context, update.effective_chat.id)
    if not session or session.question is None or session.answered:
        await update.effective_message.reply_text("O‘tkazish uchun faol savol kerak.")
        return
    if not await is_admin(update, context, session):
        await update.effective_message.reply_text(msg.admin_only("/skip"), parse_mode=ParseMode.HTML)
        return
    await cmd_skip(context, session, update.effective_chat.id)


async def accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin confirms the last wrong answer; the checker learns it as an alias."""
    session = _current(context, update.effective_chat.id)
    if not session or session.question is None or not session.latest_answer:
        await update.effective_message.reply_text("O‘rganish uchun oxirgi noto‘g‘ri javob yo‘q.")
        return
    if not await is_admin(update, context, session):
        await update.effective_message.reply_text(msg.admin_only("/accept"), parse_mode=ParseMode.HTML)
        return
    answer = session.latest_answer
    user_id = session.latest_user or update.effective_user.id
    _checker(context).learn(int(session.question["id"]), answer)
    result = session.submit(user_id, answer, _checker(context))
    if result == CORRECT:
        await _advance(update.effective_chat.id, context, session, "Javob qabul qilindi va o‘rganildi.")
    else:
        await update.effective_message.reply_text("Javob hali mos kelmadi; boshqa varianti yuboring.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = _current(context, chat_id)
    if not session:
        await update.effective_message.reply_text("Faol o‘yin yo‘q.")
        return
    _jobs(context, session)
    await _delete_control(context, session)
    _sessions(context).pop(chat_id, None)
    await update.effective_message.reply_text(session.summary())


async def on_game_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    if not data.startswith(st.PREFIX_GAME + "|"):
        return
    action = data.split("|", 1)[1]
    chat_id = query.message.chat_id
    session = _current(context, chat_id)
    if not session or session.question is None:
        await query.answer("O‘yin topilmadi. /solo bilan boshlang.", show_alert=True)
        return

    if action == "f":
        remaining = max(0, int(session.deadline) - int(time.time())) if session.deadline else session.timer_seconds
        await query.answer(f"Qolgan vaqt: {remaining} s")
        if remaining <= 0 and not session.answered and session.resolve_unanswered():
            await _advance(chat_id, context, session, "Vaqt tugadi. Ochko Qarshi tarafga.")
        return

    if action == "k":
        if not await is_admin(update, context, session):
            await query.answer("O‘tkazish faqat admin uchun", show_alert=True)
            return
        await query.answer("O‘tkazildi")
        if session.answered:
            await query.answer("Bu savol allaqachon yechilgan.", show_alert=True)
            return
        await cmd_skip(context, session, chat_id)
        return

    if action == "c":
        await query.answer(f"{session.score_text()} · {session.number}/{session.total}-savol")
        return

    if action == "x":
        await query.answer("O‘yin to‘xtatildi")
        _jobs(context, session)
        await _delete_control(context, session)
        _sessions(context).pop(chat_id, None)
        await context.bot.send_message(chat_id, session.summary(), parse_mode=ParseMode.HTML)
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass


COMMANDS = {
    "start": start, "help": help_command, "rules": rules, "packages": packages,
    "solo": solo, "quiz": solo, "duel": duel, "score": score, "skip": skip,
    "stop": stop, "accept": accept,
}


async def on_command_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


def build_app(token: str) -> Application:
    Path(ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    application = ApplicationBuilder().token(token).build()
    application.add_handlers([
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("rules", rules),
        CommandHandler("packages", packages),
        CommandHandler("solo", solo),
        CommandHandler("quiz", solo),
        CommandHandler("duel", duel),
        CommandHandler("score", score),
        CommandHandler("skip", skip),
        CommandHandler("accept", accept),
        CommandHandler("stop", stop),
        ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER),
        CallbackQueryHandler(on_settings_button, pattern="^" + st.PREFIX_CONFIG + r"\|"),
        CallbackQueryHandler(on_page_button, pattern="^" + st.PREFIX_PAGE + r"\|"),
        CallbackQueryHandler(on_game_button, pattern="^" + st.PREFIX_GAME + r"\|"),
        MessageHandler(filters.Regex(r"^/"), on_command_fallback),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
    ])
    return application


async def _post_init(application: Application) -> None:
    try:
        await application.bot.set_my_commands([BotCommand(n, t) for n, t in BOT_COMMANDS])
    except TelegramError:
        pass


def main() -> None:
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token.startswith("your_"):
        raise SystemExit("BOT_TOKEN muhit o‘zgaruvchisi kerak. .env.example ga qarang; tokenni chatga yubormang.")
    application = build_app(token)
    application.post_init = _post_init
    application.run_polling(allowed_updates=Update.ALL_TYPES)
