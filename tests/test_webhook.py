import asyncio
import unittest
from types import SimpleNamespace

try:
    import bot.webhook as webhook
    from bot.archive import Archive
except ImportError:  # fastapi / python-telegram-bot not installed yet
    webhook = None


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))


class FakeMessage:
    def __init__(self, text="", reply=None):
        self.text = text
        self.reply_to_message = SimpleNamespace(text=reply) if reply else None
        self.replies = []
        self.from_user = SimpleNamespace(id=1, first_name="Aziz")

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def update(message):
    return SimpleNamespace(
        effective_message=message, message=message,
        effective_chat=SimpleNamespace(id=-100), effective_user=message.from_user,
    )


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class WebhookLogicTests(unittest.TestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"archive": Archive("data/all")}),
            bot=self.bot, args=[],
        )

    def _start(self):
        message = FakeMessage()
        asyncio.run(webhook.start_game(update(message), self.context, "solo", "959"))
        return self.bot.sent[-1]["text"]

    def test_marker_roundtrip(self):
        question_text = self._start()
        state = webhook.unpack_state(question_text)
        self.assertEqual(int(state["p"]), 959)
        self.assertIsNotNone(state["q"])
        self.assertGreater(state["d"], 0)

    def test_correct_answer_advances(self):
        question_text = self._start()
        state = webhook.unpack_state(question_text)
        answer = self.context.application.bot_data["archive"].question(state["q"])["answer"]
        self.bot.sent.clear()
        asyncio.run(webhook.on_text(update(FakeMessage(answer, question_text)), self.context))
        self.assertEqual(len(self.bot.sent), 2)
        self.assertIn("To‘g‘ri", self.bot.sent[0]["text"])
        self.assertIsNotNone(webhook.unpack_state(self.bot.sent[1]["text"]))

    def test_wrong_answer_then_accept(self):
        question_text = self._start()
        wrong = FakeMessage("Braziliya", question_text)
        asyncio.run(webhook.on_text(update(wrong), self.context))
        self.assertTrue(wrong.replies)
        self.assertEqual(webhook.unpack_state(wrong.replies[0])["l"], "Braziliya")
        self.bot.sent.clear()
        asyncio.run(webhook.cmd_accept(update(FakeMessage("/accept", wrong.replies[0])), self.context))
        self.assertIn("qabul qilindi", self.bot.sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
