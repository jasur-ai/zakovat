import unittest

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

    def test_countdown_bar_shrinks(self):
        self.assertEqual(msg.bar(60, 60), "▰" * 10)
        self.assertEqual(msg.bar(0, 60), "▱" * 10)
        self.assertIn("30s", msg.timer_line(30, 60))
        self.assertIn("tugadi", msg.timer_line(0, 60))

    def test_settings_panel_shows_every_parameter(self):
        text = msg.settings_panel(959, "Zakovat 959", 54, 3, 10, 45, "solo", 30)
        for piece in ("959", "3", "10", "45", "30", "Yakka"):
            self.assertIn(piece, text)

    def test_packages_page_is_numbered(self):
        result = {"page": 2, "pages": 106, "count": 1266, "start": 12,
                  "items": [{"id": 1200, "title": "Tur", "questions": 20}]}
        text = msg.packages_page(result)
        self.assertIn("2/106", text)
        self.assertIn("13. <code>1200</code>", text)
        self.assertIn("1266", text)

    def test_admin_only_notice(self):
        self.assertIn("admin", msg.admin_only("/skip"))

    def test_small_talk(self):
        self.assertIsNotNone(msg.small_talk("Salom"))
        self.assertIsNotNone(msg.small_talk("Qanday o‘ynayman?"))
        self.assertIn("<b>Qoidalar", msg.small_talk("Qanday o‘ynayman?"))
        self.assertIsNone(msg.small_talk("Peru"))

    def test_question_and_summary_are_escaped(self):
        text = msg.question(1, 10, "959", "<savol>", 60, "Bilimdonlar 0 — 0 Qarshi taraf")
        self.assertIn("&lt;savol&gt;", text)
        self.assertIn("1/10", text)
        self.assertIn("Bilimdonlar <b>4</b> — <b>6</b> Qarshi taraf", msg.summary("959", 10, 4, 6, 40))


if __name__ == "__main__":
    unittest.main()
