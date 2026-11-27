#!/usr/bin/env python3
"""Look at the Mini App panel in a browser, without Telegram.

    pip install -r requirements.txt uvicorn
    python3 scripts/preview_app.py                 # http://127.0.0.1:8080/app

The script turns on the local demo flag, fills a handful of counters so the panel
has something to show, and serves the same FastAPI application Vercel runs. The
real archive is read from ``data/all``, so the sections and package numbers are
genuine.

Nothing here talks to Telegram: no bot token is used, so the statistics backend
stays "local" and every number lives in this process only. ``ZAKOVAT_APP_DEMO``
is ignored on Vercel (``bot/webapp.demo_enabled`` refuses when ``VERCEL_ENV`` is
set), so the demo proof can never open the production panel.

Dark or light is whatever your browser or system prefers; inside Telegram the
page follows the app's own theme instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Local preview only: sign initData for the owner without a bot.
os.environ.setdefault("ZAKOVAT_APP_DEMO", "1")
# No token means no Telegram calls and no store writes: the numbers stay here.
os.environ.pop("BOT_TOKEN", None)
os.environ.pop("WEBHOOK_URL", None)
os.environ.pop("ZAKOVAT_STATS_URL", None)
os.environ.pop("BLOB_READ_WRITE_TOKEN", None)


def seed() -> None:
    """A believable week of play, so every card on the panel has a number."""
    from bot import stats

    people = [(8004724563, "Ega"), (111, "Aziz"), (222, "Sardor"), (333, "Malika"),
              (444, "Jasur"), (555, "Nodira"), (666, "Bekzod")]
    chats = [8004724563, -100111, -100222, -100333, 444]
    packages = [959, 1204, 3311, 7788, 12900]
    started = 0
    for index, (user_id, _name) in enumerate(people):
        for round_index in range(3 - (index % 3)):
            package_id = packages[(index + round_index) % len(packages)]
            chat_id = chats[index % len(chats)]
            started += 1
            stats.record("game_start", user_id=user_id, chat_id=chat_id,
                         package_id=package_id, games_started=1,
                         detail=f"{package_id} · solo")
            asked = 10 + (index * 3) % 11
            right = max(1, asked - 3 - (index % 4))
            stats.record("question", user_id=user_id, chat_id=chat_id, package_id=package_id,
                         questions_asked=asked, log=False)
            stats.record("correct", user_id=user_id, chat_id=chat_id, package_id=package_id,
                         answers_correct=right, log=False)
            stats.record("wrong", user_id=user_id, chat_id=chat_id, package_id=package_id,
                         answers_wrong=asked - right - 1, log=False)
            stats.record("unanswered", user_id=user_id, chat_id=chat_id, package_id=package_id,
                         unanswered=1, log=False)
            if index % 2 == 0:
                stats.record("skip", user_id=user_id, chat_id=chat_id, skips=2, log=False)
                stats.record("refresh", user_id=user_id, chat_id=chat_id, refreshes=1,
                             log=False)
            if index % 3 == 0:
                stats.record("auto_stop", user_id=user_id, chat_id=chat_id,
                             games_auto_stopped=1, detail=f"{package_id} · javobsiz")
            else:
                stats.record("game_end", user_id=user_id, chat_id=chat_id,
                             package_id=package_id, games_finished=1,
                             detail=f"{package_id} · yakun")
    for _ in range(9):
        stats.record("clock_opened", user_id=people[_ % len(people)][0], clock_opened=1,
                     log=False)
    for name in ("/start", "/solo 959", "/stats", "/packages", "/score"):
        stats.record("command", user_id=8004724563, chat_id=8004724563, commands=1,
                     detail=name, log=False)
    import asyncio

    for user_id, name in ((111, "Aziz"), (222, "Sardor"), (333, "Malika")):
        asyncio.run(stats.set_access(user_id, name, True))
    print(f"to‘ldirildi: {started} o‘yin · {len(people)} odam · "
          f"{len(stats.access_list())} ta ruxsat")


def main() -> int:
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    try:
        import uvicorn
    except ImportError:
        print("uvicorn kerak: pip install uvicorn")
        return 1
    seed()
    print(f"\nMini App paneli:  http://127.0.0.1:{port}/app")
    print(f"Bot holati:       http://127.0.0.1:{port}/api/index")
    print(f"Mini App sinovi:  http://127.0.0.1:{port}/api/index?appprobe=1")
    print("Qora/yorug‘ rejim brauzer sozlamasiga qaraydi (DevTools → Rendering → "
          "Emulate CSS prefers-color-scheme).\n")
    uvicorn.run("bot.webhook:app", host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
