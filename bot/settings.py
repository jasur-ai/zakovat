"""Game configuration: rounds, questions per round and per-question time.

Everything travels inside Telegram callback data (max 64 bytes) so the
serverless webhook stays stateless — no database, no session store.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

PREFIX_CONFIG = "zg"
PREFIX_PAGE = "zp"
PREFIX_GAME = "zt"

ROUNDS = (1, 2, 3, 5, 10)
PER_ROUND = (5, 10, 15, 25, 0)      # 0 = paketdagi barcha savol
TIMERS = (30, 45, 60, 90, 120)

ACTION_ROUNDS = "r"
# "/solo katta" opens the whole base: a random package from turnirlar.uz as
# well, whose shard is fetched when the game starts.
WHOLE_BASE_WORDS = frozenset({
    "katta", "kattta", "butun", "hamma", "hammasi", "barcha", "turnirlar",
    "big", "all", "huge", "full",
})

ACTION_PER_ROUND = "q"
ACTION_TIMER = "t"
ACTION_SHUFFLE = "n"
ACTION_START = "go"
ACTION_CANCEL = "x"

# In-game buttons (state is read from the message they are attached to).
GAME_REFRESH = "zt|f"
GAME_SKIP = "zt|k"
GAME_SCORE = "zt|c"
GAME_STOP = "zt|x"
PREFIX_STATS = "zs"
STATS_REFRESH = "zs|r"
STATS_SECTIONS = "zs|b"
STATS_EVENTS = "zs|e"
STATS_USED = "zs|q"          # ishlatilgan savollar indeksi
STATS_PLANS = "zs|p"         # rejadagi turnirlar
STATS_ACCESS = "zs|a"        # Mini App ruxsatlari
STATS_USED_PAGE = "zs|qp"    # indeksning boshqa sahifasi

# --- turnir -----------------------------------------------------------------
# Callback data stays under 64 bytes: the tournament itself rides invisibly
# inside the message the buttons hang on, so a button only has to say *what to
# do* (and to which team), never *what the state is*.
PREFIX_TOUR = "zr"          # kurator: paket, raund, savol tanlovi
PREFIX_TEAM = "zy"          # jamoalar: qo‘shilish, tayyorlik, boshlash

TOUR_ROUNDS = (1, 2, 3, 5, 10)
TOUR_PER_ROUND = (5, 10, 15, 25, 0)
TOUR_TIMERS = (30, 45, 60, 90, 120)

TEAM_JOIN = "j"             # jamoaga qo‘shilish
TEAM_READY = "r"            # «Men tayyorman»
TEAM_NEW = "n"              # yangi jamoa yaratish
TEAM_START = "go"           # turnirni boshlash
TEAM_LEAVE = "q"            # jamoadan chiqish
TEAM_REFRESH = "f"          # ro‘yxatni yangilash
TEAM_PICK = "p"             # kurator: savol tanlash oynasi
TEAM_STOP = "x"             # turnirni to‘xtatish
TEAM_TABLE = "t"            # jadvalni ko‘rsatish
TEAM_REVEAL = "rv"          # yakuniy javoblar holati (kurator)
TEAM_SKIP = "s"             # savolni yopish (kurator)
TEAM_LIST = "ls"            # turnir savollari ro‘yxati (hammaga ochiq)
TEAM_LIST_PAGE = "lp"       # ro‘yxatning boshqa sahifasi
TEAM_QUESTION = "so"        # ro‘yxatdagi bitta savolni ochish
TEAM_MANAGE = "mn"          # kurator boshqaruv oynasi
TEAM_PLAN = "pl"            # rejadagi boshlanish vaqti haqida xabar


def _default(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name, "") or fallback)
    except ValueError:
        return fallback


@dataclass
class GameConfig:
    package_id: int = 0
    mode: str = "solo"              # "solo" or "group"
    rounds: int = 0
    per_round: int = 0
    timer: int = 0
    seed: int = 0

    def __post_init__(self) -> None:
        self.rounds = self.rounds or _default("ZAKOVAT_ROUNDS", 3)
        self.per_round = self.per_round if self.per_round else _default("ZAKOVAT_PER_ROUND", 10)
        self.timer = self.timer or _default("ZAKOVAT_TIMER_SECONDS", 60)

    @property
    def total(self) -> int:
        return self.rounds * self.per_round if self.per_round else 0

    def replace(self, **changes) -> "GameConfig":
        values = {
            "package_id": self.package_id, "mode": self.mode, "rounds": self.rounds,
            "per_round": self.per_round, "timer": self.timer, "seed": self.seed,
        }
        values.update(changes)
        return GameConfig(**values)

    def encode(self, action: str = "", value: str = "") -> str:
        parts = [
            PREFIX_CONFIG,
            "s" if self.mode == "solo" else "g",
            str(self.package_id),
            str(self.rounds),
            str(self.per_round),
            str(self.timer),
            str(self.seed),
        ]
        if action:
            parts.append(action)
            if value:
                parts.append(value)
        data = "|".join(parts)
        return data[:64]


def decode(data: str | None) -> tuple[GameConfig, str, str] | None:
    """Parse callback data back into a config plus the pressed action."""
    if not data or not data.startswith(PREFIX_CONFIG + "|"):
        return None
    parts = data.split("|")
    if len(parts) < 7:
        return None
    try:
        config = GameConfig(
            package_id=int(parts[2]),
            mode="solo" if parts[1] == "s" else "group",
            rounds=int(parts[3]),
            per_round=int(parts[4]),
            timer=int(parts[5]),
            seed=int(parts[6]),
        )
    except ValueError:
        return None
    action = parts[7] if len(parts) > 7 else ""
    value = parts[8] if len(parts) > 8 else ""
    return config, action, value


def decode_page(data: str | None) -> tuple[int, str] | None:
    """Parse package-list pagination callback data."""
    if not data or not data.startswith(PREFIX_PAGE + "|"):
        return None
    parts = data.split("|")
    try:
        return int(parts[1]), (parts[2] if len(parts) > 2 else "")
    except ValueError:
        return None


def page_data(page: int, query: str = "") -> str:
    return f"{PREFIX_PAGE}|{page}|{query[:24]}"[:64]


def settings_keyboard(config: GameConfig):
    """Inline keyboard for the pre-game settings panel."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def row(action: str, options: tuple[int, ...], current: int, label) -> list:
        buttons = []
        for option in options:
            text = label(option)
            mark = "● " if option == current else "○ "
            buttons.append(InlineKeyboardButton(
                mark + text, callback_data=config.encode(action, str(option)),
            ))
        return buttons

    rows = [
        row(ACTION_ROUNDS, ROUNDS, config.rounds, lambda v: f"{v} raund"),
        row(ACTION_PER_ROUND, PER_ROUND, config.per_round, lambda v: "hammasi" if v == 0 else f"{v} savol"),
        row(ACTION_TIMER, TIMERS, config.timer, lambda v: f"{v} s"),
        [
            InlineKeyboardButton("Boshlash", callback_data=config.encode(ACTION_START)),
            InlineKeyboardButton("Boshqa paket", callback_data=config.encode(ACTION_SHUFFLE)),
            InlineKeyboardButton("Bekor qilish", callback_data=config.encode(ACTION_CANCEL)),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def game_keyboard(admin: bool = True, clock: str = "", clock_url: str = ""):
    """Buttons under every question message.

    With a clock the refresh button carries the live countdown, so the timer is
    a control that changes on every tick and answers the moment it is tapped.

    ``clock_url`` is accepted and ignored: the Mini App clock button is gone.
    It opened a second window inside Telegram, and while that window was open
    nobody could see the question — so the countdown now lives in the strip
    itself, in digits and a twenty-segment bar, repainted on every tick.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    label = f"{clock} · Vaqtni yangilash" if clock else "Vaqtni yangilash"
    row = [InlineKeyboardButton(label, callback_data=GAME_REFRESH)]
    if admin:
        row.append(InlineKeyboardButton("O‘tkazish", callback_data=GAME_SKIP))
    row.append(InlineKeyboardButton("Hisob", callback_data=GAME_SCORE))
    row.append(InlineKeyboardButton("To‘xtatish", callback_data=GAME_STOP))
    return InlineKeyboardMarkup([row])




def tour_data(action: str, value: str | int = "") -> str:
    """Callback data of a team/registration button."""
    return f"{PREFIX_TEAM}|{action}" + (f"|{value}" if value != "" else "")


def curator_data(action: str, value: str | int = "") -> str:
    """Callback data of a curator button (package, rounds, selection…)."""
    return f"{PREFIX_TOUR}|{action}" + (f"|{value}" if value != "" else "")


def tour_keyboard(teams: list, phase: str = "register", can_start: bool = False,
                  curator: bool = False, sizes: dict | None = None,
                  start_text: str = "") -> "object":
    """Buttons of the registration card.

    One button per team: pressing it joins that team, pressing it again says
    "Men tayyorman". The label carries both numbers, so the card itself shows
    who is still missing — nobody has to ask the bot.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    sizes = sizes or {}
    min_members = int(sizes.get("min_members", 3))
    min_ready = int(sizes.get("min_ready", 2))
    rows: list[list] = []
    for index, team in enumerate(teams):
        people = list(team.get("members") or [])
        ready = sum(1 for person in people if person.get("ready"))
        mark = "OK" if (len(people) >= min_members and ready >= min_ready) else "..."
        rows.append([InlineKeyboardButton(
            f"{index + 1}. {team.get('name', 'Jamoa')} — {len(people)}/{ready} [{mark}]",
            callback_data=tour_data(TEAM_JOIN, index))])
    if phase == "register":
        rows.append([
            InlineKeyboardButton("Men tayyorman", callback_data=tour_data(TEAM_READY)),
            InlineKeyboardButton("Yangi jamoa", callback_data=tour_data(TEAM_NEW)),
        ])
        rows.append([
            InlineKeyboardButton("Chiqish", callback_data=tour_data(TEAM_LEAVE)),
            InlineKeyboardButton("Yangilash", callback_data=tour_data(TEAM_REFRESH)),
        ])
        start = "Boshlash" if can_start else "Boshlash (shartlar bajarilmagan)"
        if start_text:
            start = f"Boshlash · {start_text}" if can_start \
                else f"Boshlash · {start_text} (shartlar bajarilmagan)"
        rows.append([InlineKeyboardButton(start, callback_data=tour_data(TEAM_START))])
    # The questions belong to everybody in the group, so this row is never gated.
    rows.append([
        InlineKeyboardButton("Savollar", callback_data=tour_data(TEAM_LIST)),
        InlineKeyboardButton("Jadval", callback_data=tour_data(TEAM_TABLE)),
    ])
    if curator:
        rows.append([
            InlineKeyboardButton("Boshqaruv", callback_data=tour_data(TEAM_MANAGE)),
            InlineKeyboardButton("Savol tanlash", callback_data=tour_data(TEAM_PICK)),
            InlineKeyboardButton("Sozlamalar", callback_data=curator_data("c")),
            InlineKeyboardButton("To‘xtatish", callback_data=tour_data(TEAM_STOP)),
        ])
    return InlineKeyboardMarkup(rows)


def manage_keyboard(phase: str = "register", can_start: bool = False,
                    start_text: str = "") -> "object":
    """Everything a curator or admin can do, as buttons instead of /commands.

    The same actions the slash commands offer, in one message: the group never
    has to remember a command, and the bot never has to explain one.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list] = [
        [InlineKeyboardButton("Savollar ro‘yxati", callback_data=tour_data(TEAM_LIST)),
         InlineKeyboardButton("Jadval", callback_data=tour_data(TEAM_TABLE))],
        [InlineKeyboardButton("Sozlamalar", callback_data=curator_data("c")),
         InlineKeyboardButton("Savol tanlash", callback_data=tour_data(TEAM_PICK))],
    ]
    if phase == "register":
        start = f"Boshlash · {start_text}" if start_text else "Boshlash"
        rows.append([InlineKeyboardButton(start if can_start else f"{start} (shartlar bajarilmagan)",
                                          callback_data=tour_data(TEAM_START))])
        rows.append([InlineKeyboardButton("Ro‘yxatni yangilash",
                                          callback_data=tour_data(TEAM_REFRESH)),
                     InlineKeyboardButton("Reja haqida", callback_data=tour_data(TEAM_PLAN))])
    else:
        rows.append([InlineKeyboardButton("Javoblar holati", callback_data=tour_data(TEAM_REVEAL)),
                     InlineKeyboardButton("Savolni yopish", callback_data=tour_data(TEAM_SKIP))])
        rows.append([InlineKeyboardButton("Vaqtni yangilash",
                                          callback_data=tour_data(TEAM_REFRESH)),
                     InlineKeyboardButton("To‘xtatish", callback_data=tour_data(TEAM_STOP))])
    rows.append([InlineKeyboardButton("Kurator tayinlash", callback_data=curator_data("ku")),
                 InlineKeyboardButton("Yopish", callback_data=tour_data(TEAM_REFRESH))])
    return InlineKeyboardMarkup(rows)


def question_card_keyboard(page: int = 1) -> "object":
    """Buttons under one opened question: back to the public list."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Savollar ro‘yxati", callback_data=tour_data(TEAM_LIST_PAGE, page)),
         InlineKeyboardButton("Yopish", callback_data=tour_data(TEAM_REFRESH))]])


def question_list_keyboard(page: int, pages: int, rows: list) -> "object":
    """Paging and one button per question of the public list."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard: list[list] = []
    line: list = []
    for row in rows:
        mark = "✅" if row.get("published") else ("•" if row.get("used") else "⏳")
        line.append(InlineKeyboardButton(f"{mark} {row.get('number')}",
                                         callback_data=tour_data(TEAM_QUESTION,
                                                                 int(row.get("id") or 0))))
        if len(line) == 5:
            keyboard.append(line)
            line = []
    if line:
        keyboard.append(line)
    if pages > 1:
        nav: list = []
        if page > 1:
            nav.append(InlineKeyboardButton("‹ Orqaga",
                                            callback_data=tour_data(TEAM_LIST_PAGE, page - 1)))
        nav.append(InlineKeyboardButton(f"{page} / {pages}",
                                        callback_data=tour_data(TEAM_LIST_PAGE, page)))
        if page < pages:
            nav.append(InlineKeyboardButton("Oldinga ›",
                                            callback_data=tour_data(TEAM_LIST_PAGE, page + 1)))
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Yopish", callback_data=tour_data(TEAM_REFRESH))])
    return InlineKeyboardMarkup(keyboard)


def selection_keyboard(page: int, pages: int, rows: list, chosen: set, dropped: set,
                       query: str = "") -> "object":
    """The curator's question picker: include or exclude exact questions.

    Every line is one question of the package, marked with what the curator has
    already decided, so the selection is a document rather than a guess.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard: list[list] = []
    for row in rows:
        question_id = int(row["id"])
        if question_id in chosen:
            mark, action = "OLINDI", "u"          # u = olib tashlash (include)
        elif question_id in dropped:
            mark, action = "CHIQARILDI", "e"      # e = qaytarish (exclude)
        else:
            mark, action = "—", "i"
        text = (row.get("text") or "").replace("\n", " ")[:38]
        keyboard.append([InlineKeyboardButton(
            f"{mark} · {question_id} · {text}", callback_data=curator_data(action, question_id))])
    nav: list = []
    if page > 1:
        nav.append(InlineKeyboardButton("‹ Orqaga", callback_data=curator_data("pg", page - 1)))
    nav.append(InlineKeyboardButton(f"{page} / {max(1, pages)}", callback_data=curator_data("c")))
    if page < pages:
        nav.append(InlineKeyboardButton("Oldinga ›", callback_data=curator_data("pg", page + 1)))
    keyboard.append(nav)
    keyboard.append([
        InlineKeyboardButton("Hammasini olish", callback_data=curator_data("ai")),
        InlineKeyboardButton("Hammasini chiqarish", callback_data=curator_data("ae")),
        InlineKeyboardButton("Tanlovni tozalash", callback_data=curator_data("ac")),
    ])
    keyboard.append([
        InlineKeyboardButton("Qidirish", callback_data=curator_data("qs")),
        InlineKeyboardButton("Orqaga", callback_data=tour_data(TEAM_REFRESH)),
    ])
    return InlineKeyboardMarkup(keyboard)


def curator_keyboard(rounds: int, per_round: int, timer: int, package_id: int,
                     seed: int, bank: int, total: int) -> "object":
    """The curator's panel: how many rounds, how many questions, how long each."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def line(action: str, options: tuple, current: int, label) -> list:
        return [InlineKeyboardButton(("● " if option == current else "○ ") + label(option),
                                     callback_data=curator_data(action, option))
                for option in options]

    rows = [
        line("r", TOUR_ROUNDS, rounds, lambda value: f"{value} raund"),
        line("q", TOUR_PER_ROUND, per_round,
             lambda value: "hammasi" if value == 0 else f"{value} savol"),
        line("t", TOUR_TIMERS, timer, lambda value: f"{value} s"),
        [
            InlineKeyboardButton(f"Paket {package_id}" + (f" · {bank} savol" if bank else ""),
                                 callback_data=curator_data("pk")),
            InlineKeyboardButton("Aralashtirish", callback_data=curator_data("s", seed + 1)),
        ],
        [
            InlineKeyboardButton("Savol tanlash", callback_data=tour_data(TEAM_PICK)),
            InlineKeyboardButton(f"Jami: {total} savol", callback_data=curator_data("c")),
        ],
        [InlineKeyboardButton("Yopish", callback_data=tour_data(TEAM_REFRESH))],
    ]
    return InlineKeyboardMarkup(rows)


def question_strip_keyboard(admin: bool = True, clock: str = "") -> "object":
    """Buttons under a tournament question strip.

    The clock is not a Mini App button any more: it is the strip text itself,
    repainted while the question runs. These buttons only control the game, and
    they carry tournament callbacks (``zy|…``) — an ordinary game button would
    look for a state this strip does not have.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [[InlineKeyboardButton(f"{clock} · Vaqtni yangilash" if clock else "Vaqtni yangilash",
                                  callback_data=tour_data(TEAM_REFRESH))]]
    if admin:
        rows.append([
            InlineKeyboardButton("Javoblar holati", callback_data=tour_data(TEAM_REVEAL)),
            InlineKeyboardButton("O‘tkazish", callback_data=tour_data(TEAM_SKIP)),
            InlineKeyboardButton("Jadval", callback_data=tour_data(TEAM_TABLE)),
            InlineKeyboardButton("To‘xtatish", callback_data=tour_data(TEAM_STOP)),
        ])
    else:
        rows.append([InlineKeyboardButton("Jadval", callback_data=tour_data(TEAM_TABLE))])
    return InlineKeyboardMarkup(rows)


def tour_play_keyboard(curator: bool = False, clock: str = "") -> "object":
    """Buttons of the pinned tournament card while questions are running."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [[InlineKeyboardButton(f"{clock} · Jadval" if clock else "Jadval",
                                  callback_data=tour_data(TEAM_TABLE))]]
    if curator:
        rows.append([
            InlineKeyboardButton("Javoblar holati", callback_data=tour_data(TEAM_REVEAL)),
            InlineKeyboardButton("To‘xtatish", callback_data=tour_data(TEAM_STOP)),
        ])
    return InlineKeyboardMarkup(rows)



def page_keyboard(page: int, pages: int, query: str = ""):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if pages <= 1:
        return None
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("|‹ Boshi", callback_data=page_data(1, query)))
        row.append(InlineKeyboardButton("‹ Orqaga", callback_data=page_data(page - 1, query)))
    if page < pages:
        row.append(InlineKeyboardButton("Oldinga ›", callback_data=page_data(page + 1, query)))
        row.append(InlineKeyboardButton("Oxiri ›|", callback_data=page_data(pages, query)))
    return InlineKeyboardMarkup([row])


def stats_keyboard(view: str = "main", app_url: str = "", page: int = 1, pages: int = 1):
    """Buttons of the owner's statistics panel.

    ``app_url`` adds the Mini App row: the full dashboard opens inside Telegram,
    with the same numbers but room to show them properly.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    rows = []
    if app_url:
        rows.append([InlineKeyboardButton("Mini App — to‘liq panel",
                                          web_app=WebAppInfo(url=app_url))])
    row = [InlineKeyboardButton("Yangilash", callback_data=STATS_REFRESH)]
    if view == "main":
        row.append(InlineKeyboardButton("Bo‘limlar", callback_data=STATS_SECTIONS))
        row.append(InlineKeyboardButton("O‘yinlar", callback_data=STATS_EVENTS))
    elif view != "used":
        row.append(InlineKeyboardButton("Orqaga", callback_data=STATS_REFRESH))
    rows.append(row)
    rows.append([
        InlineKeyboardButton("Ishlatilgan savollar", callback_data=STATS_USED),
        InlineKeyboardButton("Turnir rejalari", callback_data=STATS_PLANS),
        InlineKeyboardButton("Ruxsatlar", callback_data=STATS_ACCESS),
    ])
    if view == "used" and pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("‹ Orqaga",
                                            callback_data=f"{STATS_USED_PAGE}|{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page} / {pages}", callback_data=STATS_USED))
        if page < pages:
            nav.append(InlineKeyboardButton("Oldinga ›",
                                            callback_data=f"{STATS_USED_PAGE}|{page + 1}"))
        rows.append(nav)
    return InlineKeyboardMarkup(rows)
