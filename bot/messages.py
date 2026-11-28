"""All user-facing bot texts in one place (Uzbek, HTML parse mode).

Style: no decorative emoji. Structure is carried by short capitalised headings,
numbered lists and a text countdown bar, so the game reads like a protocol
rather than a sticker chat.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime
from typing import Sequence

from . import schedule as sched
from . import tournament as tn

BAR_WIDTH = 20
IDLE_HINT = 3
URGENT_SECONDS = 10


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def bar(remaining: int, total: int, width: int = BAR_WIDTH) -> str:
    """Text countdown bar: ▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱

    Twenty segments instead of ten, so with a 60-second question one segment is
    three seconds: the bar visibly walks down between two repaints instead of
    jumping a tenth of the way at a time.
    """
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
        return "<b>VAQT TUGADI</b> · 00:00\n" + "▱" * BAR_WIDTH
    label = "VAQT TUGAYAPTI" if remaining <= URGENT_SECONDS else "QOLGAN VAQT"
    return f"<b>{clock(remaining)}</b> · {label}\n{bar(remaining, total)}"


def welcome(packages: int, questions: int, sources: Sequence[tuple[str, int, int]] | None = None) -> str:
    """sources: (manba nomi, paketlar, savollar) — bir nechta baza bo‘lsa."""
    base = f"Arxiv: {packages} paket, {questions} savol.\n"
    if sources:
        base += "".join(f"• {esc(name)} — {count} paket, {total} savol\n"
                        for name, count, total in sources)
    return (
        "<b>ZAKOVAT AGENT</b>\n"
        "Savol-javob o‘yini boti — shaxsiy chat va yopiq guruhlar uchun.\n\n"
        f"{base}\n"
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


HELP_SECTIONS = (
    ("oyin", "O‘YIN", (
        "/solo — yakka o‘yin · /solo 959 — aniq paket bilan",
        "/duel — guruh o‘yini",
        "/packages — barcha paketlar, sahifalab · /packages Zakovat — qidirish",
        "/solo katta — butun bazadan tasodifiy to‘plam (ikki manba)",
        "/score — joriy hisob va raund",
        "/skip — savolni o‘tkazish va javobini ko‘rsatish (faqat admin)",
        "/accept — javobni to‘g‘ri deb qabul qilish (faqat admin)",
        "/stop — o‘yinni to‘xtatish va yakuniy hisob",
    )),
    ("turnir", "TURNIR · RO‘YXATDAN O‘TISH", (
        "/turnir — turnir ochish (faqat admin yoki kurator)",
        "/turnir 959 vaqt=19:00 jamoa=6 tur=3 savol=10 — turnirni bir qatorda "
        "shakllantirish (reja, jamolar soni, tuzilma)",
        "/jamoa Nom — yangi jamoa (yozgan odam kapitan bo‘ladi)",
        "/qoshil Nom — jamoaga qo‘shilish · /chiqish — chiqib ketish",
        "/tayyor — «Men tayyorman» (har jamoadan kamida 2 kishi)",
        "/azolar — o‘z jamoangiz a’zolari va ularning id’si",
    )),
    ("savol", "TURNIR · SAVOL PAYTIDA", (
        "/savollar — turnir savollari ro‘yxati, sahifalab (hammaga ochiq)",
        "/savol 12345 — bitta savol; javobi e’lon qilingach hammaga ko‘rinadi",
        "/yakuniy matn — jamoaning bitta yakuniy javobi (tasmaga reply qilib)",
        "/bekor — yakuniy javobni olib tashlash",
        "/pichoq matn — o‘z jamoangizga maxfiy xabar (guruhda ko‘rinmaydi)",
        "/jadval — ochkolar va o‘rinlar",
    )),
    ("kurator", "KURATOR (faqat kurator va admin)", (
        "/tanlov [so‘z] — savollar ro‘yxati: bosib olish yoki chiqarish",
        "/olish 12345 — savolni tanlovga olish · /chiqarish 12345 — chiqarish",
        "/kurator 123456789 — kurator tayinlash · … olib — tayinlovni olish",
        "/turnirstop — turnirni to‘xtatish va yakuniy jadval",
        "Har savoldan keyin javoblar ro‘yxati shu yerga keladi: har jamoa yonidagi "
        "tugma xulosani o‘zgartiradi, «TASDIQLASH» natijani guruhga yozadi",
    )),
    ("ega", "BOT EGASI", (
        "/stats — statistika paneli · /access — Mini App ruxsatlari",
        "/stats ishlatilgan — ishlatilgan savollar indeksi · /stats reja — turnir rejalari",
        "/grant 123456789 Ism — ruxsat berish · /revoke 123456789 — olish",
    )),
)

HELP_ALIASES = {
    "oyin": "oyin", "o‘yin": "oyin", "o'yin": "oyin", "game": "oyin", "solo": "oyin",
    "duel": "oyin",
    "turnir": "turnir", "tournament": "turnir", "jamoa": "turnir", "ro‘yxat": "turnir",
    "savol": "savol", "yakuniy": "savol", "pichoq": "savol", "maxfiy": "savol",
    "kurator": "kurator", "curator": "kurator", "tanlov": "kurator",
    "ega": "ega", "owner": "ega", "stats": "ega", "statistika": "ega",
}


def help_text(section: str = "") -> str:
    """The command list, in groups. ``/help turnir`` narrows it to one group."""
    key = HELP_ALIASES.get(str(section or "").strip().casefold(), "")
    blocks = ["<b>BUYRUQLAR</b>" + (" · " + str(section).strip().upper() if key else "")]
    for name, title, lines in HELP_SECTIONS:
        if key and name != key:
            continue
        blocks.append("")
        blocks.append(f"<b>{title}</b>")
        blocks.extend(lines)
    blocks += [
        "",
        "<b>MA’LUMOT</b>",
        "/start — tanishtiruv · /rules — qoidalar · /help — shu ro‘yxat",
    ]
    if not key:
        blocks.append("/help turnir · /help kurator — faqat bitta bo‘lim")
    blocks += [
        "",
        "<b>HISOB QOIDASI</b>",
        "Oddiy o‘yinda: to‘g‘ri javob — Bilimdonlar +1; javobsiz, muddati o‘tgan yoki "
        "o‘tkazilgan savol — Qarshi taraf +1; ketma-ket uch savol javobsiz qolsa, o‘yin "
        "avtomatik to‘xtatiladi.",
        "Tekshirish: guruhda javob darhol hisoblanmaydi — bot xulosasi admin yoki "
        "kuratorga ro‘yxat bo‘lib boradi, ochko «TASDIQLASH» dan keyin beriladi "
        "(30 va 90 sekundda eslatma, 180 sekundda «avtomatik» e’lon). Sozlamalarda "
        "o‘chiriladi; shaxsiy chatda so‘rovchi yo‘q, o‘yin avvalgidek yuradi.",
    ]
    if key in {"", "turnir", "savol", "kurator"}:
        blocks.append("Turnirda: har savolga har jamoa bitta yakuniy javob beradi, to‘g‘ri "
                      "javob — jamoaga +1, javob bermagan jamoa ochko yo‘qotmaydi.")
    return "\n".join(blocks)


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
        "to‘xtatiladi va yakuniy hisob e’lon qilinadi.\n"
        "10. Guruhda javobni admin tasdiqlaydi: bot xulosasi taklif sifatida "
        "adminga shaxsiy xabarda ko‘rsatiladi, ochko «TASDIQLASH» bosilgach "
        "beriladi. 30 va 90 sekundda eslatma keladi, 180 sekundda bot o‘z "
        "xulosasini «avtomatik» deb e’lon qiladi. Sozlamalarda o‘chiriladi."
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
                   timer: int, mode: str, total: int, confirm: bool = True) -> str:
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
        f"Jami: {total_text}\n"
        + ("Javobni tasdiqlash: YOQIQ — guruhda javobni admin tekshiradi va ochko "
           "tasdiqdan keyin beriladi (shaxsiy chatda so‘rovchi yo‘q, bot o‘zi "
           "hisoblaydi).\n" if confirm else
           "Javobni tasdiqlash: O‘CHIQ — bot o‘z xulosasini darhol hisoblaydi.\n")
        + "\nQiymatlarni tugmalardan tanlang, so‘ng «Boshlash» ni bosing."
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


def packages_page(result: dict, query: str = "", is_remote=None) -> str:
    """is_remote(package_id) — paket katta bazadan (turnirlar.uz) olinganmi."""
    items = result["items"]
    start = result["start"]
    scope = f"«{esc(query)}» bo‘yicha" if query else "barcha"
    lines = [
        f"<b>PAKETLAR</b> — {scope} · {result['count']} ta",
        f"Sahifa {result['page']} / {result['pages']}",
        "",
    ]
    remote_rows = 0
    for offset, package in enumerate(items, start=start + 1):
        title = esc(package.get("title") or "(sarlavhasiz)")
        count = package.get("questions") or 0
        mark = ""
        if is_remote is not None and is_remote(package["id"]):
            mark = " *"
            remote_rows += 1
        lines.append(f"{offset}. {package['id']} — {title} ({count} savol){mark}")
    lines.append("")
    lines.append("Boshlash: /solo 959     Qidirish: /packages so‘z")
    if remote_rows:
        lines.append("* — turnirlar.uz bazasidan: savollar o‘yin boshida yuklanadi")
    return "\n".join(lines)


def remote_unavailable(title: str = "") -> str:
    """A package from the big base whose shard could not be fetched."""
    head = f"«{esc(title)}» " if title else "Bu to‘plam "
    return (head + "turnirlar.uz bazasidan olinadi va hozir yuklanmadi. "
            "Bir ozdan so‘ng qayta urinib ko‘ring yoki <code>/packages</code> "
            "orqali boshqa to‘plam tanlang.")


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
    "tour_composed": "turnir rejalashtirildi",
    "tour_reminder": "turnir eslatmasi",
    "tour_package_swapped": "paket almashtirildi",
    "tour_plan_dropped": "rejadan chiqarildi",
    "tour_cancelled": "turnir rejasi bekor qilindi",
    "review_opened": "javoblar tekshiruvga olindi",
    "review_confirmed": "javoblar tasdiqlandi",
    "review_auto": "javoblar avtomatik e’lon qilindi",
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


# ------------------------------------------------------------------ turnir
def tour_intro(title: str, package_title: str, rounds: int, per_round: int, timer: int,
               total: int, sizes: dict, start_text: str = "", team_limit: int = 0) -> str:
    """The registration card: what is being played and what the rules require."""
    lines = [
        f"<b>TURNIR · {esc(title)}</b>",
        f"Paket: {esc(package_title or '(sarlavhasiz)')}",
        f"Tuzilma: {rounds} raund × {per_round} savol = {total} savol · har savolga {timer} s",
        f"• Jamolar soni: ko‘pi bilan {int(team_limit or sizes['max_teams'])} ta",
    ]
    if start_text:
        lines.append(f"• Boshlanish: <b>{esc(start_text)}</b> — o‘sha paytgacha ro‘yxatdan "
                     "o‘tib, «Men tayyorman» tugmasini bosib qo‘ying")
    lines += [
        "",
        "<b>RO‘YXATDAN O‘TISH</b>",
        f"• Kamida {sizes['min_teams']} ta jamoa, har birida {sizes['min_members']}–"
        f"{sizes['max_members']} kishi",
        f"• Har jamoadan kamida {sizes['min_ready']} kishi «Men tayyorman» tugmasini bosadi",
        "• Jamoa yaratish: «Yangi jamoa» tugmasi, so‘ng nomini yozing",
        "• Qo‘shilish: o‘z jamoangiz tugmasini bosing",
        "",
        "<b>O‘YIN TARTIBI</b>",
        "• Har savolga har jamoa <b>bitta yakuniy javob</b> beradi",
        "• Yakuniy javobni jamoaning istalgan a’zosi /yakuniy bilan yozadi",
        "• Vaqt tugaguncha javobni o‘zgartirish mumkin — oxirgisi hisoblanadi",
        "• To‘g‘ri yakuniy javob — jamoaga +1 ochko; bir savolda bir nechta jamoa "
        "ochko olishi mumkin",
        "• Savollar va e’lon qilingan javoblar shu guruhda hammaga ko‘rinadi",
    ]
    return "\n".join(lines)


def tour_reminder(title: str, start_text: str, teams: list, sizes: dict,
                  left: int = 0) -> str:
    """The message the bot posts when a scheduled tournament's time has come."""
    ready = [team for team in teams
             if int(team.get("ready") or 0) >= int(sizes.get("min_ready") or 2)]
    lines = [f"<b>ESLATMA · {esc(title)}</b>",
             f"Boshlanish vaqti: <b>{esc(start_text)}</b>"]
    if left > 0:
        lines.append(f"Guruhga {left // 60} daqiqa qoldi.")
    lines.append(f"Jamolar: {len(teams)} ta · tayyor: {len(ready)} ta "
                 f"(kamida {sizes.get('min_teams', 3)} jamoa va har biridan "
                 f"{sizes.get('min_ready', 2)} kishi «Men tayyorman» degan bo‘lishi kerak)")
    lines.append("Kurator yoki admin pastdagi «Boshlash» tugmasini bossa, o‘yin ochiladi.")
    return "\n".join(lines)


def tour_teams(teams: list, phase: str, sizes: dict) -> str:
    """The roster as it stands, one line per team."""
    if not teams:
        return "\n".join(["", "<b>JAMOALAR</b>", "• Hali hech kim ro‘yxatdan o‘tmagan."])
    lines = ["", "<b>JAMOALAR</b>"]
    for index, team in enumerate(teams, start=1):
        ready = sum(1 for person in team["members"] if person.get("ready"))
        mark = "tayyor" if (len(team["members"]) >= sizes["min_members"]
                            and ready >= sizes["min_ready"]) else "kutmoqda"
        lines.append(f"{index}. <b>{esc(team['name'])}</b> — {len(team['members'])} kishi · "
                     f"{ready} tayyor · {mark}")
        names = [esc(person.get("name") or person.get("username") or f"id {person['id']}")
                 for person in team["members"][:8]]
        if names:
            lines.append(f"   {' · '.join(names)}")
    if phase == "register":
        lines.append("")
        lines.append("Jamoani tark etish: /chiqish · holat: /turnir holat")
    return "\n".join(lines)


def tour_missing(problems: list) -> str:
    """What still has to happen before the tournament may start."""
    lines = ["<b>BOSHLASH UCHUN YETMAYAPTI</b>"]
    lines += [f"• {esc(item)}" for item in problems]
    return "\n".join(lines)


def tour_ready(total: int, people: int, teams: int) -> str:
    return ("Barcha shartlar bajarildi: "
            f"{teams} ta jamoa, {people} kishi, {total} savol. «Boshlash» tugmasini bosing.")


def tour_started(title: str, teams: list, total: int, timer: int, private: bool) -> str:
    lines = [
        f"<b>TURNIR BOSHLANDI · {esc(title)}</b>",
        f"• Savollar: {total} · har biriga {timer} sekund",
        "• Jamoalar: " + " · ".join(esc(team["name"]) for team in teams),
        "",
    ]
    if private:
        lines += [
            "<b>JAVOBLAR GURUHDA KO‘RINMAYDI</b>",
            "• Bot javobingizni o‘chiradi va faqat o‘z jamoangizga shaxsiy xabar bilan ko‘rsatadi",
            "• Yakuniy javob: savol ustidagi tasmaga reply qilib <code>/yakuniy javob</code>",
        ]
    else:
        lines += [
            "<b>JAVOBLAR GURUHDA KO‘RINADI</b>",
            "• Botda xabar o‘chirish huquqi yo‘q, shuning uchun javoblar ochiq qoladi",
            "• Yakuniy javob: tasmaga reply qilib <code>/yakuniy javob</code>",
        ]
    return "\n".join(lines)


def tour_strip(number: int, total: int, round_label: str, finals: int, teams: int,
               countdown: str) -> str:
    """The countdown strip of a tournament question."""
    head = f"SAVOL {number} / {total}"
    if round_label:
        head += f" · {round_label}"
    return f"{head}\nYakuniy javob bergan: {finals} / {teams} jamoa\n{countdown}"


def tour_question(number: int, total: int, round_label: str, package_title: str, text: str,
                  timer: int, link: str = "", standings_line: str = "") -> str:
    lines = [f"<b>SAVOL {number} / {total}</b>"]
    if round_label:
        lines[0] += f" · {round_label}"
    if package_title:
        lines.append(esc(package_title))
    lines.append("")
    lines.append(esc(text or "").replace("\n", "<br>"))
    if link:
        lines += ["", f"<a href=\"{esc(link)}\">Rasm / manba</a>"]
    lines += ["", f"Javob uchun {timer} sekund. Har jamoa bitta yakuniy javob beradi:"]
    lines.append("tasmaga reply qilib <code>/yakuniy javob</code> — vaqt tugaguncha "
                 "o‘zgartirish mumkin.")
    if standings_line:
        lines += ["", standings_line]
    return "\n".join(lines)


def tour_private_question(number: int, total: int, team: str, text: str, timer: int) -> str:
    """What one member sees in private: the question and how to answer."""
    return "\n".join([
        f"<b>TURNIR · {esc(team)}</b>",
        f"Savol {number} / {total} · javob uchun {timer} s",
        "",
        esc(text or ""),
        "",
        "Javobingizni shu yerga yozing — uni faqat o‘z jamoangiz ko‘radi.",
        "Yakuniy javob uchun guruhda tasmaga reply qilib "
        "<code>/yakuniy javob</code> deb yozing.",
    ])


def tour_final_set(team: str, answer: str, by: str, remaining: int, changed: bool) -> str:
    head = "YAKUNIY JAVOB O‘ZGARTIRILDI" if changed else "YAKUNIY JAVOB QABUL QILINDI"
    return "\n".join([
        f"<b>{head}</b>",
        f"• Jamoa: {esc(team)}",
        f"• Javob: {esc(answer)}",
        f"• Bergan: {esc(by)}",
        f"• Qolgan vaqt: {clock(remaining)}",
        "",
        f"Vaqt tugaguncha o‘zgartirish mumkin: yana <code>/yakuniy yangi javob</code>.",
    ])


def tour_final_seen(number: int, total: int, finals: list, teams: int) -> str:
    """The group line after a final answer lands: who answered, never what."""
    names = " · ".join(esc(item) for item in finals)
    return (f"Savol {number} / {total} · yakuniy javob berdi: {names} "
            f"({len(finals)} / {teams} jamoa)")


def tour_result(number: int, total: int, answer: str, comment: str, winners: list,
                finals: list, standings_line: str, decided: str = "") -> str:
    """The published result.

    ``decided`` says who stood behind the verdicts — a curator's name, or that the
    bot published on its own because nobody confirmed in time. A score nobody can
    trace to a person is not a professional score.
    """
    lines = [f"<b>SAVOL {number} / {total} YAKUNI</b>"]
    if decided:
        lines.append(f"• {esc(decided)}")
    if winners:
        lines.append("• Ochko oldi: " + " · ".join(esc(name) for name in winners))
    else:
        lines.append("• Hech qaysi jamoa ochko olmadi.")
    lines.append(f"• To‘g‘ri javob: <b>{esc(answer)}</b>")
    if comment:
        lines.append(f"• Izoh: {esc(comment)[:600]}")
    if finals:
        lines.append("")
        lines.append("<b>YAKUNIY JAVOBLAR</b>")
        for row in finals:
            mark = "to‘g‘ri" if row.get("correct") else ("javob bermadi" if not row.get("answer")
                                                         else "noto‘g‘ri")
            if row.get("changed"):
                mark += " (o‘zgartirildi)"
            answer_text = esc(row.get("answer") or "—")
            lines.append(f"• {esc(row.get('team'))}: {answer_text} — {mark}")
    if standings_line:
        lines += ["", standings_line]
    return "\n".join(lines)


def tour_standings(rows: list, title: str = "", heading: str = "JADVAL") -> str:
    """The score table: place, team, points, how many questions it solved."""
    lines = [f"<b>{esc(heading)}</b>"]
    if title:
        lines.append(esc(title))
    lines.append("")
    if not rows:
        lines.append("• Hozircha jamoa yo‘q.")
        return "\n".join(lines)
    best = int(rows[0].get("score") or 0)
    for row in rows:
        name = esc(row["name"])
        score = int(row["score"] or 0)
        line = f"{row['place']}. {name} — <b>{score}</b> ochko"
        if row.get("solved"):
            line += f" · {row['solved']} savol topildi"
        if row.get("size"):
            line += f" · {row['size']} kishi"
        if score == best and best:
            line += " · yetakchi"
        lines.append(line)
    return "\n".join(lines)


def tour_over(title: str, rows: list, asked: int, rounds: int) -> str:
    lines = [f"<b>TURNIR YAKUNLANDI · {esc(title)}</b>",
             f"• O‘ynalgan savollar: {asked}" + (f" · {rounds} raund" if rounds > 1 else "")]
    if rows:
        leaders = [row for row in rows if int(row["score"] or 0) == int(rows[0]["score"] or 0)]
        if int(rows[0]["score"] or 0) == 0:
            lines.append("• Hech qaysi jamoa ochko to‘plamadi.")
        elif len(leaders) > 1:
            lines.append("• G‘oliblar (teng ochko): " + " · ".join(esc(row["name"])
                                                                   for row in leaders))
        else:
            lines.append(f"• G‘olib: <b>{esc(rows[0]['name'])}</b> — "
                         f"{rows[0]['score']} ochko")
    lines += ["", tour_standings(rows, heading="YAKUNIY JADVAL")]
    return "\n".join(lines)


def tour_round_banner(number: int, rounds: int, standings_line: str) -> str:
    return f"<b>{number}-RAUND BOSHLANDI</b> · {rounds} raunddan" + (
        f"\n{standings_line}" if standings_line else "")


def tour_stopped(title: str, asked: int, total: int, rows: list) -> str:
    lines = [f"<b>TURNIR TO‘XTATILDI · {esc(title)}</b>",
             f"• O‘ynalgan: {asked} / {total} savol", "",
             tour_standings(rows, heading="JADVAL")]
    return "\n".join(lines)


def tour_denied(reason: str) -> str:
    return "\n".join(["<b>TURNIR UCHUN RUXSAT YO‘Q</b>", f"• {esc(reason)}"])


def tour_team_created(name: str, captain: str, sizes: dict) -> str:
    return "\n".join([
        f"<b>JAMOA RO‘YXATDAN O‘TDI</b>",
        f"• {esc(name)} · kapitan {esc(captain)}",
        f"• Kamida {sizes['min_members']} kishi, ko‘pi bilan {sizes['max_members']} kishi",
        f"• Boshlash uchun kamida {sizes['min_ready']} kishi «Men tayyorman» deydi",
        "",
        "A’zolarni guruhdagi tugmadan qo‘shing yoki /azolar bilan id larni ko‘ring.",
    ])


def tour_joined(team: str, name: str, size: int, ready: int, sizes: dict) -> str:
    lines = [f"<b>{esc(team)}</b> · {esc(name)} qo‘shildi",
             f"• Jamoada {size} kishi · {ready} tayyor"]
    if size < sizes["min_members"]:
        lines.append(f"• Yana {sizes['min_members'] - size} kishi kerak")
    if ready < sizes["min_ready"]:
        lines.append(f"• Yana {sizes['min_ready'] - ready} kishi «Men tayyorman» deyishi kerak")
    return "\n".join(lines)


def tour_left(team: str, name: str, dropped: bool) -> str:
    if dropped:
        return f"<b>{esc(team)}</b> bo‘shagani uchun o‘chirildi ({esc(name)} oxirgi a’zo edi)."
    return f"{esc(name)} <b>{esc(team)}</b> jamoasidan chiqdi."


def tour_not_registered() -> str:
    return ("Siz biror jamoada emassiz. Guruhdagi turnir xabaridan o‘z jamoangiz "
            "tugmasini bosing yoki «Yangi jamoa» bilan yangi jamoa yarating.")


# ------------------------------------------------------------------ pichoq
def whisper_usage() -> str:
    return "\n".join([
        "<b>MAXFIY XABAR</b>",
        "• /pichoq matn — o‘z jamoangizdagi barcha a’zolarga",
        "• /pichoq jamoa matn — xuddi shu, hammaga",
        "• /pichoq @a’zo matn — bitta a’zoga (faqat o‘z jamoangizdan)",
        "• /pichoq 123456789 matn — id orqali, yana o‘z jamoangizdan",
        "• /azolar — jamoangiz a’zolari va ularning id’si",
        "",
        "<b>KIM KO‘RADI</b>",
        "• Faqat o‘z jamoangiz a’zolari — guruhda xabar ko‘rinmaydi, bot uni o‘chiradi",
        "• Boshqa jamoalar, kuzatuvchilar va begona odamlar hech narsa ko‘rmaydi",
        "• Kurator va admin istalgan jamoaga yoza oladi: /pichoq JamoaNomi matn",
        "• Botda «Delete messages» huquqi bo‘lmasa, xabar yuborilmaydi — guruhda "
        "ko‘rinib qolgan matn maxfiy bo‘lolmaydi",
    ])


def whisper_received(sender: str, text: str, chat_title: str) -> str:
    return "\n".join([
        f"<b>MAXFIY XABAR</b> · {esc(chat_title)}",
        f"Kimdan: {esc(sender)}",
        "",
        esc(text),
        "",
        "Javob berish: /pichoq " + esc(sender) + " javobingiz",
    ])


def whisper_sent(target: str, text: str) -> str:
    return f"Xabar {esc(target)} ga shaxsiy yuborildi. Guruhda ko‘rinmaydi.\n{esc(text)[:300]}"


def whisper_team_sent(team: str, count: int, failed: int, text: str) -> str:
    lines = [f"Xabar <b>{esc(team)}</b> jamoasining {count} a’zosiga yuborildi."]
    if failed:
        lines.append(f"• {failed} kishiga yetib bormadi: ular bot bilan shaxsiy chatni "
                     "boshlamagan (/start).")
    lines.append(esc(text)[:300])
    return "\n".join(lines)


def whisper_failed(reason: str) -> str:
    return f"Xabar yuborilmadi: {esc(reason)}"


def whisper_dropped(chat_title: str) -> str:
    """The neutral group line when a whisper could not be deleted.

    Deliberately says nothing about who wrote what: the group must not learn
    that somebody tried to talk in private.
    """
    return (f"{esc(chat_title)} da bot xabarlarni o‘chira olmaydi — maxfiy xabar "
            "yuborilmadi. Admin botga «Delete messages» huquqini bersin.")


def whisper_no_tournament() -> str:
    """/pichoq outside a tournament: there is no team, so there is nobody to hide it for."""
    return "\n".join([
        "<b>MAXFIY XABAR</b>",
        "Maxfiy xabar faqat turnir jamoasi ichida yuradi — hozir bu chatda turnir yo‘q.",
        "",
        "• /turnir — turnir ochish (admin yoki kurator)",
        "• /jamoa Nom — jamoa yaratish · /qoshil Nom — jamoaga qo‘shilish",
        "• Keyin: /pichoq matn — faqat o‘z jamoangiz ko‘radi",
    ])


def answer_dropped(chat_title: str) -> str:
    """The neutral group line when an answer could not be deleted.

    It says nothing about who answered or what they wrote — only that the group
    still shows it and how to fix that for good.
    """
    return (f"{esc(chat_title)} da javob yuborildi, lekin botda xabar o‘chirish huquqi "
            "yo‘q — javobingizni o‘zingiz o‘chirib tashlang. Admin botga «Delete "
            "messages» huquqini bersa, javoblar faqat o‘z jamoangizga ko‘rinadi.")


def whisper_not_private(text: str) -> str:
    """Told to the sender when the group copy stayed readable."""
    return "\n".join([
        "<b>MAXFIY XABAR YUBORILMADI</b>",
        "Bu guruhda bot xabarlarni o‘chira olmaydi, shuning uchun yozganingiz hali "
        "guruhda ko‘rinib turibdi.",
        "",
        "• Xabaringizni guruhdan o‘chirib tashlang",
        "• Admin botga «Delete messages» huquqini bersin — keyin /pichoq ishlaydi",
        "",
        f"Yozganingiz: {esc(text)[:300]}",
    ])


def whisper_outside(reason: str) -> str:
    """A whisper that would leave the team is refused."""
    return "\n".join([
        "<b>MAXFIY XABAR YUBORILMADI</b>",
        f"• {esc(reason)}",
        "",
        "Maxfiy xabar faqat o‘z jamoangiz a’zolariga ketadi:",
        "• /pichoq matn — butun jamoaga",
        "• /pichoq @a’zo matn — bitta a’zoga",
        "• /azolar — kim qatnashayotgani va ularning id’si",
    ])


def members_list(teams: list, title: str, me: int, extra: list | None = None) -> str:
    """Who may receive a whisper: your own team (a curator sees every team).

    ``extra`` is the rest of the group the bot has seen — shown to a curator
    only, because appointing one of them is the curator's job.
    """
    lines = [f"<b>A’ZOLAR</b> · {esc(title)}"]
    shown = 0
    for team in teams:
        captain = int(getattr(team, "captain", 0) or 0)
        lines.append("")
        lines.append(f"<b>{esc(team.name)}</b> · {team.size} kishi")
        for person in team.members:
            mark = " · siz" if int(person.user_id) == int(me) else ""
            if captain and int(person.user_id) == captain:
                mark += " · kapitan"
            username = f" @{person.username}" if getattr(person, "username", "") else ""
            lines.append(f"• <code>{person.user_id}</code>{username} — "
                         f"{esc(person.name or '')}{mark}")
            shown += 1
    if not shown:
        lines.append("• Hali hech kim ro‘yxatdan o‘tmagan: /jamoa Nom")
    if extra:
        lines += ["", "<b>GURUHDA BOT KO‘RGANLAR</b> (faqat kuratorga)"]
        for row in extra[:40]:
            username = f" @{row['username']}" if row.get("username") else ""
            lines.append(f"• <code>{row.get('id')}</code>{username} — "
                         f"{esc(row.get('name') or '')}")
        if len(extra) > 40:
            lines.append(f"• … yana {len(extra) - 40} ta")
    lines += ["", "Maxfiy xabar: /pichoq matn — o‘z jamoangizga"]
    return "\n".join(lines)


# ----------------------------------------------------------- yakuniy javob
def final_usage() -> str:
    return "\n".join([
        "<b>YAKUNIY JAVOB</b>",
        "Savol ustidagi tasmaga reply qilib yozing:",
        "<code>/yakuniy javob matni</code>",
        "",
        "• Vaqt tugaguncha o‘zgartirish mumkin — oxirgisi hisoblanadi",
        "• Turnirda javob jamoaga tegishli: istalgan a’zo yozadi",
        "• /bekor — jamoaning yakuniy javobini olib tashlaydi",
    ])


def final_answer(team: str, answer: str, by: str, remaining: int, changed: bool) -> str:
    head = "YAKUNIY JAVOB O‘ZGARTIRILDI" if changed else "YAKUNIY JAVOB QABUL QILINDI"
    lines = [f"<b>{head}</b>"]
    if team:
        lines.append(f"• Jamoa: {esc(team)}")
    lines += [f"• Javob: {esc(answer)}", f"• Bergan: {esc(by)}",
              f"• Qolgan vaqt: {clock(remaining)}", "",
              "O‘zgartirish: yana <code>/yakuniy yangi javob</code> · bekor qilish: <code>/bekor</code>"]
    return "\n".join(lines)


def final_cleared(team: str, by: str) -> str:
    return f"<b>YAKUNIY JAVOB BEKOR QILINDI</b> · {esc(team)} · {esc(by)}"


def tour_no_tournament() -> str:
    return "\n".join([
        "Bu guruhda ochiq turnir yo‘q.",
        "• Turnir ochish (admin yoki kurator): <code>/turnir</code> yoki <code>/turnir 959</code>",
        "• Paketlar ro‘yxati: <code>/packages</code>",
    ])


def tour_card_play(title: str, number: int, total: int, round_label: str, standings: str,
                   finals: int, teams: int, timer: int) -> str:
    """The pinned card while the tournament is running."""
    head = f"<b>TURNIR · {esc(title)}</b>"
    lines = [head, f"• Savol {number} / {total}" + (f" · {round_label}" if round_label else ""),
             f"• Har savolga {timer} s · yakuniy javob bergan: {finals} / {teams} jamoa",
             "", standings, "",
             "Yakuniy javob: tasmaga reply qilib <code>/yakuniy javob</code>.",
             "Maxfiy xabar: <code>/pichoq matn</code> — faqat o‘z jamoangiz ko‘radi."]
    return "\n".join(lines)


def tour_curator_panel(title: str, package_id: int, package_title: str, bank: int,
                       rounds: int, per_round: int, timer: int, total: int, seed: int,
                       chosen: int, dropped: int, curators: int) -> str:
    wanted = max(1, int(rounds or 1)) * (int(per_round or 0) or int(bank or 0))
    lines = [
        f"<b>TURNIR SOZLAMALARI · {esc(title)}</b>",
        f"• Paket: <b>{package_id}</b> — {esc(package_title or '(sarlavhasiz)')}",
        f"• Tanlov: {bank} savol qoldi · olingan {chosen} · chiqarilgan {dropped}",
        f"• Tuzilma: {rounds} raund × {per_round or bank} savol = {total} savol",
        f"• Har savolga: {timer} s · aralashtirish urug‘i: {seed}",
        f"• Kuratorlar: {curators} kishi (admin va tayinlanganlar)",
        "",
        "<b>QANDAY O‘ZGARTIRILADI</b>",
        "• Tugmalardan raund, savol soni va vaqtni tanlang",
        "• «Savol tanlash» — paketdagi savollar ro‘yxati: bosib OLISH yoki CHIQARISH",
        "• Qidirish: <code>/tanlov so‘z</code> · aniq savol: <code>/olish 12345</code>, "
        "<code>/chiqarish 12345</code>",
        "• Boshqa paket: <code>/turnir paket_raqami</code>",
        "• Kurator tayinlash: <code>/kurator 123456789</code> · olish: "
        "<code>/kurator 123456789 olib</code>",
    ]
    if bank and wanted > bank:
        lines.insert(5, f"• <b>Diqqat:</b> so‘ralgan {wanted} savol, to‘plamda {bank} ta bor — "
                        f"bir savol ikki marta berilmaydi, {total} savol o‘ynaladi")
    return "\n".join(lines)


def tour_picker(rows: list, query: str, page: int, pages: int, chosen: int, dropped: int,
                bank: int, package_title: str) -> str:
    lines = ["<b>SAVOL TANLASH</b>",
             f"• Paket: {esc(package_title or '(sarlavhasiz)')}",
             f"• Tanlov: olingan {chosen} · chiqarilgan {dropped} · jami {bank} savol qoldi"]
    if query:
        lines.append(f"• Qidiruv: «{esc(query)}» — bekor qilish: <code>/tanlov</code>")
    lines.append(f"• Sahifa {page} / {max(1, pages)}")
    lines.append("")
    if not rows:
        lines.append("Bu sahifada savol yo‘q.")
    for row in rows:
        mark = "OLINDI" if row.get("included") else ("CHIQARILDI" if row.get("excluded") else "—")
        text = " ".join(str(row.get("text") or "").split())[:70]
        lines.append(f"[{mark}] <code>{row['id']}</code> · {esc(text)}")
    lines += ["", "Tugma bosilganda savol OLINADI; olingan savol tugmasi uni CHIQARADI.",
              "Aniq id bilan: <code>/olish 12345 12346</code> · <code>/chiqarish 12347</code>"]
    return "\n".join(lines)


def tour_selection_done(chosen: list, dropped: list, bank: int) -> str:
    lines = ["<b>TANLOV YANGILANDI</b>"]
    if chosen:
        lines.append(f"• Olingan: {', '.join(str(item) for item in chosen[:40])}"
                     + (" …" if len(chosen) > 40 else ""))
    if dropped:
        lines.append(f"• Chiqarilgan: {', '.join(str(item) for item in dropped[:40])}"
                     + (" …" if len(dropped) > 40 else ""))
    lines.append(f"• To‘plamda {bank} savol qoldi")
    return "\n".join(lines)


def tour_search_empty(query: str, package_title: str) -> str:
    return (f"«{esc(query)}» bo‘yicha savol topilmadi "
            f"({esc(package_title or 'paket')}). Boshqa so‘z bilan urinib ko‘ring.")


def tour_prompt_ask(kind: str) -> str:
    """What a button says when it hands its question over to the group chat.

    The next plain message the same person writes is the answer, so the text says
    exactly that — no command to remember, and no surprise about who is asked.
    """
    if kind == tn.PROMPT_PACKAGE:
        return ("Paket raqamini yoki nomidagi so‘zni shu guruhga yozing — turnir o‘sha "
                "paketga o‘tadi. Eski tanlov va aralashtirish bekor bo‘ladi. So‘z paket "
                "nomida haqiqatan uchrasa qabul qilinadi: tasodifiy gap bazani "
                "ko‘chirmaydi, raqam esa har doim aniq.")
    if kind == tn.PROMPT_SEARCH:
        return ("Qidiruv so‘zini shu guruhga yozing — savollar ro‘yxati shu so‘z bilan "
                "yangilanadi.")
    return "Jamoangiz nomini shu guruhga yozing — bot uni darhol oladi."


def tour_package_swapped(title: str, package_id: int, questions: int, name: str,
                         previous: int = 0) -> str:
    """The swap, out loud — including the number that puts the old bank back.

    The answer is whatever the curator typed, so a word that happens to be a
    package name really does move the whole bank and clear the hand-picked
    selection. Naming the previous package in the same message makes that one
    message away from being undone: press the button again, type the number.
    """
    lines = [
        "<b>PAKET ALMASHTIRILDI</b>",
        f"• Yangi paket: <b>{esc(title or package_id)}</b> "
        f"(id <code>{int(package_id)}</code>, {int(questions)} savol)",
        f"• Almashtirdi: {esc(name)}",
        "• Eski tanlov bekor: savollar yangi paketdan, yangi aralashtirish bilan olinadi.",
    ]
    if int(previous) and int(previous) != int(package_id):
        lines.append(f"• Qaytarish: «Paketni almashtirish» va <code>{int(previous)}</code>")
    lines.append("• Davomi: <code>/tanlov</code> yoki karta tugmalari.")
    return "\n".join(lines)


def tour_package_unknown(text: str) -> str:
    return (f"«{esc(text)}» bo‘yicha paket topilmadi. Raqam yozing (masalan <code>959</code>) "
            "yoki paket nomidagi so‘zni. Savol almashtirishni bekor qilish uchun istalgan "
            "boshqa tugmani bosing.")


def tour_package_locked() -> str:
    return ("O‘yin boshlangan — paketni almashtirib bo‘lmaydi. Avval <code>/turnirstop</code> "
            "bilan to‘xtating, keyin yangi turnir oching.")


def review_snippet(answer: str, limit: int = 0) -> str:
    """One answer as it appears in a list: whole when short, cut with a mark."""
    value = " ".join(str(answer or "").split())
    limit = int(limit or tn.ANSWER_SNIPPET)
    if len(value) <= limit:
        return value
    return value[:limit - 1].rstrip() + "…"


def tour_review_waiting(number: int, total: int, answers: int, teams: int,
                        seconds: int) -> str:
    """What the group sees while the verdicts are being checked.

    No answer and no verdict is published here: the list goes to the curator, and
    the group only learns that the question closed and how many answers there are.
    """
    lines = [f"<b>SAVOL {int(number)} / {int(total)} YAKUNLANDI</b>",
             f"• Yakuniy javoblar: {int(answers)} / {int(teams)} jamoa"]
    lines.append("• Javoblar kuratorga tekshirish uchun yuborildi — tasdiqlangach "
                 "natija shu yerga yoziladi.")
    if int(seconds) > 0:
        lines.append(f"• Kurator javob bermasa, {int(seconds)} sekunddan keyin bot "
                     "o‘z xulosasini e’lon qiladi.")
    return "\n".join(lines)


def tour_review_list(number: int, total: int, answer: str, rows: list, left: int,
                     reminded: int = 0) -> str:
    """The confirmation list itself — one line per team, verdict included.

    The curator sees the right answer too: judging a borderline spelling without
    it is guesswork, and this message goes only to people who may run the game.
    """
    answered = sum(1 for row in rows if str(row.get("answer") or "").strip())
    lines = [f"<b>SAVOL {int(number)} / {int(total)} — TEKSHIRISH</b>",
             f"• To‘g‘ri javob: <b>{esc(answer or '—')}</b>",
             f"• Javob berdi: {int(answered)} / {len(rows)} jamoa",
             "",
             "<b>RO‘YXAT</b>"]
    for row in rows:
        position = int(row.get("position") or 0) + 1
        name = esc(row.get("team") or "—")
        given = str(row.get("answer") or "").strip()
        if not given:
            lines.append(f"{position}. {name} — javob bermadi")
            continue
        mark = "TO‘G‘RI" if row.get("correct") else "NOTO‘G‘RI"
        if row.get("changed"):
            mark += " · o‘zgartirildi"
        lines.append(f"{position}. {name} — «{esc(review_snippet(given))}» — <b>{mark}</b>")
    lines += ["",
              "• Xulosani o‘zgartirish: jamoa yonidagi tugmani bosing",
              "• «TASDIQLASH» ochkolarni beradi va natijani guruhga yozadi"]
    if int(left) > 0:
        lines.append(f"• Qolgan vaqt: {clock(int(left))} — keyin bot o‘zi e’lon qiladi")
    if int(reminded):
        lines.append(f"• Eslatma: {int(reminded)} marta yuborildi")
    return "\n".join(lines)


def tour_review_nudge(number: int, left: int) -> str:
    """A reminder to the curator who has not confirmed yet."""
    if int(left) <= 0:
        return (f"Savol {int(number)} javoblari hali tasdiqlanmagan — bot o‘z xulosasini "
                "e’lon qiladi.")
    return (f"Savol {int(number)} javoblari hali tasdiqlanmagan. Qolgan vaqt: "
            f"{clock(int(left))} — keyin bot o‘zi e’lon qiladi.")


def tour_review_flip(name: str, correct: bool) -> str:
    """The toast after one mark is turned over."""
    return f"{name} — {'TO‘G‘RI' if correct else 'NOTO‘G‘RI'}"


def tour_review_closed(number: int, total: int, winners: list, decided: str) -> str:
    """What a confirmation list turns into once the verdicts are final.

    The buttons go with it: a list that can still be pressed after the result was
    published is a way to change a score nobody would notice.
    """
    lines = [f"<b>SAVOL {int(number)} / {int(total)} — TASDIQLANDI</b>"]
    if winners:
        lines.append("• Ochko oldi: " + " · ".join(esc(name) for name in winners))
    else:
        lines.append("• Hech qaysi jamoa ochko olmadi.")
    if decided:
        lines.append(f"• {esc(decided)}")
    lines.append("• Ro‘yxat yopildi — natija guruhga yozildi.")
    return "\n".join(lines)


def tour_review_decided(name: str, auto: bool = False) -> str:
    """The line the group sees under a published result."""
    if auto:
        return "Xulosa avtomatik e’lon qilindi (kurator tasdiqlamadi)"
    return f"Javoblarni tasdiqladi: {name}"


def tour_review_state(left: int, answers: int = 0) -> str:
    """One line for the pinned card while a confirmation list is open."""
    held = f" · {int(answers)} javob kutilmoqda" if int(answers) else ""
    if int(left) > 0:
        return f"● Javoblar tekshirilmoqda — tasdiq kutilmoqda ({clock(int(left))}){held}"
    return f"● Javoblar tekshirilmoqda{held}"


def tour_confirm_note(on: bool) -> str:
    """What the curator panel says about the confirmation switch."""
    if on:
        return ("Javobni tasdiqlash: YOQIQ — har savoldan keyin ro‘yxat kuratorga "
                "yuboriladi, ochko tasdiqdan keyin beriladi.")
    return ("Javobni tasdiqlash: O‘CHIQ — bot o‘z xulosasini darhol e’lon qiladi "
            "(/solo dagi kabi).")


# ------------------------------------------------------------------ game copy
def game_review_card(name: str, answer: str, number: int, total: int, left: int,
                     correct: bool) -> str:
    """One answer of an ordinary game, waiting for an admin's word."""
    lines = ["<b>JAVOB TEKSHIRILMOQDA</b>",
             f"• Savol: {int(number)} / {int(total)}"]
    if str(answer or "").strip():
        lines += [f"• Kim: {esc(name or '—')}",
                  f"• Javob: «{esc(review_snippet(answer, 200))}»"]
    else:
        lines.append("• Javob berilmadi — ochko Qarshi tarafga ketishi kerak")
    lines += [f"• Bot xulosasi: <b>{'TO‘G‘RI' if correct else 'NOTO‘G‘RI'}</b>",
              "",
              "• «TASDIQLASH» — bot xulosasi to‘g‘ri: shu bilan hisoblanadi",
              "• «RAD ETISH» — xulosa noto‘g‘ri: teskari qaror qabul qilinadi"]
    if int(left) > 0:
        lines.append(f"• Qolgan vaqt: {clock(int(left))} — keyin bot o‘zi hisoblaydi")
    return "\n".join(lines)


def game_review_waiting(name: str, number: int, total: int, left: int) -> str:
    """What the group sees while an admin checks one answer."""
    who = f" — {esc(name)} javobi" if str(name or "").strip() else " — javob"
    line = f"● Savol {int(number)} / {int(total)}{who} admin tekshiruvida"
    if int(left) > 0:
        line += f" · {clock(int(left))} keyin avtomatik"
    return line


def game_review_nudge(number: int, left: int) -> str:
    if int(left) <= 0:
        return f"Savol {int(number)} javobi hali tasdiqlanmagan — bot o‘zi hisoblaydi."
    return (f"Savol {int(number)} javobi hali tasdiqlanmagan. Qolgan vaqt: "
            f"{clock(int(left))}.")


def game_confirm_note(on: bool) -> str:
    """What the settings panel says about the confirmation step of a game."""
    if on:
        return ("Javobni tasdiqlash: YOQIQ. Guruh o‘yinida har javob adminga shaxsiy "
                "xabarda ko‘rsatiladi, ochko tasdiqdan keyin beriladi. 30 va 90 "
                "sekundda eslatma bor, 180 sekundda bot o‘z xulosasini «avtomatik» "
                "deb e’lon qiladi. Shaxsiy chatda so‘rovchi yo‘q — o‘yin "
                "avvalgidek davom etadi.")
    return ("Javobni tasdiqlash: O‘CHIQ. Bot o‘z xulosasini darhol hisoblaydi — "
            "avvalgi tartib. /accept buyrug‘i baribir ishlaydi.")


def game_review_closed(number: int, total: int, decided: str, score: str) -> str:
    """A parked answer of an ordinary game, after somebody has decided it.

    The buttons go with the verdict: a card that can still be pressed once the
    point has been given is a way to change a score nobody would notice.
    """
    lines = [f"<b>SAVOL {int(number)} / {int(total)} — TEKSHIRUV YOPILDI</b>"]
    if decided:
        lines.append(f"• {esc(decided)}")
    if score:
        lines.append(f"• Hisob: {esc(score)}")
    return "\n".join(lines)


def game_review_decided(name: str, auto: bool = False, rejected: bool = False) -> str:
    if rejected:
        return f"Javob rad etildi: {name}"
    if auto:
        return "Javob avtomatik tasdiqlandi (admin javob bermadi)"
    return f"Javobni tasdiqladi: {name}"


def tour_question_card(question: dict, show_answer: bool, used: bool = False,
                       number: int = 0, total: int = 0) -> str:
    """One question, as everybody in the group may see it.

    The answer follows the game: it is shown once it has been published (or in a
    private chat, or to a curator), and the "ishlatildi" mark says the question
    has already been asked somewhere.
    """
    head = f"<b>SAVOL {question.get('id')}</b>"
    if number and total:
        head = f"<b>SAVOL {number} / {total}</b> · <code>{question.get('id')}</code>"
    lines = [head, esc(question.get("text") or "")]
    if used:
        lines.append("• Holat: <b>ishlatildi</b>")
    if question.get("comment") and show_answer:
        lines.append(f"• Izoh: {esc(question['comment'])[:600]}")
    if show_answer:
        lines.append(f"• Javob: <b>{esc(question.get('answer') or '')}</b>")
    else:
        lines.append("• Javob e’lon qilingach shu guruhda hammaga ko‘rinadi.")
    return "\n".join(lines)


def tour_question_list(title: str, rows: list, published: int, page: int = 1,
                       pages: int = 1) -> str:
    """The whole tournament question set, open to everybody in the group.

    A row shows its answer only once that question has been played: the game
    stays fair, and nothing has to be asked from a curator in private.
    """
    if not rows:
        return ("Turnir savollari hozircha tanlanmagan. Kurator paketni yoki tanlovni "
                "belgilagach, ro‘yxat shu yerda chiqadi.")
    lines = [f"<b>TURNIR SAVOLLARI</b> · {esc(title or 'Turnir')}",
             f"E’lon qilingan javoblar: {published} ta · sahifa {page}/{pages}"]
    for row in rows:
        mark = "●" if row.get("published") else "○"
        body = " ".join(str(row.get("text") or "").split())[:90]
        line = f"{mark} <b>{row.get('number')}</b> · {esc(body)}…"
        if row.get("published") and row.get("answer"):
            line += f"\n   Javob: <b>{esc(str(row.get('answer'))[:120])}</b>"
        elif row.get("used"):
            line += "\n   <i>bu savol oldin ishlatilgan</i>"
        lines.append(line)
    lines.append("")
    lines.append("Batafsil: <code>/savol 12345</code> · javoblar e’lon qilingach ochiladi.")
    return "\n".join(lines)


def tour_manage_text(tour, start_text: str = "", ready: bool = False) -> str:
    """The curator's control panel: everything that used to need a /command."""
    lines = [f"<b>BOSHQARUV</b> · {esc(tour.title)}",
             f"Paket: <code>{int(tour.package_id)}</code> · "
             f"{int(tour.rounds or 1)} raund × {int(tour.per_round or 0)} savol · "
             f"{int(tour.timer)} s",
             f"Jamolar: {len(tour.teams)} / {tour.team_limit} ta · "
             f"o‘yinchi: {tour.people} kishi"]
    if start_text:
        lines.append(f"Boshlanish: <b>{esc(start_text)}</b>")
    lines.append("Holat: " + ("boshlashga tayyor" if ready
                              else "shartlar to‘liq bajarilmagan"))
    lines.append("")
    lines.append("Pastdagi tugmalar buyruqlarning o‘rnini bosadi: boshlash, "
                 "sozlash, savol tanlash, jadval, javoblar holati, to‘xtatish.")
    return "\n".join(lines)


def tour_plan_text(tour) -> str:
    """What the schedule says, and what happens at that moment."""
    from . import schedule as sched

    if not int(tour.start_at or 0):
        return "\n".join([
            "<b>REJA</b>",
            "Bu turnir uchun vaqt belgilanmagan — «Boshlash» tugmasi hozir ishlaydi.",
            "",
            "Vaqtni belgilash: <code>/turnir vaqt=19:00</code> (yangi turnir) yoki "
            "Mini App panelidagi «Turnir shakllantirish».",
        ])
    left = int(tour.start_at) - sched.now()
    lines = ["<b>REJA</b>",
             f"Boshlanish: <b>{esc(sched.when_text(int(tour.start_at)))}</b> "
             f"({esc(sched.clock_text(int(tour.start_at)))})",
             f"Qolgan vaqt: {esc(sched.left_text(int(tour.start_at)))}"
             if left > 0 else "Vaqti keldi — «Boshlash» tugmasi ishlaydi"]
    lines += [
        f"Jamolar: {len(tour.teams)} / {tour.team_limit} ta · o‘yinchi: {tour.people}",
        "",
        "Belgilangan paytda bot shu guruhga eslatma va «Boshlash» tugmasini yuboradi. "
        "O‘yin o‘zi boshlanmaydi — kurator yoki admin tugmani bosishi kerak.",
        f"Shartlar: kamida {tn_min_teams()} jamoa, har biridan kamida {tn_min_ready()} kishi "
        "«Men tayyorman» degan bo‘lishi kerak.",
    ]
    return "\n".join(lines)


def tn_min_teams() -> int:
    from . import tournament as tn

    return int(tn.MIN_TEAMS)


def tn_min_ready() -> int:
    from . import tournament as tn

    return int(tn.MIN_READY)


def tour_curator_picker(tour, people: list) -> str:
    """Who is in the group, with the ids a curator needs to appoint one of them."""
    lines = ["<b>KURATOR TAYINLASH</b>",
             "Quyidagi id ni nusxalab, buyruqni yozing:",
             "<code>/kurator 123456789</code> — tayinlash · "
             "<code>/kurator 123456789 olib</code> — olish"]
    curators = [int(item) for item in (tour.curators or [])]
    if curators:
        lines.append("")
        lines.append("Hozirgi kuratorlar: " + " · ".join(f"<code>{item}</code>"
                                                         for item in curators))
    rows = [row for row in (people or []) if isinstance(row, (list, tuple)) and len(row) >= 2]
    if rows:
        lines.append("")
        lines.append("Guruhda ko‘ringan odamlar:")
        for row in rows[:20]:
            who = esc(str(row[1] or row[0]))
            lines.append(f"• <code>{int(row[0])}</code> — {who}")
    else:
        lines.append("")
        lines.append("Bot bu guruhda hali hech kimni ko‘rmagan: kimdir bir marta "
                     "«Men tayyorman» yoki «Yangi jamoa» tugmasini bosgach, "
                     "ro‘yxat shu yerda chiqadi.")
    return "\n".join(lines)


def tour_curator_added(name: str, count: int) -> str:
    return f"<b>KURATOR TAYINLANDI</b> · {esc(name)}\n• Endi kuratorlar soni: {count}"


def tour_curator_removed(name: str, count: int) -> str:
    return f"<b>KURATORLIK OLINDI</b> · {esc(name)}\n• Qolgan kuratorlar: {count}"


def tour_curator_usage() -> str:
    return "\n".join([
        "<b>KURATOR TAYINLASH</b>",
        "• <code>/kurator 123456789</code> — tayinlash",
        "• <code>/kurator @username</code> — username orqali (agar bot uni ko‘rgan bo‘lsa)",
        "• <code>/kurator 123456789 olib</code> — kuratorlikni olish",
        "",
        "Kurator turnirni ochadi, savollarni oladi yoki chiqaradi, raund va vaqtni "
        "sozlaydi, turnirni boshlaydi va to‘xtatadi. Bu buyruq faqat guruh adminida ishlaydi.",
    ])


def tour_denied_action(action: str) -> str:
    return ("Bu amal faqat guruh admini yoki kurator uchun: " + esc(action)
            + ". Kurator tayinlash: <code>/kurator 123456789</code>")


def tour_started_private(open_mode: bool) -> str:
    if open_mode:
        return ("\n\n<b>OGOHLANTIRISH</b>: botda xabar o‘chirish huquqi yo‘q, shuning uchun "
                "javoblar guruhda ko‘rinib qoladi. Maxfiy o‘yin uchun botga guruhda "
                "«Xabarlarni o‘chirish» huquqini bering.")
    return ""


def tour_final_deadline() -> str:
    return "Bu savol uchun vaqt tugadi — yakuniy javob qabul qilinmadi."


def tour_final_closed() -> str:
    return "Hozir ochiq savol yo‘q. Keyingi savol chiqqach /yakuniy ishlaydi."


def tour_final_rejected(problem: str) -> str:
    return f"Yakuniy javob qabul qilinmadi: {esc(problem)}"


def tour_private_note(team: str, text: str) -> str:
    """What a member gets in private while the group sees nothing."""
    return f"<b>{esc(team)}</b> · jamoangizga maxfiy xabar\n\n{esc(text)}"


def tour_spec_help() -> str:
    """How one /turnir line carries the whole plan."""
    return "\n".join([
        "<b>TURNIRNI BIR QATORDA SHAKLLANTIRISH</b>",
        "<code>/turnir 959 vaqt=19:00 jamoa=6 tur=3 savol=10 sekund=60 sarlavha=Kuzgi liga</code>",
        "",
        "• <code>vaqt=</code> — 19:00 · ertaga 10:30 · 28.11 19:00 · +45 (daqiqa)",
        "  bir necha so‘z ham bo‘ladi: <code>vaqt=ertaga 19:00</code>",
        "• <code>jamoa=</code> — nechta jamoa o‘ynashi mumkin (3–12)",
        "• <code>tur=</code> — raundlar soni · <code>savol=</code> — har raundda nechta savol",
        "• <code>sekund=</code> — har savolga vaqt (30–120)",
        "• <code>sarlavha=</code> — turnir nomi (bir necha so‘z: <code>sarlavha=Kuzgi liga</code>)",
        "• Paket o‘rniga matn ham yozsa bo‘ladi: <code>/turnir talabalar ligasi vaqt=ertaga 18:00</code>",
        "",
        "Belgilangan vaqtda bot shu guruhga eslatma va «Boshlash» tugmasini yuboradi.",
    ])


def tour_help() -> str:
    return "\n".join([
        "<b>TURNIR BUYRUQLARI</b>",
        "<i>Kurator va admin uchun</i>",
        "• <code>/turnir</code> — turnir ochish (paket: <code>/turnir 959</code>)",
        "• <code>/turnir 959 vaqt=19:00 jamoa=6</code> — rejadagi turnir (bir qatorda)",
        "• <code>/tanlov so‘z</code> — savollar ichidan qidirish",
        "• <code>/olish 12345 12346</code> — savollarni tanlovga olish",
        "• <code>/chiqarish 12347</code> — savolni tanlovdan chiqarish",
        "• <code>/savol 12345</code> — bitta savolni ko‘rish",
        "• <code>/kurator 123456789</code> — kurator tayinlash",
        "",
        "<i>Hamma uchun</i>",
        "• <code>/savollar</code> — turnir savollari ro‘yxati (javobi e’lon qilinganlari bilan)",
        "• <code>/savol 12345</code> — bitta savol; javobi ochilgach hammaga ko‘rinadi",
        "",
        "<i>Jamoalar uchun</i>",
        "• <code>/jamoa Bilimdonlar</code> — jamoa yaratish",
        "• <code>/qoshil Bilimdonlar</code> — jamoaga qo‘shilish (yoki <code>/qoshil 2</code>)",
        "• <code>/tayyor</code> — «Men tayyorman»",
        "• <code>/chiqish</code> — jamoadan chiqish",
        "",
        "<i>O‘yin paytida</i>",
        "• <code>/yakuniy javob</code> — jamoaning yakuniy javobi (tasmaga reply qilib)",
        "• <code>/bekor</code> — yakuniy javobni olib tashlash",
        "• <code>/pichoq matn</code> — o‘z jamoangizga maxfiy xabar",
        "• <code>/pichoq @a’zo matn</code> — bitta a’zoga (faqat o‘z jamoangizdan)",
        "• <code>/azolar</code> — o‘z jamoangiz a’zolari va ularning id’si",
        "• <code>/jadval</code> — ochkolar jadvali",
    ])


def tour_rules_text(sizes: dict) -> str:
    return "\n".join([
        "<b>TURNIR QOIDALARI</b>",
        f"• Kamida {sizes['min_teams']} ta jamoa ro‘yxatdan o‘tadi",
        f"• Har jamoada {sizes['min_members']}–{sizes['max_members']} kishi",
        f"• Har jamoadan kamida {sizes['min_ready']} kishi «Men tayyorman» deydi",
        "• Har savolga har jamoa bitta yakuniy javob beradi: <code>/yakuniy javob</code>",
        "• Vaqt tugaguncha javobni o‘zgartirish mumkin — oxirgisi hisoblanadi",
        "• To‘g‘ri javob — jamoaga +1 ochko; bir savolda bir nechta jamoa ochko oladi",
        "• Javoblar guruhda ko‘rinmaydi: bot ularni o‘chiradi va faqat o‘z jamoangizga "
        "shaxsiy xabar bilan ko‘rsatadi",
        "• <code>/pichoq matn</code> — faqat o‘z jamoangiz ko‘radi; boshqa jamoaga yoki "
        "turnirda qatnashmayotgan odamga maxfiy xabar ketmaydi",
        "• Bot guruhda xabar o‘chira olmasa, maxfiy xabar umuman yuborilmaydi — admin "
        "botga «Delete messages» huquqini berishi kerak",
        "• Yakunda ochkolar jadvali e’lon qilinadi",
    ])


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


def _stamp_zone(seconds: object) -> str:
    """A stamp in the zone curators read, not in the server's UTC."""
    try:
        return datetime.fromtimestamp(int(seconds or 0), tz=sched.ZONE).strftime("%d.%m %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "--.-- --:--"


def stats_used(data: dict, rows: list | None = None, page: int = 1, pages: int = 1) -> str:
    """The "ishlatildi" index: which questions have already been asked."""
    used = dict(data.get("used") or {})
    rows = list(used.get("items") if rows is None else rows)
    head = ["<b>ISHLATILGAN SAVOLLAR</b>",
            f"Indeksda {used.get('count', 0)} ta savol (chegara {used.get('limit', 0)}) · "
            f"{used.get('packages', 0)} paket · jami {used.get('uses', 0)} marta"]
    if used.get("top_packages"):
        head.append("Ko‘p ishlatilgan paketlar: " + " · ".join(
            f"{package} ({amount})" for package, amount in used["top_packages"][:5]))
    if not rows:
        head.append("")
        head.append("• Hozircha indeks bo‘sh: o‘yin yoki turnirda savol berilgach, "
                    "u shu yerga tushadi.")
        return "\n".join(head)
    head.append("")
    head.append(f"Sahifa {page}/{pages}")
    for row in rows:
        when = _stamp_zone(row.get("t"))
        where = f"paket {row.get('p')}" if row.get("p") else "paketi aniqlanmagan"
        times = f" · {row.get('n')} marta" if int(row.get("n") or 1) > 1 else ""
        kind = f" · {row.get('m')}" if row.get("m") else ""
        snippet = " ".join(str(row.get("x") or "").split())[:60]
        line = f"• <code>{row.get('id')}</code> — {when} · {where}{kind}{times}"
        if snippet:
            line += f"\n  {esc(snippet)}…"
        head.append(line)
    head.append("")
    head.append("Bir savolni ochish: <code>/savol 12345</code>")
    return "\n".join(head)


def stats_plans(data: dict) -> str:
    """Every tournament the bot has to announce, soonest first."""
    rows = list(data.get("plans") or [])
    groups = {int(key): value for key, value in (data.get("groups") or {}).items()
              if str(key).lstrip("-").isdigit()}
    lines = ["<b>TURNIR REJALARI</b>",
             f"Yozilgan eslatmalar: {len(rows)} ta · guruhlar: {len(groups)} ta"]
    if not rows:
        lines.append("")
        lines.append("• Hozircha reja yo‘q. Turnirni ochishda vaqtini yozing: "
                     "<code>/turnir 959 vaqt=19:00</code> yoki Mini App panelidagi "
                     "«Turnir shakllantirish».")
    for row in rows:
        chat = int(row.get("chat") or 0)
        name = groups.get(chat) or str(chat)
        left = int(row.get("left") or 0)
        state = "vaqti keldi" if left <= 0 else f"{sched.left_text(row.get('start'), sched.now())} qoldi"
        lines.append(f"• <b>{esc(str(row.get('title') or 'Turnir'))}</b> — {esc(name)}\n"
                     f"  {_stamp_zone(row.get('start'))} · {state} · karta "
                     f"<code>{row.get('message')}</code>")
    if groups:
        lines.append("")
        lines.append("Panel tanlay oladigan guruhlar: " + " · ".join(
            esc(value or key) if value else str(key) for key, value in list(groups.items())[:8]))
    return "\n".join(lines)


def stats_access(data: dict) -> str:
    """Who may open the Mini App, and how to change it."""
    rows = list(data.get("access") or [])
    lines = ["<b>MINI APP RUXSATLARI</b>", f"Berilgan: {len(rows)} ta"]
    for row in rows[:40]:
        name = esc(str(row.get("name") or "(ismsiz)"))
        lines.append(f"• <code>{row.get('id')}</code> — {name} · {row.get('label') or ''}".rstrip(" ·"))
    lines.append("")
    lines.append("Qo‘shish: <code>/grant 123456789 Ism</code> · olish: "
                 "<code>/revoke 123456789</code>")
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
