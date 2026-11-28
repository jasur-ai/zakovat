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

# A game with no activity stops itself: after this many questions in a row
# without a single guess, the round is closed automatically.
IDLE_LIMIT = 3

# The confirmation window of one parked answer: remind, remind again, then the
# bot publishes its own verdict rather than let a game stand still.
REVIEW_REMIND = (30, 90)

# One parked answer, as it travels inside the message state: long enough to read,
# short enough that the hidden run does not push the visible text out.
REVIEW_TEXT_LIMIT = 70
REVIEW_NAME_LIMIT = 24


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
    attempts: int = 0
    idle_streak: int = 0
    idle_limit: int = IDLE_LIMIT
    auto_stopped: bool = False
    can_delete: bool = True
    finished: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    # The confirmation step: an answer that has been judged but not published.
    # Nothing here scores anything — ``decide`` does, once a person has looked.
    needs_confirm: bool = True
    review_at: int = 0
    review_due: int = 0
    review_mark: str = "0"        # the bot's verdict: "1" right, "0" wrong
    review_text: str = ""         # the answer that is waiting
    review_user: int = 0          # who gave it
    review_name: str = ""
    review_noted: int = 0         # reminders already sent
    review_to: list = field(default_factory=list)   # [[user_id, message_id], …]
    review_msg: int = 0           # the group copy, when a DM was impossible
    review_kind: str = ""         # "answer" | "timeout" | "skip"

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
        self.attempts = 0
        self.latest_answer = None
        self.latest_user = None
        return self.question

    def submit(self, user_id: int, text: str, checker: Any) -> str:
        """Check an answer once per question and update the score."""
        if self.finished or self.question is None or self.answered:
            return IGNORED
        if self.mode == "solo" and user_id != self.owner_id:
            return FOREIGN
        # Only a real participant's guess counts as activity for the idle rule.
        self.attempts += 1
        if checker.is_correct(int(self.question["id"]), str(self.question.get("answer", "")), text):
            self.correct += 1
            self.answered = True
            self.idle_streak = 0
            self.history.append({"id": self.question["id"], "result": "correct", "user_id": user_id})
            return CORRECT
        self.latest_answer = text
        self.latest_user = user_id
        return WRONG

    # ------------------------------------------------------------- confirmation
    def reviewing(self) -> bool:
        """True while an answer is parked and waiting for somebody's word."""
        return bool(self.review_at) and self.question is not None

    def review_left(self, now: int) -> int:
        return max(0, int(self.review_due or 0) - int(now or 0))

    def review_nudge(self, now: int) -> bool:
        """True when another reminder is due (once per step of REVIEW_REMIND)."""
        if not self.reviewing():
            return False
        step = int(self.review_noted)
        if step >= len(REVIEW_REMIND):
            return False
        return int(now or 0) - int(self.review_at) >= int(REVIEW_REMIND[step])

    def judge(self, user_id: int, text: str, checker: Any) -> str:
        """Is this answer right? The score stays untouched — see ``park_review``."""
        if self.finished or self.question is None or self.reviewing():
            return IGNORED
        if self.mode == "solo" and user_id != self.owner_id:
            return FOREIGN
        if checker.is_correct(int(self.question["id"]), str(self.question.get("answer", "")),
                              str(text or "")):
            return CORRECT
        return WRONG

    def park_review(self, now: int, due: int, text: str = "", user_id: int = 0,
                    name: str = "", correct: bool = False, kind: str = "answer") -> bool:
        """Hold the question: the point is proposed, not given.

        ``answered`` stays False on purpose — the flag is rebuilt from the state
        line, and ``reviewing()`` is what keeps further guesses out.
        """
        if self.finished or self.question is None or self.reviewing():
            return False
        if str(text or "").strip():
            # Only a real guess counts as activity: a question nobody answered has
            # to keep growing the idle streak that eventually stops the game.
            self.attempts += 1
        self.review_at = int(now or 0)
        self.review_due = int(due or now or 0)
        self.review_mark = "1" if correct else "0"
        self.review_text = str(text or "").strip()[:REVIEW_TEXT_LIMIT]
        self.review_user = int(user_id or 0)
        self.review_name = str(name or "").strip()[:REVIEW_NAME_LIMIT]
        self.review_noted = 0
        self.review_kind = str(kind or "answer")
        return True

    def flip_review(self) -> str:
        """The confirmer disagrees with the bot: the verdict turns over."""
        if not self.reviewing():
            return IGNORED
        self.review_mark = "0" if self.review_mark == "1" else "1"
        return CORRECT if self.review_mark == "1" else WRONG

    def decide(self, user_id: int = 0, auto: bool = False, flip: bool = False) -> str:
        """Publish the parked answer: award the point the verdict says and count it.

        ``flip`` is «RAD ETISH» — the person looked and the bot was wrong, so the
        opposite of its verdict is what gets scored.
        """
        if not self.reviewing():
            return IGNORED
        if flip:
            self.review_mark = "0" if self.review_mark == "1" else "1"
        correct = self.review_mark == "1"
        if correct:
            self.correct += 1
            self.idle_streak = 0
        else:
            self.wrong += 1
            self.idle_streak = self.idle_streak + 1 if self.attempts == 0 else 0
        self.answered = True
        entry: dict[str, Any] = {
            "id": self.question["id"],
            "result": "correct" if correct else ("wrong" if self.review_text else "unanswered"),
            "user_id": self.review_user or None,
            "attempts": self.attempts,
        }
        if auto:
            entry["auto"] = 1
        elif int(user_id or 0):
            entry["by"] = int(user_id)
        if flip:
            entry["changed"] = 1
        self.history.append(entry)
        self.clear_review()
        return CORRECT if correct else WRONG

    def clear_review(self) -> None:
        self.review_at = 0
        self.review_due = 0
        self.review_mark = "0"
        self.review_text = ""
        self.review_user = 0
        self.review_name = ""
        self.review_noted = 0
        self.review_to = []
        self.review_msg = 0
        self.review_kind = ""

    def resolve_unanswered(self) -> bool:
        """A skipped or timed-out question gives one point to the other side.

        Questions closed without a single guess also grow the idle streak that
        eventually stops the game on its own.
        """
        if self.finished or self.question is None or self.answered:
            return False
        self.wrong += 1
        self.answered = True
        self.idle_streak = self.idle_streak + 1 if self.attempts == 0 else 0
        self.history.append({
            "id": self.question["id"], "result": "unanswered",
            "user_id": None, "attempts": self.attempts,
        })
        return True

    @property
    def idle_limit_reached(self) -> bool:
        """True when the game should stop itself for lack of any guessing."""
        return self.idle_limit > 0 and self.idle_streak >= self.idle_limit

    def score(self) -> tuple[int, int]:
        return self.correct, self.wrong

    def score_text(self) -> str:
        return f"{LEFT_LABEL} {self.correct} — {self.wrong} {RIGHT_LABEL}"

    def summary(self) -> str:
        head = f"O‘YIN TUGADI · {self.score_text()}"
        if self.total:
            percent = round(self.correct * 100 / self.total)
            head += f"\nTo‘g‘ri javoblar: {self.correct}/{self.total} ({percent}%)"
        return head
