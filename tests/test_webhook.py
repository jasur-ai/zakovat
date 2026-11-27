import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

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


class PublicUrlTests(unittest.TestCase):
    """The webhook must point at a publicly reachable domain, not VERCEL_URL."""

    def _base(self, env):
        if not webhook:
            self.skipTest("install requirements.txt to test the webhook")
        with mock.patch.dict(os.environ, env, clear=True):
            return webhook.public_base_url()

    def test_explicit_webhook_url_wins(self):
        self.assertEqual(
            self._base({"WEBHOOK_URL": "https://zakovat.example", "VERCEL_URL": "zakovat-abc-team.vercel.app"}),
            "https://zakovat.example",
        )

    def test_production_url_beats_deployment_url(self):
        # Vercel's generated deployment URL is protected by Standard Protection.
        self.assertEqual(
            self._base({
                "VERCEL_PROJECT_PRODUCTION_URL": "zakovat-peach.vercel.app",
                "VERCEL_URL": "zakovat-ih087biqs-team.vercel.app",
            }),
            "https://zakovat-peach.vercel.app",
        )

    def test_vercel_url_is_last_resort(self):
        self.assertEqual(self._base({"VERCEL_URL": "zakovat-abc-team.vercel.app"}), "https://zakovat-abc-team.vercel.app")

    def test_nothing_set(self):
        self.assertEqual(self._base({}), "")


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class CommandFallbackTests(unittest.TestCase):
    """Commands without a bot_command entity must still be answered."""

    def test_entityless_command_is_answered(self):
        from bot.selftest import RecordingBot, _update

        async def run():
            bot = RecordingBot()
            application = webhook.build_app(bot)
            await application.initialize()
            try:
                await application.process_update(_update(1, bot, "/rules", with_entities=False))
                await application.process_update(_update(2, bot, "/solo 959", with_entities=False))
            finally:
                await application.shutdown()
            return [item["text"] for item in bot._sent]

        sent = asyncio.run(run())
        self.assertTrue(any("Qoidalar" in text for text in sent))
        self.assertTrue(any("savol" in text for text in sent))

    def test_self_test_reports_every_step(self):
        async def run():
            return await webhook.self_test()

        lines = asyncio.run(run())
        joined = "\n".join(lines)
        self.assertIn("/start → 1 xabar", joined)
        self.assertIn("/rules → 1 xabar", joined)
        self.assertIn("/solo 959 → 2 xabar", joined)
        self.assertIn("javob", joined)


if __name__ == "__main__":
    unittest.main()
