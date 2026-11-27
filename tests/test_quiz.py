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

    def test_idle_streak_grows_only_without_guesses(self):
        session = self.session()
        # a wrong guess is still activity: it must not grow the idle streak
        self.assertEqual(session.submit(2, "not the answer", self.checker), "wrong")
        self.assertTrue(session.resolve_unanswered())
        self.assertEqual(session.attempts, 1)
        self.assertEqual(session.idle_streak, 0)

        session.index += 1
        session.advance()
        self.assertEqual(session.attempts, 0)
        self.assertTrue(session.resolve_unanswered())
        self.assertEqual(session.idle_streak, 1)
        self.assertFalse(session.idle_limit_reached)

    def test_correct_answer_resets_the_idle_streak(self):
        session = self.session()
        for _ in range(2):                      # two silent questions in a row
            self.assertTrue(session.resolve_unanswered())
            session.index += 1
            session.advance()
        self.assertEqual(session.idle_streak, 2)
        self.assertEqual(session.submit(2, "3", self.checker), CORRECT)
        self.assertEqual(session.idle_streak, 0)
        self.assertEqual(session.attempts, 1)

    def test_three_silent_questions_reach_the_idle_limit(self):
        session = self.session()
        for index in range(3):
            self.assertTrue(session.resolve_unanswered())
            self.assertEqual(session.idle_limit_reached, index == 2)
            session.index += 1
            session.advance()
        self.assertEqual(session.idle_streak, 3)
        self.assertTrue(session.idle_limit_reached)

    def test_idle_limit_can_be_disabled(self):
        session = self.session()
        session.idle_limit = 0
        for _ in range(4):
            session.resolve_unanswered()
            session.index += 1
            session.advance()
        self.assertFalse(session.idle_limit_reached)

    def test_history_records_whether_anybody_guessed(self):
        session = self.session()
        session.submit(2, "wrong", self.checker)
        session.resolve_unanswered()
        session.index += 1
        session.advance()
        session.resolve_unanswered()
        attempts = [entry.get("attempts") for entry in session.history]
        self.assertEqual(attempts, [1, 0])


if __name__ == "__main__":
    unittest.main()
