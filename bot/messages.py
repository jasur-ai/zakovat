"""All user-facing bot texts in one place (Uzbek, HTML parse mode)."""
from __future__ import annotations

import html
import re

BAR_WIDTH = 10


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def bar(remaining: int, total: int, width: int = BAR_WIDTH) -> str:
    """Visual countdown: ▰▰▰▱▱▱▱▱▱▱"""
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, round(width * remaining / total)))
    return "▰" * filled + "▱" * (width - filled)


def timer_line(remaining: int, total: int) -> str:
    if remaining <= 0:
        return "⌛ <b>Vaqt tugadi</b>"
    return f"⏳ <b>{remaining}s</b> qoldi · {bar(remaining, total)}"


def welcome(packages: int, questions: int) -> str:
    return (
        "🎯 <b>Zakovat Agent</b> — savol-javob boti\n\n"
        f"Arxivda <b>{packages} paket</b> va <b>{questions} savol</b> bor. "
        "O‘yinni shaxsiy chatda yoki yopiq guruhda o‘tkazaman.\n\n"
        "<b>Boshlash</b>\n"
        "• <code>/solo</code> — yakka o‘yin (faqat siz javob yozasiz)\n"
        "• <code>/duel</code> — guruh o‘yini (hamma javob yozadi)\n"
        "• Paketni belgilash: <code>/solo 959</code>\n\n"
        "O‘yin boshlanishidan oldin bot <b>raundlar soni</b>, <b>har raunddagi savollar</b> "
        "va <b>har savolga vaqt</b>ni so‘raydi — tugmalardan tanlaysiz.\n\n"
        "<b>Hisob:</b> to‘g‘ri javob → Bilimdonlar +1; javob yo‘q, vaqt tugadi yoki o‘tkazildi → "
        "Qarshi taraf +1. 10 savoldan 4 tasiga javob berilsa hisob <b>4–6</b> bo‘ladi.\n\n"
        "Barcha buyruqlar: /help · qoidalar: /rules"
    )


def help_text() -> str:
    return (
        "🆘 <b>Buyruqlar</b>\n\n"
        "<b>O‘yin</b>\n"
        "<code>/solo</code> — yakka o‘yin (sozlamalar so‘raladi)\n"
        "<code>/duel</code> — guruh o‘yini\n"
        "<code>/solo 959</code> — aniq paket bilan\n"
        "<code>/stop</code> — o‘yinni to‘xtatib, yakuniy hisobni ko‘rsatish\n\n"
        "<b>Savol paytida</b>\n"
        "Javobni savol xabariga <b>reply</b> qilib yozing\n"
        "<code>/score</code> — joriy hisob\n"
        "<code>/skip</code> — <b>faqat admin</b>: savolni o‘tkazadi va javobini darhol ko‘rsatadi\n"
        "<code>/accept</code> — <b>faqat admin</b>: sizning javobingizni to‘g‘ri deb qabul qiladi\n"
        "⏱ tugmasi — qolgan vaqtni yangilab ko‘rsatadi\n\n"
        "<b>Ma’lumot</b>\n"
        "<code>/packages</code> — barcha paketlar (sahifalab)\n"
        "<code>/packages Zakovat</code> — sarlavhadan qidirish\n"
        "<code>/rules</code> — qoidalar\n"
        "<code>/start</code> — tanishtiruv\n\n"
        "⏱ Vaqt tugasa yoki savol o‘tkazilsa, ochko <b>Qarshi taraf</b>ga o‘tadi."
    )


def rules() -> str:
    return (
        "📋 <b>Qoidalar</b>\n\n"
        "1️⃣ O‘yin boshlanishida <b>raundlar soni</b>, <b>har raunddagi savollar</b> va "
        "<b>har savolga sekund</b> tanlanadi.\n"
        "2️⃣ Bot savol yuboradi. Javobni shu xabarga <b>reply</b> qilib yozing.\n"
        "3️⃣ Savol ostida <b>⏳ qolgan vaqt</b> ko‘rsatiladi; ⏱ tugmasi uni yangilaydi.\n"
        "4️⃣ <b>To‘g‘ri javob</b> → <b>Bilimdonlar +1</b>. <b>Javob yo‘q / vaqt tugadi / o‘tkazildi</b> → "
        "<b>Qarshi taraf +1</b>.\n"
        "5️⃣ 10 savoldan 4 tasiga javob berilsa hisob <b>4–6</b> bo‘ladi.\n"
        "6️⃣ Yakka o‘yinda faqat boshlagan kishi javob yozadi; guruhda hamma ishtirok etadi.\n"
        "7️⃣ <code>/skip</code> va <code>/accept</code> — <b>faqat admin</b> uchun.\n"
        "8️⃣ Har javobdan keyin to‘g‘ri javob va izoh ko‘rsatiladi.\n\n"
        "Boshlash: <code>/solo</code> yoki <code>/duel</code>."
    )


def group_hello() -> str:
    return (
        "👋 Assalomu alaykum! Men <b>Zakovat Agent</b>.\n\n"
        "Bu guruhda savol-javob o‘yini o‘tkazaman: <code>/duel</code> yozing — "
        "raundlar, savollar soni va vaqtni o‘zingiz tanlaysiz.\n\n"
        "Qoidalar: to‘g‘ri javob → Bilimdonlar +1, javob yo‘q yoki vaqt tugadi → Qarshi taraf +1. "
        "Javobni savol xabariga reply qilib yozing. To‘liq qoidalar: /rules\n\n"
        "Faqat yopiq guruhda foydalaning."
    )


def settings_panel(package_id: int, title: str, available: int, rounds: int, per_round: int,
                   timer: int, mode: str, total: int) -> str:
    label = "Yakka o‘yin" if mode == "solo" else "Guruh o‘yini"
    per_round_text = "barcha savol" if per_round <= 0 else f"{per_round} savol"
    total_text = f"{total} savol" if total else "barcha savol"
    return (
        f"⚙️ <b>O‘yin sozlamalari</b> · {esc(label)}\n\n"
        f"📦 Paket: <code>{package_id}</code> — {esc(title)}\n"
        f"📚 Bazada: {available} savol\n"
        f"🔁 Raundlar: <b>{rounds}</b>\n"
        f"❓ Har raundda: <b>{per_round_text}</b>\n"
        f"⏱ Har savolga: <b>{timer} sekund</b>\n"
        f"🧮 Jami: <b>{total_text}</b>\n\n"
        "Tugmalardan tanlang, keyin <b>▶️ Boshlash</b> ni bosing."
    )


def game_start(label: str, title: str, total: int, timer: int, score: str,
               rounds: int = 1, per_round: int = 0) -> str:
    structure = f"🔁 {rounds} raund × {per_round} savol" if per_round else f"🔁 {rounds} raund"
    return (
        f"🎬 <b>{esc(label)}</b> boshlandi\n"
        f"📦 Paket: {esc(title)}\n"
        f"{structure}\n"
        f"🔢 Jami savol: {total} · ⏱ har biriga {timer} sekund\n"
        f"📊 {esc(score)}\n\n"
        "Javoblarni savol xabariga <b>reply</b> qilib yozing. "
        "O‘yinni tugatish: <code>/stop</code>."
    )


def round_banner(number: int, rounds: int, score: str) -> str:
    return f"🔔 <b>{number}-raund</b> boshlandi ({number}/{rounds})\n📊 {esc(score)}"


def question(number: int, total: int, title: str, text: str, timer: int, score: str,
             attachment: str = "", round_label: str = "", remaining: int | None = None) -> str:
    head = f"<b>❓ {number}/{total}-savol</b>"
    if round_label:
        head += f" · {esc(round_label)}"
    result = f"{head} · {esc(title)}\n\n{esc(str(text)[:2800])}\n\n"
    result += "✍️ Javobni shu xabarga <b>reply</b> qilib yozing\n"
    result += timer_line(timer if remaining is None else remaining, timer) + "\n"
    result += f"📊 {esc(score)}"
    if attachment:
        result += f"\n🖼 {esc(attachment)}"
    return result


def reveal(headline: str, answer: str, comment: str = "", attachment: str = "", score: str = "") -> str:
    result = f"{headline}\n✅ <b>Javob:</b> {esc(str(answer)[:1500])}"
    if comment:
        result += f"\n💬 <b>Izoh:</b> {esc(str(comment)[:1500])}"
    if attachment:
        result += f"\n📎 {esc(attachment)}"
    if score:
        result += f"\n\n📊 {esc(score)}"
    return result


def summary(title: str, total: int, correct: int, wrong: int, percent: int, rounds: int = 1) -> str:
    head = f"🏁 <b>O‘yin tugadi</b> · {esc(title)}\n"
    if rounds > 1:
        head += f"🔁 Raundlar: {rounds}\n"
    return (
        head
        + f"📊 Bilimdonlar <b>{correct}</b> — <b>{wrong}</b> Qarshi taraf\n"
        + f"✅ To‘g‘ri javoblar: {correct}/{total} ({percent}%)\n\n"
        "Yana o‘ynash: /solo yoki /duel"
    )


def packages_page(result: dict, query: str = "") -> str:
    items = result["items"]
    start = result["start"]
    scope = f"«{esc(query)}» bo‘yicha" if query else "barcha"
    lines = [
        f"📦 <b>Paketlar ({scope})</b> — {result['count']} ta",
        f"📄 Sahifa <b>{result['page']}/{result['pages']}</b>",
        "",
    ]
    for offset, package in enumerate(items, start=start + 1):
        title = esc(package.get("title") or "(sarlavhasiz)")
        count = package.get("questions") or 0
        lines.append(f"{offset}. <code>{package['id']}</code> — {title} ({count})")
    lines.append("")
    lines.append("Boshlash: <code>/solo 959</code> · Qidirish: <code>/packages so‘z</code>")
    return "\n".join(lines)


def admin_only(action: str = "bu amal") -> str:
    return f"⛔ {esc(action)} faqat <b>admin</b> uchun: guruhda admin bo‘ling yoki o‘yinni o‘zingiz boshlang."


def small_talk(text: str) -> str | None:
    """Reply to casual messages when no quiz is active. Never used for answers."""
    value = re.sub(r"\s+", " ", (text or "").strip().casefold())
    value = value.strip("!.,?")
    greetings = {
        "salom", "assalomu alaykum", "assalom", "salam", "hi", "hello", "yo‘v", "yov",
    }
    if value in greetings or value.startswith("assalomu alaykum"):
        return (
            "👋 Assalomu alaykum! Men Zakovat Agentman.\n\n"
            "Savol-javob o‘yini boshlash uchun <code>/solo</code> (yakka) yoki "
            "<code>/duel</code> (guruh) yozing. Qoidalar: /rules"
        )
    if value in {"rahmat", "thanks", "spasibo"} or value.startswith("rahmat"):
        return "😊 Arzimaydi! Yaxshi o‘yin tilayman. /solo bilan davom etamizmi?"
    if any(word in value for word in ("qanday o‘ynayman", "qanday oynayman", "qoida", "qoidalar", "nima qilish")):
        return rules()
    if value in {"yordam", "help", "komak"} or value.startswith("yordam"):
        return help_text()
    if value in {"paketlar", "paket", "savollar", "baza"}:
        return "📦 Paketlarni ko‘rish uchun <code>/packages</code> yozing — hammasi sahifalab chiqadi."
    if value in {"hisob", "score", "natija"}:
        return "📊 Hisobni ko‘rish uchun o‘yin paytida <code>/score</code> yozing yoki savol xabariga reply qiling."
    if value in {"kim", "nima", "bot", "about"}:
        return (
            "🤖 Men Zakovat Agent — arxivlangan 30 853 savol bilan ishlaydigan o‘yin botiman. "
            "Tanishtiruv: /start · buyruqlar: /help"
        )
    return None
