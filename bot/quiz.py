"""Pure game-state logic: two-sided scoring, no Telegram dependency."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

CORRECT = "correct"
WRONG = "wrong"
FOREIGN = "foreign"   # answer from somebody else in a solo game
IGNORED = "ignored"   # no active question / question already resolved

LEFT_LABEL = "Bilimdonlar"
RIGHT_LABEL = "Qarshi taraf"


def build_playlist(questions: list[dict[str, Any]], rounds: int = 1, per_round: int = 0,
                   seed: int = 0) -> list[dict[str, Any]]:
    """Deterministic order of `rounds × per_round` questions drawn from a package.

    `per_round <= 0` means "every question of the package, one round". The same
    seed always gives the same playlist, which is what lets a stateless webhook
    continue a game from a marker inside the question message.
    """
    if not questions:
        return []
    rounds = max(1, int(rounds or 1))
    per_round = int(per_round or 0)
    if per_round <= 0:
        rounds, per_round = 1, len(questions)
    rng = random.Random(seed)
    playlist: list[dict[str, Any]] = []
    total = rounds * per_round
    while len(playlist) < total:
        batch = list(questions)
        rng.shuffle(batch)
        playlist.extend(batch)
    return playlist[:total]


@dataclass
class Session:
    chat_id: int
    mode: str                       # "solo" or "group"
    package_id: int
    package_title: str
    questions: list[dict[str, Any]]
    owner_id: int
    timer_seconds: int = 60
    rounds: int = 1
    per_round: int = 0
    seed: int = 0
    index: int = 0
    question: dict[str, Any] | None = None
    answered: bool = False
    correct: int = 0
    wrong: int = 0
    latest_answer: str | None = None
    latest_user: int | None = None
    job_name: str | None = None
    control_message_id: int | None = None
    deadline: int = 0
    finished: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def round_size(self) -> int:
        """Questions in one round; 0 means the whole package is a single round."""
        return self.per_round or self.total

    @property
    def current_round(self) -> int:
        size = self.round_size
        return (self.index // size) + 1 if size else 1

    @property
    def round_position(self) -> int:
        size = self.round_size
        return (self.index % size) + 1 if size else 1

    def begin(self) -> dict[str, Any] | None:
        return self.advance()

    def advance(self) -> dict[str, Any] | None:
        """Move to the next question; None means the game is over."""
        if self.index >= self.total:
            self.question = None
            self.finished = True
            return None
        self.question = self.questions[self.index]
        self.answered = False
        self.latest_answer = None
        self.latest_user = None
        return self.question

    def submit(self, user_id: int, text: str, checker: Any) -> str:
        """Check an answer once per question and update the score."""
        if self.finished or self.question is None or self.answered:
            return IGNORED
        if self.mode == "solo" and user_id != self.owner_id:
            return FOREIGN
        if checker.is_correct(int(self.question["id"]), str(self.question.get("answer", "")), text):
            self.correct += 1
            self.answered = True
            self.history.append({"id": self.question["id"], "result": "correct", "user_id": user_id})
            return CORRECT
        self.latest_answer = text
        self.latest_user = user_id
        return WRONG

    def resolve_unanswered(self) -> bool:
        """A skipped or timed-out question gives one point to the other side."""
        if self.finished or self.question is None or self.answered:
            return False
        self.wrong += 1
        self.answered = True
        self.history.append({"id": self.question["id"], "result": "unanswered", "user_id": None})
        return True

    def score(self) -> tuple[int, int]:
        return self.correct, self.wrong

    def score_text(self) -> str:
        return f"{LEFT_LABEL} {self.correct} — {self.wrong} {RIGHT_LABEL}"

    def summary(self) -> str:
        head = f"🏁 O‘yin tugadi: {self.score_text()}"
        if self.total:
            percent = round(self.correct * 100 / self.total)
            head += f"\nTo‘g‘ri javoblar: {self.correct}/{self.total} ({percent}%)"
        return head
