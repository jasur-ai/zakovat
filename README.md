# Zakovat savollar arxivi + Telegram bot

`zakovatklubi.uz` savollar bazasi JSON ko‘rinishda GitHub’da saqlangan va yopiq Telegram guruhida / shaxsiy chatda o‘yin o‘tkazadigan bot mavjud. Bot **tashqi AI API va API kalitisiz** ishlaydi.

## Arxiv holati

- `data/all/manifest.json` — 1266 paket, **30853 savol** indeksi;
- `data/all/questions/questions-0001.json … 0007.json` — barcha savollar (matn, javob, izoh, havolalar, paket id);
- `data/zakovat-959.json`, `data/zakovat-959.flat.json` — sinov uchun 54 savollik 959-paket (2024-yil 19–20 dekabr).

Arxiv avvalgi branchda GitHub Actions orqali yaratilgan va shu branchga qaytarib olingan. To‘liq bazani qayta yuklash uchun `.github/workflows/export-all.yml` (`workflow_dispatch` yoki `repository_dispatch` `archive-zakovat`) ishlatiladi.

## Botni ishga tushirish

```bash
python3 -m pip install -r requirements.txt
export BOT_TOKEN='...'          # BotFather tokeni; faylga yozmang, chatga yubormang
python3 run_bot.py
```

`.env.example` — faqat namuna. **Token hech qaerga commit qilinmaydi.** Suhbatda ilgari paydo bo‘lgan tokenlar kompromat hisoblanadi — ularni BotFather orqali yangilang.

## O‘yin mantig‘i

- `/solo [id|yoki qidiruv]` — yakka o‘yin: javobni faqat o‘yinni boshlagan kishi yozadi;
- `/duel [id|yoki qidiruv]` — guruh o‘yini: hamma ishtirokchi javob yozadi, bir savolga birinchi to‘g‘ri javob ochko oladi;
- `/packages [qidiruv]` — paket topish (masalan `/packages Zakovat`);
- `/score` — hisob; `/skip` — savolni o‘tkazish; `/stop` — tugatish;
- `/accept` — admin oxirgi noto‘g‘ri javobni to‘g‘ri deb tasdiqlaydi va bot uni o‘rganadi.

**Hisob:** to‘g‘ri javob → **Bilimdonlar** +1; javob yo‘q / vaqt tugadi / o‘tkazildi → **Qarshi taraf** +1. 10 savoldan 4 tasiga javob berilsa hisob **4–6** bo‘ladi. Yakka o‘yinda ham shu qoida, faqat bitta odam bilan.

Har savol uchun vaqt `ZAKOVAT_TIMER_SECONDS` (standart 60 s) bilan belgilanadi. Izoh (`comment`) javobdan keyin ko‘rsatiladi.

## Javob tekshirish — lokal „agent“ (API kalitsiz)

`bot/answers.py`:

1. matnni kichik harfga o‘tkazadi, kirill/lotin yozuvni bir xil ko‘rinishga keltiradi (`Пиёз пўсти` = `Piyoz po'sti`), apostroflar va tinish belgilarini tekislaydi;
2. arxivdagi javobdan barcha muqobil variantlarni ajratadi (`Javob: Pinokkio.`, `A / B`, qavs ichidagi izohlar);
3. aniq moslik, raqamlar (`5` = `besh`), so‘z tartibi va yaqinlik (SequenceMatcher) bo‘yicha solishtiradi;
4. `/accept` bilan tasdiqlangan javoblar `data/answer_aliases.json` ga saqlanadi — keyingi o‘yinlarda shu varian ham to‘g‘ri hisoblanadi.

Hech qanday LLM, token yoki tashqi xizmat chaqirilmaydi.

## Kod tuzilishi

| Fayl | Vazifasi |
| --- | --- |
| `bot/archive.py` | manifest va shardlardan paket/savol olish, qidiruv |
| `bot/quiz.py` | sof o‘yin logikasi va hisob (Telegramga bog‘liq emas) |
| `bot/answers.py` | javob normalizatsiyasi, moslik va o‘rganish |
| `bot/app.py` | Telegram handlerlari, vaqt o‘tishi, xabarlar |
| `run_bot.py` | kirish nuqtasi |
| `scripts/export_all.py` | butun bazani API’dan eksport qilish |

## Sinov

```bash
python3 -m unittest discover -s tests -v
```

## Xavfsizlik va foydalanish

- Savollar faqat yopiq guruh / shaxsiy o‘yin uchun. Sayt materiallarining litsenziyasi aniq emas; ommaviy tarqatish tavsiya etilmaydi.
- Ilgari suhbatda paydo bo‘lgan Telegram bot tokeni, `vcp_...` va GitHub tokeni ishlatilmadi, faylga yozilmadi — ularni yangilash kerak.
- Ba’zi savollarda rasm bor: bot ularning havolasini xabar ichida ko‘rsatadi.
