import tempfile
import unittest
from pathlib import Path

from bot.answers import AnswerChecker, normalize, variants


class NormalizeTests(unittest.TestCase):
    def test_cyrillic_and_apostrophes(self):
        self.assertEqual(normalize("Пиёз пўсти."), "piyoz po'sti")
        self.assertEqual(normalize("O‘zbek gʻishti"), "o'zbek g'ishti")

    def test_answer_prefix_is_stripped(self):
        self.assertIn("pinokkio", variants("Javob: Pinokkio."))

    def test_number_words(self):
        checker = AnswerChecker(Path(tempfile.mkdtemp()) / "aliases.json")
        self.assertTrue(checker.is_correct(1, "5", "besh"))
        self.assertTrue(checker.is_correct(1, "Javob: 5.", "5 ta"))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.checker = AnswerChecker(Path(tempfile.mkdtemp()) / "aliases.json")

    def test_exact_and_near(self):
        self.assertTrue(self.checker.is_correct(1, "Peru", "Peru"))
        self.assertTrue(self.checker.is_correct(1, "Peru", "peru."))
        self.assertFalse(self.checker.is_correct(1, "Peru", "Braziliya"))
        self.assertFalse(self.checker.is_correct(1, "Peru", "per"))

    def test_word_order(self):
        self.assertTrue(self.checker.is_correct(2, "Oyoq kiyim otish", "otish oyoq kiyim"))

    def test_cyrillic_user_answer(self):
        self.assertTrue(self.checker.is_correct(3, "Piyoz po'sti.", "Пиёз пўсти"))

    def test_learning_without_api(self):
        self.assertFalse(self.checker.is_correct(4, "Butterfly", "kapalak"))
        self.assertTrue(self.checker.learn(4, "kapalak"))
        self.assertFalse(self.checker.learn(4, "kapalak"))  # no duplicates
        self.assertTrue(self.checker.is_correct(4, "Butterfly", "KAPALAK!"))
        self.assertTrue(self.checker.alias_path.exists())
        self.assertEqual(AnswerChecker(self.checker.alias_path).aliases["4"], ["kapalak"])


if __name__ == "__main__":
    unittest.main()
