"""All user-facing bot texts in one place (Uzbek, HTML parse mode).

Style: no decorative emoji. Structure is carried by short capitalised headings,
numbered lists and a text countdown bar, so the game reads like a protocol
rather than a sticker chat.
"""
from __future__ import annotations

import html
import re
import time

BAR_WIDTH = 10
IDLE_HINT = 3


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def bar(remaining: int, total: int, width: int = BAR_WIDTH) -> str:
    """Text countdown bar: ▰▰▰▱▱▱▱▱▱▱"""
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, round(width * remaining / total)))
    return "▰" * filled + "▱" * (width - filled)


def timer_line(remaining: int, total: int) -> str:
    if remaining <= 0:
        return "Vaqt tugadi"
    return f"Qolgan vaqt: {remaining} s   {bar(remaining, total)}"


def clock(remaining: int) -> str:
    """Countdown digits, the way a timer reads: 01:00, 00:47, 00:03."""
    remaining = max(0, int(remaining))
    return f"{remaining // 60:02d}:{remaining % 60:02d}"


def countdown(remaining: int, total: int) -> str:
    """The live countdown of the strip: clock digits over a shrinking bar.

    Every tick rewrites both, so the numbers and the bar visibly move instead of
    one time value being written down once.
    """
    if remaining <= 0:
        return "VAQT TUGADI  00:00\n" + "▱" * BAR_WIDTH
    return f"QOLGAN VAQT  {clock(remaining)}\n{bar(remaining, total)}"


def welcome(packages: int, questions: int) -> str:
    return (
        "<b>ZAKOVAT AGENT</b>\n"
        "Savol-javob o‘yini boti — shaxsiy chat va yopiq guruhlar uchun.\n\n"
        f"Arxiv: {packages} paket, {questions} savol.\n\n"
        "<b>O‘YINNI BOSHLASH</b>\n"
        "• /solo — yakka o‘yin, javobni faqat boshlagan kishi yozadi\n"
        "• /duel — guruh o‘yini, barcha ishtirokchi javob yozadi\n"
        "• /solo 959 — aniq paket bilan\n\n"
        "<b>SOZLAMALAR</b>\n"
        "O‘yindan oldin uchta parametr tanlanadi: raundlar soni, har raunddagi "
        "savollar va har savolga ajratiladigan vaqt.\n\n"
        "<b>HISOB</b>\n"
        "To‘g‘ri javob — Bilimdonlar +1.\n"
        "Javob berilmagan, muddati o‘tgan yoki o‘tkazilgan savol — Qarshi taraf +1.\n"
        "10 savoldan 4 tasiga javob berilsa hisob 4–6 bo‘ladi.\n\n"
        "Ketma-ket uch savolga hech qanday taxmin yozilmasa, o‘yin avtomatik to‘xtatiladi.\n\n"
        "Buyruqlar: /help     Qoidalar: /rules"
    )


def help_text() -> str:
    return (
        "<b>BUYRUQLAR</b>\n\n"
        "<b>O‘yin</b>\n"
        "/solo — yakka o‘yin\n"
        "/duel — guruh o‘yini\n"
        "/solo 959 — aniq paket bilan\n"
        "/stop — o‘yinni to‘xtatish va yakuniy hisob\n\n"
        "<b>Savol paytida</b>\n"
        "Javob — savol xabariga reply qilib yoziladi\n"
        "/score — joriy hisob va raund\n"
        "/skip — savolni o‘tkazish va javobini ko‘rsatish (faqat admin)\n"
        "/accept — javobni to‘g‘ri deb qabul qilish (faqat admin)\n"
        "«Vaqtni yangilash» tugmasi — qolgan vaqtni ko‘rsatadi\n\n"
        "<b>Ma’lumot</b>\n"
        "/packages — barcha paketlar, sahifalab\n"
        "/packages Zakovat — sarlavhadan qidirish\n"
        "/packages sahifa 5 — aniq sahifa\n"
        "/rules — qoidalar\n"
        "/start — tanishtiruv\n\n"
        "<b>HISOB QOIDASI</b>\n"
        "To‘g‘ri javob — Bilimdonlar +1. Javobsiz, muddati o‘tgan yoki o‘tkazilgan "
        "savol — Qarshi taraf +1. Ketma-ket uch savol javobsiz qolsa, o‘yin "
        "avtomatik to‘xtatiladi."
    )


def rules() -> str:
    return (
        "<b>QOIDALAR</b>\n\n"
        "1. O‘yin boshlanishida raundlar soni, har raunddagi savollar va har "
        "savolga vaqt tanlanadi.\n"
        "2. Bot savol yuboradi. Javob shu xabarga reply qilib yoziladi.\n"
        "3. Har savol ostida qolgan vaqt ko‘rsatiladi va u o‘zi yangilanib boradi.\n"
        "4. To‘g‘ri javob — Bilimdonlar +1. Javob berilmasa, vaqt tugasa yoki "
        "savol o‘tkazilsa — Qarshi taraf +1.\n"
        "5. 10 savoldan 4 tasiga javob berilsa hisob 4–6 bo‘ladi.\n"
        "6. Yakka o‘yinda javobni faqat o‘yinni boshlagan kishi yozadi; guruhda "
        "barcha ishtirok etadi.\n"
        "7. /skip va /accept faqat admin uchun: guruhda administrator yoki o‘yinni "
        "boshlagan kishi.\n"
        "8. Har savoldan keyin to‘g‘ri javob va izoh ko‘rsatiladi.\n"
        "9. Ketma-ket uch savolga hech qanday taxmin yozilmasa, o‘yin avtomatik "
        "to‘xtatiladi va yakuniy hisob e’lon qilinadi."
    )


def group_hello() -> str:
    return (
        "<b>ZAKOVAT AGENT</b>\n\n"
        "Bu guruhda savol-javob o‘yini o‘tkazaman. Boshlash: /duel — raundlar, "
        "savollar soni va vaqt tanlanadi.\n\n"
        "Hisob: to‘g‘ri javob — Bilimdonlar +1; javobsiz, muddati o‘tgan yoki "
        "o‘tkazilgan savol — Qarshi taraf +1. Javobni savol xabariga reply qilib "
        "yozing.\n\n"
        "Qoidalar: /rules"
    )


def settings_panel(package_id: int, title: str, available: int, rounds: int, per_round: int,
                   timer: int, mode: str, total: int) -> str:
    label = "Yakka o‘yin" if mode == "solo" else "Guruh o‘yini"
    per_round_text = "barcha savol" if per_round <= 0 else f"{per_round} savol"
    total_text = f"{total} savol" if total else "barcha savol"
    return (
        f"<b>SOZLAMALAR</b> · {esc(label)}\n\n"
        f"Paket: {package_id} — {esc(title)}\n"
        f"Bazada: {available} savol\n"
        f"Raundlar: {rounds}\n"
        f"Har raundda: {per_round_text}\n"
        f"Har savolga: {timer} sekund\n"
        f"Jami: {total_text}\n\n"
        "Qiymatlarni tugmalardan tanlang, so‘ng «Boshlash» ni bosing."
    )


def repeat_notice(available: int, extra: int) -> str:
    return f"Bazada {available} savol bor; qolgan {extra} savol takrorlanadi."


def game_start(label: str, title: str, total: int, timer: int, score: str,
               rounds: int = 1, per_round: int = 0) -> str:
    structure = f"{rounds} raund × {per_round} savol" if per_round else f"{rounds} raund"
    return (
        f"<b>{esc(label).upper()} BOSHLANDI</b>\n\n"
        f"Paket: {esc(title)}\n"
        f"Tuzilma: {structure}\n"
        f"Jami savol: {total} · har biriga {timer} sekund\n"
        f"Hisob: {esc(score)}\n\n"
        "Javoblarni savol xabariga reply qilib yozing. To‘xtatish: /stop"
    )


def round_banner(number: int, rounds: int, score: str) -> str:
    return f"<b>{number}-RAUND</b> ({number}/{rounds})\nHisob: {esc(score)}"


def question(number: int, total: int, title: str, text: str, timer: int, score: str,
             attachment: str = "", round_label: str = "", remaining: int | None = None) -> str:
    """The question itself. The live countdown lives on the strip above it, so a
    timer line is rendered here only when `remaining` is given explicitly."""
    head = f"<b>SAVOL {number} / {total}</b>"
    if round_label:
        head += f" · {esc(round_label)}"
    result = f"{head}\n{esc(title)}\n\n{esc(str(text)[:2800])}\n\n"
    result += f"Javobni shu xabarga reply qilib yozing. Vaqt: {timer} sekund.\n"
    if remaining is not None:
        result += timer_line(remaining, timer) + "\n"
    result += f"Hisob: {esc(score)}"
    if attachment:
        result += f"\nRasm: {esc(attachment)}"
    return result


def reveal(headline: str, answer: str, comment: str = "", attachment: str = "", score: str = "") -> str:
    result = f"{headline}\n<b>Javob:</b> {esc(str(answer)[:1500])}"
    if comment:
        result += f"\n<b>Izoh:</b> {esc(str(comment)[:1500])}"
    if attachment:
        result += f"\nRasm: {esc(attachment)}"
    if score:
        result += f"\n\nHisob: {esc(score)}"
    return result


def summary(title: str, total: int, correct: int, wrong: int, percent: int, rounds: int = 1) -> str:
    head = f"<b>O‘YIN YAKUNI</b> · {esc(title)}\n"
    if rounds > 1:
        head += f"Raundlar: {rounds}\n"
    return (
        head
        + f"Hisob: Bilimdonlar {correct} — {wrong} Qarshi taraf\n"
        + f"To‘g‘ri javoblar: {correct}/{total} ({percent}%)\n\n"
        + "Yangi o‘yin: /solo yoki /duel"
    )


def auto_stop(streak: int, title: str, total: int, correct: int, wrong: int,
              percent: int, rounds: int = 1) -> str:
    return (
        f"<b>O‘YIN AVTOMATIK TO‘XTATILDI</b>\n"
        f"Ketma-ket {streak} savolga hech qanday taxmin yozilmadi.\n\n"
        + summary(title, total, correct, wrong, percent, rounds)
    )


def packages_page(result: dict, query: str = "") -> str:
    items = result["items"]
    start = result["start"]
    scope = f"«{esc(query)}» bo‘yicha" if query else "barcha"
    lines = [
        f"<b>PAKETLAR</b> — {scope} · {result['count']} ta",
        f"Sahifa {result['page']} / {result['pages']}",
        "",
    ]
    for offset, package in enumerate(items, start=start + 1):
        title = esc(package.get("title") or "(sarlavhasiz)")
        count = package.get("questions") or 0
        lines.append(f"{offset}. {package['id']} — {title} ({count} savol)")
    lines.append("")
    lines.append("Boshlash: /solo 959     Qidirish: /packages so‘z")
    return "\n".join(lines)


def admin_only(action: str = "bu amal") -> str:
    return (
        f"{esc(action)} faqat admin uchun: guruhda administrator bo‘ling yoki "
        "o‘yinni o‘zingiz boshlang."
    )


def no_delete_rights() -> str:
    return ("Eslatma: botda xabarlarni o‘chirish ruxsati yo‘q, shuning uchun hisoblagich "
            "o‘zi yangilanmaydi. Jonli taymer uchun botni guruhda administrator qiling "
            "(«Xabarlarni o‘chirish» ruxsati bilan).")


def idle_warning(streak: int, limit: int = IDLE_HINT) -> str:
    """Shown when a question closes without any guess at all."""
    left = max(0, limit - streak)
    if left <= 1:
        return ("Ogohlantirish: ketma-ket {0} savol javobsiz qoldi. Yana bir savolga "
                "taxmin yozilmasa, o‘yin avtomatik to‘xtatiladi.").format(streak)
    return ("Ogohlantirish: ketma-ket {0} savol javobsiz qoldi. Yana {1} savol javobsiz "
            "qolsa, o‘yin avtomatik to‘xtatiladi.").format(streak, left)


def small_talk(text: str) -> str | None:
    """Reply to casual messages when no quiz is active. Never used for answers."""
    value = re.sub(r"\s+", " ", (text or "").strip().casefold())
    value = value.strip("!.,?")
    greetings = {
        "salom", "assalomu alaykum", "assalom", "salam", "hi", "hello", "yo‘v", "yov",
    }
    if value in greetings or value.startswith("assalomu alaykum"):
        return (
            "Assalomu alaykum. Men Zakovat Agentman.\n\n"
            "O‘yinni boshlash: /solo (yakka) yoki /duel (guruh). Qoidalar: /rules"
        )
    if value in {"rahmat", "thanks", "spasibo"} or value.startswith("rahmat"):
        return "Arzimaydi. Davom etish: /solo"
    if any(word in value for word in ("qanday o‘ynayman", "qanday oynayman", "qoida", "qoidalar", "nima qilish")):
        return rules()
    if value in {"yordam", "help", "komak"} or value.startswith("yordam"):
        return help_text()
    if value in {"paketlar", "paket", "savollar", "baza"}:
        return "Paketlar ro‘yxati: /packages — barcha paketlar sahifalab chiqadi."
    if value in {"hisob", "score", "natija"}:
        return "Joriy hisob: /score yoki savol xabariga reply qiling."
    if value in {"kim", "nima", "bot", "about"}:
        return (
            "Zakovat Agent — arxivlangan 30 853 savol bilan ishlaydigan o‘yin boti. "
            "Tanishtiruv: /start     Buyruqlar: /help"
        )
    return None


EVENT_LABELS = {
    "game_start": "o‘yin boshlandi",
    "game_end": "o‘yin yakunlandi",
    "auto_stop": "avto-to‘xtatildi",
    "stopped": "to‘xtatildi",
    "correct": "to‘g‘ri javob",
    "wrong": "noto‘g‘ri javob",
    "unanswered": "javobsiz qoldi",
    "question": "savol berildi",
    "skip": "o‘tkazildi",
    "refresh": "vaqt yangilandi",
    "clock_opened": "jonli soat ochildi",
    "app_opened": "mini app ochildi",
    "app_denied": "ruxsatsiz urinish",
    "grant": "ruxsat berildi",
    "revoke": "ruxsat bekor qilindi",
    "probe": "saqlash tekshiruvi",
    "command": "buyruq",
}


def stats_denied(user_id: int | None = None) -> str:
    """Refusal copy; it carries the asker's id so the owner can grant it."""
    lines = [
        "<b>KIRISH YO‘Q</b>",
        "Bu bo‘lim faqat bot egasi va u ruxsat bergan foydalanuvchilar uchun.",
        "Urinish qayd etildi.",
    ]
    if user_id:
        lines.append("")
        lines.append(f"Sizning id: <code>{int(user_id)}</code>")
        lines.append("Uni bot egasiga yuboring — u /grant bilan ruxsat beradi.")
    return "\n".join(lines)


def access_usage() -> str:
    """How the owner hands out Mini App access."""
    return "\n".join([
        "<b>MINI APP RUXSATLARI</b>",
        "• /grant 123456789 Ism — ruxsat berish",
        "• /revoke 123456789 — ruxsatni olish",
        "• /access — kimlar ruxsat olganini ko‘rish",
        "",
        "Foydalanuvchi id sini bilish oson: u botga istalgan xabar yozsa, rad javobida "
        "o‘z id sini ko‘radi va sizga yuboradi.",
    ])


def access_list(rows: list, app_url: str = "") -> str:
    """Who may open the Mini App right now."""
    lines = ["<b>MINI APP RUXSATLARI</b>"]
    if app_url:
        lines.append(f"Manzil: {esc(app_url)}")
        lines.append("Egada va ruxsat olganlarda chat menyusida «Statistika» tugmasi bor.")
    lines.append("")
    if not rows:
        lines.append("Hozircha hech kimga ruxsat berilmagan.")
        lines.append("Qo‘shish: /grant 123456789 Ism")
        return "\n".join(lines)
    lines.append(f"• Ruxsat olganlar: {len(rows)}")
    for row in rows[:20]:
        name = esc(str(row.get("name") or ""))
        stamp = _stamp(row.get("since"))
        lines.append(f"• {row.get('id')}{f' — {name}' if name else ''} · {stamp}")
    if len(rows) > 20:
        lines.append(f"• … yana {len(rows) - 20} ta")
    lines.append("")
    lines.append("Bekor qilish: /revoke 123456789")
    return "\n".join(lines)


def access_changed(target: int, name: str, granted: bool, saved: bool, menu: str) -> str:
    """Confirmation after a grant or a revoke."""
    label = f"{target} — {esc(name)}" if name else str(target)
    lines = [f"<b>{'RUXSAT BERILDI' if granted else 'RUXSAT BEKOR QILINDI'}</b>",
             f"• {label}",
             f"• Saqlash: {'yozildi' if saved else 'yozilmadi — saqlagich bilan bog‘lanish yo‘q'}",
             f"• Menyu tugmasi: {esc(menu)}",
             ""]
    if granted:
        lines.append("Foydalanuvchi bot bilan chatini ochsa, pastdagi menyuda «Statistika» "
                     "tugmasini ko‘radi.")
    else:
        lines.append("Foydalanuvchining menyusi oddiy holatga qaytdi va Mini App unga yopildi.")
    return "\n".join(lines)


def _stamp(seconds: object) -> str:
    try:
        return time.strftime("%d.%m %H:%M", time.gmtime(int(seconds or 0)))
    except (TypeError, ValueError, OSError):
        return "--.-- --:--"


def _storage_line(data: dict) -> str:
    host = (data.get("storage") or "").split("//")[-1].split("/")[0]
    if data.get("stored") and data.get("dry_run"):
        return f"Manba: {esc(host or 'tashqi saqlash')} · sinov rejimi, yozilmadi"
    if data.get("stored"):
        state = "yozildi" if data.get("synced") else "o‘qildi, yozish amalga oshmadi"
        return f"Manba: {esc(host or 'tashqi saqlash')} · {state}"
    return "Manba: faqat shu nusxa · ZAKOVAT_STATS_URL berilsa, barcha o‘yinlar bir joyda yig‘iladi"


def stats_panel(data: dict) -> str:
    """Owner dashboard: games, questions, archive sections and the last events."""
    counters = data.get("counters") or {}
    archive = data.get("archive") or {}
    instance = data.get("instance") or {}
    lines = ["<b>STATISTIKA</b> · Zakovat Agent", _storage_line(data), ""]

    lines.append("<b>O‘YINLAR</b>")
    lines.append(f"• Boshlangan: {counters.get('games_started', 0)} · "
                 f"Yakunlangan: {counters.get('games_finished', 0)}")
    lines.append(f"• Avto-to‘xtagan: {counters.get('games_auto_stopped', 0)} · "
                 f"To‘xtatilgan: {counters.get('games_stopped', 0)}")
    lines.append(f"• Foydalanuvchilar: {data.get('users', 0)} · Chatlar: {data.get('chats', 0)}")
    lines.append(f"• O‘ynalgan paketlar: {data.get('packages_played', 0)}")
    lines.append("")

    lines.append("<b>SAVOLLAR</b>")
    lines.append(f"• Berilgan: {counters.get('questions_asked', 0)}")
    lines.append(f"• To‘g‘ri: {counters.get('answers_correct', 0)} · "
                 f"Noto‘g‘ri: {counters.get('answers_wrong', 0)} · "
                 f"Javobsiz: {counters.get('unanswered', 0)}")
    lines.append(f"• Aniqlik: {data.get('accuracy', 0)}%")
    lines.append(f"• O‘tkazishlar: {counters.get('skips', 0)} · "
                 f"Yangilash bosishlari: {counters.get('refreshes', 0)}")
    lines.append(f"• Jonli soat oynasi: {counters.get('clock_opened', 0)} marta")
    lines.append(f"• Mini App ochildi: {counters.get('app_opened', 0)} marta · "
                 f"ruxsat olganlar: {len(data.get('access') or {})} · /access")
    lines.append(f"• Buyruqlar: {counters.get('commands', 0)}")
    lines.append("")

    if archive:
        lines.append("<b>ARXIV</b>")
        lines.append(f"• Turkumlar (paketlar): {archive.get('packages', 0)}")
        lines.append(f"• Savollar: {archive.get('questions', 0)}")
        lines.append(f"• Bo‘limlar: {archive.get('section_count', 0)}")
        lines.append(f"• Rasmi bor savollar: {archive.get('pictures', 0)}")
        lines.append(f"• O‘rtacha paket: {archive.get('average', 0)} savol")
        biggest = archive.get("biggest") or {}
        if biggest.get("title"):
            lines.append(f"• Eng katta paket: {esc(biggest['title'])} "
                         f"({biggest.get('questions', 0)} savol)")
        lines.append(f"• Kirillcha paketlar: {archive.get('cyrillic', 0)}")
        if archive.get("exported_at"):
            lines.append(f"• Eksport: {esc(str(archive['exported_at'])[:10])}")
        lines.append("")

    packages = data.get("top_packages") or []
    if packages:
        lines.append("<b>KO‘P O‘YNALGAN PAKETLAR</b>")
        for package_id, times in packages:
            lines.append(f"• {esc(package_id)} — {times} marta")
        lines.append("")

    sections = ((data.get("archive") or {}).get("sections") or [])[:6]
    if sections:
        lines.append("<b>BO‘LIMLAR</b>")
        for entry in sections:
            lines.append(f"• {esc(entry.get('name'))} — {entry.get('packages', 0)} paket, "
                         f"{entry.get('questions', 0)} savol")
        lines.append("")

    events = (data.get("events") or [])[:5]
    if events:
        lines.append("<b>OXIRGI HARAKATLAR</b>")
        for event in events:
            label = EVENT_LABELS.get(str(event.get("k") or ""), str(event.get("k") or "—"))
            detail = f" · {esc(event['x'])}" if event.get("x") else ""
            lines.append(f"• {_stamp(event.get('t'))} — {label}{detail}")
        lines.append("")

    if instance:
        counters_now = instance.get("counters") or {}
        lines.append("<b>SHU NUSXADA</b>")
        lines.append(f"• Ishga tushgani: {instance.get('uptime', 0)} s · "
                     f"o‘yinlar: {counters_now.get('games_started', 0)} · "
                     f"savollar: {counters_now.get('questions_asked', 0)}")
        lines.append(f"• Foydalanuvchilar: {instance.get('users', 0)} · "
                     f"chatlar: {instance.get('chats', 0)} · "
                     f"paketlar: {instance.get('packages', 0)}")
    return "\n".join(lines)


def stats_sections(data: dict) -> str:
    """All archive sections with their package and question counts."""
    sections = ((data.get("archive") or {}).get("sections")) or []
    if not sections:
        return "<b>BO‘LIMLAR</b>\n• ma’lumot yo‘q"
    lines = ["<b>BO‘LIMLAR</b> · hammasi"]
    for entry in sections:
        lines.append(f"• {esc(entry.get('name'))} — {entry.get('packages', 0)} paket, "
                     f"{entry.get('questions', 0)} savol")
    return "\n".join(lines)


def stats_events(data: dict, limit: int = 25) -> str:
    """The most recent recorded events, newest first."""
    events = (data.get("events") or [])[:limit]
    if not events:
        return "<b>OXIRGI HARAKATLAR</b>\n• hozircha yozuv yo‘q"
    lines = ["<b>OXIRGI HARAKATLAR</b>"]
    for event in events:
        label = EVENT_LABELS.get(str(event.get("k") or ""), str(event.get("k") or "—"))
        detail = f" · {esc(event['x'])}" if event.get("x") else ""
        lines.append(f"• {_stamp(event.get('t'))} — {label}{detail}")
    return "\n".join(lines)
