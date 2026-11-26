import tempfile
import unittest
from pathlib import Path

from bot.answers import AnswerChecker
from bot.quiz import CORRECT, FOREIGN, IGNORED, Session


class FakeChecker(AnswerChecker):
    """Exact answer for tests, no filesystem needed."""


class QuizTests(unittest.TestCase):
    def setUp(self):
        self.checker = AnswerChecker(Path(tempfile.mkdtemp()) / "aliases.json")
        self.questions = [{"id": i + 1, "text": f"Savol {i + 1}", "answer": str(i + 1)} for i in range(10)]

    def session(self, mode="group", owner_id=1):
        session = Session(
            chat_id=-100, mode=mode, package_id=959, package_title="959",
            questions=self.questions, owner_id=owner_id, timer_seconds=60,
        )
        session.begin()
        return session

    def test_four_of_ten_is_four_six(self):
        session = self.session()
        for i in range(4):
            self.assertEqual(session.submit(2, str(i + 1), self.checker), CORRECT)
            session.index += 1
            session.advance()
        for _ in range(6):
            self.assertTrue(session.resolve_unanswered())
            session.index += 1
            session.advance()
        self.assertEqual(session.score(), (4, 6))
        self.assertEqual(session.score_text(), "Bilimdonlar 4 — 6 Qarshi taraf")
        self.assertIn("4/10 (40%)", session.summary())

    def test_solo_accepts_only_owner(self):
        session = self.session(mode="solo")
        self.assertEqual(session.submit(2, "1", self.checker), FOREIGN)
        self.assertEqual(session.submit(1, "1", self.checker), CORRECT)
        self.assertEqual(session.submit(1, "2", self.checker), IGNORED)

    def test_skip_and_timeout_count_once(self):
        session = self.session()
        self.assertTrue(session.resolve_unanswered())
        self.assertFalse(session.resolve_unanswered())
        self.assertEqual(session.score(), (0, 1))
        self.assertFalse(session.submit(2, "1", self.checker) == CORRECT)


if __name__ == "__main__":
    unittest.main()
