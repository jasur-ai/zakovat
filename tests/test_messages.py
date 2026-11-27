import unittest
from types import SimpleNamespace

from bot import messages as msg


class MessageTests(unittest.TestCase):
    def test_welcome_contains_archive_and_rules(self):
        text = msg.welcome(1266, 30853)
        for piece in ("1266", "30853", "Bilimdonlar", "Qarshi taraf", "4–6", "/rules"):
            self.assertIn(piece, text)

    def test_rules_and_group_hello(self):
        self.assertIn("reply", msg.rules())
        self.assertIn("/duel", msg.group_hello())

    def test_help_lists_commands(self):
        text = msg.help_text()
        for command in ("/solo", "/duel", "/packages", "/score", "/skip", "/accept", "/stop", "/rules"):
            self.assertIn(command, text)
        self.assertIn("admin", text)

    def test_help_is_grouped_and_can_be_narrowed(self):
        full = msg.help_text()
        for heading in ("<b>O‘YIN</b>", "<b>TURNIR · RO‘YXATDAN O‘TISH</b>",
                        "<b>TURNIR · SAVOL PAYTIDA</b>", "<b>KURATOR", "<b>BOT EGASI</b>"):
            self.assertIn(heading, full)
        self.assertLess(len(full), 4096)

        narrow = msg.help_text("turnir")
        self.assertIn("/jamoa", narrow)
        self.assertNotIn("/solo", narrow)                 # one group means one group
        self.assertNotIn("/help turnir", narrow)          # and no pointer to the others
        self.assertEqual(msg.help_text("yo‘q"), full)     # an unknown word lists everything
        self.assertNotIn("turnir", msg.help_text("oyin").lower())   # the polling bot's help

    def test_the_whisper_copy_says_who_may_read_it(self):
        usage = msg.whisper_usage()
        self.assertIn("/pichoq matn", usage)
        self.assertIn("Faqat o‘z jamoangiz", usage)
        self.assertIn("Delete messages", usage)
        self.assertIn("/pichoq jamoa matn", usage)        # the old copy dropped "pichoq"
        refused = msg.whisper_outside("boshqa jamoada")
        self.assertIn("YUBORILMADI", refused)
        self.assertIn("boshqa jamoada", refused)
        self.assertIn("/azolar", refused)
        self.assertIn("o‘chira olmaydi", msg.whisper_dropped("Guruh"))
        self.assertIn("o‘chirish huquqi yo‘q", msg.answer_dropped("Guruh"))
        self.assertIn("turnir yo‘q", msg.whisper_no_tournament())

    def test_the_member_list_is_team_shaped(self):
        team = SimpleNamespace(name="Bilimdonlar", captain=101, size=2,
                               members=[SimpleNamespace(user_id=101, name="Kapitan",
                                                        username="kapitan"),
                                        SimpleNamespace(user_id=102, name="A’zo", username="")])
        text = msg.members_list([team], "Zakovat klubi", 102,
                                extra=[{"id": 7, "name": "Kuzatuvchi", "username": "kuz"}])
        self.assertIn("<b>A’ZOLAR</b> · Zakovat klubi", text)
        self.assertIn("<b>Bilimdonlar</b> · 2 kishi", text)
        self.assertIn("<code>101</code> @kapitan", text)
        self.assertIn("kapitan", text)
        self.assertIn("· siz", text)
        self.assertIn("GURUHDA BOT KO‘RGANLAR", text)
        self.assertIn("/pichoq matn", text)
        empty = msg.members_list([], "Guruh", 1)
        self.assertIn("Hali hech kim", empty)
        self.assertNotIn("GURUHDA BOT KO‘RGANLAR", empty)

    def test_countdown_bar_shrinks(self):
        # Twenty segments: with a 60-second question one segment is three seconds,
        # so the bar visibly walks down between two repaints.
        self.assertEqual(msg.bar(60, 60), "▰" * 20)
        self.assertEqual(msg.bar(0, 60), "▱" * 20)
        self.assertEqual(len(msg.bar(30, 60)), 20)
        self.assertIn("30 s", msg.timer_line(30, 60))
        self.assertIn("tugadi", msg.timer_line(0, 60))

    def test_clock_reads_like_a_timer(self):
        self.assertEqual(msg.clock(60), "01:00")
        self.assertEqual(msg.clock(47), "00:47")
        self.assertEqual(msg.clock(3), "00:03")
        self.assertEqual(msg.clock(-5), "00:00")

    def test_countdown_shows_digits_and_a_shrinking_bar(self):
        head, _, track = msg.countdown(45, 60).partition("\n")
        self.assertEqual(head, "<b>00:45</b> · QOLGAN VAQT")   # digits first: they are the clock
        self.assertEqual(track, "▰" * 15 + "▱" * 5)
        shorter = msg.countdown(12, 60)
        self.assertIn("<b>00:12</b>", shorter)
        self.assertLess(shorter.count("▰"), track.count("▰"))
        self.assertIn("VAQT TUGADI", msg.countdown(0, 60))

    def test_the_last_seconds_say_the_time_is_almost_up(self):
        self.assertIn("VAQT TUGAYAPTI", msg.countdown(10, 60))
        self.assertIn("VAQT TUGAYAPTI", msg.countdown(1, 60))
        self.assertNotIn("VAQT TUGAYAPTI", msg.countdown(11, 60))
        self.assertIn("<b>VAQT TUGADI</b>", msg.countdown(0, 60))

    def test_settings_panel_shows_every_parameter(self):
        text = msg.settings_panel(959, "Zakovat 959", 54, 3, 10, 45, "solo", 30)
        for piece in ("959", "3", "10", "45", "30", "Yakka"):
            self.assertIn(piece, text)

    def test_packages_page_is_numbered(self):
        result = {"page": 2, "pages": 106, "count": 1266, "start": 12,
                  "items": [{"id": 1200, "title": "Tur", "questions": 20}]}
        text = msg.packages_page(result)
        self.assertIn("Sahifa 2 / 106", text)
        self.assertIn("13. 1200 — Tur (20 savol)", text)
        self.assertIn("1266", text)

    def test_admin_only_notice(self):
        self.assertIn("admin", msg.admin_only("/skip"))

    def test_small_talk(self):
        self.assertIsNotNone(msg.small_talk("Salom"))
        self.assertIsNotNone(msg.small_talk("Qanday o‘ynayman?"))
        self.assertIn("<b>QOIDALAR", msg.small_talk("Qanday o‘ynayman?"))
        self.assertIsNone(msg.small_talk("Peru"))

    def test_question_and_summary_are_escaped(self):
        text = msg.question(1, 10, "959", "<savol>", 60, "Bilimdonlar 0 — 0 Qarshi taraf")
        self.assertIn("&lt;savol&gt;", text)
        self.assertIn("SAVOL 1 / 10", text)
        self.assertIn("Bilimdonlar 4 — 6 Qarshi taraf", msg.summary("959", 10, 4, 6, 40))


    def test_no_decorative_emoji_anywhere(self):
        """Professional look: capitalised headings and text bars, never emoji."""
        import re

        emoji = re.compile(
            "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002300-\U000023FF\U0000FE0F]"
        )
        texts = [
            msg.welcome(1266, 30853), msg.help_text(), msg.rules(), msg.group_hello(),
            msg.settings_panel(959, "T", 54, 3, 10, 60, "solo", 30), msg.repeat_notice(54, 6),
            msg.game_start("Yakka o‘yin", "T", 30, 60, "0 — 0", 3, 10), msg.round_banner(2, 3, "1 — 1"),
            msg.question(1, 30, "T", "savol", 60, "0 — 0", "", "1-raund"),
            msg.reveal("To‘g‘ri", "Peru", "izoh", "", "1 — 0"),
            msg.summary("T", 30, 10, 20, 33, 3), msg.auto_stop(3, "T", 3, 0, 3, 0, 1),
            msg.packages_page({"page": 1, "pages": 106, "count": 1266, "start": 0,
                               "items": [{"id": 959, "title": "T", "questions": 54}]}),
            msg.admin_only("/skip"), msg.idle_warning(1, 3), msg.no_delete_rights(),
            msg.timer_line(30, 60), msg.small_talk("salom"),
        ]
        for text in texts:
            self.assertIsNone(emoji.search(text), text)
        self.assertIn("<b>O‘YIN AVTOMATIK TO‘XTATILDI</b>",
                      msg.auto_stop(3, "T", 3, 0, 3, 0, 1))

    def test_idle_warning_counts_down_to_auto_stop(self):
        self.assertIn("2 savol", msg.idle_warning(1, 3))
        self.assertIn("Yana bir savolga", msg.idle_warning(2, 3))

    def test_question_has_no_static_countdown_by_default(self):
        """The live countdown lives on the strip above, not inside the question."""
        text = msg.question(1, 30, "T", "savol", 60, "0 — 0")
        self.assertNotIn("Qolgan vaqt", text)
        self.assertIn("Qolgan vaqt", msg.question(1, 30, "T", "savol", 60, "0 — 0", remaining=42))


if __name__ == "__main__":
    unittest.main()
