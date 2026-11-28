"""Tournament logic: teams, registration, final answers, standings.

Pure Python — no Telegram, no I/O, no database. The webhook layer turns this
into messages and carries it between requests inside the countdown strip (see
``dump``/``load``), exactly like the ordinary game carries its state.

The rules the bot enforces are the ones a real Zakovat tournament needs:

* at least ``MIN_TEAMS`` teams have to register;
* every team needs ``MIN_MEMBERS`` to ``MAX_MEMBERS`` people;
* at least ``MIN_READY`` members of every team must press "Men tayyorman";
* one question is answered by **one final answer per team** (``/yakuniy``),
  which anybody from that team may give and may change until the question
  closes;
* a correct final answer is a point for that team — several teams can score on
  the same question, nobody is punished for a wrong one.

Answers stay inside the team: the bot deletes them from the group when it is
allowed to and sends them to the team's own members in private instead.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

from . import schedule

MIN_TEAMS = 3          # kamida uchta jamoa ro‘yxatdan o‘tishi kerak
MIN_READY = 2          # har bir jamoadan kamida ikki kishi "tayyorman" deydi
MIN_MEMBERS = 3        # jamoada kamida uch kishi
MAX_MEMBERS = 7        # va ko‘pi bilan yetti kishi
MAX_TEAMS = 12         # ro‘yxat chegarasi (holat xabarga sig‘ishi uchun)
MAX_NAME = 24          # jamoa nomi uzunligi
DIRECTORY_LIMIT = 120  # guruhda ko‘rilgan odamlar xotirasi (holat xabarga sig‘ishi uchun)
SELECTION_LIMIT = 150  # tanlovda saqlanadigan eng ko‘p savol id si
TRIM_LEVELS = 5        # 0 = to‘liq holat … 4 = eng ixcham
FINAL_LIMIT = 200      # yakuniy javob uzunligi

PHASE_REGISTER = "register"
PHASE_PLAY = "play"
PHASE_DONE = "done"

STATE_PREFIX = "T|"    # the strip body of a tournament, not of an ordinary game


@dataclass
class Member:
    user_id: int
    name: str = ""
    username: str = ""
    ready: bool = False

    def label(self) -> str:
        return self.name or self.username or f"id {self.user_id}"

    def dump(self) -> list:
        return [int(self.user_id), self.name[:20], self.username[:20], 1 if self.ready else 0]

    @classmethod
    def load(cls, row: Any) -> "Member":
        row = list(row or [])
        return cls(user_id=int(row[0] or 0), name=str(row[1] or ""), username=str(row[2] or ""),
                   ready=bool(len(row) > 3 and row[3]))


@dataclass
class Team:
    name: str
    captain: int = 0
    members: list[Member] = field(default_factory=list)
    score: int = 0
    final: str = ""            # this question's final answer
    final_by: int = 0
    final_at: int = 0
    solved: list[int] = field(default_factory=list)
    attempts: int = 0

    # ------------------------------------------------------------------ roster
    def member(self, user_id: int) -> Member | None:
        for person in self.members:
            if int(person.user_id) == int(user_id):
                return person
        return None

    def add(self, user_id: int, name: str = "", username: str = "") -> str:
        """Add somebody; returns an empty string when it worked."""
        if self.member(user_id):
            return "Siz bu jamoada allaqachon borsiz."
        if len(self.members) >= MAX_MEMBERS:
            return f"Jamoa to‘lgan — ko‘pi bilan {MAX_MEMBERS} kishi."
        self.members.append(Member(int(user_id), (name or "")[:20], (username or "")[:20]))
        return ""

    def remove(self, user_id: int) -> bool:
        before = len(self.members)
        self.members = [person for person in self.members if int(person.user_id) != int(user_id)]
        return len(self.members) != before

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def ready_count(self) -> int:
        return sum(1 for person in self.members if person.ready)

    @property
    def is_ready(self) -> bool:
        return self.size >= MIN_MEMBERS and self.ready_count >= MIN_READY

    def set_ready(self, user_id: int, ready: bool = True) -> str:
        person = self.member(user_id)
        if person is None:
            return "Avval jamoaga qo‘shiling."
        person.ready = bool(ready)
        return ""

    # ----------------------------------------------------------------- answers
    def submit(self, user_id: int, text: str, when: int) -> str:
        """Set (or change) this team's final answer for the open question."""
        if self.member(user_id) is None:
            return "Bu jamoa a’zosi emassiz."
        value = " ".join(str(text or "").split())[:FINAL_LIMIT]
        if not value:
            return "Javob bo‘sh."
        self.final = value
        self.final_by = int(user_id)
        self.final_at = int(when)
        self.attempts += 1
        return ""

    def clear_final(self, when: int = 0) -> None:
        """Take the answer back. ``when`` timestamps the cancellation, so a
        concurrent request that answered a moment later still wins."""
        self.final = ""
        self.final_by = 0
        self.final_at = int(when or 0)

    # ------------------------------------------------------------------ coding
    def dump(self) -> list:
        return [self.name[:MAX_NAME], int(self.captain), [m.dump() for m in self.members],
                int(self.score), self.final[:FINAL_LIMIT], int(self.final_by),
                int(self.final_at), list(self.solved)[-40:], int(self.attempts)]

    @classmethod
    def load(cls, row: Any) -> "Team":
        row = list(row or [])
        team = cls(name=str(row[0] or "Jamoa")[:MAX_NAME], captain=int(row[1] or 0))
        team.members = [Member.load(item) for item in (row[2] or [])]
        team.score = int(row[3] or 0)
        team.final = str(row[4] or "")[:FINAL_LIMIT]
        team.final_by = int(row[5] or 0)
        team.final_at = int(row[6] or 0)
        team.solved = [int(item) for item in (row[7] or [])]
        team.attempts = int(row[8] or 0) if len(row) > 8 else 0
        return team


@dataclass
class Tournament:
    chat_id: int
    title: str
    package_id: int
    creator: int
    timer: int = 60
    rounds: int = 1
    per_round: int = 10
    seed: int = 0
    phase: str = PHASE_REGISTER
    teams: list[Team] = field(default_factory=list)
    include: list[int] = field(default_factory=list)   # tanlangan savol id lari
    exclude: list[int] = field(default_factory=list)   # chiqarib tashlanganlar
    index: int = 0
    question_id: int = 0
    deadline: int = 0
    control_message_id: int | None = None
    card_message_id: int | None = None    # ro‘yxat kartasi (pinlangan — holat shu yerda)
    curators: list[int] = field(default_factory=list)   # admin tayinlagan boshqaruvchilar
    page: int = 1                       # savol tanlash oynasidagi sahifa
    query: str = ""                     # tanlash oynasidagi qidiruv so‘zi
    naming: int = 0                     # jamoa nomini yozishi kutilayotgan a’zo
    directory: list[list] = field(default_factory=list)   # [[id, ism, username], …]
    hinted: list[int] = field(default_factory=list)       # javob tartibi eslatilganlar
    open_mode: bool = False          # javoblar guruhda ko‘rinadi (o‘chirish huquqi yo‘q)
    can_delete: bool = True
    questions: list[dict] = field(default_factory=list, repr=False)
    history: list[dict] = field(default_factory=list)
    asked: int = 0
    finished: bool = False
    rev: int = 0                     # every mutation counts: the newest state wins
    can_pin: bool = False            # the card could be pinned, so it is the authority
    start_at: int = 0                # rejadagi boshlanish vaqti (0 = hoziroq mumkin)
    max_teams: int = MAX_TEAMS       # nechta jamoa o‘ynashi mumkin
    announced: int = 0               # eslatma xabari yuborilgan payt

    # ------------------------------------------------------------------- teams
    def team(self, name: str) -> Team | None:
        wanted = " ".join(str(name or "").split()).lower()
        for group in self.teams:
            if group.name.lower() == wanted:
                return group
        return None

    def take_finals(self, other: "Tournament | None") -> int:
        """Adopt the finals another request wrote for the same question.

        Serverless has no compare-and-swap: when two members answer at the same
        moment, both requests read the same card and the one that writes last
        would erase the other team's answer. So before writing back, any final
        that is *newer* than the local copy (a later ``final_at``, same open
        question) is taken over. Returns how many were taken.
        """
        if other is None or self.phase != PHASE_PLAY or other.phase != PHASE_PLAY:
            return 0
        if int(other.question_id) != int(self.question_id) or not self.question_id:
            return 0
        taken = 0
        for team in self.teams:
            twin = next((row for row in other.teams if row.name == team.name), None)
            if twin is None or int(twin.final_at) <= int(team.final_at):
                continue
            team.final, team.final_by, team.final_at = twin.final, twin.final_by, twin.final_at
            taken += 1
        return taken

    def bump(self) -> int:
        """Count one change of state; countdown hops compare these numbers."""
        self.rev = int(self.rev) + 1
        return self.rev

    def see(self, user_id: int, name: str = "", username: str = "") -> bool:
        """Remember somebody the bot saw in this chat; True when it is new.

        The directory is what makes ``/pichoq @username`` and ``/azolar`` work:
        the Bot API cannot list the members of a supergroup, so the bot keeps
        the people it has actually seen, newest last, and never grows past
        DIRECTORY_LIMIT entries.
        """
        wanted = int(user_id or 0)
        if not wanted:
            return False
        for row in self.directory:
            if int(row[0]) == wanted:
                changed = (name and row[1] != name[:24]) or (username and row[2] != username[:24])
                if changed:
                    row[1] = (name or row[1])[:24]
                    row[2] = (username or row[2])[:24]
                return bool(changed)
        self.directory.append([wanted, (name or "")[:24], (username or "")[:24]])
        self.directory = self.directory[-DIRECTORY_LIMIT:]
        return True

    def find(self, target: str) -> int:
        """Resolve ``@username``, a bare username or an id to a user id."""
        needle = str(target or "").strip().lstrip("@").lower()
        if not needle:
            return 0
        if needle.isdigit():
            return int(needle)
        for row in self.directory:
            if str(row[2] or "").lower() == needle:
                return int(row[0])
        for group in self.teams:
            for person in group.members:
                if str(person.username or "").lower() == needle:
                    return int(person.user_id)
        return 0

    def directory_rows(self) -> list[dict]:
        return [{"id": int(row[0]), "name": str(row[1] or ""), "username": str(row[2] or "")}
                for row in self.directory]

    def index_of(self, team: "Team | None") -> int:
        for position, group in enumerate(self.teams):
            if group is team:
                return position
        return -1

    def is_curator(self, user_id: int) -> bool:
        """Somebody the admin appointed to run this tournament."""
        return int(user_id or 0) in {int(item) for item in self.curators}

    def appoint(self, user_id: int, grant: bool = True) -> str:
        """Add or remove an appointed curator."""
        wanted = int(user_id or 0)
        if not wanted:
            return "Id ko‘rsatilmadi."
        holders = {int(item) for item in self.curators}
        if grant:
            holders.add(wanted)
        else:
            holders.discard(wanted)
        self.curators = sorted(holders)[:20]
        return ""

    def team_of(self, user_id: int) -> Team | None:
        for group in self.teams:
            if group.member(user_id) is not None:
                return group
        return None

    def team_at(self, position: int) -> Team | None:
        return self.teams[position] if 0 <= position < len(self.teams) else None

    def add_team(self, name: str, user_id: int, person_name: str = "",
                 username: str = "") -> tuple[Team | None, str]:
        """Register a team with its captain as the first member."""
        name = " ".join(str(name or "").split())[:MAX_NAME]
        if not name:
            return None, "Jamoa nomini yozing."
        limit = self.team_limit
        if len(self.teams) >= limit:
            return None, f"Jamolar soni chegarada ({limit} ta)."
        if self.team(name) is not None:
            return None, "Bunday nomli jamoa bor."
        if self.team_of(user_id) is not None:
            return None, "Siz allaqachon boshqa jamoadasiz."
        if self.phase != PHASE_REGISTER:
            return None, "Ro‘yxatdan o‘tish yopildi."
        group = Team(name=name, captain=int(user_id))
        group.add(user_id, person_name, username)
        group.set_ready(user_id, True)          # the captain is ready by definition
        self.teams.append(group)
        return group, ""

    def join(self, position: int, user_id: int, person_name: str = "",
             username: str = "", ready: bool = False) -> tuple[Team | None, str]:
        """Somebody joins (or says "Men tayyorman" for) one team."""
        if self.phase != PHASE_REGISTER:
            return None, "Ro‘yxatdan o‘tish yopildi."
        group = self.team_at(position)
        if group is None:
            return None, "Bunday jamoa yo‘q."
        if self.team_of(user_id) is not None and self.team_of(user_id) is not group:
            return None, "Siz allaqachon boshqa jamoadasiz."
        problem = group.add(user_id, person_name, username)
        if problem:
            return group, problem
        if ready:
            group.set_ready(user_id, True)
        return group, ""

    def set_ready(self, position: int, user_id: int, ready: bool = True) -> tuple[Team | None, str]:
        group = self.team_at(position)
        if group is None:
            return None, "Bunday jamoa yo‘q."
        if self.phase != PHASE_REGISTER:
            return None, "Ro‘yxatdan o‘tish yopildi."
        problem = group.set_ready(user_id, ready)
        return group, problem

    def leave(self, user_id: int) -> tuple[Team | None, str]:
        if self.phase != PHASE_REGISTER:
            return None, "Ro‘yxatdan o‘tish yopildi."
        group = self.team_of(user_id)
        if group is None:
            return None, "Siz biror jamoada emassiz."
        was_captain = int(group.captain) == int(user_id)
        group.remove(user_id)
        if was_captain and group.members:
            group.captain = int(group.members[0].user_id)
        if not group.members:
            self.teams = [item for item in self.teams if item is not group]
            return None, "Jamoa bo‘shagani uchun o‘chirildi."
        return group, ""

    # ------------------------------------------------------------------- rules
    def missing(self, available: int | None = None, now: int = 0) -> list[str]:
        """What still has to happen before the tournament may start.

        ``available`` is how many questions the current selection leaves in the
        archive; the caller knows that, this module never reads files. ``now``
        lets a scheduled tournament say how long it still has to wait.
        """
        problems: list[str] = []
        waiting = self.start_delay(now)
        if waiting:
            problems.append(f"Belgilangan vaqtgacha {waiting // 60} daqiqa qoldi "
                            f"(boshlanish: {when_text(self.start_at)}).")
        if available is not None and int(available) <= 0:
            problems.append("Savollar to‘plami bo‘sh — paket yoki tanlovni tekshiring.")
        if len(self.teams) < MIN_TEAMS:
            problems.append(f"Kamida {MIN_TEAMS} ta jamoa ro‘yxatdan o‘tishi kerak "
                            f"(hozir {len(self.teams)} ta).")
        for group in self.teams:
            if group.size < MIN_MEMBERS:
                problems.append(f"«{group.name}»: kamida {MIN_MEMBERS} kishi kerak "
                                f"(hozir {group.size}).")
            elif group.ready_count < MIN_READY:
                problems.append(f"«{group.name}»: kamida {MIN_READY} kishi «Men tayyorman» "
                                f"tugmasini bosishi kerak (hozir {group.ready_count}).")
        return problems

    @property
    def team_limit(self) -> int:
        """How many teams may play: the composer's choice, inside the hard cap."""
        try:
            return max(MIN_TEAMS, min(int(self.max_teams or MAX_TEAMS), MAX_TEAMS))
        except (TypeError, ValueError):
            return MAX_TEAMS

    def start_delay(self, now: int = 0) -> int:
        """Seconds left until the planned start; 0 when it may start now."""
        moment = int(now or 0)
        if not self.start_at or not moment:
            return 0
        return max(0, int(self.start_at) - moment)

    def ready_to_start(self, available: int | None = None, now: int = 0) -> bool:
        return not self.missing(available, now)

    @property
    def people(self) -> int:
        return sum(group.size for group in self.teams)

    def roster(self) -> list[Member]:
        """Everybody registered, in team order — used for private messages."""
        return [person for group in self.teams for person in group.members]

    # ---------------------------------------------------------------- questions
    def bank(self, questions: list[dict]) -> list[dict]:
        """The exact question set this tournament plays."""
        pool = [item for item in questions if item and item.get("id") is not None]
        if self.include:
            wanted = {int(item) for item in self.include}
            pool = [item for item in pool if int(item["id"]) in wanted]
        if self.exclude:
            dropped = {int(item) for item in self.exclude}
            pool = [item for item in pool if int(item["id"]) not in dropped]
        return pool

    @property
    def bank_size(self) -> int:
        """Distinct questions in the running playlist (0 before it is built)."""
        return len({int(item["id"]) for item in self.questions})

    def playlist(self, questions: list[dict]) -> list[dict]:
        """`rounds × per_round` questions in a stable order for this seed.

        A question is never asked twice: when the curator asks for more than the
        bank holds, the playlist stops at the bank.
        """
        pool = self.bank(questions)
        if not pool:
            return []
        rounds = max(1, int(self.rounds or 1))
        per_round = int(self.per_round or 0)
        if per_round <= 0:
            rounds, per_round = 1, len(pool)
        wanted = min(rounds * per_round, len(pool))
        rng = random.Random(int(self.seed or 0))
        batch = list(pool)
        rng.shuffle(batch)
        return batch[:wanted]

    # --------------------------------------------------------------------- play
    def start(self, questions: list[dict]) -> dict | None:
        """Move from registration to the first question."""
        self.questions = self.playlist(questions)
        if not self.questions:
            return None
        self.phase = PHASE_PLAY
        self.index = 0
        return self.open_question(0)

    def open_question(self, index: int, now: int = 0) -> dict | None:
        if index >= len(self.questions):
            self.phase = PHASE_DONE
            self.finished = True
            self.question_id = 0
            return None
        self.index = index
        self.question_id = int(self.questions[index]["id"])
        self.deadline = int(now)
        for group in self.teams:
            group.clear_final()
        return self.questions[index]

    @property
    def question(self) -> dict | None:
        if self.phase != PHASE_PLAY or not self.questions:
            return None
        return self.questions[self.index] if self.index < len(self.questions) else None

    @property
    def total(self) -> int:
        return len(self.questions)

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def round_size(self) -> int:
        return self.per_round or self.total

    @property
    def current_round(self) -> int:
        size = self.round_size
        return (self.index // size) + 1 if size else 1

    def submit_final(self, user_id: int, text: str, now: int) -> tuple[Team | None, str]:
        """One team's final answer; anybody from that team may change it."""
        if self.phase != PHASE_PLAY or self.question is None:
            return None, "Hozir savol ochiq emas."
        if self.deadline and now and int(now) > int(self.deadline):
            return None, "Vaqt tugadi — javob qabul qilinmadi."
        group = self.team_of(user_id)
        if group is None:
            return None, "Siz ro‘yxatdan o‘tgan jamoa a’zosi emassiz."
        problem = group.submit(user_id, text, now)
        return group, problem

    def close(self, checker: Any, now: int = 0) -> dict:
        """Score the open question and remember what happened."""
        question = self.question
        if question is None:
            return {"asked": False, "winners": [], "finals": []}
        expected = str(question.get("answer", ""))
        winners: list[str] = []
        finals: list[dict] = []
        for group in self.teams:
            answer = group.final.strip()
            finals.append({"team": group.name, "answer": answer,
                           "by": int(group.final_by or 0),
                           "correct": bool(answer) and checker.is_correct(
                               int(question["id"]), expected, answer)})
            if finals[-1]["correct"]:
                group.score += 1
                group.solved.append(int(question["id"]))
                winners.append(group.name)
        self.asked += 1
        self.history.append({"id": int(question["id"]), "round": self.current_round,
                             "winners": winners, "finals": finals[:MAX_TEAMS],
                             "at": int(now or 0)})
        self.history = self.history[-60:]
        return {"asked": True, "winners": winners, "finals": finals,
                "answer": expected, "question_id": int(question["id"])}

    def advance(self, now: int = 0) -> dict | None:
        """Move to the next question; None means the tournament is over."""
        return self.open_question(self.index + 1, now)

    # ---------------------------------------------------------------- standings
    def standings(self) -> list[dict]:
        """The table, best first; equal scores keep registration order."""
        rows = [{"place": 0, "name": group.name, "score": int(group.score),
                 "size": group.size, "ready": group.ready_count,
                 "solved": len(group.solved), "attempts": int(group.attempts),
                 "captain": int(group.captain)} for group in self.teams]
        rows.sort(key=lambda row: (-row["score"], row["name"].lower()))
        for position, row in enumerate(rows, start=1):
            row["place"] = position
        return rows

    def leader(self) -> str:
        rows = self.standings()
        if not rows:
            return ""
        best = rows[0]["score"]
        names = [row["name"] for row in rows if row["score"] == best]
        return " · ".join(names[:3]) + (f" — {best}" if best else "")

    # ------------------------------------------------------------------- coding
    def dump(self) -> dict:
        """Compact JSON-ready dict: short keys, arrays, no duplicated text."""
        return {
            "v": 1, "c": int(self.chat_id), "t": self.title[:40], "p": int(self.package_id),
            "by": int(self.creator), "tm": int(self.timer), "r": int(self.rounds),
            "n": int(self.per_round), "s": int(self.seed), "ph": self.phase,
            "T": [group.dump() for group in self.teams],
            "in": [int(item) for item in self.include][-200:],
            "ex": [int(item) for item in self.exclude][-200:],
            "i": int(self.index), "q": int(self.question_id), "d": int(self.deadline),
            "k": int(self.control_message_id or 0), "o": 1 if self.open_mode else 0,
            "x": 1 if self.can_delete else 0, "h": self.history[-20:], "a": int(self.asked),
            "cd": int(self.card_message_id or 0), "cu": [int(x) for x in self.curators][:20],
            "pg": int(self.page), "qs": self.query[:24], "nm": int(self.naming),
            "dr": [list(row) for row in self.directory][-DIRECTORY_LIMIT:],
            "hn": [int(x) for x in self.hinted][-DIRECTORY_LIMIT:],
            "rv": int(self.rev), "pn": 1 if self.can_pin else 0,
            "sa": int(self.start_at), "mt": int(self.team_limit),
            "an": int(self.announced),
        }

    def body(self, level: int = 0) -> str:
        """The strip body: a prefix plus compact JSON.

        No base64: the invisible codec compresses the bytes itself, and the
        prefix is what tells a tournament strip from an ordinary game strip.

        ``level`` says how much of the soft state to give up so that everything
        still fits inside one 4096-character message. The roster is never
        dropped — a tournament without its teams is worthless — so the order is:
        the member directory, then the question history, then an over-long
        selection, and only then the members' display names.
        """
        data = self.dump()
        if level >= 1:
            data["dr"] = []
            data["hn"] = []
        if level >= 2:
            data["h"] = data["h"][-4:]
        if level >= 3:
            data["in"] = data["in"][-SELECTION_LIMIT:]
            data["ex"] = data["ex"][-SELECTION_LIMIT:]
        if level >= 4:
            data["h"] = []
            for row in data["T"]:
                row[2] = [[person[0], "", person[2], person[3]] for person in row[2]]
        return STATE_PREFIX + json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def load(cls, data: dict, questions: list[dict] | None = None) -> "Tournament":
        tour = cls(chat_id=int(data.get("c") or 0), title=str(data.get("t") or "Turnir"),
                   package_id=int(data.get("p") or 0), creator=int(data.get("by") or 0),
                   timer=int(data.get("tm") or 60), rounds=int(data.get("r") or 1),
                   per_round=int(data.get("n") or 0), seed=int(data.get("s") or 0),
                   phase=str(data.get("ph") or PHASE_REGISTER))
        tour.teams = [Team.load(row) for row in (data.get("T") or [])]
        tour.include = [int(item) for item in (data.get("in") or [])]
        tour.exclude = [int(item) for item in (data.get("ex") or [])]
        tour.index = int(data.get("i") or 0)
        tour.question_id = int(data.get("q") or 0)
        tour.deadline = int(data.get("d") or 0)
        tour.control_message_id = int(data.get("k") or 0) or None
        tour.open_mode = bool(data.get("o"))
        tour.can_delete = bool(data.get("x", True))
        tour.history = list(data.get("h") or [])
        tour.asked = int(data.get("a") or 0)
        tour.card_message_id = int(data.get("cd") or 0) or None
        tour.curators = [int(item) for item in (data.get("cu") or [])][:20]
        tour.page = max(1, int(data.get("pg") or 1))
        tour.query = str(data.get("qs") or "")[:24]
        tour.naming = int(data.get("nm") or 0)
        tour.directory = [[int(row[0]), str(row[1] or ""), str(row[2] or "")]
                          for row in (data.get("dr") or []) if isinstance(row, (list, tuple))
                          and len(row) >= 1][:DIRECTORY_LIMIT]
        tour.hinted = [int(item) for item in (data.get("hn") or [])][:DIRECTORY_LIMIT]
        tour.rev = int(data.get("rv") or 0)
        tour.can_pin = bool(data.get("pn"))
        tour.start_at = int(data.get("sa") or 0)
        tour.max_teams = int(data.get("mt") or MAX_TEAMS)
        tour.announced = int(data.get("an") or 0)
        tour.questions = list(questions or [])
        tour.finished = tour.phase == PHASE_DONE
        return tour

    @classmethod
    def from_body(cls, body: str, questions: list[dict] | None = None) -> "Tournament | None":
        """Parse a strip body back into a tournament, or None when it is not one."""
        if not str(body or "").startswith(STATE_PREFIX):
            return None
        try:
            data = json.loads(str(body)[len(STATE_PREFIX):])
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        tour = cls.load(data, questions)
        if tour.questions or tour.phase != PHASE_PLAY:
            return tour
        return tour if tour.question_id else None


def rebuild(tournament: Tournament, questions: list[dict]) -> Tournament:
    """Attach the real question texts to a tournament read back from a strip."""
    tournament.questions = tournament.playlist(questions)
    if tournament.phase == PHASE_PLAY and tournament.questions:
        tournament.index = max(0, min(tournament.index, len(tournament.questions) - 1))
        found = tournament.question_id
        position = next((index for index, item in enumerate(tournament.questions)
                         if int(item["id"]) == int(found)), tournament.index)
        tournament.index = position
        tournament.question_id = int(tournament.questions[position]["id"])
    return tournament


def when_text(stamp: int) -> str:
    """A planned start time in the copy the bot uses everywhere else."""
    return schedule.when_text(stamp)


def sizes() -> dict:
    """The registration rules, so the copy and the checks never drift apart."""
    return {"min_teams": MIN_TEAMS, "min_ready": MIN_READY, "min_members": MIN_MEMBERS,
            "max_members": MAX_MEMBERS, "max_teams": MAX_TEAMS}
