import asyncio
import base64
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    import bot.webhook as webhook
    from bot import messages as msg
    from bot import settings as st
    from bot.archive import Archive
except ImportError:  # fastapi / python-telegram-bot not installed yet
    webhook = None


def visible(text: str) -> str:
    """What a reader actually sees: the invisible state run removed."""
    return "".join(ch for ch in str(text or "") if ch not in webhook.INVISIBLE)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.photos = []
        self.deleted = []
        self.edits = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent) + len(self.photos))

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs.get("message_id"))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True

    @property
    def id(self):
        return 1


class FakeMessage:
    def __init__(self, text="", reply=None):
        self.text = text
        self.caption = None
        self.reply_to_message = SimpleNamespace(text=reply, caption=None) if reply else None
        self.replies = []
        self.from_user = SimpleNamespace(id=1, first_name="Aziz")

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def update(message):
    return SimpleNamespace(
        effective_message=message, message=message, callback_query=None,
        effective_chat=SimpleNamespace(id=-100, type="private"),
        effective_user=message.from_user,
    )


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class WebhookLogicTests(unittest.TestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.archive = Archive("data/all")
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"archive": self.archive}),
            bot=self.bot, args=[],
        )

    def _start(self):
        """Send the first question of a seeded 1×10 solo game and return its text."""
        from bot.quiz import build_playlist

        playlist = build_playlist(self.archive.questions(959), rounds=1, per_round=10, seed=7)
        session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=10, seed=7,
        )
        session.begin()
        asyncio.run(webhook.send_question(self.bot, -100, session))
        self.session = session
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
        # reveal + countdown strip + next question
        self.assertEqual(len(self.bot.sent), 3)
        self.assertIn("To‘g‘ri", self.bot.sent[0]["text"])
        self.assertIsNotNone(webhook.unpack_state(self.bot.sent[1]["text"]))
        self.assertIsNotNone(webhook.unpack_state(self.bot.sent[2]["text"]))
        # the answered question's countdown strip is removed
        self.assertTrue(self.bot.deleted)

    def test_question_carries_round_and_timer(self):
        question_text = self._start()
        strip_text = self.bot.sent[-2]["text"]
        self.assertIn("QOLGAN VAQT  01:00", strip_text)
        self.assertIn("Savol 1 / 10", strip_text)
        self.assertEqual(visible(strip_text).splitlines()[-1], "▰" * 10)
        self.assertIn("Vaqt: 60 sekund", question_text)
        state = webhook.unpack_state(question_text)
        self.assertEqual(int(state["r"]), 1)
        self.assertEqual(int(state["n"]), 10)
        self.assertEqual(int(state["t"]), 60)

    def test_wrong_answer_then_accept(self):
        question_text = self._start()
        wrong = FakeMessage("Braziliya", question_text)
        asyncio.run(webhook.on_text(update(wrong), self.context))
        self.assertTrue(wrong.replies)
        self.assertEqual(webhook.unpack_state(wrong.replies[0])["l"], "Braziliya")
        self.bot.sent.clear()
        asyncio.run(webhook.cmd_accept(update(FakeMessage("/accept", wrong.replies[0])), self.context))
        self.assertIn("qabul qilindi", self.bot.sent[0]["text"])


class FakeApplication:
    """Stands in for the PTB Application inside handle_tick()."""

    def __init__(self, bot, archive):
        self.bot = bot
        self.bot_data = {"archive": archive}

    async def initialize(self):
        return None

    async def shutdown(self):
        return None


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class MarkerTests(unittest.TestCase):
    """The game state rides inside the message without ever being readable."""

    def setUp(self):
        self.archive = Archive("data/all")
        playlist = webhook.build_playlist(self.archive.questions(959), 1, 10, 7)
        self.session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=10, seed=7,
        )
        self.session.begin()
        self.session.control_message_id = 4242

    def test_marker_is_compact(self):
        marker = webhook.pack_state(self.session, int(time.time()) + 60)
        self.assertLess(len(marker), 160)
        self.assertTrue(marker.startswith("s|959|"))

    def test_state_is_invisible_and_not_a_spoiler(self):
        text = webhook.with_state("Savol matni", webhook.pack_state(self.session, 1700000000))
        # A spoiler could still be tapped open; this leaves nothing on screen.
        self.assertEqual(visible(text).strip(), "Savol matni")
        self.assertNotIn("#ZS", text)
        self.assertNotIn("tg-spoiler", text)
        state = webhook.unpack_state(text)
        self.assertEqual(int(state["p"]), 959)
        self.assertEqual(int(state["k"]), 4242)

    def test_state_survives_new_text_around_it(self):
        run = webhook.hide_state(webhook.pack_state(self.session, 1700000000))
        state = webhook.unpack_state("Boshqa sarlavha\n" + run)
        self.assertEqual(int(state["p"]), 959)

    def test_the_state_sits_inside_the_message_not_at_its_tail(self):
        text = webhook.with_state("Savol 1 / 10\nQOLGAN VAQT  00:47\n" + "▰" * 7 + "▱" * 3,
                                  webhook.pack_state(self.session, 1700000000))
        self.assertTrue(text.endswith("▰▰▰▰▰▰▰▱▱▱"))       # the tail stays visible text
        self.assertEqual(int(webhook.unpack_state(text)["p"]), 959)
        # only the first line survives a client that trims the end of a message
        self.assertEqual(int(webhook.unpack_state(text.split("\n")[0])["p"]), 959)

    def test_a_damaged_state_is_ignored(self):
        run = webhook.hide_state(webhook.pack_state(self.session, 1700000000))
        self.assertIsNone(webhook.unpack_state("matn\n" + webhook.ZW0 + run[1:]))
        self.assertIsNone(webhook.unpack_state("matn"))

    def test_marker_carries_idle_state(self):
        self.session.idle_streak = 2
        self.session.attempts = 1
        self.session.can_delete = False
        text = webhook.with_state("matn", webhook.pack_state(self.session, 1700000000))
        state = webhook.unpack_state(text)
        self.assertEqual(state["z"], 2)
        self.assertEqual(state["y"], 1)
        self.assertFalse(state["x"])

    def test_state_never_pushes_a_message_over_the_limit(self):
        text = webhook.with_state("x" * 5000, webhook.pack_state(self.session, 1700000000))
        self.assertLessEqual(webhook.utf16_length(text), 4096)
        self.assertEqual(int(webhook.unpack_state(text)["p"]), 959)

    def test_the_old_spoiler_look_can_be_switched_back_on(self):
        with mock.patch.object(webhook, "HIDDEN_MODE", "spoiler"):
            hidden = webhook.hide_state(webhook.pack_state(self.session, 1700000000))
        self.assertIn('<span class="tg-spoiler">', hidden)
        self.assertIn("#ZS2 s|959|", hidden)
        self.assertEqual(int(webhook.unpack_state("matn\n" + hidden)["k"]), 4242)

    def test_legacy_base64_marker_still_parses(self):
        payload = {"m": "solo", "p": 959, "i": 2, "c": 1, "w": 1, "o": 7, "q": 23297,
                   "d": 1700000000, "r": 1, "n": 10, "t": 60, "s": 5, "k": 9, "l": None, "a": []}
        token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        state = webhook.unpack_state("matn\n\n#ZS " + token)
        self.assertEqual(int(state["p"]), 959)
        self.assertEqual(int(state["q"]), 23297)


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class IdleStopTests(unittest.TestCase):
    """Three questions without a single guess stop the game by themselves."""

    def setUp(self):
        self.bot = FakeBot()
        self.archive = Archive("data/all")
        playlist = webhook.build_playlist(self.archive.questions(959), 1, 10, 7)
        self.session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=10, seed=7,
        )
        self.session.begin()
        self.session.control_message_id = 11

    def _texts(self):
        return "\n".join(item.get("text") or "" for item in self.bot.sent)

    def test_warnings_then_auto_stop(self):
        with mock.patch.object(webhook, "TICKS_ENABLED", False):
            for _ in range(3):
                self.assertTrue(self.session.resolve_unanswered())
                asyncio.run(webhook.next_question(self.bot, -100, self.session, "O‘tkazildi"))
        texts = self._texts()
        self.assertIn("Yana 2 savol javobsiz qolsa", texts)
        self.assertIn("Yana bir savolga taxmin yozilmasa", texts)
        self.assertIn("O‘YIN AVTOMATIK TO‘XTATILDI", texts)
        self.assertIn("Ketma-ket 3 savolga", texts)
        self.assertTrue(self.session.auto_stopped)
        # the game really ended: no new question was sent after the stop
        self.assertNotIn("SAVOL 4 / 10", texts)

    def test_a_wrong_guess_is_not_idle(self):
        checker = webhook.checker_for([])
        with mock.patch.object(webhook, "TICKS_ENABLED", False):
            for _ in range(3):
                # owner_id=1: a real guess counts as activity even when wrong
                self.assertEqual(self.session.submit(1, "butunlay boshqa javob", checker), "wrong")
                self.session.resolve_unanswered()
                asyncio.run(webhook.next_question(self.bot, -100, self.session, "O‘tkazildi"))
        self.assertEqual(self.session.idle_streak, 0)
        self.assertNotIn("AVTOMATIK TO‘XTATILDI", self._texts())


    def test_a_stranger_in_solo_is_not_activity(self):
        """In solo mode only the owner plays, so a stranger keeps the game idle."""
        checker = webhook.checker_for([])
        with mock.patch.object(webhook, "TICKS_ENABLED", False):
            for _ in range(3):
                self.assertEqual(self.session.submit(999, "taxmin", checker), webhook.FOREIGN)
                self.session.resolve_unanswered()
                asyncio.run(webhook.next_question(self.bot, -100, self.session, "O‘tkazildi"))
        self.assertIn("AVTOMATIK TO‘XTATILDI", self._texts())


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class LiveCountdownTests(unittest.TestCase):
    """handle_tick() repaints the strip and resolves the question when time is up."""

    def setUp(self):
        self.bot = FakeBot()
        self.archive = Archive("data/all")
        playlist = webhook.build_playlist(self.archive.questions(959), 1, 10, 7)
        self.session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=10, seed=7,
        )
        self.session.begin()
        self.session.control_message_id = 77
        self.application = FakeApplication(self.bot, self.archive)

    def _tick(self, deadline):
        marker = webhook.pack_state(self.session, deadline).strip()
        payload = {"tick": marker, "chat": -100, "wait": 0}
        with mock.patch.object(webhook, "get_application", lambda: self.application), \
             mock.patch.object(webhook, "TICKS_ENABLED", False):
            return json.loads(asyncio.run(webhook.handle_tick(payload)).body)

    def test_tick_repaints_the_remaining_time(self):
        result = self._tick(int(time.time()) + 37)
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], 37)
        self.assertTrue(self.bot.edits)
        text = self.bot.edits[-1]["text"]
        self.assertIn(msg.countdown(result["remaining"], 60), text)
        self.assertIn("▰▰▰▰▰▰", text)
        self.assertNotIn("#ZS", visible(text))                  # nothing readable is left
        self.assertIsNotNone(webhook.unpack_state(text))        # yet the state is still there
        label = self.bot.edits[-1]["reply_markup"].inline_keyboard[0][0].text
        self.assertTrue(label.startswith(msg.clock(result["remaining"])))

    def test_the_countdown_moves_from_tick_to_tick(self):
        self._tick(int(time.time()) + 40)
        self._tick(int(time.time()) + 12)
        self.assertEqual(len(self.bot.edits), 2)
        lines = [visible(edit["text"]).splitlines()[1] for edit in self.bot.edits]
        self.assertNotEqual(lines[0], lines[1])                 # the clock really moves
        self.assertIn("00:1", lines[1])

    def test_bg_mode_answers_at_once_and_paints_afterwards(self):
        marker = webhook.pack_state(self.session, int(time.time()) + 30)
        bucket: list = []
        token = webhook._bg_tasks.set(bucket)
        try:
            response = asyncio.run(
                webhook.handle_tick({"tick": marker, "chat": -100, "wait": 0, "mode": "bg"}))
        finally:
            webhook._bg_tasks.reset(token)
        payload = json.loads(response.body)
        self.assertEqual(payload["mode"], "bg")
        self.assertEqual(len(bucket), 1)
        self.assertFalse(self.bot.edits)                        # Telegram is not kept waiting
        self.assertIsNotNone(response.background)
        with mock.patch.object(webhook, "get_application", lambda: self.application), \
             mock.patch.object(webhook, "TICKS_ENABLED", False):
            asyncio.run(response.background())
        self.assertTrue(self.bot.edits)                         # the hop ran after the answer

    def test_tick_resolves_when_time_is_up(self):
        result = self._tick(int(time.time()) - 1)
        self.assertTrue(result.get("resolved"))
        self.assertIn(77, self.bot.deleted)          # the strip is claimed by deletion
        texts = "\n".join(item.get("text") or "" for item in self.bot.sent)
        self.assertIn("Vaqt tugadi", texts)
        self.assertIn("SAVOL 2 / 10", texts)
        # the silent question is remembered in the marker of the next strip
        marker = webhook.unpack_state(self.bot.sent[-2]["text"])
        self.assertEqual(marker["z"], 1)
        self.assertIn("Yana 2 savol javobsiz qolsa", texts)

    def test_a_tick_without_a_state_is_refused(self):
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            result = json.loads(asyncio.run(webhook.handle_tick(
                {"tick": "#ZS2 x", "chat": -100, "wait": 0, "mode": "sleep"})).body)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no state")
        self.assertFalse(self.bot.edits)

    def test_a_tick_for_an_unknown_question_is_refused(self):
        marker = webhook.pack_state(self.session, int(time.time()) + 30)
        with mock.patch.object(webhook, "ARCHIVE", Archive("data/all")), \
             mock.patch.object(webhook, "get_application", lambda: self.application), \
             mock.patch.object(webhook, "MAX_TICK_HORIZON", 10 ** 9):
            payload = {"tick": marker.replace("|23", "|99999923", 1), "chat": -100,
                       "wait": 0, "mode": "sleep"}
            result = json.loads(asyncio.run(webhook.handle_tick(payload)).body)
        self.assertFalse(result["ok"])
        self.assertFalse(self.bot.edits)

    def test_a_deadline_too_far_ahead_is_refused(self):
        marker = webhook.pack_state(self.session, int(time.time()) + webhook.MAX_TICK_HORIZON + 60)
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            result = json.loads(asyncio.run(webhook.handle_tick(
                {"tick": marker, "chat": -100, "wait": 0, "mode": "sleep"})).body)
        self.assertEqual(result["reason"], "deadline too far ahead")


class StripKeyboardTests(unittest.TestCase):
    """The countdown is a control: the clock sits on the button and answers a tap."""

    def setUp(self):
        if not webhook:
            self.skipTest("install requirements.txt to test the webhook")

    def test_refresh_button_carries_the_live_clock(self):
        keyboard = st.game_keyboard(clock=msg.clock(47))
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "00:47 · Vaqtni yangilash")

    def test_without_a_clock_the_label_stays_plain(self):
        keyboard = st.game_keyboard()
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "Vaqtni yangilash")


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
        self.assertTrue(any("QOIDALAR" in text for text in sent))
        self.assertTrue(any("savol" in text for text in sent))

    def test_self_test_reports_every_step(self):
        async def run():
            return await webhook.self_test()

        lines = asyncio.run(run())
        joined = "\n".join(lines)
        self.assertIn("/start → 1 xabar", joined)
        self.assertIn("/help → 1 xabar", joined)
        self.assertIn("/rules → 1 xabar", joined)
        self.assertIn("/packages → 1 xabar", joined)
        self.assertIn("/solo 959 → 1 xabar", joined)          # sozlamalar paneli
        self.assertIn("Boshlash → ", joined)                     # o‘yin boshlandi
        self.assertIn("javob", joined)                          # to‘g‘ri javob tekshirildi
        self.assertIn("O‘tkazish 1 → ", joined)                 # admin skip
        self.assertIn("O‘YIN AVTOMATIK TO‘XTATILDI", joined)    # 3 savol javobsiz qoldi
        self.assertNotIn("Sinov xatoligi", joined)


if __name__ == "__main__":
    unittest.main()
