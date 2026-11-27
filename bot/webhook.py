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
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, ChatMemberHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from . import messages as msg
from .answers import AnswerChecker, normalize
from .archive import Archive
from .quiz import CORRECT, FOREIGN, IGNORED, WRONG, Session

MARKER = "\n\n#ZS "
TIMER = int(os.getenv("ZAKOVAT_TIMER_SECONDS", "60"))
ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = Archive(
    ROOT / os.getenv("ZAKOVAT_ARCHIVE_DIR", "data/all"),
    ROOT / "data" / "zakovat-959.flat.json",
)
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
    attachments = question.get("attachments") or []
    text = msg.question(
        session.number, session.total, session.package_title, question.get("text"),
        TIMER, session.score_text(),
        attachments[0] if attachments and isinstance(attachments[0], str) else "",
    )
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
    attachments = question.get("answer_attachments") or []
    text = msg.reveal(
        headline, answer, question.get("comment") or "",
        attachments[0] if attachments and isinstance(attachments[0], str) else "",
        session.score_text(),
    )
    await bot.send_message(chat_id=chat_id, text=text[:4090], parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def next_question(bot, chat_id: int, session: Session, headline: str, aliases: list[str] | None = None) -> None:
    await reveal(bot, chat_id, session, headline)
    session.index += 1
    session.advance()
    if session.question is None:
        percent = round(session.correct * 100 / session.total) if session.total else 0
        await bot.send_message(
            chat_id=chat_id, text=msg.summary(
                session.package_title, session.total, session.correct, session.wrong, percent,
            ),
            parse_mode=ParseMode.HTML,
        )
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
    await update.effective_message.reply_text(
        msg.game_start(label, title, session.total, TIMER, session.score_text()),
        parse_mode=ParseMode.HTML,
    )
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
    packages, questions = ARCHIVE.counts()
    miniapp = os.getenv("MINIAPP_URL") or public_base_url()
    await update.effective_message.reply_text(
        msg.welcome(packages, questions, miniapp),
        parse_mode=ParseMode.HTML,
        reply_markup=msg.miniapp_button(miniapp),
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(msg.rules(), parse_mode=ParseMode.HTML)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator"):
        await context.bot.send_message(chat.id, msg.group_hello(), parse_mode=ParseMode.HTML)


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
            await next_question(context.bot, chat_id, session, "⏰ Vaqt tugadi. Ochko Qarshi tarafga qo‘shildi.", aliases)
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


_recent_errors: list[str] = []


async def on_handler_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the last few handler errors so they can be shown on /api/index."""
    error = context.error
    entry = f"{time.strftime('%H:%M:%S')} {type(error).__name__}: {error}"
    _recent_errors.insert(0, entry)
    _recent_errors[:] = _recent_errors[:5]
    logging.getLogger(__name__).exception("Handler failed", exc_info=error)


COMMANDS = {
    "start": cmd_start,
    "help": cmd_start,
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
        await update.effective_message.reply_text(
            msg.rules() if name in {"menu", "commands"} else msg.welcome(*ARCHIVE.counts(), public_base_url()),
            parse_mode=ParseMode.HTML,
        )
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
        CommandHandler("help", cmd_start),
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
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data),
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


async def ensure_webhook(application: Application, force: bool = False) -> None:
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
            allowed_updates=["message", "my_chat_member"],
        )
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
async def setup():
    """Idempotent manual bootstrap: open once after deploying."""
    try:
        application = get_application()
        await application.initialize()
        try:
            await ensure_webhook(application, force=True)
            info = await application.bot.get_webhook_info()
        finally:
            await application.shutdown()
        if _setup_error:
            return status_page("Xatolik", [_setup_error], 500)
        return status_page("Webhook", [
            f"Webhook o‘rnatildi: {info.url}",
            f"{info.pending_update_count} kutilayotgan xabar",
            f"Oxirgi xatolik: {info.last_error_message or 'yo‘q'}",
        ])
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
        return await setup()
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
    page = Path(__file__).resolve().parent.parent / "public" / "index.html"
    if page.exists():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Zakovat Agent</h1><p>Mini App fayli topilmadi.</p>")


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "archive": ARCHIVE.counts()[1]})


@app.post("/api/webhook")
@app.post("/api/index")
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
