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
        self.reply_records = []
        self.from_user = SimpleNamespace(id=1, first_name="Aziz")

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.reply_records.append({"text": text, **kwargs})


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
        self.assertIn("<b>01:00</b> · QOLGAN VAQT", strip_text)
        self.assertIn("Savol 1 / 10", strip_text)
        self.assertEqual(visible(strip_text).splitlines()[-1], "▰" * 20)
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
        self.assertIn("/stats → 1 xabar", joined)               # statistika paneli chiqdi
        self.assertIn("STATISTIKA", joined)
        self.assertNotIn("Sinov xatoligi", joined)
        self.assertNotIn("tugma topilmadi", joined)

    def test_the_dry_run_finds_its_buttons_next_to_the_clock(self):
        """With a public address the strip grows a Mini App row — the run must cope."""
        async def run():
            with mock.patch.dict(os.environ, {"WEBHOOK_URL": "https://zakovat.example"}):
                return await webhook.self_test()

        joined = "\n".join(asyncio.run(run()))
        self.assertIn("O‘YIN AVTOMATIK TO‘XTATILDI", joined)
        self.assertIn("STATISTIKA", joined)
        self.assertNotIn("tugma topilmadi", joined)
        self.assertNotIn("#ZS", joined)                         # no state leaks into the run


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class StatsAccessTests(unittest.TestCase):
    """The statistics panel exists for one Telegram id only."""

    def setUp(self):
        from bot import stats

        self.stats = stats
        self.bot = FakeBot()
        self.archive = Archive("data/all")
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"archive": self.archive}),
            bot=self.bot, args=[],
        )
        stats.reset()
        self._url = stats.STORE_URL
        stats.STORE_URL = ""            # keep the tests offline

    def tearDown(self):
        self.stats.STORE_URL = self._url
        self.stats.reset()

    def test_the_owner_id_is_the_requested_one(self):
        self.assertEqual(self.stats.OWNER_ID, int(os.getenv("ZAKOVAT_OWNER_ID", "8004724563")))
        self.assertEqual(self.stats.OWNER_ID, 8004724563)

    def test_a_stranger_never_sees_the_numbers(self):
        message = FakeMessage("/stats")
        asyncio.run(webhook.cmd_stats(update(message), self.context))
        text = message.replies[-1]
        self.assertIn("faqat bot egasi", text)
        self.assertNotIn("STATISTIKA", text)
        self.assertNotIn("ARXIV", text)

    def test_the_owner_gets_the_whole_dashboard(self):
        message = FakeMessage("/stats")
        with mock.patch.object(self.stats, "OWNER_ID", 1):
            asyncio.run(webhook.cmd_stats(update(message), self.context))
        record = message.reply_records[-1]
        self.assertIn("STATISTIKA", record["text"])
        self.assertIn("ARXIV", record["text"])
        packages, _ = self.archive.local_counts()
        self.assertIn(f"Turkumlar (paketlar): {packages}", record["text"])
        labels = [button.text for button in record["reply_markup"].inline_keyboard[0]]
        self.assertEqual(labels, ["Yangilash", "Bo‘limlar", "O‘yinlar"])

    def test_the_sections_view_can_be_opened(self):
        message = FakeMessage("/stats bolimlar")
        with mock.patch.object(self.stats, "OWNER_ID", 1):
            self.context.args = ["bolimlar"]
            asyncio.run(webhook.cmd_stats(update(message), self.context))
        text = message.replies[-1]
        self.assertIn("BO‘LIMLAR", text)
        self.assertIn("Talabalar ligasi", text)

    def test_a_played_game_lands_in_the_counters(self):
        from bot.quiz import build_playlist

        playlist = build_playlist(self.archive.questions(959), rounds=1, per_round=3, seed=7)
        session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=3, seed=7,
        )
        session.begin()
        with mock.patch.object(webhook, "TICKS_ENABLED", False):
            asyncio.run(webhook.send_question(self.bot, -100, session))
            question = self.bot.sent[-1]["text"]
            answer = self.archive.question(webhook.unpack_state(question)["q"])["answer"]
            asyncio.run(webhook.on_text(update(FakeMessage(answer, question)), self.context))
        data = asyncio.run(self.stats.snapshot())
        self.assertGreaterEqual(data["counters"]["questions_asked"], 2)
        self.assertEqual(data["counters"]["answers_correct"], 1)
        self.assertEqual(data["users"], 1)
        self.assertEqual(data["packages_played"], 1)

    def test_an_unanswered_question_is_counted_once(self):
        from bot.quiz import build_playlist

        playlist = build_playlist(self.archive.questions(959), rounds=1, per_round=3, seed=7)
        session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=3, seed=7,
        )
        session.begin()
        self.assertTrue(webhook.close_question(session))
        self.assertFalse(webhook.close_question(session))    # never twice
        data = asyncio.run(self.stats.snapshot())
        self.assertEqual(data["counters"]["unanswered"], 1)
        self.assertEqual(len(data["events"]), 1)


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class StripClockTests(unittest.TestCase):
    """The clock is part of the question strip; the Mini App clock is gone.

    A separate window inside Telegram meant the question itself was not on
    screen while the clock ran, so there is no clock button, no clock page and
    no /api/timer route any more — the countdown lives in the message.
    """

    def setUp(self):
        self.archive = Archive("data/all")
        playlist = webhook.build_playlist(self.archive.questions(959), 1, 5, 7)
        self.session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="Intellektual <Olimpiada>",
            questions=playlist, owner_id=1, timer_seconds=60, rounds=1, per_round=5, seed=7,
        )
        self.session.begin()

    def test_no_clock_module_and_no_timer_route_are_left(self):
        import importlib

        with self.assertRaises(ImportError):
            importlib.import_module("bot.clock")
        routes = {getattr(route, "path", "") for route in webhook.app.routes}
        self.assertNotIn("/api/timer", routes)
        self.assertIn("/app", routes)                # the statistics Mini App stays

    def test_clock_url_is_empty_whatever_the_public_address_is(self):
        self.assertEqual(webhook.clock_url(self.session, int(time.time()) + 60, 45), "")
        with mock.patch.object(webhook, "public_base_url", lambda: "https://zakovat.example"):
            self.assertEqual(webhook.clock_url(self.session, int(time.time()) + 60, 45), "")

    def test_the_keyboard_has_no_web_app_button(self):
        keyboard = st.game_keyboard(clock=msg.clock(45),
                                    clock_url="https://zakovat.example/api/timer")
        self.assertFalse(_has_webapp(keyboard))
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "00:45 · Vaqtni yangilash")

    def test_the_strip_carries_the_clock_itself(self):
        deadline = int(time.time()) + 47
        text = webhook.strip_text(self.session, 47, deadline)
        shown = visible(text)
        self.assertIn("<b>00:47</b>", shown)              # digits, repainted every hop
        self.assertIn("QOLGAN VAQT", shown)
        track = shown.splitlines()[-1]
        self.assertEqual(track.count("▰") + track.count("▱"), 20)
        self.assertIsNotNone(webhook.unpack_state(text))   # and the state stays invisible

    def test_the_clock_moves_while_the_question_runs(self):
        deadline = int(time.time()) + 60
        first = visible(webhook.strip_text(self.session, 60, deadline))
        later = visible(webhook.strip_text(self.session, 55, deadline))
        last = visible(webhook.strip_text(self.session, 0, deadline))
        self.assertIn("<b>01:00</b>", first)
        self.assertIn("<b>00:55</b>", later)
        self.assertGreater(first.count("▰"), later.count("▰"))
        self.assertIn("<b>VAQT TUGADI</b>", last)
        self.assertEqual(later.count("▰"), 18)             # one segment is three seconds

    def test_repainting_sends_no_web_app_button(self):
        class Picky(FakeBot):
            async def edit_message_text(self, **kwargs):
                if _has_webapp(kwargs.get("reply_markup")):
                    raise AssertionError("the clock button came back")
                return await super().edit_message_text(**kwargs)

        bot = Picky()
        self.session.control_message_id = 77
        deadline = int(time.time()) + 30
        asyncio.run(webhook.paint_strip(bot, -100, self.session, 30, deadline))
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("<b>00:30</b>", visible(bot.edits[0]["text"]))


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class FloodCareTests(unittest.TestCase):
    """A Telegram flood pause must not drop the countdown chain."""

    def setUp(self):
        from telegram.error import RetryAfter

        self.RetryAfter = RetryAfter
        self.bot = FakeBot()
        self.archive = Archive("data/all")
        playlist = webhook.build_playlist(self.archive.questions(959), 1, 5, 7)
        self.session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=5, seed=7,
        )
        self.session.begin()
        self.session.control_message_id = 77
        self.application = FakeApplication(self.bot, self.archive)

    def _tick(self, deadline):
        marker = webhook.pack_state(self.session, deadline)
        payload = {"tick": marker, "chat": -100, "wait": 0, "mode": "sleep"}
        with mock.patch.object(webhook, "get_application", lambda: self.application), \
             mock.patch.object(webhook, "TICKS_ENABLED", False):
            return json.loads(asyncio.run(webhook.handle_tick(payload)).body)

    def test_a_throttled_edit_comes_back_later(self):
        async def throttled(**kwargs):
            raise self.RetryAfter(7)

        self.bot.edit_message_text = throttled
        waits = []

        async def fake_schedule(session, deadline, aliases=None, wait=None, mode=None):
            waits.append(wait)

        with mock.patch.object(webhook, "schedule_tick", fake_schedule), \
             mock.patch.object(webhook, "TICK_SECONDS", 5):
            result = self._tick(int(time.time()) + 30)
        self.assertTrue(result["ok"])
        self.assertEqual(result["throttled"], 17)        # 7 s pause + two tick steps
        self.assertEqual(waits, [17])

    def test_an_unmodified_message_is_not_a_failure(self):
        async def unmodified(**kwargs):
            raise webhook.TelegramError("message is not modified")

        self.bot.edit_message_text = unmodified
        result = self._tick(int(time.time()) + 30)
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], 30)
        self.assertNotIn("stopped", result)


def _has_webapp(markup) -> bool:
    return any(getattr(button, "web_app", None)
               for row in getattr(markup, "inline_keyboard", None) or []
               for button in row)


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class WebAppFallbackTests(unittest.TestCase):
    """A refused Mini App button must never cost the player the countdown."""

    def setUp(self):
        self.archive = Archive("data/all")
        playlist = webhook.build_playlist(self.archive.questions(959), 1, 5, 7)
        self.session = webhook.Session(
            chat_id=-100, mode="solo", package_id=959, package_title="959", questions=playlist,
            owner_id=1, timer_seconds=60, rounds=1, per_round=5, seed=7,
        )
        self.session.begin()
        self.session.control_message_id = 77

    def test_repainting_never_offers_a_web_app_button(self):
        class Picky(FakeBot):
            def __init__(self):
                super().__init__()
                self.refused = 0

            async def edit_message_text(self, **kwargs):
                if _has_webapp(kwargs.get("reply_markup")):
                    self.refused += 1
                    raise webhook.TelegramError("Bad Request: WEBAPP_URL_INVALID")
                return await super().edit_message_text(**kwargs)

        bot = Picky()
        deadline = int(time.time()) + 30
        asyncio.run(webhook.paint_strip(bot, -100, self.session, 30, deadline))
        self.assertEqual(bot.refused, 0)          # there is nothing left to refuse
        self.assertEqual(len(bot.edits), 1)
        self.assertFalse(_has_webapp(bot.edits[0]["reply_markup"]))
        self.assertIn("<b>00:30</b>", visible(bot.edits[0]["text"]))

    def test_sending_the_strip_without_the_clock_button(self):
        class PickySend(FakeBot):
            async def send_message(self, **kwargs):
                if _has_webapp(kwargs.get("reply_markup")):
                    raise webhook.TelegramError("Bad Request: WEBAPP_URL_INVALID")
                return await super().send_message(**kwargs)

        bot = PickySend()
        deadline = int(time.time()) + 60
        asyncio.run(webhook.send_control(bot, -100, self.session, deadline))
        self.assertEqual(len(bot.sent), 1)
        self.assertFalse(_has_webapp(bot.sent[0]["reply_markup"]))
        self.assertEqual(self.session.control_message_id, 1)   # the strip still got its id

    def test_other_errors_are_not_swallowed(self):
        class Broken(FakeBot):
            async def edit_message_text(self, **kwargs):
                raise webhook.TelegramError("message is not modified")

        with self.assertRaises(webhook.TelegramError):
            asyncio.run(webhook.paint_strip(Broken(), -100, self.session, 30,
                                            int(time.time()) + 30))


try:
    from tests.test_archive import (Fetcher, build_local, build_remote, payloads_for,
                                    remote_row)
except ImportError:                                  # discovery puts tests/ on the path
    from test_archive import (Fetcher, build_local, build_remote, payloads_for,
                              remote_row)

from bot.archive import REMOTE_PACKAGE_FLOOR


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class WholeBaseTests(unittest.TestCase):
    """The turnirlar.uz half of the archive, as the bot sees it."""

    def setUp(self):
        import pathlib
        import shutil
        import tempfile

        self.base = pathlib.Path(tempfile.mkdtemp(prefix="webhook-base-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        build_local(self.base / "all")
        self.built = build_remote(self.base / "turnirlar")
        self.fetcher = Fetcher(payloads_for(self.built))
        self.archive = Archive(self.base / "all", self.base / "snapshot.json",
                               self.base / "turnirlar", fetcher=self.fetcher,
                               cache_dir=self.base / "cache")
        self.bot = FakeBot()
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"archive": self.archive}),
            bot=self.bot, args=[],
        )
        self.remote_id = REMOTE_PACKAGE_FLOOR + 11
        self.remote_title = (self.archive.package_info(self.remote_id) or {})["title"]

    def test_the_welcome_names_both_sources(self):
        message = FakeMessage("/start")
        with mock.patch.object(webhook, "ARCHIVE", self.archive):
            asyncio.run(webhook.cmd_start(update(message), self.context))
        text = message.replies[-1]
        self.assertIn("zakovatklubi.uz — 1 paket, 2 savol", text)
        self.assertIn("turnirlar.uz — 4 paket, 12 savol", text)
        self.assertIn("Arxiv: 5 paket, 14 savol.", text)

    def test_a_single_source_is_not_listed_twice(self):
        archive = Archive(self.base / "all", self.base / "snapshot.json", None,
                          cache_dir=self.base / "cache")
        self.assertEqual(archive.welcome_sources(), [])
        message = FakeMessage("/start")
        with mock.patch.object(webhook, "ARCHIVE", archive):
            asyncio.run(webhook.cmd_start(update(message), self.context))
        self.assertIn("Arxiv: 1 paket, 2 savol.", message.replies[-1])
        self.assertNotIn("turnirlar.uz —", message.replies[-1])

    def test_the_package_list_marks_rows_from_the_big_base(self):
        message = FakeMessage("/packages")
        asyncio.run(webhook.render_packages(self.context, 1, "", message=message))
        text = message.replies[-1]
        self.assertIn(f"{self.remote_id} — {self.remote_title} (3 savol) *", text)
        self.assertIn("* — turnirlar.uz bazasidan", text)

    def test_a_local_only_list_has_no_mark_and_no_footnote(self):
        message = FakeMessage("/packages Mahalliy")
        asyncio.run(webhook.render_packages(self.context, 1, "Mahalliy", message=message))
        text = message.replies[-1]
        self.assertNotIn("*", text)

    def test_katta_opens_the_whole_base(self):
        for word in ("katta", "KATTA", "butun", " hamma "):
            resolved = webhook._resolve_package(self.context, word)
            self.assertIsNotNone(resolved, word)
            self.assertTrue(self.archive.is_remote(resolved[0]) or resolved[0] == 5)

    def test_a_random_game_stays_local_unless_asked(self):
        picks = {webhook._resolve_package(self.context, "")[0] for _ in range(20)}
        self.assertEqual(picks, {5})
        self.assertEqual(self.fetcher.calls, [])

    def test_a_remote_package_is_played_after_its_shard_is_fetched(self):
        config = st.GameConfig(package_id=self.remote_id, mode="solo", rounds=1,
                               per_round=3, timer=45, seed=7)
        asyncio.run(webhook.start_from_config(self.context, config, -100, 1))
        texts = " ".join(row["text"] for row in self.bot.sent)
        self.assertIn(self.remote_title, texts)
        self.assertIn("Uzoq savol", texts)
        self.assertTrue(self.fetcher.calls[0].endswith("turnirlar-0001.json.gz"))

    def test_an_unreachable_shard_says_so_instead_of_starting_an_empty_game(self):
        self.archive._fetch = Fetcher(fail=True)
        self.archive.cache_dir = self.base / "empty-cache"
        config = st.GameConfig(package_id=self.remote_id, mode="solo", rounds=1,
                               per_round=3, timer=45, seed=7)
        asyncio.run(webhook.start_from_config(self.context, config, -100, 1))
        text = self.bot.sent[-1]["text"]
        self.assertIn("turnirlar.uz bazasidan olinadi", text)
        self.assertIn(self.remote_title, text)
        self.assertEqual(len(self.bot.sent), 1)          # no question was sent

    def test_an_empty_local_package_keeps_its_own_wording(self):
        self.archive.manifest["packages"].append({"id": 77, "title": "Bo‘sh", "questions": 0})
        self.archive.packages[77] = self.archive.manifest["packages"][-1]
        config = st.GameConfig(package_id=77, mode="solo", rounds=1, per_round=3,
                               timer=45, seed=7)
        asyncio.run(webhook.start_from_config(self.context, config, -100, 1))
        self.assertIn("Bu paketda savol yo‘q", self.bot.sent[-1]["text"])

    def test_a_remote_package_is_resolved_by_its_number(self):
        resolved = webhook._resolve_package(self.context, str(self.remote_id))
        self.assertEqual(resolved, (self.remote_id, self.remote_title))
        self.assertEqual(self.fetcher.calls, [])          # the manifest is enough

    def test_searching_the_big_base_needs_no_download(self):
        found = webhook._resolve_package(self.context, "to‘plami 21")
        self.assertEqual(found[0], REMOTE_PACKAGE_FLOOR + 21)
        self.assertEqual(self.fetcher.calls, [])

    def test_a_remote_package_carries_its_own_metadata(self):
        info = self.archive.package_info(self.remote_id)
        self.assertEqual(info["origin"], "Boshqa manba")
        self.assertEqual(info["section"], "Professional")
        self.assertEqual(info["date"], "16.11.2025")
        self.assertEqual(info["url"], "https://turnirlar.uz/savollar/paket/11")
        self.assertEqual(info["shard"], 1)

    def test_unused_helpers_stay_importable(self):
        self.assertTrue(callable(remote_row))


if __name__ == "__main__":
    unittest.main()
