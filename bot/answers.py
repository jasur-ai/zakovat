"""API-key-free answer checking.

The checker is a small deterministic "agent": it normalizes Uzbek text in
Latin/Cyrillic, extracts acceptable answer variants from the archive, compares
them with several strategies, and learns new variants when the admin confirms
an answer with /accept. No external service or API key is used.
"""
from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

CYRILLIC = {
    "қ": "q", "ғ": "g'", "ў": "o'", "ҳ": "h", "ш": "sh", "ч": "ch", "нг": "ng",
    "ё": "yo", "ю": "yu", "я": "ya", "ъ": "'", "э": "e", "ц": "ts", "ь": "",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ы": "i",
}

NUMBER_WORDS = {
    "nol": "0", "ноль": "0", "bir": "1", "ikki": "2", "uch": "3",
    "to'rt": "4", "tort": "4", "besh": "5", "olti": "6", "yetti": "7",
    "sakkiz": "8", "to'qqiz": "9", "toqqiz": "9", "o'n": "10", "on": "10",
}

PREFIX_RE = re.compile(r"^\s*(?:to'?g'?ri\s+)?(?:javob|answer|j)\s*[:.\-]?\s*", re.I)
# A team that copies an archive answer types the prefix with it ("Javob: X"), so
# the same prefix is taken off the guess before comparing. It has to be stricter
# than PREFIX_RE: a bare "j" is only a prefix when a separator follows it, or a
# real answer like "Jizzax" would lose its first letter.
USER_PREFIX_RE = re.compile(
    r"^\s*(?:to'?g'?ri\s+)?(?:(?:javob|answer)\s*[:.\-]?\s+|j\s*[:.\-]\s*)", re.I)


def normalize(text: str) -> str:
    """Lowercase, transliterate Cyrillic Uzbek, unify apostrophes, drop punctuation."""
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    for char, replacement in sorted(CYRILLIC.items(), key=lambda item: -len(item[0])):
        value = value.replace(char, replacement)
    for char in "‘’ʻ`´ʼ":
        value = value.replace(char, "'")
    value = re.sub(r"[^0-9a-z'\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    # "besh" and "5" should compare equal when the whole answer is a number.
    words = [NUMBER_WORDS.get(token, token) for token in value.split()]
    return " ".join(words)


def variants(expected: str) -> list[str]:
    """All acceptable normalized variants of an archive answer."""
    value = normalize(expected)
    value = PREFIX_RE.sub("", value).strip(" .,:;-")
    parts = re.split(r"\s*(?:/|\||;|,|\byoki\b|\bbo'?lsa\b)\s*", value)
    result: list[str] = []
    for part in parts:
        for candidate in (part, *re.findall(r"\(([^)]+)\)", part)):
            candidate = candidate.strip(" .,:;-'")
            if candidate and candidate not in result:
                result.append(candidate)
    return result or ([value] if value else [])


def _close_enough(user: str, expected: str) -> bool:
    if user == expected:
        return True
    user_digits = re.findall(r"\d+", user)
    expected_digits = re.findall(r"\d+", expected)
    if user_digits and expected_digits and user_digits == expected_digits:
        return True

    user_tokens = set(user.split())
    expected_tokens = set(expected.split())
    if user_tokens and expected_tokens:
        if len(expected_tokens) > 1 and expected_tokens <= user_tokens and len(user_tokens) - len(expected_tokens) <= 2:
            return True
        if len(user_tokens) > 1 and user_tokens <= expected_tokens and len(expected_tokens) - len(user_tokens) <= 2:
            return True
        ordered = SequenceMatcher(None, user, expected).ratio()
        unordered = SequenceMatcher(
            None, " ".join(sorted(user_tokens)), " ".join(sorted(expected_tokens))
        ).ratio()
        score = max(ordered, unordered)
        if score >= 0.88 or (len(expected) >= 12 and score >= 0.82):
            return True
    return False


class AnswerChecker:
    """Local answer matcher with a persisted alias dictionary."""

    def __init__(self, alias_path: str | Path = "data/answer_aliases.json"):
        self.alias_path = Path(alias_path)
        self.aliases: dict[str, list[str]] = {}
        if self.alias_path.exists():
            try:
                with self.alias_path.open(encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self.aliases = {str(k): [str(v) for v in vals] for k, vals in loaded.items()}
            except (OSError, json.JSONDecodeError):
                self.aliases = {}

    def is_correct(self, question_id: int, expected: str, user_text: str) -> bool:
        user = normalize(user_text)
        if not user:
            return False
        bare = USER_PREFIX_RE.sub("", user).strip(" .,:;-'")
        guesses = [user] + ([bare] if bare and bare != user else [])
        options = variants(expected) + self.aliases.get(str(question_id), [])
        return any(_close_enough(guess, option)
                   for guess in guesses for option in options if option)

    def learn(self, question_id: int, user_text: str) -> bool:
        """Remember a confirmed answer variant; returns True if it was new."""
        value = normalize(user_text)
        if not value:
            return False
        key = str(question_id)
        learned = self.aliases.setdefault(key, [])
        if value in learned:
            return False
        learned.append(value)
        self.alias_path.parent.mkdir(parents=True, exist_ok=True)
        with self.alias_path.open("w", encoding="utf-8") as fh:
            json.dump(self.aliases, fh, ensure_ascii=False, indent=2)
        return True
