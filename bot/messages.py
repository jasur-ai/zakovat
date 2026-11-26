"""All user-facing bot texts in one place (Uzbek, HTML parse mode)."""
from __future__ import annotations

import html
import re


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def welcome(packages: int, questions: int, miniapp_url: str = "") -> str:
    text = (
        "🎯 <b>Zakovat Agent</b> — savol javob boti\n\n"
        f"Menda arxivlangan savollar bazasi bor: <b>{packages} paket</b>, <b>{questions} savol</b>. "
        "Sayt yopilsa ham savollar shu yerda saqlanadi. O‘yinlarni yopiq guruhda yoki shaxsiy chatda o‘tkazish uchun yaratilganman.\n\n"
        "<b>Qanday o‘ynash mumkin?</b>\n"
        "• <b>Yakka:</b> /solo 959 — savollarga o‘zingiz javob yozasiz.\n"
        "• <b>Guruh:</b> /duel 959 — hamma javob yozadi, birinchi to‘g‘ri javob ochko oladi.\n"
        "• <b>Paket tanlash:</b> /packages Zakovat yoki to‘g‘ridan-id: /solo 959.\n\n"
        "<b>Hisob qoidasi:</b> to‘g‘ri javob → Bilimdonlar +1; javob yo‘q, vaqt tugadi yoki o‘tkazildi → Qarshi taraf +1. "
        "10 savoldan 4 tasiga javob berilsa hisob <b>4–6</b> bo‘ladi.\n\n"
        "<b>Buyruqlar:</b> /start, /help, /rules, /packages, /solo, /duel, /score, /skip, /accept, /stop\n\n"
        "Javoblarni savol xabariga <b>reply</b> qilib yozing. Bot AI kalit talab qilmaydi: "
        "matnni normalizatsiya qiladi, izohni ham ko‘rsatadi.\n\n"
        "Masalan: <code>/solo 959</code>"
    )
    return text


def miniapp_button(url: str):
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Mini App ochish", web_app=WebAppInfo(url=url))]])


def rules() -> str:
    return (
        "📋 <b>Qoidalar</b>\n\n"
        "1️⃣ Bot savol yuboradi. Javobni shu xabarga <b>reply</b> qilib yozing.\n"
        "2️⃣ Har savolga <b>60 sekund</b> (sozlash mumkin). Vaqt tugagach keyingi xabarda hisob yangilanadi.\n"
        "3️⃣ <b>To‘g‘ri javob</b> → <b>Bilimdonlar +1</b>. <b>Javob yo‘q / noto‘g‘ri / o‘tkazildi</b> → <b>Qarshi taraf +1</b>.\n"
        "4️⃣ 10 savoldan 4 tasiga javob berilsa hisob <b>4–6</b> bo‘ladi.\n"
        "5️⃣ Yakka o‘yinda faqat boshlagan kishi javob yozadi; guruhda hamma ishtirok etadi.\n"
        "6️⃣ Har javobdan keyin to‘g‘ri javob va izoh ko‘rsatiladi.\n"
        "7️⃣ Admin javobni noto‘g‘ri baholasa, unga reply qilib <code>/accept</code> yozadi — bot variantni o‘rganadi.\n\n"
        "Boshlash: <code>/solo 959</code> yoki <code>/duel 959</code>."
    )


def group_hello() -> str:
    return (
        "👋 Assalomu alaykum! Men <b>Zakovat Agent</b>.\n\n"
        "Bu guruhda savol-javob o‘yini o‘tkazaman. Guruh o‘yini uchun <code>/duel 959</code> yozing "
        "(959 — sinov paketi; boshqa id ni <code>/packages</code> bilan topasiz).\n\n"
        "Qoidalar: to‘g‘ri javob → Bilimdonlar +1, javob yo‘q yoki vaqt tugadi → Qarshi taraf +1. "
        "Savol xabariga reply qilib javob yozing. To‘liq qoidalar: /rules.\n\n"
        "Faqat yopiq guruhda foydalaning."
    )


def game_start(label: str, title: str, total: int, timer: int, score: str) -> str:
    return (
        f"🎬 <b>{esc(label)}</b> boshlandi\n"
        f"📦 Paket: {esc(title)}\n"
        f"🔢 Savollar soni: {total} · ⏱ Har biriga {timer} sekund\n"
        f"📊 {esc(score)}\n\n"
        "Javoblarni savol xabariga <b>reply</b> qilib yozing. O‘yinni tugatish: /stop."
    )


def question(number: int, total: int, title: str, text: str, timer: int, score: str,
             attachment: str = "") -> str:
    result = (
        f"<b>❓ {number}/{total}-savol</b> · {esc(title)}\n\n"
        f"{esc(str(text)[:2800])}\n\n"
        f"✍️ Javobni shu xabarga <b>reply</b> qilib yozing · ⏱ {timer}s\n"
        f"📊 {esc(score)}"
    )
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


def summary(title: str, total: int, correct: int, wrong: int, percent: int) -> str:
    return (
        f"🏁 <b>O‘yin tugadi</b> · {esc(title)}\n"
        f"📊 Bilimdonlar <b>{correct}</b> — <b>{wrong}</b> Qarshi taraf\n"
        f"✅ To‘g‘ri javoblar: {correct}/{total} ({percent}%)\n\n"
        "Yana o‘ynash: /solo 959 yoki /duel 959"
    )


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
            "Savol-javob o‘yini boshlash uchun <code>/solo 959</code> (yakka) yoki "
            "<code>/duel 959</code> (guruh) yozing. Qoidalar: /rules."
        )
    if value in {"rahmat", "thanks", "spasibo"} or value.startswith("rahmat"):
        return "😊 Arzimaydi! Yaxshi o‘yin tilayman. /solo 959 bilan davom etamizmi?"
    if any(word in value for word in ("qanday o‘ynayman", "qanday oynayman", "qoida", "qoidalar", "nima qilish")):
        return rules()
    if value in {"yordam", "help", "komak"} or value.startswith("yordam"):
        return (
            "🆘 Men savol-javob botiman.\n\n"
            "/start — tanishtiruv\n/rules — qoidalar\n/packages — paketlar\n"
            "/solo 959 — yakka o‘yin\n/duel 959 — guruh o‘yini\n\n"
            "Savolga javobni unga reply qilib yozing."
        )
    if value in {"paketlar", "paket", "savollar", "baza"}:
        return "📦 Paketlarni ko‘rish uchun <code>/packages</code> yoki qidiruv bilan <code>/packages Zakovat</code> yozing."
    if value in {"hisob", "score", "natija"}:
        return "📊 Hisobni ko‘rish uchun o‘yin paytida <code>/score</code> yozing yoki savol xabariga reply qiling."
    if value in {"kim", "nima", "bot", "about"}:
        return (
            "🤖 Men Zakovat Agent — arxivlangan 30 853 savol bilan ishlaydigan o‘yin botiman. "
            "Tanishtiruv: /start · qoidalar: /rules."
        )
    return None
