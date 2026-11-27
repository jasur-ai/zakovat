import unittest

from bot import settings as st
from bot.archive import Archive
from bot.quiz import build_playlist


class GameConfigTests(unittest.TestCase):
    def test_defaults_are_filled(self):
        config = st.GameConfig(package_id=959)
        self.assertGreaterEqual(config.rounds, 1)
        self.assertGreaterEqual(config.per_round, 1)
        self.assertGreaterEqual(config.timer, 10)
        self.assertEqual(config.total, config.rounds * config.per_round)

    def test_callback_data_roundtrip(self):
        config = st.GameConfig(package_id=959, mode="group", rounds=5, per_round=15, timer=90, seed=4242)
        encoded = config.encode(st.ACTION_START)
        self.assertLessEqual(len(encoded), 64)
        parsed = st.decode(encoded)
        self.assertIsNotNone(parsed)
        restored, action, value = parsed
        self.assertEqual(restored.package_id, 959)
        self.assertEqual(restored.mode, "group")
        self.assertEqual((restored.rounds, restored.per_round, restored.timer, restored.seed), (5, 15, 90, 4242))
        self.assertEqual(action, st.ACTION_START)

    def test_action_and_value_roundtrip(self):
        config = st.GameConfig(package_id=12, rounds=2, per_round=10, timer=60, seed=1)
        parsed = st.decode(config.encode(st.ACTION_PER_ROUND, "25"))
        self.assertEqual(parsed[1], st.ACTION_PER_ROUND)
        self.assertEqual(parsed[2], "25")

    def test_foreign_data_is_ignored(self):
        self.assertIsNone(st.decode("zp|3|"))
        self.assertIsNone(st.decode("garbage"))
        self.assertIsNone(st.decode(None))

    def test_page_data_roundtrip(self):
        self.assertEqual(st.decode_page(st.page_data(7, "zakova")), (7, "zakova"))
        self.assertIsNone(st.decode_page("zg|s|959|3|10|60|1"))


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.questions = [{"id": i, "text": f"q{i}"} for i in range(1, 26)]

    def test_rounds_multiply_questions(self):
        playlist = build_playlist(self.questions, rounds=3, per_round=10, seed=5)
        self.assertEqual(len(playlist), 30)

    def test_same_seed_is_reproducible(self):
        first = [q["id"] for q in build_playlist(self.questions, 3, 10, 99)]
        second = [q["id"] for q in build_playlist(self.questions, 3, 10, 99)]
        self.assertEqual(first, second)

    def test_different_seed_changes_order(self):
        first = [q["id"] for q in build_playlist(self.questions, 1, 10, 1)]
        second = [q["id"] for q in build_playlist(self.questions, 1, 10, 2)]
        self.assertNotEqual(first, second)

    def test_zero_per_round_means_whole_package(self):
        playlist = build_playlist(self.questions, rounds=1, per_round=0, seed=3)
        self.assertEqual(len(playlist), 25)
        self.assertEqual(sorted(q["id"] for q in playlist), list(range(1, 26)))

    def test_repeat_wraps_without_crash(self):
        playlist = build_playlist(self.questions[:3], rounds=4, per_round=5, seed=1)
        self.assertEqual(len(playlist), 20)


class PackageListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = Archive("data/all")

    def test_first_page_lists_every_newest_package(self):
        result = self.archive.list_packages("", page=1, per_page=12)
        self.assertEqual(len(result["items"]), 12)
        self.assertEqual(result["count"], self.archive.counts()[0])
        self.assertGreater(result["pages"], 50)
        ids = [item["id"] for item in result["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_last_page_is_reachable(self):
        result = self.archive.list_packages("", page=10_000, per_page=12)
        self.assertEqual(result["page"], result["pages"])
        self.assertTrue(result["items"])

    def test_search_narrows_the_list(self):
        result = self.archive.list_packages("zakovat", page=1, per_page=12)
        self.assertTrue(result["items"])
        for item in result["items"]:
            self.assertIn("zakovat", (item.get("title") or "").casefold())

    def test_package_info_needs_no_shard_loading(self):
        info = self.archive.package_info(959)
        self.assertIsNotNone(info)
        self.assertGreater(info["questions"], 0)


if __name__ == "__main__":
    unittest.main()
