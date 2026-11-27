"""Offline self-test: pushes synthetic Telegram updates through the real handlers.

Nothing is sent to Telegram — a recording Bot captures every message the bot
would have sent, so `/api/index?test=1` can prove the deployment works (env
vars, archive files, handlers, settings panel, countdown, answer checking)
without a real chat.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from telegram import Bot, Chat, ChatMember, Message, Update, User

FAKE_TOKEN = "123456:ABCDEF-selftest-token-not-real"


class RecordingBot(Bot):
    """Bot that records outgoing messages instead of calling the Telegram API."""

    __slots__ = ("_sent", "_deleted")

    def __init__(self, token: str = FAKE_TOKEN):
        super().__init__(token)
        self._sent: list[dict[str, Any]] = []
        self._deleted: list[int] = []

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
        self._sent.append({
            "kind": "text", "chat_id": chat_id, "text": text, "message": message,
            "reply_markup": kwargs.get("reply_markup"),
        })
        return message

    async def send_photo(self, *args: Any, **kwargs: Any) -> Message:
        chat_id = kwargs.get("chat_id", args[0] if args else 0)
        caption = kwargs.get("caption") or ""
        message = Message(
            message_id=len(self._sent) + 1,
            date=int(time.time()),
            chat=Chat(id=int(chat_id or 0), type="private"),
            caption=caption,
        )
        self._sent.append({"kind": "photo", "chat_id": chat_id, "text": f"[rasm] {caption}",
                           "message": message, "reply_markup": kwargs.get("reply_markup")})
        return message

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        self._sent.append({"kind": "edit", "text": kwargs.get("text", ""), "message": None,
                           "reply_markup": kwargs.get("reply_markup")})
        return None

    async def delete_message(self, *args: Any, **kwargs: Any) -> None:
        self._deleted.append(kwargs.get("message_id", args[1] if len(args) > 1 else 0))

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def get_chat_member(self, *args: Any, **kwargs: Any) -> ChatMember:
        return ChatMember(kwargs.get("user_id", 777), "administrator")


def _message_payload(message: Message) -> dict[str, Any]:
    payload = {
        "message_id": message.message_id,
        "date": int(time.time()),
        "chat": {"id": 4242, "type": "private"},
        "from": {"id": 1, "is_bot": True, "first_name": "Zakovat"},
    }
    if message.text:
        payload["text"] = message.text
    if message.caption:
        payload["caption"] = message.caption
    return payload


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
        payload["message"]["reply_to_message"] = _message_payload(reply_to)
    return Update.de_json(payload, bot)


def _callback_update(update_id: int, bot: Bot, data: str, message: Message) -> Update:
    payload = {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": 777, "is_bot": False, "first_name": "Sinov"},
            "chat_instance": "selftest",
            "data": data,
            "message": _message_payload(message),
        },
    }
    return Update.de_json(payload, bot)


def _preview(text: str, limit: int = 150) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit] + ("…" if len(value) > limit else "")


def _is_panel(record: dict[str, Any]) -> bool:
    """True for messages carrying an inline keyboard (settings panel or countdown strip)."""
    return bool(getattr(record.get("reply_markup"), "inline_keyboard", None))


def _button(record: dict[str, Any], row: int, column: int) -> str | None:
    markup = record.get("reply_markup")
    try:
        return markup.inline_keyboard[row][column].callback_data
    except (AttributeError, IndexError, TypeError):
        return None


async def run_self_test(build_app: Callable[[Bot], Any]) -> list[str]:
    """Walk the whole user journey through the live handlers, offline."""
    from .webhook import ARCHIVE, unpack_state

    bot = RecordingBot(os.getenv("BOT_TOKEN") or FAKE_TOKEN)
    application = build_app(bot)
    await application.initialize()
    lines: list[str] = []
    step = 0

    async def send(text: str, reply_to: Message | None = None, with_entities: bool = True) -> list[dict]:
        nonlocal step
        step += 1
        before = len(bot._sent)
        await application.process_update(_update(step, bot, text, reply_to, with_entities))
        fresh = bot._sent[before:]
        lines.append(f"{text} → {len(fresh)} xabar")
        lines.extend(f"   • {_preview(item['text'])}" for item in fresh)
        return fresh

    async def press(record: dict[str, Any], row: int, column: int, label: str) -> list[dict]:
        nonlocal step
        data = _button(record, row, column)
        if not data or not record.get("message"):
            lines.append(f"{label} → tugma topilmadi")
            return []
        step += 1
        before = len(bot._sent)
        await application.process_update(_callback_update(step, bot, data, record["message"]))
        fresh = bot._sent[before:]
        lines.append(f"{label} → {len(fresh)} xabar")
        lines.extend(f"   • {_preview(item['text'])}" for item in fresh)
        return fresh

    try:
        await send("/start")
        await send("/help")
        await send("/rules")
        await send("/packages")

        panel = await send("/solo 959")
        if not panel:
            return lines
        started = await press(panel[0], -1, 0, "▶️ Boshlash")
        if not started:
            return lines

        control = next((item for item in started if _is_panel(item)), None)
        question = started[-1]
        state = unpack_state(question["message"].text)
        answer = ""
        if state and state.get("q"):
            record = ARCHIVE.question(int(state["q"]))
            answer = str((record or {}).get("answer", ""))
        answered = []
        if answer:
            answered = await send(answer, reply_to=question["message"])
        else:
            lines.append("javob sinovi o‘tkazib yuborildi: savol topilmadi")

        fresh_control = next((item for item in reversed(answered) if _is_panel(item)), control)
        if fresh_control:
            await press(fresh_control, 0, 1, "⏭ O‘tkazish (admin)")

        await send("/rules", with_entities=False)
    finally:
        await application.shutdown()
    return lines
