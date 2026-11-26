"""Telegram bot wired to the local Zakovat archive.

Run with BOT_TOKEN in the environment (never in a file):
    python3 run_bot.py
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, ChatMemberHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from . import messages as msg
from .answers import AnswerChecker
from .archive import Archive
from .quiz import CORRECT, FOREIGN, IGNORED, Session

ARCHIVE_DIR = os.getenv("ZAKOVAT_ARCHIVE_DIR", "data/all")
ALIAS_FILE = os.getenv("ZAKOVAT_ALIAS_FILE", "data/answer_aliases.json")
DEFAULT_TIMER = int(os.getenv("ZAKOVAT_TIMER_SECONDS", "60"))


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


def _cancel_timer(context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    if session.job_name:
        for job in context.job_queue.get_jobs_by_name(session.job_name):
            job.schedule_removal()
        session.job_name = None


def _resolve_package(context: ContextTypes.DEFAULT_TYPE, argument: str) -> tuple[int, str] | None:
    archive = _archive(context)
    argument = (argument or "").strip()
    if not argument:
        package_id = archive.random_package_id()
        package = archive.package(package_id)
        return package_id, (package or {}).get("title", "")
    if argument.isdigit():
        package = archive.package(int(argument))
        if package and package["questions"]:
            return int(argument), package["title"]
        return None
    matches = archive.search(argument, limit=1)
    if matches:
        return int(matches[0]["id"]), matches[0]["title"]
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0].isdigit():
        await _new_game(update, context, "solo", context.args[0])
        return
    packages, questions = _archive(context).counts()
    miniapp = os.getenv("MINIAPP_URL", "")
    await update.effective_message.reply_text(
        msg.welcome(packages, questions, miniapp),
        parse_mode=ParseMode.HTML,
        reply_markup=msg.miniapp_button(miniapp),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.rules(), parse_mode=ParseMode.HTML)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator"):
        await context.bot.send_message(chat.id, msg.group_hello(), parse_mode=ParseMode.HTML)


async def packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args or [])
    found = _archive(context).search(query, limit=12)
    if not found:
        await update.effective_message.reply_text("Bunday paket topilmadi. Masalan: /packages Zakovat")
        return
    lines = [f"<b>Topilgan paketlar ({query or 'eng so‘nggilari'}):</b>"]
    lines += [
        f"• <code>{p['id']}</code> — {_esc(p['title'])} ({p['questions']} savol)"
        for p in found
    ]
    lines.append("\nBoshlash: <code>/solo 959</code> yoki <code>/duel 959</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _new_game(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, argument: str | None = None) -> None:
    chat = update.effective_chat
    if argument is None:
        argument = " ".join(context.args or [])
    resolved = _resolve_package(context, argument)
    if not resolved:
        await update.effective_message.reply_text(
            "Paket topilmadi. Id yoki qidiruv so‘zini bering, masalan: /solo 959"
        )
        return
    package_id, title = resolved
    questions = _archive(context).questions(package_id)
    if not questions:
        await update.effective_message.reply_text("Bu paketda savol yo‘q.")
        return

    old = _current(context, chat.id)
    if old:
        _cancel_timer(context, old)
    session = Session(
        chat_id=chat.id,
        mode=mode,
        package_id=package_id,
        package_title=title,
        questions=questions,
        owner_id=update.effective_user.id,
        timer_seconds=DEFAULT_TIMER,
    )
    _sessions(context)[chat.id] = session
    label = "Yakka o‘yin" if mode == "solo" else "Guruh o‘yini"
    await update.effective_message.reply_text(
        msg.game_start(label, title, session.total, session.timer_seconds, session.score_text()),
        parse_mode=ParseMode.HTML,
    )
    await _send_question(chat.id, context, session)


async def solo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _new_game(update, context, "solo")


async def duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _new_game(update, context, "group")


async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = (update.effective_message.web_app_data.data or "").strip()
    if data.startswith("start:"):
        await _new_game(update, context, "solo", data.split(":", 1)[1])


async def _send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    question = session.begin() if session.question is None and session.index == 0 and not session.finished else session.question
    if question is None:
        percent = round(session.correct * 100 / session.total) if session.total else 0
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg.summary(session.package_title, session.total, session.correct, session.wrong, percent),
            parse_mode=ParseMode.HTML,
        )
        _sessions(context).pop(chat_id, None)
        return
    attachments = question.get("attachments") or []
    text = msg.question(
        session.number, session.total, session.package_title, question.get("text"),
        session.timer_seconds, session.score_text(),
        attachments[0] if attachments and isinstance(attachments[0], str) else "",
    )
    message = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    session.job_name = f"timeout-{chat_id}-{session.number}"
    context.job_queue.run_once(
        _on_timeout,
        when=session.timer_seconds,
        data={"chat_id": chat_id, "question_id": question["id"], "issue": session.number},
        name=session.job_name,
    )
    message  # keep the sent message available for future reply-based features


async def _reveal(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: Session, headline: str) -> None:
    question = session.question
    if not question:
        return
    answer = re.sub(r"^\s*(?:to'?g'?ri\s+)?javob\s*[:.]\s*", "", str(question.get("answer", "")), flags=re.I)
    answer_attachments = question.get("answer_attachments") or []
    text = msg.reveal(
        headline, answer, question.get("comment") or "",
        answer_attachments[0] if answer_attachments and isinstance(answer_attachments[0], str) else "",
        session.score_text(),
    )
    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _advance(chat_id: int, context: ContextTypes.DEFAULT_TYPE, session: Session, headline: str) -> None:
    _cancel_timer(context, session)
    await _reveal(chat_id, context, session, headline)
    session.index += 1
    session.advance()
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
        await _advance(data["chat_id"], context, session, "⏰ Vaqt tugadi. Ochko Qarshi tarafga qo‘shildi.")


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
        await update.message.reply_text("❌ Noto‘g‘ri. Yana urinib ko‘ring yoki /skip qiling.")
    elif result == CORRECT:
        await _advance(
            update.effective_chat.id, context, session,
            f"🎉 To‘g‘ri! {_esc(update.effective_user.first_name)} Bilimdonlar uchun ochko qozondi.",
        )


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _current(context, update.effective_chat.id)
    if not session:
        await update.effective_message.reply_text("Hozirda o‘yin yo‘q. /solo yoki /duel bilan boshlang.")
        return
    await update.effective_message.reply_text(
        f"{_esc(session.score_text())}\nSavol: {session.number}/{session.total}"
    )


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _current(context, update.effective_chat.id)
    if not session or session.question is None or session.answered:
        await update.effective_message.reply_text("O‘tkazish uchun faol savol kerak.")
        return
    if session.resolve_unanswered():
        await _advance(update.effective_chat.id, context, session, "⏭ Savol o‘tkazildi, ochko Qarshi tarafga.")


async def accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin confirms the last wrong answer; the checker learns it as an alias."""
    session = _current(context, update.effective_chat.id)
    if not session or session.question is None or not session.latest_answer:
        await update.effective_message.reply_text("O‘rganish uchun oxirgi noto‘g‘ri javob yo‘q.")
        return
    answer = session.latest_answer
    user_id = session.latest_user or update.effective_user.id
    learned = _checker(context).learn(int(session.question["id"]), answer)
    result = session.submit(user_id, answer, _checker(context))
    if result == CORRECT:
        await _advance(update.effective_chat.id, context, session, "✅ Javob qabul qilindi va o‘rganildi.")
    else:
        await update.effective_message.reply_text("Javob hali mos kelmadi; boshqa varianti yuboring.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = _current(context, chat_id)
    if not session:
        await update.effective_message.reply_text("Faol o‘yin yo‘q.")
        return
    _cancel_timer(context, session)
    _sessions(context).pop(chat_id, None)
    await update.effective_message.reply_text(session.summary())


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
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
    ])
    return application


def main() -> None:
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token.startswith("your_"):
        raise SystemExit("BOT_TOKEN muhit o‘zgaruvchisi kerak. .env.example ga qarang; tokenni chatga yubormang.")
    build_app(token).run_polling(allowed_updates=Update.ALL_TYPES)
