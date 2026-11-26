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
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from .answers import AnswerChecker, normalize
from .archive import Archive
from .quiz import CORRECT, FOREIGN, IGNORED, WRONG, Session

MARKER = "\n\n#ZS "
TIMER = int(os.getenv("ZAKOVAT_TIMER_SECONDS", "60"))
ARCHIVE = Archive(os.getenv("ZAKOVAT_ARCHIVE_DIR", "data/all"))
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


def restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[Session, dict] | None:
    message = update.effective_message
    reply = message.reply_to_message if message else None
    data = unpack_state(reply.text if reply else None)
    if not data or data.get("q") is None:
        return None
    archive: Archive = context.application.bot_data["archive"]
    question = archive.question(int(data["q"]))
    if not question:
        return None
    session = Session(
        chat_id=update.effective_chat.id,
        mode=data.get("m", "solo"),
        package_id=int(data["p"]),
        package_title=(archive.package(int(data["p"])) or {}).get("title", ""),
        questions=archive.questions(int(data["p"])),
        owner_id=int(data["o"]),
        timer_seconds=TIMER,
    )
    session.index = int(data.get("i", 0))
    session.correct = int(data.get("c", 0))
    session.wrong = int(data.get("w", 0))
    session.question = question
    session.answered = False
    return session, data


async def send_question(bot, chat_id: int, session: Session, aliases: list[str] | None = None) -> None:
    question = session.question
    if not question:
        return
    deadline = int(time.time()) + TIMER
    text = (
        f"<b>{session.number}/{session.total}-savol</b> · {_esc(session.package_title)}\n\n"
        f"{_esc(str(question.get('text'))[:2800])}\n\n"
        f"<i>Javobingizni shu xabarga reply qilib yozing · {TIMER}s · </i>{session.score_text()}"
    )
    attachments = question.get("attachments") or []
    if attachments and isinstance(attachments[0], str):
        text += f"\n🖼 {_esc(attachments[0])}"
    text += pack_state(session, deadline, aliases=aliases)
    await bot.send_message(
        chat_id=chat_id,
        text=text[:4090],
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="Javobingizni yozing..."),
        disable_web_page_preview=True,
    )


async def reveal(bot, chat_id: int, session: Session, headline: str) -> None:
    question = session.question
    if not question:
        return
    answer = re.sub(r"^\s*(?:to'?g'?ri\s+)?javob\s*[:.]\s*", "", str(question.get("answer", "")), flags=re.I)
    text = f"{headline}\n✅ Javob: <b>{_esc(answer[:1500])}</b>"
    if question.get("comment"):
        text += f"\n💬 Izoh: {_esc(str(question['comment'])[:1500])}"
    text += f"\n\n{_esc(session.score_text())}"
    await bot.send_message(chat_id=chat_id, text=text[:4090], parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def next_question(bot, chat_id: int, session: Session, headline: str, aliases: list[str] | None = None) -> None:
    await reveal(bot, chat_id, session, headline)
    session.index += 1
    session.advance()
    if session.question is None:
        await bot.send_message(chat_id=chat_id, text=session.summary())
    else:
        await send_question(bot, chat_id, session, aliases)


def _resolve_package(context: ContextTypes.DEFAULT_TYPE, argument: str):
    archive: Archive = context.application.bot_data["archive"]
    argument = (argument or "").strip()
    if argument.isdigit():
        package = archive.package(int(argument))
        return (int(argument), package["title"]) if package and package["questions"] else None
    if not argument:
        package_id = archive.random_package_id()
        return package_id, (archive.package(package_id) or {}).get("title", "")
    found = archive.search(argument, limit=1)
    return (int(found[0]["id"]), found[0]["title"]) if found else None


async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, argument: str) -> None:
    resolved = _resolve_package(context, argument)
    if not resolved:
        await update.effective_message.reply_text("Paket topilmadi. Masalan: /solo 959")
        return
    package_id, title = resolved
    archive: Archive = context.application.bot_data["archive"]
    questions = archive.questions(package_id)
    if not questions:
        await update.effective_message.reply_text("Bu paketda savol yo‘q.")
        return
    session = Session(
        chat_id=update.effective_chat.id,
        mode=mode,
        package_id=package_id,
        package_title=title,
        questions=questions,
        owner_id=update.effective_user.id,
        timer_seconds=TIMER,
    )
    session.begin()
    label = "Yakka o‘yin" if mode == "solo" else "Guruh o‘yini"
    await update.effective_message.reply_text(f"🎬 {label}: {_esc(title)} · {session.total} savol")
    await send_question(context.bot, update.effective_chat.id, session)


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    await start_game(update, context, mode, " ".join(context.args or []))


async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = (update.effective_message.web_app_data.data or "").strip()
    if data.startswith("start:"):
        await start_game(update, context, "solo", data.split(":", 1)[1])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args and context.args[0].isdigit():
        await start_game(update, context, "solo", context.args[0])
        return
    text = (
        "🎯 <b>Zakovat Agent</b>\n\n"
        "Savollarga replies qilib javob yozing. To‘g‘ri javob → Bilimdonlar +1, "
        "javob yo‘q yoki vaqt tugadi → Qarshi taraf +1.\n\n"
        "/solo 959 — yakka o‘yin\n/duel 959 — guruh o‘yini\n/packages [qidiruv]\n"
        "/score, /skip, /stop, /accept — boshqaruv\n\n"
        "Savol xabariga reply qilib yozish shart (ForceReply)."
    )
    keyboard = None
    miniapp = os.getenv("MINIAPP_URL") or os.getenv("VERCEL_URL") or ""
    if miniapp and not miniapp.startswith("http"):
        miniapp = "https://" + miniapp
    if miniapp:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Mini App ochish", web_app=WebAppInfo(url=miniapp))]])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cmd_packages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    archive: Archive = context.application.bot_data["archive"]
    query = " ".join(context.args or [])
    found = archive.search(query, limit=12)
    if not found:
        await update.effective_message.reply_text("Bunday paket topilmadi. Masalan: /packages Zakovat")
        return
    lines = [f"<b>Paketlar ({_esc(query or 'so‘nggilari')}):</b>"]
    lines += [f"• <code>{p['id']}</code> — {_esc(p['title'])} ({p['questions']} savol)" for p in found]
    await update.effective_message.reply_text("\n".join(lines)[:4000], parse_mode=ParseMode.HTML)


async def cmd_solo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start(update, context, "solo")


async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start(update, context, "group")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        return
    session, data = restored
    aliases = list(data.get("a") or [])
    deadline = int(data.get("d", 0))
    chat_id = update.effective_chat.id

    if int(time.time()) > deadline:
        if session.resolve_unanswered():
            await next_question(context.bot, chat_id, session, "⏰ Vaqt tugadi, ochko Qarshi tarafga.", aliases)
        return

    result = session.submit(update.effective_user.id, update.message.text, checker_for(aliases))
    if result == CORRECT:
        await next_question(
            context.bot, chat_id, session,
            f"✅ To‘g‘ri! {_esc(update.effective_user.first_name)} ochko qozondi.", aliases,
        )
    elif result == FOREIGN:
        await update.message.reply_text("Bu yakka o‘yin — faqat boshlagan kishi javob yozadi.")
    elif result == WRONG and session.mode == "solo":
        text = "❌ Noto‘g‘ri. Yana urinib ko‘ring." + pack_state(session, deadline, latest=update.message.text, aliases=aliases)
        await update.message.reply_text(text[:4090], reply_markup=ForceReply(input_field_placeholder="Yangi javob..."))


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("Hisobni ko‘rish uchun savol xabariga reply qiling.")
        return
    session, _ = restored
    await update.effective_message.reply_text(f"{session.score_text()} · Savol {session.number}/{session.total}")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored or not restored[0].resolve_unanswered():
        await update.effective_message.reply_text("O‘tkazish uchun savol xabariga reply qiling.")
        return
    session, data = restored
    await next_question(context.bot, update.effective_chat.id, session, "⏭ O‘tkazildi, ochko Qarshi tarafga.", data.get("a") or [])


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("To‘xtatish uchun savol xabariga reply qiling.")
        return
    await update.effective_message.reply_text(restored[0].summary())


async def cmd_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    restored = restore(update, context)
    if not restored:
        await update.effective_message.reply_text("Javobni qabul qilish uchun savol xabariga reply qiling.")
        return
    session, data = restored
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
        await next_question(context.bot, update.effective_chat.id, session, "✅ Javob qabul qilindi va o‘rganildi.", aliases)
    else:
        await update.effective_message.reply_text("Bu javob hali mos kelmadi.")


def build_telegram_app() -> Application:
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set in Vercel")
    application = ApplicationBuilder().token(token).updater(None).build()
    application.bot_data["archive"] = ARCHIVE
    application.add_handlers([
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_start),
        CommandHandler("packages", cmd_packages),
        CommandHandler("solo", cmd_solo),
        CommandHandler("quiz", cmd_solo),
        CommandHandler("duel", cmd_duel),
        CommandHandler("score", cmd_score),
        CommandHandler("skip", cmd_skip),
        CommandHandler("stop", cmd_stop),
        CommandHandler("accept", cmd_accept),
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
    ])
    return application


_application: Application | None = None


def get_application() -> Application:
    global _application
    if _application is None:
        _application = build_telegram_app()
    return _application


app = FastAPI()


@app.get("/")
async def home():
    page = Path(__file__).resolve().parent.parent / "public" / "index.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Zakovat Agent</h1><p>Mini App fayli topilmadi.</p>")


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "archive": ARCHIVE.counts()[1]})


@app.post("/api/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if SECRET and not secrets.compare_digest(x_telegram_bot_api_secret_token or "", SECRET):
        return Response(status_code=403)
    data = await request.json()
    update = Update.de_json(data, get_application().bot)
    if not update:
        return Response(status_code=400)
    application = get_application()
    await application.initialize()
    try:
        await application.process_update(update)
    finally:
        await application.shutdown()
    return JSONResponse({"ok": True})
