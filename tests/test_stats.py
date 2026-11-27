import asyncio
import httpx
import json
import unittest
from unittest import mock

try:
    from bot import messages as msg
    from bot import stats
    from bot.archive import Archive
except ImportError:  # dependencies not installed yet
    stats = None


STORE: dict = {}


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeClient:
    """Stands in for httpx.AsyncClient so the store can be tested offline."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append(("GET", url))
        if not STORE:
            return FakeResponse(200, "")
        return FakeResponse(200, json.dumps(STORE, ensure_ascii=False))

    async def put(self, url, content=None, headers=None):
        self.calls.append(("PUT", url))
        STORE.clear()
        STORE.update(json.loads(content))
        return FakeResponse(200, '{"ok":true}')


@unittest.skipUnless(stats, "install requirements.txt to test statistics")
class ArchiveSectionTests(unittest.TestCase):
    """Package titles are grouped into sections without any storage."""

    def test_known_series_are_recognised(self):
        self.assertEqual(stats.section_of("Professional va Birinchi liga kuzgi mavsumi 10-tur"),
                         "Professional va Birinchi liga")
        self.assertEqual(stats.section_of("Ikkinchi liga 4-tur"), "Ikkinchi liga")
        self.assertEqual(stats.section_of("Talabalar ligasi 8-tur"), "Talabalar ligasi")
        self.assertEqual(stats.section_of("Ayollar ligasi 2-tur"), "Ayollar ligasi")
        self.assertEqual(stats.section_of("Maktablardagi Zakovat klubi"), "Maktablar")
        self.assertEqual(stats.section_of("Intellektual Olimpiada doirasidagi “Zakovat” o‘yini"),
                         "Intellektual Olimpiada")
        self.assertEqual(stats.section_of("Toshkent shahri 3-tur"), "Hududiy turlar")

    def test_unknown_titles_fall_into_one_section(self):
        self.assertEqual(stats.section_of("Noma’lum turnir"), stats.OTHER_SECTION)

    def test_archive_stats_cover_the_whole_export(self):
        archive = Archive("data/all")
        info = stats.archive_stats(archive)
        self.assertEqual(info["packages"], 1266)
        self.assertEqual(info["questions"], 30853)
        self.assertGreaterEqual(info["section_count"], 5)
        self.assertGreater(info["pictures"], 0)
        self.assertGreater(info["biggest"]["questions"], 0)
        total = sum(entry["packages"] for entry in info["sections"])
        self.assertEqual(total, info["packages"])        # nothing is counted twice


@unittest.skipUnless(stats, "install requirements.txt to test statistics")
class CounterTests(unittest.TestCase):
    def setUp(self):
        stats.reset()
        STORE.clear()
        self._url, self._dry = stats.STORE_URL, stats.DRY_RUN
        stats.STORE_URL = ""
        stats.DRY_RUN = False

    def tearDown(self):
        stats.STORE_URL, stats.DRY_RUN = self._url, self._dry
        stats.reset()

    def test_events_are_counted_and_logged(self):
        due = stats.record("game_start", user_id=7, chat_id=-100, package_id=959,
                           games_started=1, detail="solo · 959")
        self.assertFalse(due)                            # no store: nothing to flush
        stats.record("question", chat_id=-100, package_id=959, questions_asked=1, log=False)
        data = asyncio.run(stats.snapshot())
        self.assertEqual(data["counters"]["games_started"], 1)
        self.assertEqual(data["counters"]["questions_asked"], 1)
        self.assertEqual(data["users"], 1)
        self.assertEqual(data["chats"], 1)
        self.assertEqual(data["packages_played"], 1)
        self.assertEqual([event["k"] for event in data["events"]], ["game_start"])

    def test_accuracy_is_derived(self):
        stats.record("correct", answers_correct=3, log=False)
        stats.record("wrong", answers_wrong=1, log=False)
        stats.record("unanswered", unanswered=1, log=False)
        data = asyncio.run(stats.snapshot())
        self.assertEqual(data["asked"], 5)
        self.assertEqual(data["accuracy"], 60)

    def test_unknown_counter_names_are_ignored(self):
        stats.record("weird", not_a_counter=5)
        self.assertEqual(asyncio.run(stats.snapshot())["counters"]["games_started"], 0)


@unittest.skipUnless(stats, "install requirements.txt to test statistics")
class StoreTests(unittest.TestCase):
    """With a JSON document store the numbers survive a fresh instance."""

    def setUp(self):
        stats.reset()
        STORE.clear()
        self._url, self._dry = stats.STORE_URL, stats.DRY_RUN
        self._client = stats.httpx.AsyncClient
        stats.httpx.AsyncClient = FakeClient
        stats.STORE_URL = "https://store.example/doc"

    def tearDown(self):
        stats.httpx.AsyncClient = self._client
        stats.STORE_URL, stats.DRY_RUN = self._url, self._dry
        stats.reset()
        STORE.clear()

    def test_deltas_are_written_and_read_back(self):
        stats.record("game_start", user_id=7, chat_id=-100, package_id=959,
                     games_started=1, detail="solo · 959")
        stats.record("correct", user_id=7, chat_id=-100, package_id=959,
                     answers_correct=2, log=False)
        self.assertTrue(asyncio.run(stats.sync(force=True)))
        self.assertEqual(STORE["c"]["games_started"], 1)
        self.assertEqual(STORE["c"]["answers_correct"], 2)
        self.assertIn("7", STORE["u"])

        # a second instance sees the same totals
        stats.reset()
        data = asyncio.run(stats.snapshot())
        self.assertEqual(data["counters"]["games_started"], 1)
        self.assertEqual(data["counters"]["answers_correct"], 2)
        self.assertEqual(data["users"], 1)
        self.assertTrue(data["synced"])

    def test_a_second_flush_adds_to_the_stored_total(self):
        stats.record("game_start", user_id=7, chat_id=-100, games_started=1)
        asyncio.run(stats.sync(force=True))
        stats.record("game_start", user_id=8, chat_id=-100, games_started=1)
        asyncio.run(stats.sync(force=True))
        self.assertEqual(STORE["c"]["games_started"], 2)
        self.assertEqual(len(STORE["u"]), 2)

    def test_a_broken_store_never_raises(self):
        stats.STORE_URL = "https://store.example/doc"

        class Broken(FakeClient):
            async def get(self, url, headers=None):
                raise OSError("tarmoq yo‘q")

        stats.httpx.AsyncClient = Broken
        stats.record("game_start", games_started=1)
        self.assertFalse(asyncio.run(stats.sync(force=True)))
        data = asyncio.run(stats.snapshot())
        self.assertEqual(data["counters"]["games_started"], 1)   # still counted locally

    def test_dry_run_does_not_write(self):
        stats.DRY_RUN = True
        stats.record("game_start", games_started=1)
        self.assertFalse(asyncio.run(stats.sync(force=True)))
        self.assertEqual(STORE, {})


@unittest.skipUnless(stats, "install requirements.txt to test statistics")
class PanelTests(unittest.TestCase):
    def setUp(self):
        stats.reset()
        STORE.clear()
        self._url = stats.STORE_URL
        stats.STORE_URL = ""

    def tearDown(self):
        stats.STORE_URL = self._url
        stats.reset()

    def test_panel_shows_every_section_of_the_dashboard(self):
        archive = Archive("data/all")
        stats.record("game_start", user_id=7, chat_id=-100, package_id=959,
                     games_started=1, detail="solo · 959")
        stats.record("question", questions_asked=4, log=False)
        stats.record("correct", answers_correct=2, log=False)
        stats.record("unanswered", unanswered=2, skips=1)
        stats.record("auto_stop", games_finished=1, games_auto_stopped=1, detail="959 · 2–4")
        stats.record("clock_opened", clock_opened=3, log=False)
        data = asyncio.run(stats.snapshot(archive))
        text = msg.stats_panel(data)
        for heading in ("STATISTIKA", "O‘YINLAR", "SAVOLLAR", "ARXIV", "BO‘LIMLAR",
                        "OXIRGI HARAKATLAR", "SHU NUSXADA"):
            self.assertIn(heading, text)
        self.assertIn("Boshlangan: 1", text)
        self.assertIn("Avto-to‘xtagan: 1", text)
        self.assertIn("Berilgan: 4", text)
        self.assertIn("Aniqlik: 50%", text)
        self.assertIn("Jonli soat oynasi: 3 marta", text)
        self.assertIn("Turkumlar (paketlar): 1266", text)
        self.assertIn("Savollar: 30853", text)
        self.assertIn("avto-to‘xtatildi", text)
        self.assertNotIn("question", text)               # noisy events stay out of the feed
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4090)

    def test_sections_and_events_views(self):
        archive = Archive("data/all")
        stats.record("game_start", detail="solo · 959")
        data = asyncio.run(stats.snapshot(archive))
        self.assertIn("BO‘LIMLAR", msg.stats_sections(data))
        self.assertIn("Talabalar ligasi", msg.stats_sections(data))
        self.assertIn("o‘yin boshlandi", msg.stats_events(data))
        self.assertIn("hozircha yozuv yo‘q", msg.stats_events({"events": []}))

    def test_the_panel_has_no_emoji(self):
        data = asyncio.run(stats.snapshot(Archive("data/all")))
        text = msg.stats_panel(data) + msg.stats_sections(data) + msg.stats_events(data)
        for char in text:
            self.assertFalse(0x1F000 <= ord(char) <= 0x1FAFF or 0x2600 <= ord(char) <= 0x27BF,
                             f"emoji found: {char!r}")


def _fake_store_transport(store: dict, *, fail_first: bool = False):
    """An httpx transport standing in for the free JSON document services."""
    calls = {"jsonblob": 0, "kvdb": 0}

    def handler(request):
        url = str(request.url)
        body = request.content.decode("utf-8")
        if url == "https://jsonblob.com/api/jsonBlob" and request.method == "POST":
            calls["jsonblob"] += 1
            store["https://jsonblob.com/api/jsonBlob/990011"] = body
            return httpx.Response(201, headers={"location": "/api/jsonBlob/990011"}, text="created")
        if request.url.host == "kvdb.io" and request.url.path == "/" and request.method == "POST":
            calls["kvdb"] += 1
            if fail_first:                      # the first service in the list refuses
                return httpx.Response(503, text="<html>blocked</html>")
            store["https://kvdb.io/bucket42/zakovat-stats"] = body
            return httpx.Response(200, text="bucket42")
        if url in store:
            if request.method == "GET":
                return httpx.Response(200, text=store[url])
            store[url] = body
            return httpx.Response(200, text="ok")
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler), calls


@unittest.skipUnless(stats, "install requirements.txt to test statistics")
class StoreProvisioningTests(unittest.TestCase):
    """The bot can create its own storage — nobody has to register anything."""

    def test_creating_a_store_reports_a_working_address(self):
        store: dict = {}
        transport, calls = _fake_store_transport(store)
        report = asyncio.run(stats.create_store(transport=transport))
        self.assertTrue(report["ok"])
        self.assertEqual(report["xizmat"], "kvdb")
        self.assertEqual(report["manzil"], "https://kvdb.io/bucket42/zakovat-stats")
        self.assertEqual(calls["kvdb"], 1)
        self.assertIn("init-", store[report["manzil"]])       # the document was seeded

    def test_a_refusing_service_falls_through_to_the_next(self):
        store: dict = {}
        transport, calls = _fake_store_transport(store, fail_first=True)
        report = asyncio.run(stats.create_store(transport=transport))
        self.assertTrue(report["ok"])
        self.assertEqual(report["xizmat"], "jsonblob")        # kvdb refused, the next one worked
        self.assertEqual(calls["kvdb"], 1)
        first = report["urinishlar"][0]
        self.assertEqual(first["xizmat"], "kvdb")
        self.assertEqual(first["kod"], 503)                   # the reason is visible
        self.assertIn("blocked", first["javob"])

    def test_a_dead_network_is_reported_not_raised(self):
        def handler(request):
            raise httpx.ConnectError("no route")

        report = asyncio.run(stats.create_store(transport=httpx.MockTransport(handler)))
        self.assertFalse(report["ok"])
        self.assertTrue(report["urinishlar"] or report.get("xato"))

    def test_the_check_keeps_what_is_already_stored(self):
        url = "https://jsonblob.com/api/jsonBlob/990011"
        stored = {"v": 1, "s": 1, "c": {"games_started": 7, "questions_asked": 40},
                  "u": {"8004724563": 9}, "h": {}, "p": {"959": 3},
                  "e": [{"t": 5, "k": "game_start", "x": "eski"}]}
        store = {url: json.dumps(stored)}
        transport, _ = _fake_store_transport(store)
        with mock.patch.object(stats, "STORE_URL", url):
            report = asyncio.run(stats.probe(transport=transport))
        self.assertTrue(report["ok"])
        self.assertEqual(report["saqlangan_hisoblar"]["games_started"], 7)
        after = json.loads(store[url])
        self.assertEqual(after["c"]["games_started"], 7)      # history survived the check
        self.assertEqual(after["c"]["questions_asked"], 40)
        self.assertEqual(after["u"]["8004724563"], 9)
        self.assertEqual(after["e"][-1]["x"], "eski")
        self.assertTrue(any(event.get("k") == "probe" for event in after["e"]))


if __name__ == "__main__":
    unittest.main()
