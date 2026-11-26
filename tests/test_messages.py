import unittest

from bot import messages as msg


class MessageTests(unittest.TestCase):
    def test_welcome_contains_archive_and_rules(self):
        text = msg.welcome(1266, 30853)
        for piece in ("1266", "30853", "Bilimdonlar", "Qarshi taraf", "4–6", "/rules"):
            self.assertIn(piece, text)

    def test_rules_and_group_hello(self):
        self.assertIn("reply", msg.rules())
        self.assertIn("/duel 959", msg.group_hello())

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
