"""All user-facing bot texts in one place (Uzbek, HTML parse mode).

Style: no decorative emoji. Structure is carried by short capitalised headings,
numbered lists and a text countdown bar, so the game reads like a protocol
rather than a sticker chat.
"""
from __future__ import annotations

import html
import re

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
