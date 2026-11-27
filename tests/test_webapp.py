import asyncio
import json
import os
import re
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

try:
    import bot.webhook as webhook
    from bot import dashboard, messages as msg, settings as st, stats, webapp as wa
    from bot.archive import Archive
except ImportError:  # fastapi / python-telegram-bot not installed yet
    webhook = None


TOKEN = "123456:TEST"
OWNER = 8004724563
STRANGER = 424242
BASE = "https://zakovat.test"


def _telegram_transport(state: dict):
    """Stand in for the Bot API calls the Telegram store and menu button make."""
    import httpx

    def handler(request):
        method = str(request.url).rsplit("/", 1)[-1]
        try:
            body = json.loads(request.content.decode("utf-8"))
        except ValueError:
            body = {}
        if method == "getChat":
            result = {"id": body.get("chat_id"), "type": "private"}
            pinned = state.get("pinned")
            if pinned:
                result["pinned_message"] = {"message_id": pinned,
                                            "text": state["messages"][pinned]}
            return httpx.Response(200, text=json.dumps({"ok": True, "result": result}))
        if method == "sendMessage":
            number = state["next"]
            state["next"] += 1
            state["messages"][number] = body.get("text", "")
            return httpx.Response(200, text=json.dumps(
                {"ok": True, "result": {"message_id": number}}))
        if method == "pinChatMessage":
            state["pinned"] = body.get("message_id")
            return httpx.Response(200, text=json.dumps({"ok": True, "result": True}))
        if method == "editMessageText":
            number = body.get("message_id")
            if state["messages"].get(number) == body.get("text"):
                return httpx.Response(400, text=json.dumps(
                    {"ok": False, "description": "Bad Request: message is not modified"}))
            state["messages"][number] = body.get("text", "")
            return httpx.Response(200, text=json.dumps({"ok": True, "result": True}))
        if method == "setChatMenuButton":
            state.setdefault("menus", {})[body.get("chat_id")] = body.get("menu_button")
            return httpx.Response(200, text=json.dumps({"ok": True, "result": True}))
        return httpx.Response(400, text=json.dumps({"ok": False, "description": method}))

    return httpx.MockTransport(handler)


def _telegram_state() -> dict:
    return {"messages": {}, "next": 100, "pinned": None, "menus": {}}


class MenuBot:
    """A bot double that records the menu-button calls a grant makes."""

    def __init__(self, fail=False):
        self.menus = []
        self.chats = []
        self.fail = fail

    async def set_chat_menu_button(self, chat_id=None, menu_button=None):
        self.menus.append((chat_id, menu_button))
        if self.fail:
            from telegram.error import BadRequest

            raise BadRequest("chat not found")
        return True

    async def get_chat(self, chat_id):
        self.chats.append(chat_id)
        return SimpleNamespace(full_name="Aziz Karimov")


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.reply_records = []
        self.from_user = SimpleNamespace(id=STRANGER, first_name="Begona")

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.reply_records.append({"text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.replies))


class FakeUpdate:
    def __init__(self, message, user_id=STRANGER):
        self.effective_message = message
        self.effective_user = SimpleNamespace(id=user_id, first_name="Kim")
        self.effective_chat = SimpleNamespace(id=user_id, type="private")
        self.callback_query = None


def make_context(bot=None, args=None):
    return SimpleNamespace(bot=bot if bot is not None else MenuBot(), args=list(args or []),
                           application=SimpleNamespace(bot_data={}))


def offline(case):
    """Keep the whole test away from the network: local counters, fixed token."""
    case._patches = [mock.patch.object(wa, "bot_token", lambda: TOKEN),
                     mock.patch.object(stats, "backend", lambda: "lokal")]
    for patch in case._patches:
        patch.start()


def stop_offline(case):
    for patch in case._patches:
        patch.stop()


def stored_document(state) -> dict:
    return json.loads(state["messages"][state["pinned"]].split("\n", 1)[1])


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class SignatureTests(unittest.TestCase):
    """initData is the only proof of who is asking, so it has to be checked."""

    def setUp(self):
        stats.reset()
        offline(self)

    def tearDown(self):
        stop_offline(self)
        stats.reset()

    def test_a_correctly_signed_payload_is_accepted(self):
        init = wa.forge_init_data({"id": OWNER, "first_name": "Ega", "username": "ega"},
                                  TOKEN, chat={"id": OWNER, "type": "private"},
                                  start_param="panel")
        verified = wa.validate_init_data(init, TOKEN)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["user"]["id"], OWNER)
        self.assertEqual(verified["start_param"], "panel")
        self.assertEqual(verified["chat"]["id"], OWNER)
        self.assertLess(abs(verified["auth_date"] - time.time()), 30)

    def test_a_changed_byte_is_refused(self):
        init = wa.forge_init_data({"id": OWNER, "first_name": "Ega"}, TOKEN)
        broken = init[:-1] + ("0" if init[-1] != "0" else "1")
        self.assertIsNone(wa.validate_init_data(broken, TOKEN))

    def test_another_users_id_cannot_be_claimed(self):
        """Rewriting the id without re-signing must not let a stranger in."""
        init = wa.forge_init_data({"id": STRANGER, "first_name": "Begona"}, TOKEN)
        stolen = init.replace(str(STRANGER), str(OWNER))
        self.assertNotEqual(stolen, init)
        self.assertIsNone(wa.validate_init_data(stolen, TOKEN))

    def test_an_expired_session_is_refused(self):
        init = wa.forge_init_data({"id": OWNER}, TOKEN,
                                  auth_date=int(time.time()) - wa.INIT_DATA_TTL - 60)
        self.assertIsNone(wa.validate_init_data(init, TOKEN))

    def test_a_clock_from_the_future_is_refused(self):
        init = wa.forge_init_data({"id": OWNER}, TOKEN, auth_date=int(time.time()) + 3600)
        self.assertIsNone(wa.validate_init_data(init, TOKEN))

    def test_the_signature_belongs_to_one_bot_only(self):
        init = wa.forge_init_data({"id": OWNER}, "999:some-other-token")
        self.assertIsNone(wa.validate_init_data(init, TOKEN))

    def test_junk_and_missing_proof_are_refused(self):
        for value in ("", "   ", "not-a-payload", "hash=abc", "user=%7B%7D&hash=abc"):
            self.assertIsNone(wa.validate_init_data(value, TOKEN), value)

    def test_a_payload_without_a_user_id_is_refused(self):
        init = wa.forge_init_data({"first_name": "Anonim"}, TOKEN)
        self.assertIsNone(wa.validate_init_data(init, TOKEN))

    def test_no_token_means_no_trust(self):
        init = wa.forge_init_data({"id": OWNER}, TOKEN)
        self.assertIsNone(wa.validate_init_data(init, ""))

    def test_the_address_of_the_page_is_built_from_the_public_one(self):
        self.assertEqual(wa.app_url("https://zakovat-peach.vercel.app/"),
                         "https://zakovat-peach.vercel.app/app")
        self.assertEqual(wa.app_url(""), "")


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class AccessTests(unittest.TestCase):
    """Only the owner and the ids they granted may see the numbers."""

    def setUp(self):
        stats.reset()
        offline(self)

    def tearDown(self):
        stop_offline(self)
        stats.reset()

    def test_the_owner_is_always_allowed(self):
        self.assertTrue(wa.can_open(OWNER))
        self.assertTrue(stats.allowed(str(OWNER)))

    def test_a_stranger_is_not_allowed(self):
        self.assertFalse(wa.can_open(STRANGER))
        self.assertFalse(wa.can_open(None))
        self.assertFalse(wa.can_open("x"))

    def test_a_grant_opens_the_door_and_a_revoke_closes_it(self):
        saved, access = asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        self.assertTrue(saved)
        self.assertTrue(wa.can_open(STRANGER))
        self.assertEqual(access[str(STRANGER)]["name"], "Aziz")
        self.assertEqual(wa.access_report()[0]["id"], STRANGER)
        saved, access = asyncio.run(stats.set_access(STRANGER, "Aziz", False))
        self.assertTrue(saved)
        self.assertFalse(wa.can_open(STRANGER))
        self.assertEqual(wa.access_report(), [])

    def test_the_list_survives_a_cold_start_through_the_store(self):
        state = _telegram_state()
        transport = _telegram_transport(state)
        with mock.patch.object(stats, "backend", lambda: "telegram"):
            asyncio.run(stats.set_access(777, "Sardor", True, transport=transport))
            stats.reset()                                  # a new instance knows nobody
            self.assertFalse(stats.allowed(777))
            stored = asyncio.run(stats.load_access(transport))
        self.assertTrue(stats.allowed(777))
        self.assertEqual(stored["777"]["name"], "Sardor")
        self.assertEqual(stored_document(state)["a"]["777"]["name"], "Sardor")

    def test_counters_survive_a_grant(self):
        state = _telegram_state()
        transport = _telegram_transport(state)
        with mock.patch.object(stats, "backend", lambda: "telegram"):
            stats.record("game_start", user_id=1, games_started=3)
            asyncio.run(stats.sync(force=True, transport=transport))
            asyncio.run(stats.set_access(777, "Sardor", True, transport=transport))
            stats.reset()
            asyncio.run(stats.load_access(transport))
        document = stored_document(state)
        self.assertEqual(document["c"]["games_started"], 3)
        self.assertIn("777", document["a"])

    def test_the_list_is_bounded_and_the_newest_grant_is_kept(self):
        for index in range(stats.ACCESS_LIMIT):
            stats._access[str(1000 + index)] = {"name": f"u{index}", "since": 1000 + index}
        asyncio.run(stats.set_access(999999, "Yangi", True))
        self.assertLessEqual(len(stats.access_list()), stats.ACCESS_LIMIT)
        self.assertTrue(stats.allowed(999999))
        self.assertFalse(stats.allowed(1000))              # the oldest grant went first

    def test_a_probe_never_erases_the_list(self):
        state = _telegram_state()
        transport = _telegram_transport(state)
        with mock.patch.object(stats, "backend", lambda: "telegram"):
            asyncio.run(stats.set_access(777, "Sardor", True, transport=transport))
            report = asyncio.run(stats.probe(transport))
        self.assertTrue(report["ok"], report)
        self.assertIn("777", stored_document(state)["a"])

    def test_a_snapshot_keeps_the_list_when_there_is_no_store(self):
        """A store-less copy must not forget the grants it was given."""
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        body = asyncio.run(stats.snapshot())
        self.assertTrue(stats.allowed(STRANGER))
        self.assertIn(str(STRANGER), body["access"])
        self.assertEqual(wa.access_report()[0]["name"], "Aziz")

    def test_reset_clears_the_list_too(self):
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        stats.reset()
        self.assertEqual(stats.access_list(), {})
        self.assertFalse(stats.allowed(STRANGER))


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class PayloadTests(unittest.TestCase):
    """What an allowed visitor is shown, and what a refused one gets."""

    def setUp(self):
        stats.reset()
        offline(self)
        self.archive = Archive("data/all")

    def tearDown(self):
        stop_offline(self)
        stats.reset()

    def test_the_owner_gets_every_panel(self):
        stats.record("game_start", user_id=OWNER, games_started=2)
        stats.record("question", questions_asked=5, log=False)
        stats.record("correct", answers_correct=4, log=False)
        stats.record("wrong", answers_wrong=1, log=False)
        body = asyncio.run(wa.payload({"id": OWNER, "first_name": "Ega"}, self.archive,
                                      count=False))
        self.assertTrue(body["ok"])
        self.assertTrue(body["owner"])
        self.assertEqual(body["counters"]["games_started"], 2)
        self.assertEqual(body["accuracy"], 80)
        self.assertEqual(body["asked"], 5)
        self.assertGreaterEqual(len(body["sections"]), 10)
        self.assertEqual(body["archive"]["packages"], 1266)
        self.assertEqual(body["archive"]["questions"], 30853)
        self.assertEqual(body["user"]["name"], "Ega")
        self.assertIn("storage", body)
        self.assertIn("server", body)

    def test_opening_the_app_is_counted_once(self):
        body = asyncio.run(wa.payload({"id": OWNER, "first_name": "Ega"}, self.archive))
        self.assertTrue(body["ok"])
        self.assertEqual(body["counters"]["app_opened"], 1)

    def test_a_refusal_says_why_without_numbers(self):
        body = wa.denied()
        self.assertFalse(body["ok"])
        self.assertIn("faqat bot egasi", body["reason"])
        self.assertNotIn("counters", body)

    def test_the_display_name_falls_back_to_the_username(self):
        self.assertEqual(wa.display_name({"id": 5, "username": "aziz"}), "aziz")
        self.assertEqual(wa.display_name({"id": 5, "first_name": "Aziz", "last_name": "K."}),
                         "Aziz K.")
        self.assertEqual(wa.display_name({"id": 5}), "id 5")

    def test_the_event_labels_cover_the_new_kinds(self):
        for kind in ("app_opened", "grant", "revoke", "app_denied"):
            self.assertIn(kind, msg.EVENT_LABELS, kind)
        for kind in ("app_opened", "grant", "revoke"):
            self.assertIn(kind, wa.EVENT_LABELS, kind)


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class PageTests(unittest.TestCase):
    """The Mini App page is self-contained and dressed like Telegram."""

    def setUp(self):
        self.page = dashboard.render("user=%7B%22id%22%3A1%7D&hash=abc", api="/api/app")

    def test_the_proof_is_handed_to_the_page(self):
        self.assertIn('"user=%7B%22id%22%3A1%7D\\u0026hash=abc"', self.page)
        self.assertIn('"/api/app"', self.page)

    def test_a_quote_in_the_proof_cannot_break_out_of_the_script(self):
        page = dashboard.render('x"</script><script>alert(1)</script>', api="/api/app")
        self.assertNotIn("</script><script>alert", page)
        self.assertIn("\\u003c/script\\u003e", page)

    def test_nothing_is_loaded_from_a_third_party(self):
        urls = re.findall(r'(?:src|href)="([^"]+)"', self.page)
        self.assertEqual([url for url in urls if url.startswith("http")],
                         ["https://telegram.org/js/telegram-web-app.js"])
        self.assertNotIn("<link", self.page)               # no stylesheet, no font request
        self.assertIn("<style>", self.page)                # the CSS travels with the page

    def test_dark_is_black_but_not_pitch_black(self):
        self.assertIn("--bg:#15191d", self.page)
        self.assertIn("--surface:#1d2329", self.page)
        self.assertNotIn("--bg:#000", self.page)
        self.assertNotIn("background:#000", self.page)
        self.assertNotIn("#000000", self.page)

    def test_light_is_telegram_white_but_not_pure_white(self):
        self.assertIn("--bg:#eff1f4", self.page)
        self.assertNotIn("--bg:#fff", self.page)
        self.assertNotIn("body{background:#fff", self.page)

    def test_the_theme_follows_telegram_and_the_device(self):
        for marker in ("colorScheme", "themeParams", "data-theme", "prefers-color-scheme",
                       "setHeaderColor", "setBackgroundColor", "themeChanged"):
            self.assertIn(marker, self.page, marker)

    def test_the_page_uses_the_telegram_apis(self):
        for call in ("w.ready()", "w.expand()", "HapticFeedback", "BackButton"):
            self.assertIn(call, self.page, call)

    def test_the_tabs_are_there(self):
        for label in ("Umumiy", "Bo‘limlar", "O‘yinlar", "Ruxsatlar"):
            self.assertIn(label, self.page, label)

    def test_no_emoji_anywhere(self):
        for chunk in self.page:
            if 0x1F300 <= ord(chunk) <= 0x1FAFF or 0x2600 <= ord(chunk) <= 0x27BF:
                self.fail(f"emoji found: {chunk!r}")


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class RouteTests(unittest.TestCase):
    """The HTTP surface: the page, the numbers, the diagnostic."""

    def setUp(self):
        from fastapi.testclient import TestClient

        stats.reset()
        offline(self)
        self.archive = Archive("data/all")
        self._extra = [mock.patch.object(webhook, "ARCHIVE", self.archive),
                       mock.patch.object(webhook, "bootstrap_webhook", _nothing)]
        for patch in self._extra:
            patch.start()
        self.client = TestClient(webhook.app)

    def tearDown(self):
        self.client.close()
        for patch in self._extra:
            patch.stop()
        stop_offline(self)
        stats.reset()

    def _init(self, user_id, **kwargs):
        return wa.forge_init_data({"id": user_id, "first_name": "Kim"}, TOKEN, **kwargs)

    def test_the_page_is_served(self):
        answer = self.client.get("/app")
        self.assertEqual(answer.status_code, 200)
        self.assertIn("text/html", answer.headers["content-type"])
        self.assertIn("/api/app", answer.text)
        self.assertEqual(answer.headers.get("cache-control"), "no-store")

    def test_the_owner_gets_the_numbers(self):
        answer = self.client.post("/api/app", json={"initData": self._init(OWNER)})
        body = answer.json()
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["owner"])
        self.assertEqual(body["owner_id"], OWNER)
        self.assertIn("app_opened", body["counters"])
        self.assertEqual(body["archive"]["questions"], 30853)
        self.assertEqual(answer.headers.get("cache-control"), "no-store")

    def test_a_granted_user_gets_the_numbers_but_is_not_the_owner(self):
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        body = self.client.post("/api/app", json={"initData": self._init(STRANGER)}).json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["owner"])
        self.assertEqual(body["user"]["id"], STRANGER)

    def test_a_stranger_is_refused_and_told_their_id(self):
        body = self.client.post("/api/app", json={"initData": self._init(STRANGER)}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["sizning_id"], STRANGER)
        self.assertIn("/grant", body["yechim"])
        self.assertNotIn("counters", body)

    def test_the_refusal_is_counted(self):
        self.client.post("/api/app", json={"initData": self._init(STRANGER)})
        self.assertIn("app_denied", [item["k"] for item in stats._pending["e"]])

    def test_unsigned_and_broken_requests_are_refused(self):
        good = self._init(OWNER)
        for payload in ({}, {"initData": ""}, {"initData": "user=1&hash=x"},
                        {"initData": good[:-1] + ("0" if good[-1] != "0" else "1")}):
            body = self.client.post("/api/app", json=payload).json()
            self.assertFalse(body["ok"], payload)
            self.assertIn("imzosi", body["reason"])

    def test_the_proof_can_also_arrive_in_the_address(self):
        body = self.client.get("/api/app?initData=" + quote(self._init(OWNER), safe="")).json()
        self.assertTrue(body["ok"])

    def test_the_local_preview_opens_the_panel_at_the_root(self):
        with mock.patch.object(wa, "demo_enabled", lambda: True), \
             mock.patch.object(wa, "demo_init_data", lambda: self._init(OWNER)):
            answer = self.client.get("/")
        self.assertEqual(answer.status_code, 200)
        self.assertIn("/api/app", answer.text)
        self.assertIn("Ruxsatlar", answer.text)

    def test_production_keeps_the_landing_page_at_the_root(self):
        with mock.patch.dict(os.environ, {"VERCEL_ENV": "production", "ZAKOVAT_APP_DEMO": "1"}):
            self.assertFalse(wa.demo_enabled())      # Vercel refuses the demo flag
        self.assertFalse(wa.demo_enabled())
        answer = self.client.get("/")
        self.assertEqual(answer.status_code, 200)
        self.assertNotIn("Ruxsatlar", answer.text)
        self.assertIn("Zakovat", answer.text)

    def test_the_diagnostic_proves_the_gate_and_leaves_no_trace(self):
        before = asyncio.run(stats.snapshot(self.archive))
        answer = self.client.get("/api/index?appprobe=1&cb=7")
        body = answer.json()
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(body["ok"], body)
        self.assertTrue(all(value is True for value in body["tekshiruvlar"].values()), body)
        self.assertTrue(body["panel"]["ok"])
        self.assertGreaterEqual(body["panel"]["bo‘limlar"], 10)
        self.assertEqual(body["hisoblagichlar"], {})       # the probe counted nothing
        after = asyncio.run(stats.snapshot(self.archive))
        self.assertEqual(before["counters"], after["counters"])


async def _nothing():
    return None


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class MenuButtonTests(unittest.TestCase):
    """The button only exists in chats the owner allowed."""

    def setUp(self):
        stats.reset()
        offline(self)
        self._base = mock.patch.object(webhook, "public_base_url", lambda: BASE)
        self._base.start()

    def tearDown(self):
        self._base.stop()
        stop_offline(self)
        stats.reset()

    def test_the_address_is_absolute(self):
        self.assertEqual(webhook.app_menu_url(), BASE + "/app")

    def test_a_grant_points_the_chat_menu_at_the_app(self):
        bot = MenuBot()
        asyncio.run(webhook.apply_menu_button(bot, STRANGER, True))
        chat_id, button = bot.menus[-1]
        self.assertEqual(chat_id, STRANGER)
        self.assertEqual(button.text, webhook.APP_MENU_TEXT)
        self.assertEqual(button.web_app.url, BASE + "/app")

    def test_a_revoke_puts_the_menu_back(self):
        bot = MenuBot()
        asyncio.run(webhook.apply_menu_button(bot, STRANGER, False))
        self.assertEqual(bot.menus[-1][1].type, "default")

    def test_a_chat_telegram_does_not_know_is_reported_not_raised(self):
        verdict = asyncio.run(webhook.apply_menu_button(MenuBot(fail=True), STRANGER, True))
        self.assertIn("xato", verdict)

    def test_without_a_public_address_nothing_is_pretended(self):
        with mock.patch.object(webhook, "public_base_url", lambda: ""):
            self.assertEqual(webhook.app_menu_url(), "")
            verdict = asyncio.run(webhook.apply_menu_button(MenuBot(), STRANGER, True))
        self.assertIn("manzil yo‘q", verdict)

    def test_the_whole_list_can_be_reapplied_with_plain_http(self):
        import httpx

        state = _telegram_state()
        client = httpx.AsyncClient(transport=_telegram_transport(state), timeout=5.0)
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        report = asyncio.run(webhook.apply_menu_buttons(client=client,
                                                        ids=[OWNER, STRANGER]))
        self.assertEqual(report["manzil"], BASE + "/app")
        self.assertEqual(report[str(OWNER)], "ok")
        self.assertEqual(report[str(STRANGER)], "ok")
        self.assertEqual(report["jami"], 2)
        self.assertEqual(state["menus"][STRANGER]["web_app"]["url"], BASE + "/app")
        self.assertEqual(state["menus"][OWNER]["type"], "web_app")


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class AccessCommandTests(unittest.TestCase):
    """/grant, /revoke and /access belong to the owner alone."""

    def setUp(self):
        stats.reset()
        offline(self)

    def tearDown(self):
        stop_offline(self)
        stats.reset()

    def _run(self, callback, args, user_id=OWNER, bot=None, text="/command"):
        message = FakeMessage(text)
        asyncio.run(callback(FakeUpdate(message, user_id),
                             make_context(bot=bot, args=args)))
        return message.replies[-1], message.reply_records[-1]

    def test_a_stranger_cannot_grant(self):
        text, _ = self._run(webhook.cmd_grant, [str(777)], user_id=STRANGER)
        self.assertIn("KIRISH YO‘Q", text)
        self.assertFalse(stats.allowed(777))

    def test_a_stranger_cannot_read_the_list(self):
        text, _ = self._run(webhook.cmd_access, [], user_id=STRANGER)
        self.assertIn("KIRISH YO‘Q", text)
        self.assertNotIn("MINI APP RUXSATLARI", text)

    def test_the_refusal_hands_over_the_askers_id(self):
        text, record = self._run(webhook.cmd_access, [], user_id=STRANGER)
        self.assertIn(f"<code>{STRANGER}</code>", text)
        self.assertEqual(record.get("parse_mode"), "HTML")

    def test_the_owner_grants_and_the_menu_follows(self):
        bot = MenuBot()
        with mock.patch.object(webhook, "public_base_url", lambda: BASE):
            text, _ = self._run(webhook.cmd_grant, [str(STRANGER), "Aziz"], bot=bot)
        self.assertIn("RUXSAT BERILDI", text)
        self.assertIn("yozildi", text)
        self.assertIn("o‘rnatildi", text)
        self.assertTrue(stats.allowed(STRANGER))
        self.assertEqual(bot.menus[-1][0], STRANGER)
        self.assertIn("grant", [item["k"] for item in stats._pending["e"]])

    def test_a_name_is_asked_from_telegram_when_missing(self):
        bot = MenuBot()
        with mock.patch.object(webhook, "public_base_url", lambda: BASE):
            self._run(webhook.cmd_grant, [str(STRANGER)], bot=bot)
        self.assertEqual(stats.access_list()[str(STRANGER)]["name"], "Aziz Karimov")
        self.assertEqual(bot.chats, [STRANGER])

    def test_the_owner_revokes(self):
        bot = MenuBot()
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        with mock.patch.object(webhook, "public_base_url", lambda: BASE):
            text, _ = self._run(webhook.cmd_revoke, [str(STRANGER)], bot=bot)
        self.assertIn("RUXSAT BEKOR QILINDI", text)
        self.assertIn("Aziz", text)
        self.assertFalse(stats.allowed(STRANGER))
        self.assertEqual(bot.menus[-1][1].type, "default")
        self.assertIn("revoke", [item["k"] for item in stats._pending["e"]])

    def test_revoking_somebody_absent_is_harmless(self):
        text, _ = self._run(webhook.cmd_revoke, [str(STRANGER)])
        self.assertIn("RUXSAT BEKOR QILINDI", text)
        self.assertFalse(stats.allowed(STRANGER))

    def test_an_argument_that_is_not_an_id_gets_the_instructions(self):
        text, _ = self._run(webhook.cmd_grant, ["@aziz"])
        self.assertIn("MINI APP RUXSATLARI", text)
        self.assertIn("/grant 123456789", text)
        self.assertEqual(stats.access_list(), {})
        text, _ = self._run(webhook.cmd_revoke, [])
        self.assertIn("/revoke 123456789", text)

    def test_the_owner_cannot_lock_themselves_out(self):
        text, _ = self._run(webhook.cmd_revoke, [str(OWNER)])
        self.assertIn("bo‘lmaydi", text)
        self.assertTrue(stats.allowed(OWNER))
        text, _ = self._run(webhook.cmd_grant, [str(OWNER)])
        self.assertIn("egasisiz", text)

    def test_the_list_command_shows_who_is_in(self):
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        with mock.patch.object(webhook, "public_base_url", lambda: BASE):
            text, _ = self._run(webhook.cmd_access, [])
        self.assertIn("MINI APP RUXSATLARI", text)
        self.assertIn(str(STRANGER), text)
        self.assertIn("Aziz", text)
        self.assertIn(BASE + "/app", text)

    def test_the_owner_commands_are_listed_for_the_owner_only(self):
        owner_names = [name for name, _ in webhook.OWNER_COMMANDS]
        public_names = [name for name, _ in webhook.BOT_COMMANDS]
        for name in ("stats", "access", "grant", "revoke"):
            self.assertIn(name, owner_names, name)
            self.assertNotIn(name, public_names, name)

    def test_the_commands_are_wired_into_the_application(self):
        from telegram import Bot

        application = webhook.build_app(bot=Bot(TOKEN))
        names = set()
        for handler in application.handlers[0]:
            names.update(getattr(handler, "commands", ()) or ())
        for name in ("grant", "revoke", "access", "stats", "start"):
            self.assertIn(name, names, name)
        for name in ("grant", "revoke", "access"):
            self.assertIn(name, webhook.COMMANDS)


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class CopyTests(unittest.TestCase):
    """The panel's keyboard and the access copy."""

    def test_the_panel_offers_the_app_when_there_is_an_address(self):
        keyboard = st.stats_keyboard("main", BASE + "/app")
        first = keyboard.inline_keyboard[0][0]
        self.assertEqual(first.text, "Mini App — to‘liq panel")
        self.assertEqual(first.web_app.url, BASE + "/app")
        self.assertEqual([button.text for button in keyboard.inline_keyboard[1]],
                         ["Yangilash", "Bo‘limlar", "O‘yinlar"])

    def test_the_panel_stays_plain_without_an_address(self):
        keyboard = st.stats_keyboard("main", "")
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        self.assertIsNone(keyboard.inline_keyboard[0][0].web_app)
        self.assertEqual([button.text for button in keyboard.inline_keyboard[0]],
                         ["Yangilash", "Bo‘limlar", "O‘yinlar"])

    def test_the_statistics_panel_carries_the_button(self):
        text = msg.stats_panel({"counters": {name: 0 for name in stats.COUNTERS},
                                "users": 0, "chats": 0, "packages_played": 0,
                                "top_packages": [], "events": [], "accuracy": 0, "asked": 0,
                                "instance": {"counters": {}, "users": 0, "chats": 0,
                                             "packages": 0, "uptime": 0},
                                "storage": "", "archive": {}})
        self.assertIn("STATISTIKA", text)

    def test_the_access_copy_is_emoji_free_and_actionable(self):
        texts = [msg.access_usage(),
                 msg.access_list([{"id": 5, "name": "Aziz", "since": int(time.time())}],
                                 BASE + "/app"),
                 msg.access_list([]),
                 msg.access_changed(5, "Aziz", True, True, "o‘rnatildi"),
                 msg.access_changed(5, "Aziz", False, False, "oddiy holatga qaytdi"),
                 msg.stats_denied(5)]
        for text in texts:
            self.assertTrue(text)
            for chunk in text:
                if 0x1F300 <= ord(chunk) <= 0x1FAFF or 0x2600 <= ord(chunk) <= 0x27BF:
                    self.fail(f"emoji found: {chunk!r}")
        self.assertIn("/grant", msg.access_usage())
        self.assertIn("hech kimga", msg.access_list([]))
        self.assertIn("<code>5</code>", msg.stats_denied(5))


if __name__ == "__main__":
    unittest.main()
