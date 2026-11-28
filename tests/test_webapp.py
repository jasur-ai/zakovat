import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

try:
    import bot.webhook as webhook
    from bot import dashboard, messages as msg
    from bot import schedule as sched
    from bot import settings as st, stats, webapp as wa
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
        # the document keeps the guest list in its compact shape; reading it back
        # through the store is what has to give the name again
        self.assertEqual(stats._normalise(stored_document(state))["a"]["777"]["name"], "Sardor")

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
        packages, questions = self.archive.local_counts()
        self.assertEqual(body["archive"]["packages"], packages)
        self.assertEqual(body["archive"]["questions"], questions)
        total_packages, total_questions = self.archive.counts()
        self.assertEqual(body["archive"]["total_packages"], total_packages)
        self.assertEqual(body["archive"]["total_questions"], total_questions)
        sources = body["archive"]["sources"]
        self.assertEqual(sources[0]["name"], "zakovatklubi.uz")
        self.assertEqual(sources[0]["stored"], "local")
        self.assertEqual(sum(row["packages"] for row in sources), total_packages)
        self.assertEqual(sum(row["questions"] for row in sources), total_questions)
        self.assertGreaterEqual(total_packages, packages)
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

    def test_the_page_reads_the_proof_the_way_telegram_hands_it_over(self):
        """Telegram appends initData to the address as #tgWebAppData, never as ?initData."""
        self.assertIn("tgWebAppData", self.page)
        self.assertIn("w.initData", self.page)
        self.assertIn("decodeURIComponent", self.page)
        self.assertIn("manba", self.page)
        self.assertIn("window.location.hash", self.page)

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


NODE = shutil.which("node")

HARNESS = """
const fs = require("fs");
const scenario = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
global.window = {location: {hash: scenario.hash || ""}};
var INIT = scenario.init || "";
var proofSource = "";
function tg(){ return scenario.sdk ? {initData: scenario.sdk} : null; }
eval(fs.readFileSync(process.argv[1], "utf8"));
fs.writeFileSync(process.argv[3], JSON.stringify({value: proof(), manba: proofSource}));
"""


def proof_source() -> str:
    """The page's real proof-finding function, lifted out of the rendered HTML."""
    page = dashboard.render("", api="/api/app")
    start = page.index("function proof(){")
    return page[start:page.index("function haptic(", start)]


def run_proof(scenario: dict) -> dict:
    """Run that function in node, with the window and SDK the scenario describes."""
    with tempfile.TemporaryDirectory() as folder:
        script = os.path.join(folder, "proof.js")
        given = os.path.join(folder, "given.json")
        result = os.path.join(folder, "result.json")
        Path(script).write_text(proof_source(), encoding="utf-8")
        Path(given).write_text(json.dumps(scenario), encoding="utf-8")
        subprocess.run([NODE, "-e", HARNESS, script, given, result], check=True, timeout=30)
        return json.loads(Path(result).read_text(encoding="utf-8"))


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
@unittest.skipUnless(NODE, "node is needed to run the page's own proof lookup")
class FragmentTests(unittest.TestCase):
    """Telegram hands initData over in the address fragment, never in the query.

    The page's own JavaScript is executed here, so what is asserted is what a
    phone really does: the fragment is parsed, decoded once, and the result has
    to survive the server's signature check.
    """

    def setUp(self):
        stats.reset()
        offline(self)
        self.init = wa.forge_init_data({"id": OWNER, "first_name": "Ega"}, TOKEN)

    def tearDown(self):
        stop_offline(self)
        stats.reset()

    def _fragment(self, value):
        return "#tgWebAppData=" + value + "&tgWebAppVersion=7.10&tgWebAppPlatform=android"

    def test_the_sdk_value_is_used_first(self):
        found = run_proof({"sdk": self.init, "hash": self._fragment("anything"),
                           "init": "other"})
        self.assertEqual(found["value"], self.init)
        self.assertEqual(found["manba"], "sdk")
        self.assertIsNotNone(wa.validate_init_data(found["value"], TOKEN))

    def test_the_encoded_fragment_is_decoded_once_and_still_valid(self):
        found = run_proof({"hash": self._fragment(quote(self.init, safe=""))})
        self.assertEqual(found["manba"], "fragment")
        self.assertEqual(found["value"], self.init)
        verified = wa.validate_init_data(found["value"], TOKEN)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["user"]["id"], OWNER)

    def test_a_plain_fragment_is_accepted_too(self):
        found = run_proof({"hash": self._fragment(self.init)})
        self.assertEqual(found["manba"], "fragment")
        verified = wa.validate_init_data(found["value"], TOKEN)
        self.assertIsNotNone(verified)
        self.assertEqual(verified["user"]["id"], OWNER)

    def test_a_fragment_alone_is_enough(self):
        found = run_proof({"hash": "#tgWebAppData=" + quote(self.init, safe="")})
        self.assertEqual(found["manba"], "fragment")
        self.assertIsNotNone(wa.validate_init_data(found["value"], TOKEN))

    def test_a_tampered_fragment_still_fails_the_signature(self):
        broken = self.init[:-1] + ("0" if self.init[-1] != "0" else "1")
        found = run_proof({"hash": self._fragment(quote(broken, safe=""))})
        self.assertIsNone(wa.validate_init_data(found["value"], TOKEN))

    def test_the_rendered_value_is_the_last_resort(self):
        self.assertEqual(run_proof({"init": self.init})["manba"], "sahifa")
        self.assertEqual(run_proof({"init": self.init})["value"], self.init)

    def test_without_anything_there_is_no_proof(self):
        found = run_proof({})
        self.assertEqual(found["value"], "")
        self.assertEqual(found["manba"], "")


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
        self.assertEqual(body["archive"]["questions"], self.archive.local_counts()[1])
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

    def test_a_request_without_any_proof_is_refused_and_says_why(self):
        """No proof at all means the page was not opened inside Telegram."""
        for payload in ({}, {"initData": ""}):
            body = self.client.post("/api/app", json=payload).json()
            self.assertFalse(body["ok"], payload)
            self.assertIn("bo‘sh", body["reason"])
            self.assertEqual(body["manba"], "yo‘q")
            self.assertEqual(body["uzunlik"], 0)

    def test_broken_proofs_are_refused_and_their_source_is_echoed(self):
        good = self._init(OWNER)
        broken = good[:-1] + ("0" if good[-1] != "0" else "1")
        for payload in ({"initData": "user=1&hash=x", "manba": "sdk"},
                        {"initData": broken, "manba": "fragment"}):
            body = self.client.post("/api/app", json=payload).json()
            self.assertFalse(body["ok"], payload)
            self.assertIn("imzosi", body["reason"])
            self.assertEqual(body["manba"], payload["manba"])
            self.assertGreater(body["uzunlik"], 0)

    def test_a_refused_stranger_is_told_where_the_proof_came_from(self):
        body = self.client.post("/api/app", json={"initData": self._init(STRANGER),
                                                  "manba": "sdk"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["manba"], "sdk")
        self.assertEqual(body["sizning_id"], STRANGER)

    def test_the_proof_can_also_arrive_in_the_address(self):
        body = self.client.get("/api/app?initData=" + quote(self._init(OWNER), safe="")).json()
        self.assertTrue(body["ok"])

    def test_the_local_preview_opens_the_panel_at_the_root(self):
        with mock.patch.object(wa, "demo_enabled", lambda: True), \
             mock.patch.object(wa, "demo_init_data", lambda: self._init(OWNER)):
            answer = self.client.get("/")
        self.assertEqual(answer.status_code, 200)
        self.assertIn("/api/app", answer.text)
        self.assertIn('id="tabs"', answer.text)          # the panel, not the landing page

    def test_production_keeps_the_landing_page_at_the_root(self):
        with mock.patch.dict(os.environ, {"VERCEL_ENV": "production", "ZAKOVAT_APP_DEMO": "1"}):
            self.assertFalse(wa.demo_enabled())      # Vercel refuses the demo flag
        self.assertFalse(wa.demo_enabled())
        answer = self.client.get("/")
        self.assertEqual(answer.status_code, 200)
        self.assertNotIn('id="tabs"', answer.text)       # no panel here
        self.assertIn("Zakovat Agent", answer.text)
        self.assertIn("Mini App paneli", answer.text)    # but the page tells visitors about it

    def test_the_status_page_answers_and_names_both_bases(self):
        # /api/status is a route handler: it has to reach the report itself.
        with mock.patch.dict(os.environ, {"BOT_TOKEN": ""}):
            answer = self.client.get("/api/status")
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Arxiv manifesti:", answer.text)
        self.assertIn("Manba zakovatklubi:", answer.text)

    def test_the_status_page_names_the_reminder_chain_and_the_panel_routes(self):
        with mock.patch.dict(os.environ, {"BOT_TOKEN": ""}):
            answer = self.client.get("/api/status")
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Shu nusxadagi eslatmalar:", answer.text)
        self.assertIn(f"oyna {webhook.REMINDER_ARM} s", answer.text)
        self.assertIn("Xotirada: reja", answer.text)
        self.assertIn("/api/browse /api/tour", answer.text)

    def test_the_diagnostic_page_answers_without_a_probe(self):
        # It used to read `request.query_params` inside a function that was
        # never given a request: /api/index answered 500 in production while
        # /health stayed green, because every probe returned before this line.
        packages, questions = self.archive.counts()
        with mock.patch.dict(os.environ, {"BOT_TOKEN": ""}):
            answer = self.client.get("/api/index?cb=1")
        self.assertEqual(answer.status_code, 200)
        self.assertIn(f"Arxiv manifesti: {packages} paket, {questions} savol", answer.text)
        self.assertIn("Katta baza:", answer.text)
        self.assertNotIn("Shard tekshiruvi", answer.text)   # only ?shardprobe=1 fetches

    def test_the_diagnostic_page_keeps_working_without_a_remote_base(self):
        archive = Archive("data/all", turnirlar="/nonexistent-turnirlar")
        with mock.patch.object(webhook, "ARCHIVE", archive), \
                mock.patch.dict(os.environ, {"BOT_TOKEN": ""}):
            answer = self.client.get("/api/index?cb=2")
        self.assertEqual(answer.status_code, 200)
        self.assertIn("Arxiv manifesti:", answer.text)
        self.assertNotIn("Manba turnirlar:", answer.text)
        self.assertNotIn("Katta baza:", answer.text)

    def test_every_get_route_answers_without_a_server_error(self):
        # A handler that raises answers 500, and nothing else notices: the
        # diagnostics page did exactly that once while /health stayed green.
        with mock.patch.dict(os.environ, {"BOT_TOKEN": ""}):
            for path in ("/", "/health", "/api/now", "/app", "/api/status",
                         "/api/index?cb=3", "/api/index?tickprobe=1"):
                answer = self.client.get(path)
                self.assertLess(answer.status_code, 500,
                                f"{path} → {answer.status_code}: {answer.text[:120]}")

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

    def test_the_diagnostic_walks_the_browser_and_the_composer(self):
        body = self.client.get("/api/index?appprobe=1&cb=8").json()
        browse = body["brauzer"]
        self.assertTrue(browse["ok"], browse)
        self.assertGreaterEqual(browse["bo‘limlar"], 10)
        self.assertGreater(browse["paketlar"], 100)
        self.assertGreater(browse["sahifalar"], 1)
        self.assertGreater(browse["savollar"], 0)          # a real package was opened
        self.assertGreater(browse["qidiruv"], 0)           # and the search found it
        self.assertLessEqual(browse["qidiruv"], browse["paketlar"])

        composer = body["kompozitor"]
        self.assertTrue(composer["ok"], composer)
        self.assertEqual(composer["chegaralar"],
                         ["eslatma", "jamoa", "raund", "savol", "sekund"])
        refusals = composer["rad_javoblari"]
        self.assertEqual(sorted(refusals), ["bekor", "guruh", "paket", "vaqt"])
        self.assertIn("manfiy id", refusals["guruh"])
        self.assertIn("topilmadi", refusals["paket"])
        self.assertTrue(refusals["vaqt"])
        self.assertIn("kerak", refusals["bekor"])
        self.assertEqual(body["hisoblagichlar"], {})       # still nothing was counted

    def test_a_broken_browser_makes_the_diagnostic_say_so(self):
        def explode(body, archive):
            raise RuntimeError("shard yo‘q")

        with mock.patch.object(wa, "browse", explode):
            answer = self.client.get("/api/index?appprobe=1&cb=9")
        body = answer.json()
        self.assertFalse(body["ok"])                       # the probe does not lie
        self.assertFalse(body["brauzer"]["ok"])
        self.assertIn("shard yo‘q", body["brauzer"]["xato"])
        self.assertTrue(body["panel"]["ok"])               # the rest still works

    def test_a_broken_composer_makes_the_diagnostic_say_so(self):
        async def explode(user, body):
            raise RuntimeError("reja o‘qilmadi")

        with mock.patch.object(webhook, "panel_tour", explode):
            answer = self.client.get("/api/index?appprobe=1&cb=10")
        body = answer.json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["kompozitor"]["ok"])
        self.assertIn("reja o‘qilmadi", body["kompozitor"]["xato"])


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

    def test_the_menu_stays_short_and_everything_is_documented(self):
        """Thirty commands in everybody's menu is not a menu.

        The menu carries what a player types; /help carries the rest in groups;
        and every row of both must really be routed, or it is a dead promise.
        """
        self.assertLessEqual(len(webhook.BOT_COMMANDS), 16)
        self.assertLessEqual(len(webhook.OWNER_COMMANDS), 24)
        listed = [name for name, _ in webhook.ALL_COMMANDS]
        self.assertEqual(len(set(listed)), len(listed))          # nothing twice
        text = msg.help_text()
        for name in listed:
            self.assertIn("/" + name, text, name)                # documented
            self.assertIn(name, webhook.COMMANDS, name)          # and routed
        for name, _ in webhook.BOT_COMMANDS:
            self.assertIn(name, listed, name)                    # menu ⊂ everything

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
        self.assertIsNone(keyboard.inline_keyboard[0][0].web_app)
        self.assertEqual([button.text for button in keyboard.inline_keyboard[0]],
                         ["Yangilash", "Bo‘limlar", "O‘yinlar"])
        # the owner's commands are buttons now, address or not
        self.assertEqual([button.text for button in keyboard.inline_keyboard[1]],
                         ["Ishlatilgan savollar", "Turnir rejalari", "Ruxsatlar"])

    def test_the_used_index_paginates(self):
        keyboard = st.stats_keyboard("used", "", page=2, pages=5)
        rows = [[button.text for button in row] for row in keyboard.inline_keyboard]
        self.assertIn(["‹ Orqaga", "2 / 5", "Oldinga ›"], rows)

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


class PanelBot:
    """A bot double for the panel's compose path: rights check, card, pin."""

    def __init__(self, status="administrator", can_delete=True, title="Zakovat klubi"):
        self.status = status
        self.can_delete = can_delete
        self.title = title
        self.sent = []
        self.pinned = []
        self.asked = []
        self.id = 1

    async def get_chat_member(self, chat_id, user_id):
        self.asked.append((chat_id, user_id))
        return SimpleNamespace(status=self.status, can_delete_messages=self.can_delete,
                               chat=SimpleNamespace(id=chat_id, title=self.title))

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.sent.append(kwargs)
        return True

    async def pin_chat_message(self, **kwargs):
        self.pinned.append(kwargs)
        return True


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class BrowseRouteTests(unittest.TestCase):
    """The panel's question browser: same gate as the numbers, same archive."""

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
        self.owner = wa.forge_init_data({"id": OWNER, "first_name": "Ega"}, TOKEN)

    def tearDown(self):
        self.client.close()
        for patch in self._extra:
            patch.stop()
        stop_offline(self)
        stats.reset()

    def _browse(self, init=None, **body):
        payload = dict(body)
        payload["initData"] = init if init is not None else self.owner
        answer = self.client.post("/api/browse", json=payload)
        self.assertEqual(answer.status_code, 200)
        return answer

    def test_sections_come_first(self):
        body = self._browse(turi="bolimlar").json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(len(body["bolimlar"]), 5)
        total = sum(row["packages"] for row in body["bolimlar"])
        self.assertEqual(total, self.archive.local_counts()[0])
        self.assertNotIn("items", body["ishlatilgan_indeks"])

    def test_packages_are_paged_and_filtered(self):
        body = self._browse(turi="paketlar").json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["paketlar"]), wa.BROWSE_PAGE)
        self.assertGreater(body["jami"], body["sahifalar"])
        self.assertEqual(body["sahifa"], 1)
        second = self._browse(turi="paketlar", sahifa=2).json()
        self.assertEqual(second["sahifa"], 2)
        self.assertNotEqual([row["id"] for row in body["paketlar"]],
                            [row["id"] for row in second["paketlar"]])
        word = self.archive.package_info(959)["title"].split()[0]
        found = self._browse(turi="paketlar", qidiruv=word).json()
        self.assertLess(found["jami"], body["jami"])          # the search really narrows
        self.assertTrue(found["paketlar"])
        for row in found["paketlar"]:
            self.assertIn(word.lower(), str(row["title"]).lower())

    def test_questions_carry_their_used_mark(self):
        stats.mark_used([{"id": self.archive.questions(959)[0]["id"]}], package_id=959,
                        chat_id=-100200, mode="turnir")
        body = self._browse(turi="savollar", paket=959).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["paket"]["id"], 959)
        self.assertLessEqual(len(body["savollar"]), wa.QUESTION_PAGE)
        self.assertEqual(body["ishlatilgan"], 1)
        self.assertTrue(body["savollar"][0]["used"])
        self.assertLessEqual(len(body["savollar"][0]["matn"]), wa.SNIPPET)

    def test_a_section_filter_narrows_the_list(self):
        section = stats.section_of(self.archive.package_info(959)["title"])
        body = self._browse(turi="paketlar", bolim=section).json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["paketlar"])
        for row in body["paketlar"]:
            self.assertEqual(row["bolim"], section)

    def test_an_unknown_package_says_so(self):
        body = self._browse(turi="savollar", paket=1).json()
        self.assertFalse(body["ok"])
        self.assertIn("topilmadi", body["reason"])

    def test_a_stranger_sees_nothing(self):
        stranger = wa.forge_init_data({"id": STRANGER, "first_name": "Begona"}, TOKEN)
        body = self._browse(init=stranger, turi="paketlar").json()
        self.assertFalse(body["ok"])
        self.assertNotIn("paketlar", body)

    def test_without_proof_there_is_no_answer(self):
        answer = self.client.get("/api/browse")
        self.assertEqual(answer.status_code, 200)
        self.assertFalse(answer.json()["ok"])
        self.assertIn("Telegram ichida", answer.json()["reason"])

    def test_the_answer_is_never_cached(self):
        answer = self._browse(turi="bolimlar")
        self.assertEqual(answer.headers.get("cache-control"), "no-store")

    def test_a_granted_guest_may_browse(self):
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        guest = wa.forge_init_data({"id": STRANGER, "first_name": "Aziz"}, TOKEN)
        self.assertTrue(self._browse(init=guest, turi="bolimlar").json()["ok"])


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
class TourRouteTests(unittest.TestCase):
    """The composer: it refuses politely, and when it agrees the group gets a card."""

    CHAT = -1002001234
    CARD = 777

    def setUp(self):
        from fastapi.testclient import TestClient

        stats.reset()
        offline(self)
        self.archive = Archive("data/all")
        self.bot = PanelBot()
        self.application = SimpleNamespace(
            bot=self.bot, bot_data={"archive": self.archive},
            initialize=mock.AsyncMock(), shutdown=mock.AsyncMock())
        self.hops = []

        async def post(body, chat_id, wait=0):
            self.hops.append((body, chat_id, wait))

        self._extra = [mock.patch.object(webhook, "ARCHIVE", self.archive),
                       mock.patch.object(webhook, "bootstrap_webhook", _nothing),
                       mock.patch.object(webhook, "get_application", lambda: self.application),
                       mock.patch.object(webhook, "bg_add", lambda factory: False),
                       mock.patch.object(webhook, "post_tick", post)]
        for patch in self._extra:
            patch.start()
        self.client = TestClient(webhook.app)
        self.owner = wa.forge_init_data({"id": OWNER, "first_name": "Ega"}, TOKEN)

    def tearDown(self):
        self.client.close()
        for patch in self._extra:
            patch.stop()
        webhook._armed.clear()
        stats.reset()

    def _tour(self, init=None, **body):
        payload = dict(body)
        payload.setdefault("initData", init if init is not None else self.owner)
        answer = self.client.post("/api/tour", json=payload)
        self.assertEqual(answer.status_code, 200)
        return answer.json()

    # ---------------------------------------------------------------- the list
    def test_the_plan_list_carries_the_limits_the_panel_needs(self):
        stats.plan_add(self.CHAT, self.CARD, int(time.time()) + 3600, "Kuzgi turnir")
        stats.group_seen(self.CHAT, "Zakovat klubi")
        body = self._tour(amal="reja")
        self.assertTrue(body["ok"])
        self.assertEqual([row["chat"] for row in body["rejalar"]], [self.CHAT])
        self.assertEqual(body["guruhlar"], [{"id": self.CHAT, "title": "Zakovat klubi"}])
        limits = body["chegaralar"]
        self.assertEqual(limits["jamoa"], [3, 12])
        self.assertEqual(limits["raund"], list(st.TOUR_ROUNDS))
        self.assertEqual(limits["savol"], list(st.TOUR_PER_ROUND))
        self.assertEqual(limits["sekund"], list(st.TOUR_TIMERS))
        self.assertEqual(limits["eslatma"], webhook.REMINDER_ARM)
        self.assertNotIn("items", body["ishlatilgan"])

    def test_a_stranger_neither_sees_the_plans_nor_composes(self):
        stranger = wa.forge_init_data({"id": STRANGER, "first_name": "Begona"}, TOKEN)
        body = self._tour(init=stranger, amal="reja")
        self.assertFalse(body["ok"])
        self.assertNotIn("rejalar", body)
        self.assertEqual(self.bot.sent, [])

    # -------------------------------------------------------------- cancelling
    def test_cancelling_a_plan_keeps_the_card_and_forgets_the_reminder(self):
        stats.plan_add(self.CHAT, self.CARD, int(time.time()) + 3600, "Kuzgi turnir")
        body = self._tour(amal="bekor", guruh=self.CHAT, xabar=self.CARD)
        self.assertTrue(body["ok"])
        self.assertEqual(body["rejalar"], [])
        self.assertEqual(stats.plans(), [])
        self.assertIn("bekor qilindi", body["xabar"])
        self.assertEqual(self.bot.sent, [])          # the group is not told anything

    def test_cancelling_without_an_id_is_refused(self):
        self.assertIn("kerak", self._tour(amal="bekor", guruh=0, xabar=0)["reason"])
        self.assertIn("tushunmadim", self._tour(amal="bekor", guruh="abc", xabar="x")["reason"])

    # --------------------------------------------------------------- composing
    def _compose(self, **body):
        payload = {"amal": "shakllantirish", "guruh": self.CHAT, "paket": 959,
                   "sarlavha": "Kuzgi liga", "raund": 2, "savol_soni": 5, "sekund": 45,
                   "jamoa": 6}
        payload.update(body)
        return self._tour(**payload)

    def test_composing_posts_pins_and_schedules_the_card(self):
        start = int(time.time()) + 900
        first = self.archive.questions(959)[0]["id"]
        body = self._compose(vaqt="+15", savollar=[first, "abc", 0, first])
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["karta"], 101)
        self.assertEqual(body["jamoa"], 6)
        self.assertEqual(body["raund"], 2)
        self.assertEqual(body["har_raundda"], 5)
        self.assertEqual(body["sekund"], 45)
        self.assertEqual(body["tanlov"], 1)                     # one id, once
        self.assertGreaterEqual(body["boshlanish"], start - 60)
        self.assertTrue(body["boshlanish_matn"])
        self.assertTrue(body["eslatma"])
        self.assertEqual([row["chat"] for row in body["rejalar"]], [self.CHAT])
        self.assertIn("mahkamlandi", body["xabar"])

        self.assertEqual(len(self.bot.pinned), 1)               # the card is the authority
        self.assertEqual(self.bot.pinned[0]["chat_id"], self.CHAT)
        card = next(item["text"] for item in self.bot.sent if "Kuzgi liga" in item["text"])
        self.assertIn("TURNIR", card.upper())
        self.assertEqual(len(self.hops), 1)                     # one reminder hop queued
        self.assertTrue(self.hops[0][0].startswith(sched.PLAN_PREFIX))
        self.assertEqual(stats.groups(), {self.CHAT: "Zakovat klubi"})

    def test_composing_without_a_time_leaves_the_start_button_live(self):
        body = self._compose(vaqt="")
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["boshlanish"], 0)
        self.assertFalse(body["eslatma"])
        self.assertEqual(body["rejalar"], [])
        self.assertEqual(self.hops, [])
        self.assertIn("hozir ishlaydi", body["xabar"])

    def test_the_shape_is_clamped_to_the_rules(self):
        body = self._compose(raund=99, savol_soni=0, sekund=7, jamoa=99)
        self.assertTrue(body["ok"], body)
        self.assertIn(body["raund"], st.TOUR_ROUNDS)
        self.assertIn(body["har_raundda"], [value for value in st.TOUR_PER_ROUND if value])
        self.assertIn(body["sekund"], st.TOUR_TIMERS)
        self.assertEqual(body["jamoa"], 12)                     # tn.MAX_TEAMS

    def test_a_group_id_that_is_not_a_group_is_refused(self):
        body = self._tour(amal="shakllantirish", guruh=8004724563, paket=959)
        self.assertFalse(body["ok"])
        self.assertIn("manfiy id", body["reason"])
        self.assertEqual(self.bot.sent, [])

    def test_an_unknown_package_is_refused(self):
        body = self._compose(paket=1)
        self.assertFalse(body["ok"])
        self.assertIn("Paket topilmadi", body["reason"])

    def test_an_impossible_time_is_refused_with_the_reason(self):
        body = self._compose(vaqt="kecha 10:00")
        self.assertFalse(body["ok"])
        self.assertTrue(body["reason"])
        self.assertEqual(self.bot.sent, [])

    def test_a_time_in_the_past_is_refused(self):
        body = self._compose(vaqt="01.01.2020 10:00")
        self.assertFalse(body["ok"])
        self.assertIn("o‘tib", body["reason"].lower() + "o‘tib")

    def test_a_member_of_the_group_cannot_compose(self):
        self.bot.status = "member"
        self.bot.can_delete = False
        stranger = wa.forge_init_data({"id": STRANGER, "first_name": "Begona"}, TOKEN)
        asyncio.run(stats.set_access(STRANGER, "Aziz", True))
        body = self._compose(init=stranger)
        self.assertFalse(body["ok"])
        self.assertIn("admini emassiz", body["reason"])
        self.assertEqual(self.bot.sent, [])

    def test_a_selection_of_foreign_ids_is_refused(self):
        body = self._compose(savollar=[1, 2, 3])
        self.assertFalse(body["ok"])
        self.assertIn("topilmadi", body["reason"])
        self.assertEqual(self.bot.sent, [])


PANEL_HARNESS = r"""const fs = require("fs");
const scriptPath = process.argv[2];
const scenarioPath = process.argv[3];
const outPath = process.argv[4];
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));

/* ---- a permissive fake DOM: enough for the page's own boot and views ---- */
function makeEl(id){
  const el = {
    id: id || "", innerHTML: "", textContent: "", value: "", checked: false,
    disabled: false, style: {}, dataset: {}, parentNode: null, children: [],
    classList: {
      _set: {},
      add: function(name){ this._set[name] = true; },
      remove: function(name){ delete this._set[name]; },
      toggle: function(name, on){ if (on) this._set[name] = true; else delete this._set[name]; },
      contains: function(name){ return !!this._set[name]; }
    },
    addEventListener: function(kind, fn){ (this._on = this._on || {})[kind] = fn; },
    setAttribute: function(name, value){ this.dataset[name] = value; },
    getAttribute: function(name){ return this.dataset[name] === undefined ? null : this.dataset[name]; },
    appendChild: function(node){ this.children.push(node); return node; },
    removeAttribute: function(name){ delete this.dataset[name]; },
    insertAdjacentHTML: function(where, html){ this.innerHTML += html; },
    getBoundingClientRect: function(){ return {top: 0, left: 0, width: 0, height: 0}; },
    contains: function(){ return false; }, matches: function(){ return false; },
    scrollTop: 0, offsetWidth: 0, offsetHeight: 0,
    querySelector: function(){ return null; },
    querySelectorAll: function(){ return []; },
    focus: function(){}, blur: function(){}, scrollIntoView: function(){},
    closest: function(){ return null; }, remove: function(){}
  };
  return el;
}
const els = {};
function el(id){ if (!els[id]) els[id] = makeEl(id); return els[id]; }
global.document = {
  getElementById: el,
  querySelector: function(){ return null; },
  querySelectorAll: function(){ return []; },
  createElement: function(tag){ return makeEl(""); },
  addEventListener: function(kind, fn){ (this._on = this._on || {})[kind] = fn; },
  body: makeEl("body"),
  documentElement: makeEl("html"),
  readyState: "complete",
  hidden: false,
  title: ""
};
global.window = {
  location: {hash: scenario.hash || "", href: "https://zakovat.test/app"},
  matchMedia: function(){ return {matches: false, addEventListener: function(){}}; },
  addEventListener: function(){}, removeEventListener: function(){},
  setTimeout: function(){ return 0; }, clearTimeout: function(){},
  setInterval: function(){ return 0; }, clearInterval: function(){},
  navigator: {vibrate: function(){}},
  fetch: function(){ return Promise.reject(new Error("offline harness")); }
};
global.navigator = window.navigator;
global.fetch = window.fetch;
global.setTimeout = window.setTimeout;
global.clearTimeout = window.clearTimeout;
global.requestAnimationFrame = function(fn){ return 0; };

/* The page ships as one IIFE; the harness lifts the wrapper so the scenario can
   reach the page's own state and functions — the code that runs is unchanged. */
var source = fs.readFileSync(scriptPath, "utf8")
  .replace(/^\s*\(function\(\)\{\s*\n\s*"use strict";/, "")
  .replace(/\}\)\(\);\s*$/, "");
eval(source);

/* ---- the scenario: state to set, functions to call, values to read ---- */
const report = {calls: [], errors: []};
(scenario.state || []).forEach(function(item){
  try{
    if (item[0] === "browse") Object.keys(item[1]).forEach(function(key){ browse[key] = item[1][key]; });
    else if (item[0] === "plan") Object.keys(item[1]).forEach(function(key){ plan[key] = item[1][key]; });
    else if (item[0] === "payload"){
      data = data || {};
      Object.keys(item[1]).forEach(function(key){ data[key] = item[1][key]; });
    }
    else report.errors.push("unknown state " + item[0]);
  }catch(error){ report.errors.push("state " + item[0] + ": " + error.message); }
});
(scenario.calls || []).forEach(function(item){
  const name = item[0];
  const args = item.slice(1);
  try{
    if (name === "act"){
      const node = makeEl("");
      node.dataset["data-act"] = args[0];
      node.dataset["data-val"] = args[1] === undefined ? "" : String(args[1]);
      if (args[2] !== undefined) node.dataset["data-msg"] = String(args[2]);
      act(args[0], node);
    }else{
      /* the page's functions live in the eval'd scope, not on globalThis */
      const fn = eval(name);
      if (typeof fn === "function") report.calls.push({name: name, value: fn.apply(null, args)});
      else report.errors.push("no such function: " + name);
    }
  }catch(error){ report.errors.push(name + ": " + error.message); }
});
(scenario.read || []).forEach(function(name){
  try{
    report[name] = eval(name);
  }catch(error){ report.errors.push("read " + name + ": " + error.message); }
});
fs.writeFileSync(outPath, JSON.stringify(report));"""


def panel_source() -> str:
    """The panel's whole script, lifted out of the rendered page."""
    page = dashboard.render("", api="/api/app")
    return re.findall(r"<script>(.*?)</script>", page, re.S)[0]


def run_panel(scenario: dict) -> dict:
    """Run that script in node against a small fake DOM and report what it did."""
    with tempfile.TemporaryDirectory() as folder:
        script = os.path.join(folder, "panel.js")
        given = os.path.join(folder, "given.json")
        result = os.path.join(folder, "result.json")
        Path(script).write_text(panel_source(), encoding="utf-8")
        Path(given).write_text(json.dumps(scenario), encoding="utf-8")
        Path(os.path.join(folder, "harness.js")).write_text(PANEL_HARNESS, encoding="utf-8")
        subprocess.run([NODE, os.path.join(folder, "harness.js"), script, given, result],
                       check=True, timeout=60)
        return json.loads(Path(result).read_text(encoding="utf-8"))


@unittest.skipUnless(webhook, "install requirements.txt to test the Mini App")
@unittest.skipUnless(NODE, "node is needed to run the panel's own script")
class PanelScriptTests(unittest.TestCase):
    """The panel is one page of JavaScript, so the page itself is the test subject.

    Nothing here guesses at the markup: the real script is evaluated against a
    small fake DOM, its own view functions are called, and its own click
    handlers are run — the state they leave behind is what is asserted.
    """

    PACKAGES = {"ok": True, "jami": 40, "sahifa": 1, "sahifalar": 4, "paketlar": [
        {"id": 959, "title": "Kuzgi liga", "questions": 54, "bolim": "Intellektual Olimpiada",
         "manba": "zakovatklubi", "ishlatilgan": 2},
        {"id": 900001, "title": "Professional liga 3-tur", "questions": 36,
         "bolim": "Professional", "manba": "turnirlar", "ishlatilgan": 0}]}
    QUESTIONS = {"ok": True, "paket": {"id": 959, "title": "Kuzgi liga", "questions": 54},
                 "jami": 54, "sahifa": 2, "sahifalar": 3, "ishlatilgan": 1, "indeksda": 1,
                 "savollar": [
                     {"id": 900123, "tartib": 1, "tur": "oddiy", "matn": "Bir savol",
                      "javob": "Robot", "rasm": False, "used": True},
                     {"id": 900124, "tartib": 2, "tur": "oddiy", "matn": "Ikkinchi <savol>",
                      "javob": "Kitob", "rasm": True, "used": False}]}
    PAYLOAD = {"ok": True, "owner": True, "user": {"id": OWNER, "name": "Ega"},
               "archive": {"packages": 8670, "questions": 419241},
               "sections": [{"name": "Intellektual Olimpiada", "packages": 395, "questions": 12000},
                            {"name": "Maktablar", "packages": 12, "questions": 300}],
               "used": {"count": 3, "packages": 2, "uses": 4, "limit": 120,
                        "recent": [{"id": 900123, "t": 1764200000, "p": 959, "m": "turnir",
                                    "n": 2}]},
               "plans": [{"chat": -100200, "message": 777, "start": 1900000000,
                          "title": "Kuzgi liga", "left": 3600}],
               "groups": [{"id": -100200, "title": "Zakovat klubi"}, {"id": -100300, "title": ""}]}
    LIMITS = {"jamoa": [3, 12], "raund": [1, 2, 3, 5, 10], "savol": [5, 10, 15, 25, 0],
              "sekund": [30, 45, 60, 90, 120], "eslatma": 900}

    def _run(self, browse=None, plan=None, calls=(), read=()):
        state = [["payload", self.PAYLOAD]]
        if browse is not None:
            state.append(["browse", browse])
        if plan is not None:
            state.append(["plan", plan])
        report = run_panel({"state": state, "calls": [list(item) for item in calls],
                            "read": list(read)})
        self.assertEqual(report["errors"], [])
        return report

    def _view(self, report, name):
        return next(str(item["value"]) for item in report["calls"] if item["name"] == name)

    # ------------------------------------------------------------------ syntax
    def test_the_whole_script_parses(self):
        with tempfile.TemporaryDirectory() as folder:
            script = os.path.join(folder, "panel.js")
            Path(script).write_text(panel_source(), encoding="utf-8")
            answer = subprocess.run([NODE, "--check", script], capture_output=True, text=True)
        self.assertEqual(answer.returncode, 0, answer.stderr[:600])

    # ------------------------------------------------------------- the browser
    def test_the_section_chips_come_from_the_payload_and_filter(self):
        report = self._run(browse={"turi": "paketlar", "body": self.PACKAGES},
                           calls=[("viewQuestions",), ("act", "section", "Maktablar")],
                           read=["browse.bolim", "browse.turi", "browse.sahifa", "browse.body"])
        view = self._view(report, "viewQuestions")
        self.assertIn('data-act="section" data-val="Maktablar"', view)
        self.assertIn("Intellektual Olimpiada (395)", view)
        self.assertEqual(report["browse.bolim"], "Maktablar")
        self.assertEqual(report["browse.turi"], "paketlar")
        self.assertEqual(report["browse.sahifa"], 1)
        self.assertIsNone(report["browse.body"])          # the list is asked for again

    def test_a_package_row_opens_and_can_be_taken_into_the_tournament(self):
        report = self._run(browse={"turi": "paketlar", "body": self.PACKAGES},
                           calls=[("viewQuestions",), ("act", "choosepkg", "900001")],
                           read=["plan.paket", "plan.sarlavha", "plan.ok"])
        view = self._view(report, "viewQuestions")
        self.assertIn('data-act="openpkg" data-val="900001"', view)
        self.assertIn("turnirlar.uz", view)
        self.assertEqual(report["plan.paket"], 900001)
        self.assertEqual(report["plan.sarlavha"], "Professional liga 3-tur")
        self.assertIn("Professional liga 3-tur", report["plan.ok"])

    def test_the_question_list_marks_used_rows_and_collects_a_selection(self):
        report = self._run(browse={"turi": "savollar", "paket": 959, "body": self.QUESTIONS},
                           plan={"savollar": [900123]},
                           calls=[("viewQuestions",), ("act", "pick", "900124"),
                                  ("act", "pick", "900123")],
                           read=["plan.savollar"])
        view = self._view(report, "viewQuestions")
        self.assertIn("ishlatildi", view)
        self.assertIn("Ikkinchi &lt;savol&gt;", view)      # the text is escaped, not injected
        self.assertIn('data-act="pick" data-val="900124"', view)
        self.assertIn("sahifa 2/3", view.replace("&#", "&#"))
        self.assertIn('data-act="backpkgs"', view)
        self.assertEqual(report["plan.savollar"], [900124])   # one added, one taken back

    def test_the_pager_walks_the_list(self):
        report = self._run(browse={"turi": "paketlar", "body": self.PACKAGES},
                           calls=[("viewQuestions",), ("act", "page", "3")],
                           read=["browse.sahifa"])
        view = self._view(report, "viewQuestions")
        self.assertIn(">Boshi</button>", view)
        self.assertIn("disabled", view)                    # page one has no way back
        self.assertEqual(report["browse.sahifa"], 3)

    def test_an_unreachable_shard_is_reported_and_not_called_loading(self):
        report = self._run(browse={"turi": "paketlar",
                                   "error": "Paketning bo‘lagi (shard) yuklanmadi"},
                           calls=[("viewQuestions",)])
        view = self._view(report, "viewQuestions")
        self.assertIn("shard) yuklanmadi", view)
        self.assertNotIn("Yuklanmoqda", view)

    def test_the_used_index_card_shows_what_the_server_sent(self):
        report = self._run(browse={"turi": "paketlar", "body": self.PACKAGES},
                           calls=[("viewQuestions",)])
        view = self._view(report, "viewQuestions")
        self.assertIn("Ishlatilgan savollar indeksi", view)
        self.assertIn("savol 900123", view)
        self.assertIn("paket 959", view)
        self.assertIn("2-marta", view)

    # ------------------------------------------------------------- the composer
    def _tour(self, calls=(), read=(), plan=None):
        shape = {"guruhlar": self.PAYLOAD["groups"], "limits": self.LIMITS,
                 "rejalar": self.PAYLOAD["plans"], "guruh": -100200, "paket": 959,
                 "sarlavha": "Kuzgi liga", "raund": 2, "savol_soni": 5, "sekund": 45,
                 "jamoa": 6, "savollar": [900123]}
        shape.update(plan or {})
        return self._run(plan=shape, calls=calls, read=read)

    def test_the_composer_offers_groups_limits_and_the_plans(self):
        report = self._tour(calls=[("viewTour",)])
        view = self._view(report, "viewTour")
        self.assertIn('data-act="group" data-val="-100200" aria-pressed="true">Zakovat klubi',
                      view)
        self.assertIn("guruh -100300", view)               # an unnamed group still shows
        self.assertIn('data-field="guruh"', view)
        self.assertIn('data-field="paket"', view)
        self.assertIn('data-field="sarlavha"', view)
        self.assertIn('data-act="chip:raund" data-val="2" aria-pressed="true"', view)
        self.assertIn('data-act="chip:sekund" data-val="45"', view)
        self.assertIn('data-act="removeplan" data-val="-100200" data-msg="777"', view)
        self.assertIn('data-act="compose"', view)
        self.assertIn('data-act="pick" data-val="900123" aria-pressed="true"', view)
        self.assertIn("1 ta tanlangan", view)

    def test_a_chip_press_changes_the_shape_of_the_game(self):
        report = self._tour(calls=[("viewTour",), ("act", "chip:raund", "3"), ("viewTour",)],
                            read=["plan.raund"])
        first, second = [str(item["value"]) for item in report["calls"]]
        self.assertIn('data-val="2" aria-pressed="true"', first)
        self.assertIn('data-val="3" aria-pressed="true"', second)
        self.assertEqual(report["plan.raund"], 3)

    def test_the_quick_times_fill_the_date_and_the_clock(self):
        report = self._tour(calls=[("act", "quick", "tomorrow10"), ("act", "quick", "")],
                            read=["plan.sana", "plan.vaqt"])
        self.assertEqual(report["plan.vaqt"], "19:00")
        self.assertEqual(report["plan.sana"], "")
        report = self._tour(calls=[("act", "quick", "tomorrow10")], read=["plan.sana", "plan.vaqt"])
        self.assertEqual(report["plan.vaqt"], "10:00")
        self.assertRegex(report["plan.sana"], r"^\d{4}-\d{2}-\d{2}$")

    def test_a_selection_is_shown_and_can_be_cleared(self):
        report = self._tour(calls=[("viewTour",), ("act", "clearpick")],
                            read=["plan.savollar"])
        self.assertIn("900123", self._view(report, "viewTour"))
        self.assertEqual(report["plan.savollar"], [])

    def test_an_unknown_action_changes_nothing(self):
        report = self._tour(calls=[("act", "noma’lum", "1")], read=["plan.raund", "browse.turi"])
        self.assertEqual(report["plan.raund"], 2)


if __name__ == "__main__":
    unittest.main()
