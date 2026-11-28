"""Tournaments: selection, registration, team-private answers, the final table.

Everything here runs against fakes — no network, no Telegram. The point of the
suite is the behaviour a real group depends on:

* only a curator (admin, owner or an appointed member) shapes the tournament;
* three teams, three to seven people each, two of them saying "Men tayyorman";
* exact questions are included or excluded, and rounds are tuned to that set;
* one final answer per team per question, changeable until time runs out;
* what a team writes stays inside the team;
* the countdown is readable in the strip, not in a separate window;
* the whole state fits inside one message and comes back unchanged.
"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    import bot.webhook as webhook
    from bot import hidden
    from bot import messages as msg
    from bot import settings as st
    from bot import stats
    from bot import tournament as tn
    from bot.archive import Archive
except ImportError:  # fastapi / python-telegram-bot not installed yet
    webhook = None


ADMIN = 1
OWNER = 8004724563
CHAT = -100200
PACKAGE = 959
ARCHIVE = Archive("data/all")


def visible(text: str) -> str:
    """What a reader sees: every zero-width character removed."""
    return "".join(char for char in str(text or "") if char not in hidden.INVISIBLE)


class TourBot:
    """A Telegram double that remembers messages, edits, deletions and pins."""

    def __init__(self, admins=(ADMIN,), can_delete=True, can_pin=True):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[int] = []
        self.photos: list[dict] = []
        self.pinned: dict[int, int] = {}
        self.members: dict[int, list] = {}
        self.admins = set(admins)
        self.can_delete = can_delete
        self.can_pin = can_pin
        self.next_id = 0
        self.chat_title = "Zakovat klubi"

    # ------------------------------------------------------------------ sending
    async def send_message(self, **kwargs):
        self.next_id += 1
        record = {"message_id": self.next_id, **kwargs}
        self.sent.append(record)
        return SimpleNamespace(message_id=self.next_id, text=kwargs.get("text"))

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return SimpleNamespace(message_id=0)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        for record in self.sent:
            if record["message_id"] == kwargs.get("message_id"):
                record["text"] = kwargs.get("text")
        return True

    async def delete_message(self, chat_id=None, message_id=None, **kwargs):
        if not self.can_delete:
            raise webhook.TelegramError("Bad Request: message can't be deleted")
        self.deleted.append(int(message_id))
        return True

    async def pin_chat_message(self, chat_id=None, message_id=None, **kwargs):
        if not self.can_pin:
            raise webhook.TelegramError("Bad Request: not enough rights")
        self.pinned[int(chat_id)] = int(message_id)
        return True

    async def get_chat(self, chat_id, **kwargs):
        message_id = self.pinned.get(int(chat_id))
        pinned = None
        if message_id:
            text = next((record["text"] for record in self.sent
                         if record["message_id"] == message_id), "")
            pinned = SimpleNamespace(message_id=message_id, text=text, caption=None)
        return SimpleNamespace(id=int(chat_id), type="supergroup", title=self.chat_title,
                               pinned_message=pinned)

    async def get_chat_member(self, chat_id, user_id, **kwargs):
        status = "administrator" if int(user_id) in self.admins else "member"
        return SimpleNamespace(status=status, can_delete_messages=self.can_delete)

    async def get_chat_administrators(self, chat_id, **kwargs):
        return [SimpleNamespace(user=SimpleNamespace(id=ADMIN, first_name="Admin",
                                                     full_name="Admin", username="admin"))]

    @property
    def id(self):
        return 1

    # --------------------------------------------------------------- inspection
    def texts(self, chat_id=None) -> list[str]:
        return [visible(record.get("text") or "") for record in self.sent
                if chat_id is None or record.get("chat_id") in (chat_id, None)]

    def group_texts(self) -> list[str]:
        return [visible(record.get("text") or "") for record in self.sent
                if record.get("chat_id") == CHAT]

    def private_texts(self, user_id: int) -> list[str]:
        return [visible(record.get("text") or "") for record in self.sent
                if record.get("chat_id") == int(user_id)]

    def edits_of(self, message_id: int) -> list[dict]:
        return [edit for edit in self.edits if edit.get("message_id") == int(message_id)]

    def card(self) -> str:
        message_id = self.pinned.get(CHAT)
        record = next((item for item in self.sent if item["message_id"] == message_id), None)
        return visible(record.get("text") or "") if record else ""

    def card_state(self, archive=None) -> "tn.Tournament | None":
        """The tournament as the pinned card holds it, with its questions attached."""
        message_id = self.pinned.get(CHAT)
        record = next((item for item in self.sent if item["message_id"] == message_id), None)
        if record is None:
            return None
        tour = webhook.unpack_tournament(record.get("text"))
        if tour is None:
            return None
        return webhook.tour_attach(archive or ARCHIVE, tour)


class TourMessage:
    def __init__(self, text="", user_id=2, reply_text=None, reply_id=0, message_id=500,
                 chat_id=CHAT, chat_type="supergroup"):
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=user_id, first_name=f"A’zo{user_id}",
                                         full_name=f"A’zo{user_id}", username=f"user{user_id}")
        self.reply_to_message = None
        if reply_text is not None:
            self.reply_to_message = SimpleNamespace(text=reply_text, caption=None,
                                                    message_id=reply_id)
        self.replies: list[str] = []
        self.chat_id = chat_id
        self.chat_type = chat_type

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def tour_update(message=None, callback=None, chat_id=CHAT, chat_type="supergroup",
                user_id=2, title="Zakovat klubi"):
    user = SimpleNamespace(id=user_id, first_name=f"A’zo{user_id}", full_name=f"A’zo{user_id}",
                           username=f"user{user_id}")
    return SimpleNamespace(
        effective_message=message, message=message, callback_query=callback,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title=title),
        effective_user=user,
    )


def auto_score(bot, archive) -> None:
    """Turn the confirmation step off — the switch lives in the curator's panel.

    Half of these tests describe the path where the bot's own verdict is published
    at once; the confirmation path has a class of its own (``AnswerReviewTests``).
    """
    tour = bot.card_state(archive)
    tour.needs_confirm = False
    tour.bump()
    asyncio.run(webhook.tour_persist(bot, archive, tour))


def tour_context(bot, archive, args=None):
    return SimpleNamespace(bot=bot, args=list(args or []),
                           application=SimpleNamespace(bot_data={"archive": archive}))


def callback_query(data: str, text: str, user_id=ADMIN, message_id=11, chat_id=CHAT):
    answers: list = []
    edits: list = []

    async def answer(text=None, show_alert=False, **kwargs):
        answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(text=None, **kwargs):
        edits.append({"text": text, **kwargs})

    return SimpleNamespace(
        data=data, id="cb1",
        message=SimpleNamespace(message_id=message_id, text=text, caption=None,
                                chat_id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=f"A’zo{user_id}",
                                  full_name=f"A’zo{user_id}", username=f"user{user_id}"),
        answer=answer, edit_message_text=edit_message_text,
        _answers=answers, _edits=edits,
    )


def questions(count: int, offset: int = 1) -> list[dict]:
    return [{"id": offset + index, "text": f"Savol matni {offset + index}",
             "answer": str(offset + index), "comment": ""} for index in range(count)]


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class TournamentRuleTests(unittest.TestCase):
    """The registration rules, without Telegram anywhere near them."""

    def setUp(self):
        self.pool = questions(24)
        self.tour = tn.Tournament(chat_id=CHAT, title="Kuzgi turnir", package_id=PACKAGE,
                                  creator=ADMIN, rounds=2, per_round=5, timer=60, seed=7)

    def _register(self, teams=3, members=3, ready=2, offset=0):
        """Register ``teams`` teams; ``offset`` keeps names and ids unique per call."""
        for index in range(teams):
            position = offset + index
            captain = (position + 1) * 100
            self.tour.add_team(f"Jamoa{position + 1}", captain, f"Kapitan{position + 1}")
            for extra in range(1, members):
                self.tour.join(position, captain + extra, f"A’zo{extra}",
                               ready=extra <= ready - 1)

    def test_three_teams_are_the_floor(self):
        self.assertEqual(tn.MIN_TEAMS, 3)
        self._register(teams=2)
        problems = self.tour.missing(len(self.pool))
        self.assertTrue(any("Kamida 3 ta jamoa" in item for item in problems))
        self.assertFalse(self.tour.ready_to_start(len(self.pool)))
        self._register(teams=1, offset=2)
        self.assertTrue(self.tour.ready_to_start(len(self.pool)))

    def test_every_team_needs_three_to_seven_people(self):
        self.assertEqual((tn.MIN_MEMBERS, tn.MAX_MEMBERS), (3, 7))
        self._register(teams=3, members=2, ready=2)
        problems = self.tour.missing(len(self.pool))
        self.assertTrue(any("kamida 3 kishi" in item for item in problems))
        team = self.tour.teams[0]
        for extra in range(10):
            team.add(9000 + extra, f"Ortiq{extra}")
        self.assertEqual(team.size, tn.MAX_MEMBERS)      # the eighth person is refused
        self.assertIn("to‘lgan", team.add(9999, "Yana"))

    def test_two_members_must_say_they_are_ready(self):
        self.assertEqual(tn.MIN_READY, 2)
        self._register(teams=3, members=4, ready=1)
        problems = self.tour.missing(len(self.pool))
        self.assertTrue(any("Men tayyorman" in item for item in problems))
        self.tour.set_ready(0, 101, True)
        problems = self.tour.missing(len(self.pool))
        self.assertFalse(any("Jamoa1" in item for item in problems))

    def test_nobody_joins_two_teams(self):
        self._register()
        team, problem = self.tour.add_team("To‘rtinchi", 101, "Kapitan")
        self.assertIsNone(team)
        self.assertIn("allaqachon", problem)
        joined, problem = self.tour.join(1, 101, "Kapitan")
        self.assertIn("boshqa jamoadasiz", problem)

    def test_the_captain_is_ready_by_definition(self):
        team, problem = self.tour.add_team("Bilimdonlar", 101, "Kapitan")
        self.assertEqual(problem, "")
        self.assertEqual(team.ready_count, 1)
        self.assertEqual(team.captain, 101)

    def test_leaving_drops_an_empty_team_and_moves_the_captain(self):
        self._register(teams=1, members=3, ready=3)
        team = self.tour.teams[0]
        self.assertEqual([person.user_id for person in team.members], [100, 101, 102])
        self.tour.leave(100)                       # the captain leaves
        self.assertEqual(team.captain, 101)        # the next member takes over
        self.tour.leave(101)
        self.tour.leave(102)
        self.assertEqual(self.tour.teams, [])      # an empty team is removed

    def test_include_and_exclude_pick_exact_questions(self):
        self.tour.include = [item["id"] for item in self.pool[:10]]
        self.assertEqual(len(self.tour.bank(self.pool)), 10)
        self.tour.exclude = [self.pool[0]["id"], self.pool[1]["id"]]
        bank = self.tour.bank(self.pool)
        self.assertEqual(len(bank), 8)
        self.assertNotIn(self.pool[0]["id"], {item["id"] for item in bank})

    def test_exclude_wins_over_include(self):
        self.tour.include = [1, 2, 3]
        self.tour.exclude = [3]
        self.assertEqual(sorted(item["id"] for item in self.tour.bank(self.pool)), [1, 2])

    def test_rounds_and_per_round_shape_the_playlist(self):
        self.tour.include = [item["id"] for item in self.pool[:10]]
        self.tour.rounds, self.tour.per_round = 2, 5
        playlist = self.tour.playlist(self.pool)
        self.assertEqual(len(playlist), 10)
        self.assertEqual(len({item["id"] for item in playlist}), 10)
        self.tour.rounds, self.tour.per_round = 3, 4      # 12 asked for, 10 exist
        played = self.tour.playlist(self.pool)
        self.assertEqual(len(played), 10)                  # nothing is repeated
        self.assertEqual(len({item["id"] for item in played}), 10)
        self.tour.per_round = 0                     # 0 = the whole bank, one round
        self.assertEqual(len(self.tour.playlist(self.pool)), 10)

    def test_the_same_seed_gives_the_same_order(self):
        first = self.tour.playlist(self.pool)
        again = tn.Tournament.from_body(self.tour.body()).playlist(self.pool)
        self.assertEqual([item["id"] for item in first], [item["id"] for item in again])
        self.tour.seed += 1
        self.assertNotEqual([item["id"] for item in self.tour.playlist(self.pool)],
                            [item["id"] for item in first])

    def test_an_empty_selection_refuses_to_start(self):
        self._register()
        self.tour.exclude = [item["id"] for item in self.pool]
        self.assertIn("bo‘sh", " ".join(self.tour.missing(0)))
        self.assertFalse(self.tour.ready_to_start(0))


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class FinalAnswerTests(unittest.TestCase):
    """One final answer per team, editable until the clock runs out."""

    def setUp(self):
        self.pool = questions(12)
        self.tour = tn.Tournament(chat_id=CHAT, title="Turnir", package_id=PACKAGE,
                                  creator=ADMIN, rounds=1, per_round=6, timer=60, seed=3)
        for index in range(3):
            captain = (index + 1) * 100
            self.tour.add_team(f"Jamoa{index + 1}", captain, f"Kapitan{index + 1}")
            for extra in range(1, 3):
                self.tour.join(index, captain + extra, f"A’zo{extra}", ready=True)
        self.tour.questions = self.tour.playlist(self.pool)
        self.tour.phase = tn.PHASE_PLAY
        self.tour.open_question(0, int(time.time()) + 60)
        self.tour.deadline = int(time.time()) + 60

    def _copy(self) -> "tn.Tournament":
        """What a second request sees: the same state, read back out of the card.

        A body carries no question texts, so the copy is rebuilt against the
        pool — exactly what ``tour_state`` does before a handler touches it.
        """
        return tn.rebuild(tn.Tournament.from_body(self.tour.body()), self.pool)

    def test_only_a_member_of_the_team_answers(self):
        team, problem = self.tour.submit_final(777, "javob", int(time.time()))
        self.assertIsNone(team)
        self.assertIn("emassiz", problem)

    def test_the_answer_can_be_changed_until_the_deadline(self):
        now = int(time.time())
        self.tour.submit_final(101, "birinchi", now)
        self.tour.submit_final(102, "ikkinchi", now + 1)
        self.tour.submit_final(101, "uchinchi", now + 2)
        self.assertEqual(self.tour.teams[0].final, "uchinchi")   # the last one counts
        self.assertEqual(self.tour.teams[0].attempts, 3)

    def test_a_late_answer_is_refused(self):
        late = int(self.tour.deadline) + 5
        team, problem = self.tour.submit_final(101, "kech", late)
        self.assertIn("Vaqt tugadi", problem)
        self.assertEqual(self.tour.teams[0].final, "")

    def test_several_teams_score_on_one_question(self):
        answer = str(self.tour.question["answer"])
        now = int(time.time())
        self.tour.submit_final(101, answer, now)
        self.tour.submit_final(201, answer.upper(), now)
        self.tour.submit_final(301, "butunlay boshqa", now)

        class Checker:
            def is_correct(self, question_id, expected, text):
                return str(text).strip().casefold() == str(expected).strip().casefold()

        result = self.tour.close(Checker(), now + 1)
        self.assertEqual(sorted(result["winners"]), ["Jamoa1", "Jamoa2"])
        self.assertEqual([team.score for team in self.tour.teams], [1, 1, 0])
        self.assertEqual(self.tour.asked, 1)
        self.assertEqual(self.tour.history[-1]["winners"], result["winners"])

    def test_a_team_that_stayed_silent_is_listed_as_such(self):
        class Checker:
            def is_correct(self, question_id, expected, text):
                return True

        self.tour.submit_final(101, "javob", int(time.time()))
        result = self.tour.close(Checker(), int(time.time()))
        silent = [row for row in result["finals"] if not row["answer"]]
        self.assertEqual(len(silent), 2)
        self.assertFalse(silent[0]["correct"])          # silence is never a point

    def test_an_answer_written_by_another_request_is_kept(self):
        """Serverless has no compare-and-swap, so the card is merged, not overwritten."""
        now = int(time.time())
        first, second = self._copy(), self._copy()
        first.submit_final(101, "birinchi", now)
        second.submit_final(201, "ikkinchi", now + 1)
        second.bump()
        first.bump()

        self.assertEqual(first.take_finals(second), 1)      # the newer answer is taken over
        self.assertEqual(first.teams[0].final, "birinchi")  # its own answer stays
        self.assertEqual(first.teams[1].final, "ikkinchi")

    def test_an_older_answer_never_wins_a_merge(self):
        now = int(time.time())
        first, second = self._copy(), self._copy()
        second.submit_final(101, "eski", now - 30)
        first.submit_final(101, "yangi", now)
        self.assertEqual(first.take_finals(second), 0)
        self.assertEqual(first.teams[0].final, "yangi")

    def test_a_merge_stops_at_the_question_border(self):
        now = int(time.time())
        first, second = self._copy(), self._copy()
        second.submit_final(201, "javob", now)
        first.advance(now + 60)                            # the first one moved on
        self.assertEqual(first.take_finals(second), 0)
        self.assertTrue(all(not team.final for team in first.teams))

    def test_a_timestamped_cancellation_beats_an_older_answer(self):
        now = int(time.time())
        card = self._copy()
        card.submit_final(101, "javob", now - 10)
        local = self._copy()
        local.teams[0].clear_final(now)                    # cancelled after it was written
        self.assertEqual(local.take_finals(card), 0)
        self.assertEqual(local.teams[0].final, "")
        late = self._copy()
        late.submit_final(101, "yana", now + 5)            # answered after the cancellation
        self.assertEqual(local.take_finals(late), 1)
        self.assertEqual(local.teams[0].final, "yana")

    def test_cancel_takes_the_answer_back(self):
        self.tour.submit_final(101, "javob", int(time.time()))
        self.tour.teams[0].clear_final()
        self.assertEqual(self.tour.teams[0].final, "")
        self.assertEqual(self.tour.teams[0].attempts, 1)   # the attempt is remembered

    def test_the_table_puts_the_best_team_first(self):
        self.tour.teams[0].score = 4
        self.tour.teams[1].score = 7
        self.tour.teams[2].score = 4
        rows = self.tour.standings()
        self.assertEqual([row["name"] for row in rows], ["Jamoa2", "Jamoa1", "Jamoa3"])
        self.assertEqual([row["place"] for row in rows], [1, 2, 3])
        self.assertIn("Jamoa2", self.tour.leader())


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class StateFittingTests(unittest.TestCase):
    """The tournament travels inside one message, invisibly."""

    def setUp(self):
        self.pool = questions(40)
        self.tour = tn.Tournament(chat_id=CHAT, title="Turnir", package_id=PACKAGE,
                                  creator=ADMIN, rounds=3, per_round=10, timer=60, seed=11)
        for index in range(6):
            captain = (index + 1) * 100
            self.tour.add_team(f"Juda uzun nomli jamoa {index + 1}", captain, f"Kapitan{index}")
            for extra in range(1, 7):
                self.tour.join(index, captain + extra, f"A’zo {extra} juda uzun",
                               f"username{index}{extra}", ready=True)
        self.tour.include = [item["id"] for item in self.pool]
        for person in range(80):
            self.tour.see(9000 + person, f"Kuzatuvchi {person}", f"watcher{person}")
        self.tour.questions = self.tour.playlist(self.pool)
        self.tour.phase = tn.PHASE_PLAY
        self.tour.open_question(0, int(time.time()) + 60)

    def test_the_body_survives_the_invisible_codec(self):
        run = hidden.encode(self.tour.body())
        self.assertEqual(hidden.density(run), "v3")        # compressed: it would not fit otherwise
        back = tn.Tournament.from_body(hidden.decode(run))
        self.assertEqual(len(back.teams), 6)
        self.assertEqual(back.teams[0].size, 7)
        self.assertEqual(back.include, self.tour.include)
        self.assertEqual(back.phase, tn.PHASE_PLAY)
        self.assertEqual(back.question_id, self.tour.question_id)

    def test_an_ordinary_game_state_still_uses_the_old_density(self):
        body = "g|959|0|1|0|1|1|1795000000|3|10|60|7|0|0|0|1"
        self.assertEqual(hidden.density(hidden.encode(body)), "v2")
        self.assertEqual(hidden.decode(hidden.encode(body)), body)

    def test_a_tampered_run_is_not_state(self):
        run = list(hidden.encode(self.tour.body()))
        run[10] = hidden.ZW0 if run[10] == hidden.ZW1 else hidden.ZW1
        self.assertIsNone(hidden.decode("".join(run)))

    def test_a_full_tournament_fits_inside_one_message(self):
        text = webhook.tour_state_text("X" * 3000, self.tour)
        self.assertLessEqual(webhook.utf16_length(text), 4096)
        body = hidden.decode(text)
        self.assertTrue(str(body).startswith(tn.STATE_PREFIX))
        back = tn.Tournament.from_body(body)
        self.assertEqual(len(back.teams), 6)              # the roster always survives
        self.assertEqual(back.teams[0].size, 7)
        self.assertEqual(back.question_id, self.tour.question_id)
        self.assertEqual(visible(text).count("<b>") % 2, 0)   # no tag is cut in half

    def test_the_directory_is_the_first_thing_given_up(self):
        small = tn.Tournament.from_body(self.tour.body(1))
        self.assertEqual(small.directory, [])
        self.assertEqual(len(small.teams), 6)
        full = tn.Tournament.from_body(self.tour.body(0))
        self.assertEqual(len(full.directory), 80)

    def test_the_username_directory_resolves_people(self):
        self.tour.see(4242, "Begona", "begona")
        self.assertEqual(self.tour.find("@begona"), 4242)
        self.assertEqual(self.tour.find("begona"), 4242)
        self.assertEqual(self.tour.find("4242"), 4242)
        self.assertEqual(self.tour.find("username01"), 101)   # a team member is found too
        self.assertEqual(self.tour.find("yo‘q"), 0)

    def test_a_revision_counts_every_change(self):
        first = self.tour.rev
        self.tour.bump()
        self.assertEqual(self.tour.rev, first + 1)
        back = tn.Tournament.from_body(self.tour.body())
        self.assertEqual(back.rev, self.tour.rev)


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class CuratorFlowTests(unittest.TestCase):
    """/turnir, the picker and the curator lock, end to end against fakes."""

    def setUp(self):
        stats.reset()
        self.archive = Archive("data/all")
        self.bot = TourBot()
        self.context = tour_context(self.bot, self.archive)
        webhook._tour_probe.clear()
        self._patches = [
            mock.patch.object(webhook, "TICKS_ENABLED", False),
            mock.patch.object(stats, "backend", lambda: "lokal"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        stats.reset()
        webhook._tour_probe.clear()

    def _open(self, argument=str(PACKAGE)):
        message = TourMessage("/turnir", user_id=ADMIN)
        update = tour_update(message, user_id=ADMIN)
        self.context.args = argument.split() if argument else []
        asyncio.run(webhook.cmd_tour(update, self.context))
        self.context.args = []
        return self.bot.card_state()

    def test_a_stranger_cannot_open_a_tournament(self):
        message = TourMessage("/turnir", user_id=555)
        update = tour_update(message, user_id=555)
        asyncio.run(webhook.cmd_tour(update, self.context))
        self.assertTrue(any("faqat guruh admini" in visible(reply) for reply in message.replies))
        self.assertIsNone(self.bot.card_state())

    def test_the_admin_opens_a_pinned_card(self):
        tour = self._open()
        self.assertIsNotNone(tour)
        self.assertEqual(tour.phase, tn.PHASE_REGISTER)
        self.assertEqual(tour.creator, ADMIN)
        self.assertIn(CHAT, self.bot.pinned)             # the card is the durable copy
        card = self.bot.card()
        self.assertIn("TURNIR", card)
        self.assertIn("Kamida 3 ta jamoa", card)
        self.assertIn("Men tayyorman", card)
        self.assertNotIn("#ZS", card)                     # no readable state anywhere

    def test_the_rules_and_the_help_are_available(self):
        message = TourMessage("/turnir qoidalar", user_id=ADMIN)
        update = tour_update(message, user_id=ADMIN)
        self.context.args = ["qoidalar"]
        asyncio.run(webhook.cmd_tour(update, self.context))
        self.assertIn("Kamida 3 ta jamoa", visible(message.replies[0]))

    def test_include_and_exclude_by_id_change_the_bank(self):
        tour = self._open(str(PACKAGE))
        first, second, third = (str(int(item["id"]))
                                for item in webhook.tour_bank(self.archive, tour)[:3])
        before = len(webhook.tour_bank(self.archive, tour))
        message = TourMessage("/chiqarish", user_id=ADMIN, reply_text=self.bot.card())
        self.context.args = [first, second, third]
        asyncio.run(webhook.cmd_exclude(tour_update(message, user_id=ADMIN), self.context))
        after = self.bot.card_state()
        self.assertEqual(after.exclude, [int(first), int(second), int(third)])
        self.assertEqual(len(webhook.tour_bank(self.archive, after)), before - 3)
        self.assertIn("YANGILANDI", visible(message.replies[0]).upper())
        self.assertNotIn("Topilmadi", visible(message.replies[0]))

        self.context.args = [second]
        asyncio.run(webhook.cmd_include(tour_update(message, user_id=ADMIN), self.context))
        restored = self.bot.card_state()
        self.assertEqual(restored.exclude, [int(first), int(third)])
        self.assertIn(int(second), restored.include)   # taking it back is an inclusion

    def test_a_stranger_cannot_touch_the_selection(self):
        tour = self._open(str(PACKAGE))
        question_id = str(int(webhook.tour_bank(self.archive, tour)[0]["id"]))
        message = TourMessage(f"/olish {question_id}", user_id=555, reply_text=self.bot.card())
        self.context.args = [question_id]
        asyncio.run(webhook.cmd_include(tour_update(message, user_id=555), self.context))
        self.assertIn("faqat guruh admini", visible(message.replies[0]))
        self.assertEqual(self.bot.card_state().include, [])

    def test_the_picker_lists_questions_and_marks_decisions(self):
        tour = self._open(str(PACKAGE))
        message = TourMessage("/tanlov", user_id=ADMIN, reply_text=self.bot.card())
        self.context.args = []
        asyncio.run(webhook.cmd_selection(tour_update(message, user_id=ADMIN), self.context))
        picker = next(record for record in self.bot.sent
                      if "SAVOL TANLASH" in visible(record.get("text") or ""))
        shown = visible(picker["text"])
        self.assertIn("SAVOL TANLASH", shown)
        rows = picker["reply_markup"].inline_keyboard
        self.assertEqual(len(rows[0]), 1)
        first = rows[0][0]
        self.assertTrue(first.callback_data.startswith(st.PREFIX_TOUR + "|i|"))

        # one press includes the question, the next one takes it back out
        question_id = int(first.callback_data.split("|")[-1])
        press = callback_query(st.curator_data("i", question_id), picker["text"], user_id=ADMIN,
                               message_id=picker["message_id"])
        asyncio.run(webhook.on_curator_button(
            tour_update(callback=press, user_id=ADMIN), self.context))
        self.assertIn(question_id, self.bot.card_state().include)

        picker_text = press._edits[-1]["text"]             # the query redraws its own message
        self.assertIn("OLINDI", visible(picker_text))          # the row is marked at once
        press_again = callback_query(st.curator_data("e", question_id), picker_text,
                                     user_id=ADMIN, message_id=picker["message_id"])
        asyncio.run(webhook.on_curator_button(
            tour_update(callback=press_again, user_id=ADMIN), self.context))
        state = self.bot.card_state()
        self.assertNotIn(question_id, state.include)
        self.assertIn(question_id, state.exclude)

    def test_the_curator_panel_tunes_rounds_questions_and_time(self):
        self._open(str(PACKAGE))
        card_id = self.bot.pinned[CHAT]
        press = callback_query(st.curator_data("c"), self.bot.sent[card_id - 1]["text"],
                               user_id=ADMIN, message_id=card_id)
        asyncio.run(webhook.on_curator_button(
            tour_update(callback=press, user_id=ADMIN), self.context))
        self.assertIn("RO‘YXATDAN O‘TISH", visible(self.bot.card()))   # the card is untouched
        panel = next(record for record in self.bot.sent
                     if "TURNIR SOZLAMALARI" in visible(record.get("text") or ""))
        self.assertNotEqual(panel["message_id"], card_id)

        panel_text = visible(panel["text"])
        for action, value in (("r", 5), ("q", 15), ("t", 90)):
            press = callback_query(st.curator_data(action, value), panel_text,
                                   user_id=ADMIN, message_id=panel["message_id"])
            asyncio.run(webhook.on_curator_button(
                tour_update(callback=press, user_id=ADMIN), self.context))
            panel_text = visible(press._edits[-1]["text"])
        tour = self.bot.card_state()
        self.assertEqual((tour.rounds, tour.per_round, tour.timer), (5, 15, 90))
        bank = webhook.tour_bank_size(self.archive, tour)
        self.assertEqual(webhook.tour_total(self.archive, tour), min(75, bank))
        self.assertIn("5 raund", panel_text)
        if 75 > bank:
            self.assertIn("Diqqat", panel_text)            # more was asked for than exists

    def test_an_appointed_curator_may_run_the_tournament(self):
        tour = self._open(str(PACKAGE))
        message = TourMessage("/kurator 555", user_id=ADMIN, reply_text=self.bot.card())
        self.context.args = ["555"]
        asyncio.run(webhook.cmd_curator(tour_update(message, user_id=ADMIN), self.context))
        self.assertIn(555, self.bot.card_state().curators)
        self.assertIn("KURATOR TAYINLANDI", visible(message.replies[0]))

        # the appointed member now changes the selection, though not an admin
        bank = [str(int(item["id"])) for item in webhook.tour_bank(self.archive, tour)]
        picked = TourMessage(f"/olish {bank[0]}", user_id=555, reply_text=self.bot.card())
        self.context.args = [bank[0]]
        asyncio.run(webhook.cmd_include(tour_update(picked, user_id=555), self.context))
        self.assertIn(int(bank[0]), self.bot.card_state().include)

        # and a plain member still may not
        refused = TourMessage(f"/olish {bank[1]}", user_id=999, reply_text=self.bot.card())
        self.context.args = [bank[1]]
        asyncio.run(webhook.cmd_include(tour_update(refused, user_id=999), self.context))
        self.assertNotIn(int(bank[1]), self.bot.card_state().include)

        # taking the appointment away closes it again
        self.context.args = ["555", "olib"]
        asyncio.run(webhook.cmd_curator(tour_update(message, user_id=ADMIN), self.context))
        self.assertNotIn(555, self.bot.card_state().curators)
        self.context.args = [bank[2]]
        asyncio.run(webhook.cmd_include(tour_update(picked, user_id=555), self.context))
        self.assertNotIn(int(bank[2]), self.bot.card_state().include)

    def test_only_an_admin_appoints(self):
        self._open(str(PACKAGE))
        message = TourMessage("/kurator 555", user_id=555, reply_text=self.bot.card())
        self.context.args = ["555"]
        asyncio.run(webhook.cmd_curator(tour_update(message, user_id=555), self.context))
        self.assertEqual(self.bot.card_state().curators, [])
        self.assertIn("admin", visible(message.replies[0]).lower())


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class RegistrationFlowTests(unittest.TestCase):
    """Three teams, real people, "Men tayyorman", then the first question."""

    def setUp(self):
        stats.reset()
        self.archive = Archive("data/all")
        self.bot = TourBot()
        self.context = tour_context(self.bot, self.archive)
        webhook._tour_probe.clear()
        self._patches = [
            mock.patch.object(webhook, "TICKS_ENABLED", False),
            mock.patch.object(stats, "backend", lambda: "lokal"),
        ]
        for patch in self._patches:
            patch.start()
        message = TourMessage("/turnir", user_id=ADMIN)
        self.context.args = [str(PACKAGE)]
        asyncio.run(webhook.cmd_tour(tour_update(message, user_id=ADMIN), self.context))
        self.context.args = []

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        stats.reset()
        webhook._tour_probe.clear()

    def _card_text(self) -> str:
        return self.bot.sent[self.bot.pinned[CHAT] - 1]["text"]

    def _register_three_teams(self):
        for index, name in enumerate(("Bilimdonlar", "Zakovat", "Intellekt")):
            captain = 101 + index * 10
            message = TourMessage(f"/jamoa {name}", user_id=captain,
                                  reply_text=self._card_text())
            self.context.args = [name]
            asyncio.run(webhook.cmd_team(tour_update(message, user_id=captain), self.context))
            for extra in range(1, 3):
                member = 101 + index * 10 + extra
                join = TourMessage("/qoshil", user_id=member, reply_text=self._card_text())
                self.context.args = [name]
                asyncio.run(webhook.cmd_join(tour_update(join, user_id=member), self.context))
                ready = TourMessage("/tayyor", user_id=member, reply_text=self._card_text())
                asyncio.run(webhook.cmd_ready(tour_update(ready, user_id=member), self.context))

    def test_a_team_is_created_with_its_captain(self):
        message = TourMessage("/jamoa Bilimdonlar", user_id=101, reply_text=self._card_text())
        self.context.args = ["Bilimdonlar"]
        asyncio.run(webhook.cmd_team(tour_update(message, user_id=101), self.context))
        tour = self.bot.card_state()
        self.assertEqual([team.name for team in tour.teams], ["Bilimdonlar"])
        self.assertEqual(tour.teams[0].captain, 101)
        self.assertEqual(tour.teams[0].ready_count, 1)
        self.assertIn("JAMOA RO‘YXATDAN O‘TDI", visible(message.replies[0]))
        self.assertIn("Bilimdonlar", self.bot.card())

    def test_the_same_name_twice_is_refused(self):
        self.context.args = ["Bilimdonlar"]
        asyncio.run(webhook.cmd_team(
            tour_update(TourMessage(user_id=101, reply_text=self._card_text()), user_id=101),
            self.context))
        message = TourMessage("/jamoa Bilimdonlar", user_id=202, reply_text=self._card_text())
        asyncio.run(webhook.cmd_team(tour_update(message, user_id=202), self.context))
        self.assertIn("Bunday nomli jamoa bor", message.replies[0])
        self.assertEqual(len(self.bot.card_state().teams), 1)

    def test_the_start_button_explains_what_is_missing(self):
        self._register_three_teams()
        tour = self.bot.card_state()
        self.assertEqual(len(tour.teams), 3)
        self.assertEqual(tour.people, 9)
        self.assertTrue(all(team.is_ready for team in tour.teams))

        # one team short of the rules: the button says exactly what is missing
        short = tn.Tournament.from_body(tour.body())
        short.teams[2].members = short.teams[2].members[:1]
        press = callback_query(st.tour_data(st.TEAM_START),
                               webhook.tour_state_text("karta", short), user_id=ADMIN)
        with mock.patch.object(webhook, "tour_state", mock.AsyncMock(return_value=short)):
            asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=ADMIN),
                                               self.context))
        self.assertTrue(press._answers[0]["show_alert"])

    def test_starting_opens_the_first_question_with_a_visible_clock(self):
        self._register_three_teams()
        press = callback_query(st.tour_data(st.TEAM_START), self._card_text(), user_id=ADMIN)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=ADMIN),
                                           self.context))
        tour = self.bot.card_state()
        self.assertEqual(tour.phase, tn.PHASE_PLAY)
        group = "\n".join(self.bot.group_texts())
        self.assertIn("TURNIR BOSHLANDI", group)
        self.assertIn("SAVOL 1 /", group)
        self.assertIn("QOLGAN VAQT", group)
        self.assertIn("▰", group)
        # the strip carries the clock and nothing readable about the answer
        strip = next(record for record in self.bot.sent
                     if "SAVOL 1 /" in visible(record.get("text") or "")
                     and "Yakuniy javob bergan" in visible(record.get("text") or ""))
        self.assertIn("<b>01:00</b>", visible(strip["text"]))
        self.assertIsNotNone(webhook.unpack_tournament(strip["text"]))
        self.assertIsNone(webhook.unpack_state(strip["text"]))   # it is not an ordinary game

    def test_starting_is_refused_while_a_rule_is_unmet(self):
        self._register_three_teams()
        tour = self.bot.card_state()
        tour.teams[0].members[0].ready = False
        tour.teams[0].members[1].ready = False
        text = webhook.tour_state_text("karta", tour)
        press = callback_query(st.tour_data(st.TEAM_START), text, user_id=ADMIN)
        with mock.patch.object(webhook, "tour_state", mock.AsyncMock(return_value=tour)):
            asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=ADMIN),
                                               self.context))
        self.assertTrue(press._answers[0]["show_alert"])
        self.assertEqual(self.bot.card_state().phase, tn.PHASE_REGISTER)
        self.assertIn("Men tayyorman", "\n".join(self.bot.group_texts()))

    def test_a_member_cannot_start_the_tournament(self):
        self._register_three_teams()
        press = callback_query(st.tour_data(st.TEAM_START), self._card_text(), user_id=102)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=102),
                                           self.context))
        self.assertEqual(self.bot.card_state().phase, tn.PHASE_REGISTER)
        self.assertTrue(press._answers[0]["show_alert"])

    def test_the_join_button_adds_and_the_ready_button_marks(self):
        message = TourMessage("/jamoa Bilimdonlar", user_id=101, reply_text=self._card_text())
        self.context.args = ["Bilimdonlar"]
        asyncio.run(webhook.cmd_team(tour_update(message, user_id=101), self.context))
        press = callback_query(st.tour_data(st.TEAM_JOIN, 0), self._card_text(), user_id=102)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=102),
                                           self.context))
        self.assertEqual(self.bot.card_state().teams[0].size, 2)
        self.assertEqual(self.bot.card_state().teams[0].ready_count, 1)

        press = callback_query(st.tour_data(st.TEAM_READY), self._card_text(), user_id=102)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=102),
                                           self.context))
        self.assertEqual(self.bot.card_state().teams[0].ready_count, 2)
        self.assertIn("MEN TAYYORMAN", "\n".join(self.bot.group_texts()).upper())

    def test_a_full_team_refuses_another_member(self):
        message = TourMessage("/jamoa Bilimdonlar", user_id=101, reply_text=self._card_text())
        self.context.args = ["Bilimdonlar"]
        asyncio.run(webhook.cmd_team(tour_update(message, user_id=101), self.context))
        for extra in range(2, 9):
            join = TourMessage("/qoshil Bilimdonlar", user_id=100 + extra,
                               reply_text=self._card_text())
            self.context.args = ["Bilimdonlar"]
            asyncio.run(webhook.cmd_join(
                tour_update(join, user_id=100 + extra), self.context))
            self.context.args = []
        self.assertEqual(self.bot.card_state().teams[0].size, tn.MAX_MEMBERS)
        self.assertIn("to‘lgan", join.replies[-1])


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class PlayingFlowTests(unittest.TestCase):
    """Finals, privacy, scoring and the closing table."""

    def setUp(self):
        stats.reset()
        self.archive = Archive("data/all")
        self.bot = TourBot()
        self.context = tour_context(self.bot, self.archive)
        webhook._tour_probe.clear()
        self.application = SimpleNamespace(bot=self.bot, bot_data={"archive": self.archive},
                                           initialize=mock.AsyncMock(),
                                           shutdown=mock.AsyncMock())
        self._patches = [
            mock.patch.object(webhook, "TICKS_ENABLED", False),
            mock.patch.object(stats, "backend", lambda: "lokal"),
        ]
        for patch in self._patches:
            patch.start()
        first = TourMessage("/turnir", user_id=ADMIN)
        self.context.args = [str(PACKAGE)]
        asyncio.run(webhook.cmd_tour(tour_update(first, user_id=ADMIN), self.context))
        self.context.args = []
        self.people = {}
        for index, name in enumerate(("Bilimdonlar", "Zakovat", "Intellekt")):
            captain = 101 + index * 10
            self.people[name] = [captain, captain + 1, captain + 2]
            message = TourMessage(f"/jamoa {name}", user_id=captain, reply_text=None)
            self.context.args = [name]
            asyncio.run(webhook.cmd_team(tour_update(message, user_id=captain), self.context))
            for member in self.people[name][1:]:
                join = TourMessage("/qoshil", user_id=member)
                self.context.args = [name]
                asyncio.run(webhook.cmd_join(tour_update(join, user_id=member), self.context))
                ready = TourMessage("/tayyor", user_id=member)
                asyncio.run(webhook.cmd_ready(tour_update(ready, user_id=member), self.context))
        self.context.args = []
        press = callback_query(st.tour_data(st.TEAM_START), self._card_text(), user_id=ADMIN)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=ADMIN),
                                           self.context))
        auto_score(self.bot, self.archive)
        self.sent_before = len(self.bot.sent)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        stats.reset()
        webhook._tour_probe.clear()

    def _card_text(self) -> str:
        return self.bot.sent[self.bot.pinned[CHAT] - 1]["text"]

    def _strip_text(self) -> str:
        strips = [record for record in self.bot.sent
                  if "Yakuniy javob bergan" in visible(record.get("text") or "")]
        return strips[-1]["text"]

    def _answer_of(self, tour) -> str:
        question = self.archive.question(tour.question_id)
        return str(question.get("answer") or "")

    def test_the_question_is_public_and_explains_the_final_answer(self):
        group = "\n".join(self.bot.group_texts())
        self.assertIn("SAVOL 1 /", group)
        self.assertIn("/yakuniy", group)
        self.assertIn("bitta yakuniy javob", group)
        # the question is not spammed to nine private chats: only answers are private
        self.assertEqual(self.bot.private_texts(self.people["Bilimdonlar"][0]), [])

    def test_a_final_answer_is_taken_out_of_the_group(self):
        tour = self.bot.card_state()
        member = self.people["Bilimdonlar"][1]
        message = TourMessage(f"/yakuniy {self._answer_of(tour)}", user_id=member,
                              reply_text=self._strip_text())
        self.context.args = [self._answer_of(tour)]
        asyncio.run(webhook.cmd_final(tour_update(message, user_id=member), self.context))
        self.assertIn(message.message_id, self.bot.deleted)     # the group never reads it
        group = "\n".join(self.bot.group_texts())
        self.assertNotIn(self._answer_of(tour), group)
        state = self.bot.card_state()
        self.assertEqual(state.teams[0].final, self._answer_of(tour))
        private = "\n".join(self.bot.private_texts(self.people["Bilimdonlar"][0]))
        self.assertIn("YAKUNIY JAVOB", private)                 # the team sees it
        self.assertIn(self._answer_of(tour), private)
        other = "\n".join(self.bot.private_texts(self.people["Zakovat"][0]))
        self.assertNotIn(self._answer_of(tour), other)          # the other teams do not

    def test_the_answer_can_be_changed_until_time_runs_out(self):
        tour = self.bot.card_state()
        member = self.people["Zakovat"][0]
        answer = self._answer_of(tour)
        for text in ("birinchi taxmin", "ikkinchi taxmin", answer):
            message = TourMessage(f"/yakuniy {text}", user_id=member,
                                  reply_text=self._strip_text())
            self.context.args = text.split()
            asyncio.run(webhook.cmd_final(tour_update(message, user_id=member), self.context))
        self.assertEqual(self.bot.card_state().teams[1].final, answer)
        private = "\n".join(self.bot.private_texts(member))
        self.assertIn("O‘ZGARTIRILDI", private)

    def test_a_final_answer_after_the_deadline_is_refused(self):
        tour = self.bot.card_state()
        tour.deadline = int(time.time()) - 5
        text = webhook.tour_state_text("tasma", tour)
        member = self.people["Intellekt"][0]
        message = TourMessage("/yakuniy kech", user_id=member, reply_text=text)
        self.context.args = ["kech"]
        with mock.patch.object(webhook, "tour_state", mock.AsyncMock(return_value=tour)):
            asyncio.run(webhook.cmd_final(tour_update(message, user_id=member), self.context))
        self.assertIn("vaqt tugadi", message.replies[0])
        self.assertEqual(self.bot.card_state().teams[2].final, "")

    def test_a_non_member_cannot_answer_for_a_team(self):
        member = 424242
        message = TourMessage("/yakuniy javob", user_id=member, reply_text=self._strip_text())
        self.context.args = ["javob"]
        asyncio.run(webhook.cmd_final(tour_update(message, user_id=member), self.context))
        self.assertIn("emassiz", message.replies[0])

    def test_bekor_takes_the_final_answer_back(self):
        tour = self.bot.card_state()
        member = self.people["Bilimdonlar"][0]
        message = TourMessage("/yakuniy javob", user_id=member, reply_text=self._strip_text())
        self.context.args = ["javob"]
        asyncio.run(webhook.cmd_final(tour_update(message, user_id=member), self.context))
        self.assertEqual(self.bot.card_state().teams[0].final, "javob")
        cancel = TourMessage("/bekor", user_id=member, reply_text=self._strip_text())
        asyncio.run(webhook.cmd_final_cancel(tour_update(cancel, user_id=member), self.context))
        self.assertEqual(self.bot.card_state().teams[0].final, "")

    def test_closing_a_question_scores_every_team_and_prints_the_table(self):
        tour = self.bot.card_state()
        answer = self._answer_of(tour)
        for name in ("Bilimdonlar", "Zakovat"):
            member = self.people[name][0]
            message = TourMessage(f"/yakuniy {answer}", user_id=member,
                                  reply_text=self._strip_text())
            self.context.args = [answer]
            asyncio.run(webhook.cmd_final(tour_update(message, user_id=member), self.context))
        member = self.people["Intellekt"][0]
        wrong = TourMessage("/yakuniy butunlay boshqa javob", user_id=member,
                            reply_text=self._strip_text())
        self.context.args = ["butunlay", "boshqa", "javob"]
        asyncio.run(webhook.cmd_final(tour_update(wrong, user_id=member), self.context))
        self.context.args = []

        state = self.bot.card_state()
        state.control_message_id = next(
            record["message_id"] for record in self.bot.sent
            if "Yakuniy javob bergan" in visible(record.get("text") or ""))
        asyncio.run(webhook.tour_resolve(self.bot, self.archive, CHAT, state))
        group = "\n".join(self.bot.group_texts())
        self.assertIn("YAKUNI", group)
        self.assertIn("Ochko oldi", group)
        self.assertIn("YAKUNIY JAVOBLAR", group)
        self.assertIn("to‘g‘ri", group)
        self.assertIn("noto‘g‘ri", group)
        self.assertIn("HISOB", group)
        after = webhook.unpack_tournament(
            self.bot.sent[self.bot.pinned[CHAT] - 1]["text"])
        self.assertEqual([team.score for team in after.teams], [1, 1, 0])
        self.assertEqual(after.number, 2)                 # the next question is already open
        self.assertIn("SAVOL 2 /", group)

    def test_the_last_question_ends_with_a_full_table(self):
        tour = self.bot.card_state(self.archive)
        tour.index = tour.total - 1
        tour.open_question(tour.total - 1, int(time.time()) + 60)
        tour.teams[1].score = 5
        tour.teams[0].score = 3
        tour.control_message_id = next(
            record["message_id"] for record in self.bot.sent
            if "Yakuniy javob bergan" in visible(record.get("text") or ""))
        asyncio.run(webhook.tour_resolve(self.bot, self.archive, CHAT, tour))
        group = "\n".join(self.bot.group_texts())
        self.assertIn("TURNIR YAKUNLANDI", group)
        self.assertIn("YAKUNIY JADVAL", group)
        self.assertIn("G‘olib", group)
        self.assertIn("Zakovat", group)
        final = webhook.unpack_tournament(self.bot.sent[self.bot.pinned[CHAT] - 1]["text"])
        self.assertEqual(final.phase, tn.PHASE_DONE)
        self.assertTrue(final.finished)

    def test_the_table_is_available_at_any_moment(self):
        message = TourMessage("/jadval", user_id=self.people["Bilimdonlar"][2])
        asyncio.run(webhook.cmd_table(tour_update(message, user_id=message.from_user.id),
                                      self.context))
        self.assertIn("JADVAL", visible(message.replies[0]))
        for name in self.people:
            self.assertIn(name, visible(message.replies[0]))

    def test_a_plain_message_stays_inside_the_team(self):
        member = self.people["Bilimdonlar"][1]
        teammate = self.people["Bilimdonlar"][2]
        rival = self.people["Zakovat"][0]
        message = TourMessage("Bu javob bo‘lishi mumkin", user_id=member)
        message.message_id = 900
        with mock.patch.object(webhook, "tour_active", mock.AsyncMock(return_value=True)):
            handled = asyncio.run(webhook.tour_group_text(
                tour_update(message, user_id=member), self.context))
        self.assertTrue(handled)
        self.assertIn(900, self.bot.deleted)                       # not readable in the group
        self.assertIn("Bu javob bo‘lishi mumkin", "\n".join(self.bot.private_texts(teammate)))
        self.assertNotIn("Bu javob bo‘lishi mumkin",
                         "\n".join(self.bot.private_texts(rival)))
        self.assertNotIn("Bu javob bo‘lishi mumkin", "\n".join(self.bot.group_texts()))

    def test_two_finals_at_once_are_both_kept(self):
        """Two members answer in the same second, in two serverless requests."""
        tour = self.bot.card_state()
        answer = str(self.archive.question(tour.question_id).get("answer"))
        stale = webhook.tour_attach(self.archive,          # read before the first write
                                    tn.Tournament.from_body(tour.body()))

        first = TourMessage(f"/yakuniy {answer}", user_id=101, message_id=910,
                            reply_text=self.bot.sent[tour.control_message_id - 1]["text"],
                            reply_id=tour.control_message_id)
        self.context.args = [answer]
        asyncio.run(webhook.cmd_final(tour_update(first, user_id=101), self.context))

        stale.submit_final(111, answer, int(time.time()))  # the second request writes now
        stale.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, stale, merge_finals=True))

        finals = {team.name: team.final for team in self.bot.card_state().teams}
        self.assertEqual(sum(1 for value in finals.values() if value), 2)   # neither is lost

        # and the question still scores both of them
        tour = self.bot.card_state()
        tour.deadline = int(time.time()) - 1
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            asyncio.run(webhook.tick_work(self.bot.card_state().body(), CHAT))
        group = "\n".join(self.bot.group_texts())
        self.assertIn("Ochko oldi", group)
        scores = {team.name: team.score for team in self.bot.card_state().teams}
        self.assertEqual(sum(scores.values()), 2)

    def test_a_whisper_reaches_one_teammate_only(self):
        member = self.people["Zakovat"][0]
        mate = self.people["Zakovat"][1]
        message = TourMessage(f"/pichoq {mate} maxfiy gap", user_id=member)
        message.message_id = 901
        self.context.args = [str(mate), "maxfiy", "gap"]
        asyncio.run(webhook.cmd_whisper(tour_update(message, user_id=member), self.context))
        self.assertIn(901, self.bot.deleted)               # the group copy is gone
        self.assertIn("maxfiy gap", "\n".join(self.bot.private_texts(mate)))
        self.assertIn("MAXFIY XABAR", "\n".join(self.bot.private_texts(mate)))
        for name, members in self.people.items():
            for person in members:
                if person not in (mate, member):
                    self.assertNotIn("maxfiy gap", "\n".join(self.bot.private_texts(person)))
        self.assertNotIn("maxfiy gap", "\n".join(self.bot.group_texts()))
        self.assertEqual(message.replies, [])              # nothing is quoted back to the group

    def test_a_plain_whisper_goes_to_the_whole_team(self):
        """/pichoq matn — no address at all, so the team is the audience."""
        member = self.people["Bilimdonlar"][0]
        message = TourMessage("/pichoq tez javob bering", user_id=member, message_id=905)
        self.context.args = ["tez", "javob", "bering"]
        asyncio.run(webhook.cmd_whisper(tour_update(message, user_id=member), self.context))
        for mate in self.people["Bilimdonlar"][1:]:
            self.assertIn("tez javob bering", "\n".join(self.bot.private_texts(mate)))
        for rival in self.people["Zakovat"]:
            self.assertEqual(self.bot.private_texts(rival), [])
        self.assertNotIn("tez javob", "\n".join(self.bot.group_texts()))

    def test_a_whisper_never_leaves_the_team(self):
        """A rival's id is refused, not delivered — and the group sees nothing."""
        member = self.people["Zakovat"][0]
        rival = self.people["Bilimdonlar"][0]
        message = TourMessage(f"/pichoq {rival} maxfiy gap", user_id=member, message_id=902)
        self.context.args = [str(rival), "maxfiy", "gap"]
        asyncio.run(webhook.cmd_whisper(tour_update(message, user_id=member), self.context))
        self.assertEqual(self.bot.private_texts(rival), [])
        self.assertIn("boshqa jamoada", "\n".join(self.bot.private_texts(member)))
        self.assertIn(902, self.bot.deleted)               # the group copy still goes
        self.assertNotIn("maxfiy gap", "\n".join(self.bot.group_texts()))
        self.assertEqual(message.replies, [])

    def test_a_stranger_outside_the_tournament_is_refused(self):
        member = self.people["Intellekt"][0]
        message = TourMessage("/pichoq 424242 kim u", user_id=member, message_id=903)
        self.context.args = ["424242", "kim", "u"]
        asyncio.run(webhook.cmd_whisper(tour_update(message, user_id=member), self.context))
        self.assertEqual(self.bot.private_texts(424242), [])
        self.assertIn("qatnashmaydi", "\n".join(self.bot.private_texts(member)))

    def test_the_curator_may_write_to_any_team(self):
        message = TourMessage("/pichoq Zakovat boshladik", user_id=ADMIN, message_id=904)
        self.context.args = ["Zakovat", "boshladik"]
        asyncio.run(webhook.cmd_whisper(tour_update(message, user_id=ADMIN), self.context))
        for mate in self.people["Zakovat"]:
            self.assertIn("boshladik", "\n".join(self.bot.private_texts(mate)))
        for rival in self.people["Bilimdonlar"]:
            self.assertNotIn("boshladik", "\n".join(self.bot.private_texts(rival)))

    def test_without_delete_rights_a_whisper_is_not_sent_at_all(self):
        bot = TourBot(can_delete=False)
        context = tour_context(bot, self.archive)
        tour = self.bot.card_state()
        tour.can_delete = False
        member = self.people["Zakovat"][0]
        mate = self.people["Zakovat"][1]
        message = TourMessage("/pichoq jamoa maxfiy", user_id=member, message_id=906)
        context.args = ["jamoa", "maxfiy"]
        with mock.patch.object(webhook, "tour_state", mock.AsyncMock(return_value=tour)):
            asyncio.run(webhook.cmd_whisper(tour_update(message, user_id=member), context))
        self.assertEqual(bot.deleted, [])
        self.assertEqual(bot.private_texts(mate), [])      # nothing reaches the team
        group = "\n".join(visible(record.get("text") or "") for record in bot.sent)
        self.assertIn("o‘chira olmaydi", group)            # and the group is told why
        self.assertIn("YUBORILMADI", "\n".join(bot.private_texts(member)))

    def test_a_whisper_to_the_whole_team(self):
        member = self.people["Intellekt"][0]
        message = TourMessage("/pichoq jamoa tayyormisiz", user_id=member)
        message.message_id = 902
        self.context.args = ["jamoa", "tayyormisiz"]
        asyncio.run(webhook.cmd_whisper(tour_update(message, user_id=member), self.context))
        self.assertIn(902, self.bot.deleted)
        for teammate in self.people["Intellekt"][1:]:
            self.assertIn("tayyormisiz", "\n".join(self.bot.private_texts(teammate)))
        for rival in self.people["Bilimdonlar"]:
            self.assertNotIn("tayyormisiz", "\n".join(self.bot.private_texts(rival)))

    def test_without_delete_rights_the_group_is_warned_once(self):
        bot = TourBot(can_delete=False)
        context = tour_context(bot, self.archive)
        tour = self.bot.card_state()
        tour.can_delete = False
        tour.open_mode = True
        message = TourMessage("/yakuniy javob", user_id=self.people["Bilimdonlar"][0],
                              reply_text=webhook.tour_state_text("tasma", tour))
        context.args = ["javob"]
        with mock.patch.object(webhook, "tour_state", mock.AsyncMock(return_value=tour)):
            asyncio.run(webhook.cmd_final(tour_update(message, user_id=message.from_user.id),
                                          context))
        self.assertEqual(bot.deleted, [])
        texts = "\n".join(visible(record.get("text") or "") for record in bot.sent)
        self.assertIn("o‘chirish huquqi yo‘q", texts)

    def test_the_member_list_shows_your_own_team(self):
        member = self.people["Bilimdonlar"][0]
        message = TourMessage("/azolar", user_id=member)
        asyncio.run(webhook.cmd_members(tour_update(message, user_id=member), self.context))
        shown = visible(message.replies[0])
        self.assertIn("A’ZOLAR", shown)
        self.assertIn("<b>Bilimdonlar</b>", shown)
        self.assertIn(str(member), shown)
        self.assertIn("siz", shown)
        self.assertNotIn("<b>Zakovat</b>", shown)          # another team's ids are not yours
        for rival in self.people["Zakovat"]:
            self.assertNotIn(str(rival), shown)

    def test_the_curator_sees_every_team(self):
        message = TourMessage("/azolar", user_id=ADMIN)
        asyncio.run(webhook.cmd_members(tour_update(message, user_id=ADMIN), self.context))
        shown = visible(message.replies[0])
        for name in self.people:
            self.assertIn(f"<b>{name}</b>", shown)
        self.assertIn("GURUHDA BOT KO‘RGANLAR", shown)     # who could still be appointed
        self.assertIn("user101", shown)

    def test_stopping_prints_the_table(self):
        press = callback_query(st.tour_data(st.TEAM_STOP), self._card_text(), user_id=ADMIN)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=ADMIN),
                                           self.context))
        group = "\n".join(self.bot.group_texts())
        self.assertIn("TURNIR TO‘XTATILDI", group)
        self.assertIn("JADVAL", group)
        self.assertEqual(self.bot.card_state().phase, tn.PHASE_DONE)


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class TournamentTickTests(unittest.TestCase):
    """The countdown chain of a tournament: repaint while it runs, resolve at zero."""

    def setUp(self):
        stats.reset()
        self.archive = Archive("data/all")
        self.bot = TourBot()
        self.context = tour_context(self.bot, self.archive)
        webhook._tour_probe.clear()
        self.application = SimpleNamespace(bot=self.bot, bot_data={"archive": self.archive},
                                           initialize=mock.AsyncMock(),
                                           shutdown=mock.AsyncMock())
        self._patches = [
            mock.patch.object(webhook, "TICKS_ENABLED", False),
            mock.patch.object(stats, "backend", lambda: "lokal"),
        ]
        for patch in self._patches:
            patch.start()
        first = TourMessage("/turnir", user_id=ADMIN)
        self.context.args = [str(PACKAGE)]
        asyncio.run(webhook.cmd_tour(tour_update(first, user_id=ADMIN), self.context))
        self.context.args = []
        for index, name in enumerate(("Bilimdonlar", "Zakovat", "Intellekt")):
            captain = 101 + index * 10
            message = TourMessage(f"/jamoa {name}", user_id=captain)
            self.context.args = [name]
            asyncio.run(webhook.cmd_team(tour_update(message, user_id=captain), self.context))
            for extra in (1, 2):
                member = captain + extra
                join = TourMessage("/qoshil", user_id=member)
                self.context.args = [name]
                asyncio.run(webhook.cmd_join(tour_update(join, user_id=member), self.context))
                ready = TourMessage("/tayyor", user_id=member)
                asyncio.run(webhook.cmd_ready(tour_update(ready, user_id=member), self.context))
        self.context.args = []
        press = callback_query(st.tour_data(st.TEAM_START),
                               self.bot.sent[self.bot.pinned[CHAT] - 1]["text"], user_id=ADMIN)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=ADMIN),
                                           self.context))
        auto_score(self.bot, self.archive)

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        stats.reset()
        webhook._tour_probe.clear()

    def _body(self) -> str:
        tour = self.bot.card_state()
        strip = next(record for record in self.bot.sent
                     if "Yakuniy javob bergan" in visible(record.get("text") or ""))
        tour.control_message_id = strip["message_id"]
        return tour.body()

    def test_a_hop_repaints_the_clock(self):
        tour = self.bot.card_state()
        tour.deadline = int(time.time()) + 37
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            result = asyncio.run(webhook.tick_work(tour.body(), CHAT))
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], 37)
        painted = visible(self.bot.edits[-1]["text"])
        self.assertIn("<b>00:37</b>", painted)
        self.assertIn("Yakuniy javob bergan", painted)

    def test_a_hop_at_zero_scores_the_finals_and_moves_on(self):
        tour = self.bot.card_state()
        answer = str(self.archive.question(tour.question_id).get("answer"))
        tour.submit_final(101, answer, int(time.time()))
        tour.deadline = int(time.time()) - 1
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            result = asyncio.run(webhook.tick_work(tour.body(), CHAT))
        self.assertTrue(result.get("resolved"))
        group = "\n".join(self.bot.group_texts())
        self.assertIn("YAKUNI", group)
        self.assertIn("Ochko oldi", group)
        self.assertIn("SAVOL 2 /", group)

    def test_the_pinned_card_wins_over_a_stale_hop(self):
        """A final answer submitted after the hop was queued must not be lost."""
        tour = self.bot.card_state()
        answer = str(self.archive.question(tour.question_id).get("answer"))
        stale = tn.Tournament.from_body(tour.body())
        stale.deadline = int(time.time()) - 1
        stale.control_message_id = tour.control_message_id

        # meanwhile somebody answered: the card now knows about it
        fresh = self.bot.card_state()
        fresh.submit_final(111, answer, int(time.time()))     # Zakovat's captain answers late
        fresh.deadline = int(time.time()) - 1
        fresh.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, fresh))

        with mock.patch.object(webhook, "get_application", lambda: self.application):
            asyncio.run(webhook.tick_work(stale.body(), CHAT))
        group = "\n".join(self.bot.group_texts())
        self.assertIn("Zakovat", group)                  # the late answer was scored
        self.assertIn("to‘g‘ri", group)

    def test_a_hop_without_a_tournament_is_refused(self):
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            result = asyncio.run(webhook.tick_work(tn.STATE_PREFIX + "buzilgan", CHAT))
        self.assertFalse(result["ok"])

    def test_the_tick_body_is_routed_by_its_prefix(self):
        tour = self.bot.card_state()
        self.assertTrue(tour.body().startswith(tn.STATE_PREFIX))
        self.assertIsNone(webhook.parse_body(tour.body()))     # never mistaken for a game


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class CuratorPromptTests(unittest.TestCase):
    """A button asks, the group answers: no command has to be remembered.

    «Yangi jamoa», «Qidirish» and «Paketni almashtirish» used to answer with an
    instruction ("/jamoa nom", "/tanlov so‘z", "/turnir <paket>"). The card now
    remembers who was asked and takes that person's next plain message.
    """

    MEMBER = 101

    def setUp(self):
        stats.reset()
        self.archive = Archive("data/all")
        self.bot = TourBot()
        self.context = tour_context(self.bot, self.archive)
        webhook._tour_probe.clear()
        self._patches = [
            mock.patch.object(webhook, "TICKS_ENABLED", False),
            mock.patch.object(stats, "backend", lambda: "lokal"),
        ]
        for patch in self._patches:
            patch.start()
        message = TourMessage("/turnir", user_id=ADMIN)
        self.context.args = [str(PACKAGE)]
        asyncio.run(webhook.cmd_tour(tour_update(message, user_id=ADMIN), self.context))
        self.context.args = []

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        stats.reset()
        webhook._tour_probe.clear()

    def _card_text(self) -> str:
        return self.bot.sent[self.bot.pinned[CHAT] - 1]["text"]

    def _press(self, action: str, value="", user_id=ADMIN):
        press = callback_query(st.curator_data(action, value), self._card_text(),
                               user_id=user_id)
        asyncio.run(webhook.on_curator_button(tour_update(callback=press, user_id=user_id),
                                              self.context))
        return press

    def _team_press(self, action: str, user_id=MEMBER):
        press = callback_query(st.tour_data(action), self._card_text(), user_id=user_id)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=user_id),
                                           self.context))
        return press

    def _search_word(self) -> str:
        """A word from this package's own questions: the picker must find it."""
        first = self.archive.questions(PACKAGE)[0]
        words = [word.strip(".,!?;:«»()\u201e\u201c\u201d’'")
                 for word in str(first.get("text") or "").split()]
        return max((word for word in words if len(word) > 4), key=len)

    def _say(self, text: str, user_id: int) -> TourMessage:
        """A plain message in the group, through the real text handler."""
        message = TourMessage(text, user_id=user_id)
        asyncio.run(webhook.on_text(tour_update(message, user_id=user_id), self.context))
        return message

    def _say_offline(self, text: str, user_id: int) -> TourMessage:
        """The same, but a package from the big base does not drag a shard down."""
        real = self.archive.questions

        def questions(package_id):
            return [] if int(package_id) >= 900000 else real(package_id)

        with mock.patch.object(self.archive, "questions", questions):
            return self._say(text, user_id)

    # ------------------------------------------------------------------ search
    def test_the_search_button_asks_and_the_next_message_answers(self):
        press = self._press("qs")
        self.assertEqual(self.bot.card_state().pending_of(), (tn.PROMPT_SEARCH, ADMIN))
        self.assertIn("guruhga yozing", press._answers[-1]["text"])

        word = self._search_word()
        self._say(word, ADMIN)
        tour = self.bot.card_state()
        self.assertEqual(tour.query, word[:24])
        self.assertEqual(tour.pending_of(), ("", 0))          # answered, so closed
        self.assertEqual(self.context.args, [])                # no leftover arguments

    def test_somebody_elses_message_is_not_the_answer(self):
        self._press("qs")
        self._say("bu shunchaki gap", self.MEMBER)
        tour = self.bot.card_state()
        self.assertEqual(tour.query, "")                       # nothing was searched
        self.assertEqual(tour.pending_of(), (tn.PROMPT_SEARCH, ADMIN))

    def test_a_command_is_not_the_answer_either(self):
        self._press("qs")
        message = TourMessage("/jadval", user_id=ADMIN)
        asyncio.run(webhook.on_text(tour_update(message, user_id=ADMIN), self.context))
        self.assertEqual(self.bot.card_state().pending_of(), (tn.PROMPT_SEARCH, ADMIN))

    def test_any_other_button_cancels_the_question(self):
        self._press("qs")
        self._press("r", 3)                                    # a rounds chip instead
        tour = self.bot.card_state()
        self.assertEqual(tour.rounds, 3)
        self.assertEqual(tour.pending_of(), ("", 0))

    # ----------------------------------------------------------------- package
    def test_the_package_button_swaps_the_whole_bank(self):
        self._press("i", self.archive.questions(PACKAGE)[0]["id"])   # a selection first
        self.assertTrue(self.bot.card_state().include)
        other = self.archive.random_package_id(remote=False)
        while int(other) == int(PACKAGE):
            other = self.archive.random_package_id(remote=False)

        press = self._press("pk")
        self.assertEqual(self.bot.card_state().pending_of(), (tn.PROMPT_PACKAGE, ADMIN))
        self.assertIn("paketga o‘tadi", press._answers[-1]["text"])

        said = self._say(str(other), ADMIN)
        tour = self.bot.card_state()
        self.assertEqual(tour.package_id, int(other))
        self.assertEqual(tour.include, [])                     # the old picks are gone
        self.assertEqual(tour.exclude, [])
        self.assertEqual(tour.pending_of(), ("", 0))
        answered = "\n".join(said.replies)
        self.assertIn("PAKET ALMASHTIRILDI", answered)
        self.assertIn(str(PACKAGE), answered)                  # how to put it back
        self.assertIn("Qaytarish", answered)
        self.assertTrue(tour.questions)                        # the new bank is attached

    def test_a_word_that_is_not_a_package_keeps_the_question_open(self):
        self._press("pk")
        said = self._say("rahmat hammaga", ADMIN)
        tour = self.bot.card_state()
        self.assertEqual(tour.package_id, PACKAGE)             # nothing moved
        self.assertEqual(tour.pending_of(), (tn.PROMPT_PACKAGE, ADMIN))
        self.assertIn("topilmadi", "\n".join(said.replies))

    def test_a_package_found_by_its_name_is_accepted(self):
        title = (self.archive.package_info(PACKAGE) or {}).get("title") or ""
        word = title.split()[0]
        expected = self.archive.search(word, limit=1)
        self.assertTrue(expected)
        self._press("pk")
        self._say_offline(word, ADMIN)
        tour = self.bot.card_state()
        self.assertEqual(tour.package_id, int(expected[0]["id"]))
        self.assertEqual(tour.pending_of(), ("", 0))

    def test_an_open_game_does_not_let_the_package_go(self):
        tour = self.bot.card_state()
        tour.phase = tn.PHASE_PLAY
        tour.question_id = int(self.archive.questions(PACKAGE)[0]["id"])
        tour.deadline = int(time.time()) + 40
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        press = self._press("pk")
        self.assertIn("boshlangan", press._answers[-1]["text"])
        self.assertEqual(self.bot.card_state().pending_of(), ("", 0))

    # -------------------------------------------------------------------- team
    def test_the_new_team_button_takes_the_name_from_the_group(self):
        press = self._team_press(st.TEAM_NEW, user_id=self.MEMBER)
        self.assertEqual(self.bot.card_state().pending_of(), (tn.PROMPT_TEAM, self.MEMBER))
        self.assertIn("nomini", press._answers[-1]["text"])

        self._say("Bilimdonlar", self.MEMBER)
        tour = self.bot.card_state()
        self.assertEqual([team.name for team in tour.teams], ["Bilimdonlar"])
        self.assertEqual(tour.teams[0].captain, self.MEMBER)
        self.assertEqual(tour.pending_of(), ("", 0))

    def test_a_name_from_somebody_else_is_not_taken(self):
        self._team_press(st.TEAM_NEW, user_id=self.MEMBER)
        self._say("Men ham jamoa", self.MEMBER + 1)
        tour = self.bot.card_state()
        self.assertEqual(tour.teams, [])
        self.assertEqual(tour.pending_of(), (tn.PROMPT_TEAM, self.MEMBER))

    # ------------------------------------------------------------------- state
    def test_the_question_survives_a_cold_instance(self):
        self._press("qs")
        body = self.bot.card_state().body()
        self.assertIn('"pd":', body)
        self.assertEqual(tn.Tournament.from_body(body).pending_of(), (tn.PROMPT_SEARCH, ADMIN))

    def test_a_prompt_never_pushes_the_card_over_the_limit(self):
        tour = self.bot.card_state()
        tour.ask(tn.PROMPT_PACKAGE, ADMIN)
        for index in range(tn.SELECTION_LIMIT):
            tour.include.append(900000000 + index)
        body = tour.body(level=3)
        self.assertLessEqual(len(body), 4000)
        self.assertEqual(tn.Tournament.from_body(body).pending_of(), (tn.PROMPT_PACKAGE, ADMIN))


class PackageSwapRulesTests(unittest.TestCase):
    """Which answers move the whole bank, and which are only somebody's reply.

    ``Archive.search`` is a plain substring match over titles, so it answers "a"
    with a championship and "9" with a student league. That looseness is right for
    ``/solo olimpiada`` and wrong for swapping the questions a group is about to
    play, so ``_resolve_package_strict`` adds three rules: every word is at least
    two letters, at least one is a real word, and each word has to start a word of
    the title it matched. Nothing here is pinned to one package — the archive
    changes on every import, so the expectations are read out of it.
    """

    def setUp(self):
        self.archive = Archive("data/all")
        self.context = tour_context(TourBot(), self.archive)
        self.title = str((self.archive.package_info(PACKAGE) or {}).get("title") or "")
        self.assertTrue(self.title, "the test package needs a title")
        self.words = [word for word in webhook._title_words(self.title) if not word.isdigit()]
        self.assertTrue(self.words)
        self.long_words = [word for word in self.words if len(word) >= webhook.MIN_SWAP_WORD]
        self.assertTrue(self.long_words, "the title needs one word worth typing")

    def loose(self, answer: str):
        return webhook._resolve_package(self.context, answer)

    def strict(self, answer: str):
        return webhook._resolve_package_strict(self.context, answer)

    def test_a_number_is_taken_as_it_is(self):
        found = self.strict(str(PACKAGE))
        self.assertIsNotNone(found)
        self.assertEqual(int(found[0]), PACKAGE)

    def test_a_number_that_does_not_exist_is_refused(self):
        self.assertIsNone(self.strict("1"))
        self.assertIsNone(self.strict("99999999"))

    def test_a_word_of_a_title_finds_a_package_that_really_holds_it(self):
        checked = 0
        for word in self.long_words:
            found = self.strict(word)
            loose = self.loose(word)
            if loose:
                # No false refusal: what the search found is accepted…
                self.assertIsNotNone(found, word)
                self.assertEqual(int(found[0]), int(loose[0]), word)
            if not found:
                continue
            # …and no false acceptance: the title it lands on holds every word.
            title = webhook._title_words(found[1])
            self.assertTrue(any(item.startswith(word) for item in title), f"{word} in {found[1]}")
            checked += 1
        self.assertGreaterEqual(checked, 1)

    def test_the_start_of_a_long_word_is_enough(self):
        # "savol" has to find "…savollari" — nobody types the whole ending.
        word = max(self.long_words, key=len)
        head = word[:webhook.MIN_SWAP_WORD + 1]
        found = self.strict(head)
        self.assertIsNotNone(found, head)
        self.assertTrue(any(item.startswith(head) for item in webhook._title_words(found[1])))

    def test_punctuation_around_a_word_changes_nothing(self):
        word = self.long_words[0]
        plain = self.strict(word)
        self.assertIsNotNone(plain, word)
        for dressed in (word + "!", "?" + word, "«" + word + "»", word + "."):
            found = self.strict(dressed)
            self.assertIsNotNone(found, dressed)
            self.assertEqual(int(found[0]), int(plain[0]))

    def test_two_words_of_one_title_are_enough(self):
        pair = self.long_words[:2]
        found = self.strict(" ".join(pair))
        if found is None:
            self.skipTest("no single title holds both " + " and ".join(pair))
        title = webhook._title_words(found[1])
        for word in pair:
            self.assertTrue(any(item.startswith(word) for item in title), word)

    def test_a_single_letter_never_moves_the_bank(self):
        answered = 0
        for letter in "aeioustkn":
            if self.loose(letter):
                answered += 1
            self.assertIsNone(self.strict(letter), letter)
        self.assertGreaterEqual(answered, 3)      # the loose search is that generous

    def test_a_short_reply_to_somebody_else_is_not_a_package(self):
        for answer in ("ha", "ok", "xa", "yoq", "bor", "ha, to‘g‘ri", "xayr"):
            self.assertIsNone(self.strict(answer), answer)

    def test_a_reply_word_that_is_also_a_title_word_is_announced_not_hidden(self):
        # "yo‘q" is an ordinary reply and also a word in some package title, so the
        # swap does happen — which is why the message names the number to type back.
        found = self.strict("yo‘q")
        if found is None:
            self.skipTest("no package title holds this word today")
        said = msg.tour_package_swapped(found[1], int(found[0]), 12, "Kurator", PACKAGE)
        self.assertIn("PAKET ALMASHTIRILDI", said)
        self.assertIn(str(PACKAGE), said)
        self.assertIn("Qaytarish", said)

    def test_an_ordinary_sentence_is_not_a_package(self):
        for answer in ("rahmat hammaga", "bugun o‘yin yaxshi bo‘ldi",
                       "javob to‘g‘ri edi, hisoblang", "kim biladi bu savolni",
                       "keyingi turda ko‘rishamiz"):
            self.assertIsNone(self.strict(answer), answer)

    def test_nothing_at_all_is_not_a_package(self):
        for answer in ("", "   ", "!", "…", "??"):
            self.assertIsNone(self.strict(answer), repr(answer))

    def test_the_loose_resolver_still_answers_a_letter(self):
        # The swap is strict *because* this is loose, and /solo depends on it.
        self.assertIsNotNone(self.loose("a"))
        self.assertIsNotNone(self.loose("9"))
        self.assertIsNotNone(self.loose("olimpiada"))


class ClosedDmBot(TourBot):
    """A group whose admins never pressed /start: every private message is refused."""

    async def send_message(self, **kwargs):
        if int(kwargs.get("chat_id") or 0) > 0:
            raise webhook.TelegramError("Forbidden: bot can't initiate conversation")
        return await super().send_message(**kwargs)


@unittest.skipUnless(webhook, "install requirements.txt to test the webhook")
class AnswerReviewTests(unittest.TestCase):
    """Nobody's points are published until a person has looked at the list.

    The clock running out used to mean ``tour.close`` scored every final answer
    and the result went straight to the group: a checker that misreads one
    spelling decided the game. Now the same moment produces a *proposal* — one
    line per team, with the right answer beside it — which goes to the curators in
    private, waits for their marks and only then becomes a score.
    """

    TEAMS = (("Bilimdonlar", 101), ("Zakovat", 111), ("Intellekt", 121))

    def setUp(self):
        stats.reset()
        self.archive = Archive("data/all")
        webhook._tour_probe.clear()
        self._patches = [
            mock.patch.object(webhook, "TICKS_ENABLED", False),
            mock.patch.object(stats, "backend", lambda: "lokal"),
        ]
        for patch in self._patches:
            patch.start()
        self._build(TourBot())

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        stats.reset()
        webhook._tour_probe.clear()

    # ------------------------------------------------------------------ building
    def _build(self, bot):
        """A whole tournament on ``bot``: registered, started, one answer each."""
        self.bot = bot
        self.context = tour_context(bot, self.archive)
        self.application = SimpleNamespace(bot=bot, bot_data={"archive": self.archive},
                                           initialize=mock.AsyncMock(),
                                           shutdown=mock.AsyncMock())
        self._open_tournament()
        self._register_teams()
        self._start()
        self.right = str(self.archive.question(self.tour().question_id).get("answer") or "")
        self.assertTrue(self.right, "the test package needs an answer to check against")
        self.wrong = "Qirq yettinchi shahar ko‘chasi"
        self.assertNotIn(self.right.lower(), self.wrong.lower())

    def _open_tournament(self):
        message = TourMessage("/turnir", user_id=ADMIN)
        self.context.args = [str(PACKAGE)]
        asyncio.run(webhook.cmd_tour(tour_update(message, user_id=ADMIN), self.context))
        self.context.args = []

    def _register_teams(self):
        for name, captain in self.TEAMS:
            message = TourMessage(f"/jamoa {name}", user_id=captain)
            self.context.args = [name]
            asyncio.run(webhook.cmd_team(tour_update(message, user_id=captain), self.context))
            for extra in (1, 2):
                member = captain + extra
                join = TourMessage("/qoshil", user_id=member)
                self.context.args = [name]
                asyncio.run(webhook.cmd_join(tour_update(join, user_id=member), self.context))
                ready = TourMessage("/tayyor", user_id=member)
                asyncio.run(webhook.cmd_ready(tour_update(ready, user_id=member), self.context))
        self.context.args = []

    def _start(self):
        press = callback_query(st.tour_data(st.TEAM_START), self._card_text(), user_id=ADMIN)
        asyncio.run(webhook.on_team_button(tour_update(callback=press, user_id=ADMIN),
                                           self.context))

    def _card_text(self) -> str:
        return self.bot.sent[self.bot.pinned[CHAT] - 1]["text"]

    def tour(self) -> "tn.Tournament":
        return self.bot.card_state(self.archive)

    def _answer(self, who: int, text: str):
        tour = self.tour()
        tour.submit_final(who, text, int(time.time()))
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour, merge_finals=True))

    def _close(self):
        """The clock runs out and the tick closes the question."""
        tour = self.tour()
        tour.deadline = int(time.time()) - 1
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            return asyncio.run(webhook.tick_work(tour.body(), CHAT))

    def _hop(self, tour=None):
        """One more hop of the same chain, while the list is open."""
        state = tour or self.tour()
        with mock.patch.object(webhook, "get_application", lambda: self.application):
            return asyncio.run(webhook.tick_work(state.body(), CHAT))

    def _list_record(self, who=ADMIN):
        """The confirmation list, whether it is still open or already decided."""
        return next(record for record in self.bot.sent
                    if record.get("chat_id") == int(who)
                    and any(word in visible(record.get("text") or "")
                            for word in ("TEKSHIRISH", "TASDIQLANDI")))

    def _press(self, action: str, value="", user_id=ADMIN, chat_id=CHAT, group=CHAT):
        """A press on the confirmation list — in the curator's private chat."""
        press = callback_query(st.review_data(group, action, value),
                               self._list_record()["text"], user_id=user_id, chat_id=chat_id)
        asyncio.run(webhook.on_review_button(
            tour_update(callback=press, user_id=user_id, chat_id=chat_id, chat_type="private"),
            self.context))
        return press

    def _buttons(self, record: dict) -> list:
        board = record.get("reply_markup")
        rows = getattr(board, "inline_keyboard", None) or []
        return [button for row in rows for button in row]

    def _reviewing(self, answers: dict):
        """One team right, one wrong, one silent — the shape a real question ends in.

        ``False`` stands for a wrong answer: deliberately far from anything a bank
        answer could be, so the test does not turn red on a question that happens
        to mention it.
        """
        for (name, captain), given in zip(self.TEAMS, answers):
            if given is None:
                continue
            self._answer(captain, self.wrong if given is False else given)

    # ---------------------------------------------------------------- the window
    def test_a_closed_question_waits_for_a_person(self):
        self._reviewing((self.right, False, None))
        result = self._close()
        self.assertTrue(result.get("ok"))
        self.assertIn("review", result)                     # no score yet, a window opened

        tour = self.tour()
        self.assertTrue(tour.reviewing())
        self.assertEqual(tour.review_marks, "100")          # right, wrong, no answer
        self.assertEqual([team.score for team in tour.teams], [0, 0, 0])
        self.assertGreater(tour.review_left(int(time.time())), 0)

        group = "\n".join(self.bot.group_texts())
        self.assertIn("YAKUNLANDI", group)
        self.assertIn("kuratorga tekshirish uchun yuborildi", group)
        self.assertNotIn("YAKUNI", group)                   # the result is not published
        self.assertNotIn(self.right, group)                 # and neither is the answer
        self.assertIn("Javoblar tekshirilmoqda", self.bot.card())

    def test_the_list_goes_to_the_curator_alone(self):
        self._reviewing((self.right, False, None))
        self._close()
        listed = visible(self._list_record()["text"])
        self.assertIn("TEKSHIRISH", listed)
        self.assertIn(self.right[:20], listed)              # what to judge against
        self.assertIn("Bilimdonlar", listed)
        self.assertIn("TO‘G‘RI", listed)
        self.assertIn("NOTO‘G‘RI", listed)
        self.assertIn("javob bermadi", listed)
        self.assertNotIn("\n".join(self.bot.group_texts()), listed)
        self.assertNotIn(self.wrong, "\n".join(self.bot.group_texts()))
        labels = [button.text for button in self._buttons(self._list_record())]
        self.assertEqual(len(labels), 3)                    # two verdicts + TASDIQLASH
        self.assertIn("TASDIQLASH", labels)

    def test_a_team_without_an_answer_gets_no_button(self):
        self._reviewing((self.right, None, None))
        self._close()
        labels = [button.text for button in self._buttons(self._list_record())]
        self.assertEqual(len(labels), 2)                    # one verdict + TASDIQLASH
        self.assertIn("Bilimdonlar", labels[0])

    def test_a_flip_changes_the_verdict_for_everybody(self):
        self._reviewing((self.right, False, None))       # the checker says no
        self._close()
        record = self._list_record()
        press = self._press(st.REVIEW_FLIP, "1")

        self.assertEqual(press._answers[-1]["show_alert"], False)
        self.assertIn("Zakovat", press._answers[-1]["text"])
        tour = self.tour()
        self.assertEqual(tour.review_edit[1], "1")
        self.assertEqual(tour.review_marks[1], "0")         # the bot's word is kept
        self.assertEqual([team.score for team in tour.teams], [0, 0, 0])
        self.assertTrue(self.bot.edits_of(record["message_id"]))   # the DM was rewritten
        self.assertIn("o‘zgartirildi", visible(self.bot.edits_of(record["message_id"])[-1]["text"]))

    def test_confirming_publishes_exactly_the_marks(self):
        self._reviewing((self.right, False, None))
        self._close()
        record = self._list_record()
        self._press(st.REVIEW_FLIP, "1")                    # the curator disagrees
        self._press(st.REVIEW_CONFIRM)

        tour = self.tour()
        self.assertFalse(tour.reviewing())
        self.assertEqual([team.score for team in tour.teams], [1, 1, 0])
        self.assertEqual(tour.history[-1]["by"], ADMIN)     # who stood behind it
        self.assertNotIn("auto", tour.history[-1])
        marks = {row["team"]: row for row in tour.history[-1]["finals"]}
        self.assertTrue(marks["Zakovat"]["correct"])
        self.assertTrue(marks["Zakovat"]["changed"])

        group = "\n".join(self.bot.group_texts())
        self.assertIn("YAKUNI", group)
        self.assertIn("o‘zgartirildi", group)
        self.assertIn("tasdiqladi", group.lower())
        self.assertIn("SAVOL 2 /", group)                   # the game moved on
        closed = visible(self.bot.edits_of(record["message_id"])[-1]["text"])
        self.assertIn("TASDIQLANDI", closed)
        self.assertIsNone(self.bot.edits_of(record["message_id"])[-1]["reply_markup"])

    def test_nobody_confirming_means_the_bot_publishes_itself(self):
        self._reviewing((self.right, False, None))
        self._close()
        tour = self.tour()
        tour.review_due = int(time.time()) - 1              # the window ran out
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        self._hop()

        tour = self.tour()
        self.assertFalse(tour.reviewing())
        self.assertEqual([team.score for team in tour.teams], [1, 0, 0])
        self.assertEqual(tour.history[-1].get("auto"), 1)
        self.assertNotIn("by", tour.history[-1])
        group = "\n".join(self.bot.group_texts())
        self.assertIn("YAKUNI", group)
        self.assertIn("avtomatik", group.lower())
        self.assertIn("SAVOL 2 /", group)

    def test_a_reminder_goes_out_once_per_step(self):
        self._reviewing((self.right, False, None))
        self._close()
        nudges = len(self.bot.private_texts(ADMIN))

        tour = self.tour()
        tour.review_at = int(time.time()) - (tn.REVIEW_REMIND[0] + 1)
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        self._hop()
        self.assertEqual(self.tour().review_noted, 1)
        self.assertEqual(len(self.bot.private_texts(ADMIN)), nudges + 1)
        self.assertIn("hali tasdiqlanmagan", self.bot.private_texts(ADMIN)[-1])

        self._hop()                                          # the same step, again
        self.assertEqual(self.tour().review_noted, 1)
        self.assertEqual(len(self.bot.private_texts(ADMIN)), nudges + 1)

        tour = self.tour()
        tour.review_at = int(time.time()) - (tn.REVIEW_REMIND[1] + 1)
        tour.bump()
        asyncio.run(webhook.tour_persist(self.bot, self.archive, tour))
        self._hop()
        self.assertEqual(self.tour().review_noted, 2)
        self.assertEqual(len(self.bot.private_texts(ADMIN)), nudges + 2)
        self.assertTrue(self.tour().reviewing())             # still waiting for a person

    def test_a_question_nobody_answered_needs_no_confirmation(self):
        """No answer, no verdict to check: the game carries on without waiting."""
        self._reviewing((None, None, None))
        result = self._close()
        self.assertTrue(result.get("resolved"))
        tour = self.tour()
        self.assertFalse(tour.reviewing())
        self.assertEqual([team.score for team in tour.teams], [0, 0, 0])
        group = "\n".join(self.bot.group_texts())
        self.assertIn("YAKUNI", group)
        self.assertNotIn("kuratorga tekshirish uchun", group)
        self.assertIn("SAVOL 2 /", group)
        with self.assertRaises(StopIteration):
            self._list_record()

    # --------------------------------------------------------------- who decides
    def test_a_team_member_cannot_confirm_the_verdicts(self):
        self._reviewing((self.right, False, None))
        self._close()
        press = self._press(st.REVIEW_CONFIRM, user_id=101, chat_id=101)
        self.assertTrue(press._answers[-1]["show_alert"])
        tour = self.tour()
        self.assertTrue(tour.reviewing())
        self.assertEqual([team.score for team in tour.teams], [0, 0, 0])
        self.assertNotIn("YAKUNI", "\n".join(self.bot.group_texts()))

    def test_a_forged_group_id_reaches_nothing(self):
        self._reviewing((self.right, False, None))
        self._close()
        press = self._press(st.REVIEW_CONFIRM, group=-100999)
        self.assertTrue(press._answers[-1]["show_alert"])
        self.assertTrue(self.tour().reviewing())
        self.assertNotIn("YAKUNI", "\n".join(self.bot.group_texts()))

    def test_a_closed_list_cannot_be_pressed_again(self):
        self._reviewing((self.right, False, None))
        self._close()
        self._press(st.REVIEW_CONFIRM)
        again = self._press(st.REVIEW_CONFIRM)
        self.assertTrue(again._answers[-1]["show_alert"])
        self.assertEqual([team.score for team in self.tour().teams], [1, 0, 0])

    def test_stopping_the_tournament_takes_the_list_away(self):
        self._reviewing((self.right, False, None))
        self._close()
        record = self._list_record()
        message = TourMessage("/turnirstop", user_id=ADMIN)
        asyncio.run(webhook.cmd_tour_stop(tour_update(message, user_id=ADMIN), self.context))
        last = self.bot.edits_of(record["message_id"])[-1]
        self.assertIsNone(last["reply_markup"])              # no live buttons left behind
        self.assertFalse(self.tour().reviewing())
        self.assertIn("TO‘XTATILDI", "\n".join(self.bot.group_texts()))

    # ------------------------------------------------------------------ the switch
    def test_the_curator_can_turn_the_confirmation_off(self):
        press = callback_query(st.curator_data(st.CONFIRM_TOGGLE, "0"), self._card_text(),
                               user_id=ADMIN)
        asyncio.run(webhook.on_curator_button(tour_update(callback=press, user_id=ADMIN),
                                              self.context))
        self.assertTrue(press._answers)
        tour = self.tour()
        self.assertFalse(tour.needs_confirm)
        self.assertIn("O‘CHIQ", press._answers[-1]["text"])

        self._reviewing((self.right, False, None))
        before = len(self.bot.private_texts(ADMIN))
        self._close()
        self.assertFalse(self.tour().reviewing())            # straight to the result
        self.assertEqual([team.score for team in self.tour().teams], [1, 0, 0])
        self.assertEqual(len(self.bot.private_texts(ADMIN)), before)
        group = "\n".join(self.bot.group_texts())
        self.assertIn("YAKUNI", group)
        self.assertNotIn("avtomatik", group.lower())

    def test_the_switch_is_back_on_again(self):
        for value, wanted in (("0", False), ("1", True)):
            press = callback_query(st.curator_data(st.CONFIRM_TOGGLE, value), self._card_text(),
                                   user_id=ADMIN)
            asyncio.run(webhook.on_curator_button(tour_update(callback=press, user_id=ADMIN),
                                                  self.context))
            self.assertEqual(self.tour().needs_confirm, wanted)

    # ------------------------------------------------------------- reaching people
    def test_a_group_nobody_can_be_dmed_carries_the_list_itself(self):
        self._build(ClosedDmBot())                           # a fresh tournament, no DMs
        self._reviewing((self.right, False, None))
        self._close()

        tour = self.tour()
        self.assertTrue(tour.reviewing())
        self.assertEqual(tour.review_to, [])                 # nobody was reachable
        self.assertTrue(tour.review_msg)                     # so the group holds the list
        listed = next(record for record in self.bot.sent
                      if record["message_id"] == tour.review_msg)
        self.assertIn("TEKSHIRISH", visible(listed["text"]))
        self.assertTrue(self._buttons(listed))

    def test_the_review_survives_a_cold_instance(self):
        self._reviewing((self.right, False, None))
        self._close()
        self._press(st.REVIEW_FLIP, "1")
        cold = tn.Tournament.from_body(self.tour().body())
        self.assertTrue(cold.reviewing())
        self.assertEqual(cold.review_marks, "100")
        self.assertEqual(cold.review_edit, "110")
        self.assertEqual(cold.review_to, self.tour().review_to)
        self.assertEqual(cold.review_due, self.tour().review_due)
        self.assertTrue(cold.needs_confirm)

    def test_a_review_list_never_pushes_the_card_over_the_limit(self):
        tour = self.tour()
        tour.propose(webhook.checker_for([]), int(time.time()))
        tour.review_to = [[ADMIN + index, 900 + index] for index in range(tn.REVIEW_TARGETS + 4)]
        for index in range(tn.SELECTION_LIMIT):
            tour.include.append(900000000 + index)
        body = tour.body(level=4)
        again = tn.Tournament.from_body(body)
        self.assertLessEqual(len(body), 4000)
        self.assertEqual(len(again.review_to), tn.REVIEW_TARGETS)
        self.assertTrue(again.needs_confirm)


if __name__ == "__main__":
    unittest.main()
