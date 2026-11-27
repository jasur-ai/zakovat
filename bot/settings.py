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
    With a clock_url the strip also offers the Mini App clock face.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    label = f"{clock} · Vaqtni yangilash" if clock else "Vaqtni yangilash"
    row = [InlineKeyboardButton(label, callback_data=GAME_REFRESH)]
    if admin:
        row.append(InlineKeyboardButton("O‘tkazish", callback_data=GAME_SKIP))
    row.append(InlineKeyboardButton("Hisob", callback_data=GAME_SCORE))
    row.append(InlineKeyboardButton("To‘xtatish", callback_data=GAME_STOP))
    rows = []
    if clock_url:
        # A Mini App counts down in the user's own browser: smooth, and it keeps
        # running even when the network to the bot is slow.
        rows.append([InlineKeyboardButton("Jonli soat", web_app=WebAppInfo(url=clock_url))])
    rows.append(row)
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


def stats_keyboard(view: str = "main", app_url: str = ""):
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
    else:
        row.append(InlineKeyboardButton("Orqaga", callback_data=STATS_REFRESH))
    rows.append(row)
    return InlineKeyboardMarkup(rows)
