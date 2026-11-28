"""Times and plans: what a curator types, and what the reminder chain carries.

Everything here is pure — no Telegram, no archive, no network. The point is the
two things a scheduled tournament depends on: ``parse_when`` has to accept the
ways people really write a time and refuse everything else with a reason in
Uzbek, and ``parse_spec`` has to keep a one-line command intact, because a word
that leaks from ``sarlavha=`` into the package text silently changes the game.
"""
import json
import time
import unittest
from datetime import datetime, timedelta

try:
    from bot import schedule as sched
except ImportError:  # nothing outside the standard library is needed, but be safe
    sched = None


def now() -> int:
    return int(time.time())


def stamp_of(value: int) -> datetime:
    """The local (Tashkent) wall clock a stamp lands on, as the module sees it."""
    return datetime.utcfromtimestamp(value + sched.TZ_OFFSET * 3600)


@unittest.skipUnless(sched, "bot/schedule.py is missing")
class ParseWhenTests(unittest.TestCase):
    """Every accepted form, and every refusal with its own reason."""

    def setUp(self):
        self.reference = int(datetime(2026, 11, 28, 10, 0).timestamp()
                             - sched.TZ_OFFSET * 3600)

    def parse(self, value: str, reference: int | None = None):
        return sched.parse_when(value, reference=reference or self.reference)

    # ------------------------------------------------------------- accepted
    def test_a_bare_clock_is_today(self):
        stamp, problem = self.parse("19:00")
        self.assertEqual(problem, "")
        self.assertEqual(stamp_of(stamp), datetime(2026, 11, 28, 19, 0))

    def test_a_bare_hour_is_today(self):
        stamp, problem = self.parse("23")
        self.assertEqual(problem, "")
        self.assertEqual(stamp_of(stamp), datetime(2026, 11, 28, 23, 0))

    def test_a_clock_that_already_passed_moves_to_tomorrow(self):
        stamp, problem = self.parse("09:00")          # the reference is 10:00
        self.assertEqual(problem, "")
        self.assertEqual(stamp_of(stamp), datetime(2026, 11, 29, 9, 0))

    def test_bugun_and_ertaga(self):
        self.assertEqual(stamp_of(self.parse("bugun 23:59")[0]),
                         datetime(2026, 11, 28, 23, 59))
        self.assertEqual(stamp_of(self.parse("ertaga 10:30")[0]),
                         datetime(2026, 11, 29, 10, 30))

    def test_ertaga_alone_is_the_morning(self):
        stamp, problem = self.parse("ertaga")
        self.assertEqual(problem, "")
        self.assertGreater(stamp, self.reference)

    def test_minutes_from_now(self):
        stamp, problem = self.parse("+45")
        self.assertEqual(problem, "")
        self.assertAlmostEqual(stamp, self.reference + 45 * 60, delta=2)

    def test_a_dot_date_with_and_without_a_year(self):
        self.assertEqual(stamp_of(self.parse("28.11 19:00")[0]),
                         datetime(2026, 11, 28, 19, 0))
        self.assertEqual(stamp_of(self.parse("28.11.2026 19:15")[0]),
                         datetime(2026, 11, 28, 19, 15))
        self.assertEqual(stamp_of(self.parse("5.12 19:00")[0]),
                         datetime(2026, 12, 5, 19, 0))

    def test_a_colon_clock_written_with_a_dot(self):
        self.assertEqual(stamp_of(self.parse("19.00")[0]), datetime(2026, 11, 28, 19, 0))

    def test_a_month_name_is_understood(self):
        stamp, problem = self.parse("dekabr 19:00")
        self.assertEqual(problem, "")
        self.assertEqual(stamp_of(stamp), datetime(2026, 12, 1, 19, 0))   # the month opens

    def test_a_bare_pair_of_numbers_is_a_clock_not_a_date(self):
        # "5.12" alone reads as 05:12 — and 05:12 has passed, so it means
        # tomorrow. A date says so by carrying a clock or a year of its own.
        stamp, problem = self.parse("5.12")
        self.assertEqual(problem, "")
        self.assertEqual(stamp_of(stamp), datetime(2026, 11, 29, 5, 12))
        self.assertEqual(stamp_of(self.parse("5.12 19:00")[0]),
                         datetime(2026, 12, 5, 19, 0))
        self.assertEqual(stamp_of(self.parse("5.12.2026 19:00")[0]),
                         datetime(2026, 12, 5, 19, 0))

    def test_a_date_without_a_clock_is_that_evening(self):
        stamp, problem = self.parse("dekabr")
        self.assertEqual(problem, "")
        self.assertEqual(stamp_of(stamp), datetime(2026, 12, 1, 19, 0))

    def test_hozir_is_today_at_the_usual_hour(self):
        stamp, problem = self.parse("hozir")
        self.assertEqual(problem, "")
        self.assertEqual(stamp_of(stamp), datetime(2026, 11, 28, 19, 0))

    def test_a_whole_stamp_is_accepted(self):
        stamp, problem = self.parse(str(self.reference + 3600))
        self.assertEqual(problem, "")
        self.assertEqual(stamp, self.reference + 3600)

    def test_minutes_may_carry_their_unit(self):
        self.assertEqual(self.parse("+45min")[0], self.parse("+45")[0])
        self.assertEqual(self.parse("+45 daqiqa")[0], self.parse("+45")[0])

    def test_two_clocks_or_two_dates_are_refused(self):
        for value in ("12:00 13:00", "28.11 5.12 19:00"):
            stamp, problem = self.parse(value)
            self.assertEqual(stamp, 0, value)
            self.assertIn("Ikkita", problem)

    def test_the_panel_datetime_local_value(self):
        # <input type="datetime-local"> sends exactly this, and the browser's own
        # value is lowercased on the way through the panel's state.
        self.assertEqual(stamp_of(self.parse("2026-12-01T19:30")[0]),
                         datetime(2026, 12, 1, 19, 30))
        self.assertEqual(stamp_of(self.parse("2026-12-01t19:30")[0]),
                         datetime(2026, 12, 1, 19, 30))
        self.assertEqual(stamp_of(self.parse("2026-12-01 19:30")[0]),
                         datetime(2026, 12, 1, 19, 30))

    def test_extra_spaces_change_nothing(self):
        self.assertEqual(self.parse("  ertaga   10:30 ")[0], self.parse("ertaga 10:30")[0])

    # ------------------------------------------------------------- refused
    def test_an_hour_that_does_not_exist(self):
        for value in ("25:00", "19:75", "24:00"):
            stamp, problem = self.parse(value)
            self.assertEqual(stamp, 0, value)
            self.assertTrue(problem, value)

    def test_a_date_that_does_not_exist(self):
        for value in ("31.02 10:00", "2026-02-30T19:30", "30.02 10:00"):
            stamp, problem = self.parse(value)
            self.assertEqual(stamp, 0, value)
            self.assertTrue(problem, value)

    def test_a_word_that_is_not_a_time(self):
        for value in ("kecha 10:00", "ertaga kechqurun", "olma", "+olma"):
            stamp, problem = self.parse(value)
            self.assertEqual(stamp, 0, value)
            self.assertTrue(problem, value)

    def test_the_past_is_refused(self):
        stamp, problem = self.parse("01.01.2020 10:00")
        self.assertEqual(stamp, 0)
        self.assertIn("o‘tgan", problem.lower() + "o‘tgan")

    def test_more_than_a_month_ahead_is_refused(self):
        stamp, problem = self.parse("15.01.2027 10:00")
        self.assertEqual(stamp, 0)
        self.assertIn("bir oydan", problem)

    def test_an_empty_line_asks_for_a_time(self):
        stamp, problem = self.parse("")
        self.assertEqual(stamp, 0)
        self.assertIn("19:00", problem)
        self.assertEqual(self.parse("   ")[0], 0)

    def test_the_reasons_are_uzbek_and_short(self):
        for value in ("kecha 10:00", "25:00", "31.02 10:00"):
            problem = self.parse(value)[1]
            self.assertLessEqual(len(problem), 200)
            self.assertNotIn("<", problem)


@unittest.skipUnless(sched, "bot/schedule.py is missing")
class ParseSpecTests(unittest.TestCase):
    """One line has to carry the package, the time and the shape of the game."""

    def test_a_lone_package_number_survives(self):
        spec = sched.parse_spec("959")
        self.assertEqual(spec["error"], "")
        self.assertEqual(spec["text"], "959")
        self.assertEqual(spec["when"], "")

    def test_the_documented_full_line(self):
        # This exact line is printed by /turnir's help and quoted in the README.
        spec = sched.parse_spec(
            "959 vaqt=19:00 jamoa=6 tur=3 savol=10 sekund=60 sarlavha=Kuzgi liga")
        self.assertEqual(spec["error"], "")
        self.assertEqual(spec["text"], "959")               # nothing leaked in
        self.assertEqual(spec["when"], "19:00")
        self.assertEqual(spec["teams"], 6)
        self.assertEqual(spec["rounds"], 3)
        self.assertEqual(spec["per_round"], 10)
        self.assertEqual(spec["timer"], 60)
        self.assertEqual(spec["title"], "Kuzgi liga")        # both words
        self.assertGreater(spec["start_at"], int(time.time()))

    def test_a_time_of_several_words_is_one_time(self):
        spec = sched.parse_spec("959 vaqt=ertaga 19:00")
        self.assertEqual(spec["error"], "")
        self.assertEqual(spec["when"], "ertaga 19:00")
        self.assertEqual(spec["text"], "959")
        self.assertEqual(stamp_of(spec["start_at"]).hour, 19)

    def test_the_fields_may_come_in_any_order(self):
        first = sched.parse_spec("sarlavha=Kuzgi liga 959 vaqt=+45 jamoa=4")
        self.assertEqual(first["error"], "")
        self.assertEqual(first["title"], "Kuzgi liga")       # stops at the number
        self.assertEqual(first["text"], "959")
        self.assertEqual(first["teams"], 4)

    def test_a_title_never_swallows_the_package_number(self):
        spec = sched.parse_spec("sarlavha=Bilimdonlar ligasi 1356")
        self.assertEqual(spec["title"], "Bilimdonlar ligasi")
        self.assertEqual(spec["text"], "1356")

    def test_paket_is_the_same_as_the_bare_number(self):
        spec = sched.parse_spec("paket=1356 sarlavha=Liga")
        self.assertEqual(spec["package"], "1356")
        self.assertEqual(spec["title"], "Liga")
        self.assertEqual(spec["text"], "")

    def test_a_search_phrase_stays_whole(self):
        spec = sched.parse_spec("talabalar ligasi")
        self.assertEqual(spec["text"], "talabalar ligasi")
        self.assertEqual(spec["when"], "")

    def test_vaqt_without_an_equals_sign(self):
        spec = sched.parse_spec("talabalar ligasi vaqt ertaga 18:00")
        self.assertEqual(spec["when"], "ertaga 18:00")
        self.assertEqual(spec["text"], "talabalar ligasi")

    def test_the_aliases_all_land_on_the_same_field(self):
        for word in ("jamoa", "komanda", "teams"):
            self.assertEqual(sched.parse_spec(f"959 {word}=5")["teams"], 5)
        for word in ("tur", "raund", "rounds"):
            self.assertEqual(sched.parse_spec(f"959 {word}=3")["rounds"], 3)
        for word in ("sekund", "vaqtson", "timer"):
            self.assertEqual(sched.parse_spec(f"959 {word}=45")["timer"], 45)
        for word in ("vaqt", "qachon", "soat", "boshlanish"):
            self.assertTrue(sched.parse_when(sched.parse_spec(f"959 {word}=19:00")["when"])[0])

    def test_a_number_that_is_not_a_number_is_refused(self):
        spec = sched.parse_spec("959 jamoa=oltitа")
        self.assertIn("son bo‘lishi kerak", spec["error"])

    def test_an_impossible_time_is_refused_with_its_reason(self):
        spec = sched.parse_spec("959 vaqt=kecha 10:00")
        self.assertTrue(spec["error"])
        self.assertNotIn("start_at", spec)

    def test_an_unknown_key_is_left_in_the_search_text(self):
        spec = sched.parse_spec("959 nomalum=1")
        self.assertEqual(spec["error"], "")
        self.assertIn("nomalum=1", spec["text"])

    def test_nothing_at_all(self):
        spec = sched.parse_spec("")
        self.assertEqual(spec["error"], "")
        self.assertEqual(spec["text"], "")
        self.assertEqual(spec["when"], "")

    def test_a_long_title_is_cut_to_the_card_limit(self):
        spec = sched.parse_spec("sarlavha=" + "uzun " * 30)
        self.assertLessEqual(len(spec["title"]), sched.TITLE_LIMIT)


@unittest.skipUnless(sched, "bot/schedule.py is missing")
class PlanCodecTests(unittest.TestCase):
    """The reminder hop rides inside a message, so it has to be small and exact."""

    def test_a_plan_round_trips(self):
        start = now() + 3600
        body = sched.plan_body(-1002001234, 777, start, "Kuzgi turnir")
        self.assertTrue(body.startswith(sched.PLAN_PREFIX))
        data = sched.plan_data(body)
        self.assertEqual(data["chat"], -1002001234)
        self.assertEqual(data["message"], 777)
        self.assertEqual(data["start"], start)
        self.assertEqual(data["title"], "Kuzgi turnir")

    def test_a_long_title_is_cut(self):
        data = sched.plan_data(sched.plan_body(-100200, 1, now() + 60, "u" * 90))
        self.assertLessEqual(len(data["title"]), 40)

    def test_something_that_is_not_a_plan_is_not_one(self):
        self.assertEqual(sched.plan_data(""), {})
        self.assertEqual(sched.plan_data("T|{\"c\":1}"), {})
        self.assertEqual(sched.plan_data(sched.PLAN_PREFIX + "not json"), {})

    def test_a_tournament_strip_is_not_a_plan(self):
        self.assertEqual(sched.plan_data("T|{\"c\":-100200}"), {})
        self.assertNotEqual(sched.PLAN_PREFIX, "T|")

    def test_hops_get_shorter_as_the_start_comes_near(self):
        self.assertEqual(sched.hop_step(now() + 3000), sched.HOP)   # the serverless cap
        self.assertLessEqual(sched.hop_step(now() + 12), 12)
        self.assertGreaterEqual(sched.hop_step(now() + 12), 1)
        self.assertGreaterEqual(sched.hop_step(now() - 5), 1)       # announce at once

    def test_due_only_inside_the_window(self):
        start = now() + 600
        self.assertTrue(sched.due(start, window=900))
        self.assertFalse(sched.due(start, window=300))          # not yet time to hop
        self.assertFalse(sched.due(0, window=900))              # no plan at all

    def test_a_start_that_just_passed_is_still_due(self):
        # Every instance may have been cold at the exact minute: a reminder ten
        # minutes late still beats a group that never hears anything.
        self.assertTrue(sched.due(now() - 60, window=900))
        self.assertTrue(sched.due(now(), window=900))
        self.assertFalse(sched.too_late(now() - 60))

    def test_a_start_long_passed_is_not_news_any_more(self):
        stale = now() - sched.LATE_GRACE - 60
        self.assertFalse(sched.due(stale, window=900))
        self.assertTrue(sched.too_late(stale))
        self.assertFalse(sched.too_late(0))                     # no plan, nothing stale

    def test_left_text_reads_like_a_clock(self):
        self.assertEqual(sched.left_text(now() + 90), "1 daqiqa")
        self.assertEqual(sched.left_text(now() + 3600 + 900), "1 soat 15 daqiqa")
        self.assertEqual(sched.left_text(now() + 7200), "2 soat")
        self.assertEqual(sched.left_text(now() - 3600), "0 sekund")   # never negative

    def test_when_and_clock_text_are_readable(self):
        self.assertRegex(sched.when_text(now() + 7200), r"^(bugun|ertaga) \d{2}:\d{2}$")
        self.assertRegex(sched.clock_text(now() + 7200), r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$")
        far = now() + 20 * 86400
        self.assertRegex(sched.when_text(far), r"\d{2}\.\d{2}(\.\d{4})? \d{2}:\d{2}")
        self.assertEqual(sched.when_text(0), "")
        self.assertEqual(sched.clock_text(0), "")


if __name__ == "__main__":
    unittest.main()
