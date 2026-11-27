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

## Vercel ga deploy qilish (webhook + Mini App)

`api/index.py` — FastAPI ASGI entrypoint. Vercel Python funksiyasi `/api/index` manzilida ishlaydi: `POST /api/index` — Telegram webhook, `GET /api/index` — diagnostika, `GET /api/index?setup=1` — webhook’ni qo‘lda o‘rnatish. `/` — Mini App (statik fayl). Serverless holatda o‘yin holati xabar ichidagi kompakt markerda saqlanadi, javoblar **ForceReply** orqali qabul qilinadi; vaqt tugashi keyingi xabar kelganda hisoblanadi.

**Eng oddiy usul — repo’ni Vercel’ga qo‘shish kifoya:** Vercel dashboard → *Add New → Project* → shu reponi tanlang → *Deploy*. `vercel.json`, `.python-version` va `requirements.txt` tayyor, build buyrug‘i kerak emas. Keyingi `push` lar avtomatik deploy bo‘ladi.

Deploy qilgach Project → Settings → Environment Variables ga kirib, ikkita o‘zgaruvchi qo‘ying:

- `BOT_TOKEN` — BotFather’dan olingan **yangi** token (chatga yuborilgani allaqachon oshkor bo‘lgan);
- `WEBHOOK_SECRET` — o‘zingiz tanlagan tasodifiy sir (masalan `openssl rand -hex 24`).

Ixtiyoriy: `ZAKOVAT_TIMER_SECONDS` (standart 60), `WEBHOOK_URL` (o‘z domeningiz bo‘lsa), `MINIAPP_URL`.

**Webhook o‘zi o‘rnatiladi:** saytga birinchi marta kiritilganda bot `/api/index` manzilini Telegram’ga ro‘yxatdan o‘tkazadi. Manzil quyidagi tartibda tanlanadi:

1. `WEBHOOK_URL` (o‘zingiz belgilasangiz);
2. `VERCEL_PROJECT_PRODUCTION_URL` — Vercel’ning **ochiq** production domeni (masalan `zakovat-peach.vercel.app`);
3. `VERCEL_URL` — oxirgi zaxira.

`VERCEL_URL` dan foydalanmang: bu Vercel’ning generatsiya qilingan deployment manzili (`zakovat-<hash>-<jamoa>.vercel.app`) va **Standard Deployment Protection** uni login sahifasi bilan himoya qiladi — Telegram u yerga kira olmaydi va bot umuman javob bermaydi. Shu sababli kod production domenini afzal ko‘radi. Har kuni 03:00 da `/api/index` cron webhook’ni qayta tasdiqlaydi.

**Diagnostika (token qiymatlari ko‘rsatilmaydi):**

- `https://<loyihangiz>.vercel.app/api/index` — muhit o‘zgaruvchilari bor-yo‘qligi, tanlangan webhook manzili, arxiv holati, Telegram’ning oxirgi xatoligi va so‘nggi handler xatolari;
- `.../api/index?setup=1` — webhook’ni qayta o‘rnatadi va natijani ko‘rsatadi;
- `.../api/index?test=1` — Telegram’ga chiqmasdan `/start`, `/rules`, `/solo 959` va bitta javobni haqiqiy handlerlar orqali o‘tkazib, bot yuboradigan xabarlarni ko‘rsatadi. Deploy muvaffaqiyatli bo‘ldimi — shu sahifa aytib beradi.

`scripts/set_webhook.py` — qo‘lda bajarish uchun yana bir zaxira usul.

So‘ngra `/start`, `/rules`, `/solo 959` bilan sinab ko‘ring.

Barchasini bir komandada bajarish uchun (tokenlar faqat lokal shell muhitida):

```bash
VERCEL_TOKEN=... VERCEL_ORG_ID=... VERCEL_PROJECT_ID=... \
BOT_TOKEN=... WEBHOOK_SECRET=... bash scripts/deploy_all.sh
```

GitHub Actions orqali ham deploy qilish mumkin: repo Settings → Secrets and variables → Actions da `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` secretlarini qo‘ying, keyin **Deploy Zakovat Agent to Vercel** workflow’sini ishga tushuring (`workflow_dispatch` yoki `repository_dispatch` `deploy-zakovat`). `BOT_TOKEN` va `WEBHOOK_SECRET` ni Vercel dashboard’ida qo‘yish shart — ular repo secretlariga yozilmaydi.

Eslatma: Vercel serverless’da doimiy jarayon yo‘q, shuning uchun tilingan savollar faqat keyingi xabar/compound kelganda yopiladi. Doimiy real-time timer kerak bo‘lsa, botni `python3 run_bot.py` bilan uzluksiz serverda ishga tushurish kerak.

## O‘yin mantig‘i

- `/start` — keng tanishtiruv, `/help` — yordam, `/rules` — batafsil qoidalar;
- guruhga qo‘shilganda bot o‘zini tanishtiradi va qoidalar havolasini yuboradi;
- o‘yin yo‘q paytda `salom`, `yordam`, `qanday o‘ynayman?`, `paketlar`, `hisob`, `rahmat` kabi xabarlarga javob beradi;
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
