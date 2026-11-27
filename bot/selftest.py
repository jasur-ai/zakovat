"""Offline self-test: pushes synthetic Telegram updates through the real handlers.

Nothing is sent to Telegram — a recording Bot captures every message the bot
would have sent, so `/api/index?test=1` can prove the deployment works (env
vars, archive files, handlers, answer checking) without a real chat.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from telegram import Bot, Chat, Message, Update, User

FAKE_TOKEN = "123456:ABCDEF-selftest-token-not-real"


class RecordingBot(Bot):
    """Bot that records outgoing messages instead of calling the Telegram API."""

    __slots__ = ("_sent",)

    def __init__(self, token: str = FAKE_TOKEN):
        super().__init__(token)
        self._sent: list[dict[str, Any]] = []

    async def initialize(self) -> None:  # no network
        self._bot_user = User(id=1, is_bot=True, first_name="Zakovat", username="Zakovat_agent")
        self._requests_initialized = True
        self._bot_initialized = True

    async def shutdown(self) -> None:  # no network
        self._requests_initialized = False
        self._bot_initialized = False

    async def send_message(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = kwargs.get("chat_id", args[0] if args else 0)
        text = kwargs.get("text", args[1] if len(args) > 1 else "")
        message = Message(
            message_id=len(self._sent) + 1,
            date=int(time.time()),
            chat=Chat(id=int(chat_id or 0), type="private"),
            text=text,
        )
        self._sent.append({"chat_id": chat_id, "text": text, "message": message})
        return message


def _update(update_id: int, bot: Bot, text: str, reply_to: Message | None = None,
            with_entities: bool = True) -> Update:
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": int(time.time()),
        "text": text,
        "chat": {"id": 4242, "type": "private"},
        "from": {"id": 777, "is_bot": False, "first_name": "Sinov"},
    }
    if with_entities and text.startswith("/"):
        # Telegram marks commands with a bot_command entity; CommandHandler needs it.
        message["entities"] = [{
            "type": "bot_command", "offset": 0, "length": len(text.split()[0]),
        }]
    payload: dict[str, Any] = {"update_id": update_id, "message": message}
    if reply_to is not None:
        payload["message"]["reply_to_message"] = {
            "message_id": reply_to.message_id,
            "date": int(time.time()),
            "text": reply_to.text,
            "chat": {"id": 4242, "type": "private"},
            "from": {"id": 1, "is_bot": True, "first_name": "Zakovat"},
        }
    return Update.de_json(payload, bot)


def _preview(text: str, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit] + ("…" if len(value) > limit else "")


async def run_self_test(build_app: Callable[[Bot], Any]) -> list[str]:
    """Run /start, /rules, /solo 959 and one answer through the live handlers."""
    from .archive import Archive
    from .webhook import ARCHIVE, unpack_state

    bot = RecordingBot(os.getenv("BOT_TOKEN") or FAKE_TOKEN)
    application = build_app(bot)
    await application.initialize()
    lines: list[str] = []
    try:
        for step, text in ((1, "/start"), (2, "/rules"), (3, "/solo 959")):
            before = len(bot._sent)
            await application.process_update(_update(step, bot, text))
            fresh = bot._sent[before:]
            lines.append(f"{text} → {len(fresh)} xabar")
            lines += [f"   • {_preview(item['text'])}" for item in fresh]

        question = bot._sent[-1]["message"] if bot._sent else None
        state = unpack_state(question.text) if question else None
        answer = ""
        if state and state.get("q"):
            record = ARCHIVE.question(int(state["q"])) if isinstance(ARCHIVE, Archive) else None
            answer = str((record or {}).get("answer", ""))
        if answer:
            before = len(bot._sent)
            await application.process_update(_update(4, bot, answer, reply_to=question))
            fresh = bot._sent[before:]
            lines.append(f"javob «{_preview(answer, 40)}» → {len(fresh)} xabar")
            lines += [f"   • {_preview(item['text'])}" for item in fresh]
        else:
            lines.append("javob sinovi o‘tkazib yuborildi: savol topilmadi (arxiv deploy bo‘lmagan?)")

        before = len(bot._sent)
        await application.process_update(_update(5, bot, "/rules", with_entities=False))
        fresh = bot._sent[before:]
        lines.append(f"/rules (entity’siz) → {len(fresh)} xabar")
        lines += [f"   • {_preview(item['text'])}" for item in fresh]
    finally:
        await application.shutdown()
    return lines
