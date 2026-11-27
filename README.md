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

## Vercel ga deploy qilish (webhook)

`api/index.py` — FastAPI ASGI entrypoint. Vercel Python funksiyasi `/api/index` manzilida ishlaydi: `POST /api/index` — Telegram webhook (xabar va tugma bosishlari) va ichki `tick` so‘rovlari (jonli taymer), `GET /api/index` — diagnostika, `?setup=1` — webhook’ni qo‘lda o‘rnatish (`&drop=1` — Telegram navbatini tozalash), `?test=1` — o‘z-o‘zini sinash. `/` — bot haqidagi oddiy sahifa (`public/index.html`); Mini App ishlatilmaydi, hammasi Telegram ichida. Serverless holatda o‘yin holati xabarning ichida **ko‘rinmas belgilarda** (nol kenglikli `U+200C`/`U+200D`) saqlanadi: ekranda hech narsa ko‘rinmaydi, ochib o‘qiladigan joy ham yo‘q — spoiler emas, javoblar **ForceReply** orqali qabul qilinadi.

**Eng oddiy usul — repo’ni Vercel’ga qo‘shish kifoya:** Vercel dashboard → *Add New → Project* → shu reponi tanlang → *Deploy*. `vercel.json`, `.python-version` va `requirements.txt` tayyor, build buyrug‘i kerak emas. Keyingi `push` lar avtomatik deploy bo‘ladi.

Deploy qilgach Project → Settings → Environment Variables ga kirib, ikkita o‘zgaruvchi qo‘ying:

- `BOT_TOKEN` — BotFather’dan olingan **yangi** token (chatga yuborilgani allaqachon oshkor bo‘lgan);
- `WEBHOOK_SECRET` — o‘zingiz tanlagan tasodifiy sir (masalan `openssl rand -hex 24`).

Ixtiyoriy: `ZAKOVAT_ROUNDS`, `ZAKOVAT_PER_ROUND`, `ZAKOVAT_TIMER_SECONDS`, `ZAKOVAT_IDLE_LIMIT`, `ZAKOVAT_TICK_SECONDS`, `ZAKOVAT_TICK_MODE`, `ZAKOVAT_LIVE_TIMER`, `ZAKOVAT_HIDDEN_STATE`, `WEBHOOK_URL` (o‘z domeningiz bo‘lsa).

**Webhook o‘zi o‘rnatiladi:** saytga birinchi marta kiritilganda bot `/api/index` manzilini Telegram’ga ro‘yxatdan o‘tkazadi. Manzil quyidagi tartibda tanlanadi:

1. `WEBHOOK_URL` (o‘zingiz belgilasangiz);
2. `VERCEL_PROJECT_PRODUCTION_URL` — Vercel’ning **ochiq** production domeni (masalan `zakovat-peach.vercel.app`);
3. `VERCEL_URL` — oxirgi zaxira.

`VERCEL_URL` dan foydalanmang: bu Vercel’ning generatsiya qilingan deployment manzili (`zakovat-<hash>-<jamoa>.vercel.app`) va **Standard Deployment Protection** uni login sahifasi bilan himoya qiladi — Telegram u yerga kira olmaydi va bot umuman javob bermaydi. Shu sababli kod production domenini afzal ko‘radi. Har kuni 03:00 da `/api/index` cron webhook’ni qayta tasdiqlaydi.

**Diagnostika (token qiymatlari ko‘rsatilmaydi):**

- `https://<loyihangiz>.vercel.app/api/index` — muhit o‘zgaruvchilari bor-yo‘qligi, tanlangan webhook manzili, arxiv holati, Telegram’ning oxirgi xatoligi va so‘nggi handler xatolari;
- `.../api/index?setup=1` — webhook’ni qayta o‘rnatadi va natijani ko‘rsatadi;
- `.../api/index?test=1` — Telegram’ga chiqmasdan `/start`, `/rules`, `/solo 959`, bitta to‘g‘ri javob va **uchta javobsiz savolni** haqiqiy handlerlar orqali o‘tkazib, bot yuboradigan xabarlarni (ogohlantirish va avto-to‘xtatish bilan) ko‘rsatadi. Deploy muvaffaqiyatli bo‘ldimi — shu sahifa aytib beradi;
- `.../api/index?tickprobe=1` — jonli taymer transportini tekshiradi: funksiya o‘ziga `tick` so‘rovini yuborib, natijani qaytaradi (Telegram’ga hech narsa ketmaydi) va ko‘rinmas holat kodini ham tekshirib ko‘rsatadi;
- `.../api/index?bgprobe=1` — platforma javob yuborilgandan **keyin** ham ish bajarishiga ruxsat berishini o‘zi tekshiradi: o‘ziga bir necha sekundlik fon ishi buyuradi, ichki javob qancha tez qaytganini o‘lchaydi va ish bajarildimi — ko‘radi. `xulosa` maydoni `bg` rejimi ishlaydimi yoki `ZAKOVAT_TICK_MODE=sleep` qo‘yish kerakligini aytadi. Jonli taymer aynan shu mexanizmga tayanadi.

`scripts/set_webhook.py` — qo‘lda bajarish uchun yana bir zaxira usul.

So‘ngra `/start`, `/help`, `/packages`, `/solo` bilan sinab ko‘ring.

Barchasini bir komandada bajarish uchun (tokenlar faqat lokal shell muhitida):

```bash
VERCEL_TOKEN=... VERCEL_ORG_ID=... VERCEL_PROJECT_ID=... \
BOT_TOKEN=... WEBHOOK_SECRET=... bash scripts/deploy_all.sh
```

GitHub Actions orqali ham deploy qilish mumkin: repo Settings → Secrets and variables → Actions da `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` secretlarini qo‘ying, keyin **Deploy Zakovat Agent to Vercel** workflow’sini ishga tushuring (`workflow_dispatch` yoki `repository_dispatch` `deploy-zakovat`). `BOT_TOKEN` va `WEBHOOK_SECRET` ni Vercel dashboard’ida qo‘yish shart — ular repo secretlariga yozilmaydi.

**Jonli taymer serverless’da ham ishlaydi.** Vercel’da doimiy jarayon yo‘q, shuning uchun taymer qisqa «sakrashlar» zanjiridan iborat: har savoldan keyin funksiya o‘ziga `tick` so‘rovi yuboradi, u `ZAKOVAT_TICK_SECONDS` (standart 3) sekund kutib, hisoblagichni qayta chizadi va navbatagi `tick` ni chaqiradi.

- `ZAKOVAT_TICK_MODE=bg` (standart): `tick` so‘roviga **darhol** javob beriladi, kutish va chizish javob yuborilgandan keyin bajariladi. Telegram ham, zanjirning oldingi halqasi ham kutib qolmaydi, aloqa uzilib qolsa ham zanjir davom etadi;
- `ZAKOVAT_TICK_MODE=sleep`: zaxira usul — halqa javob ichida kutadi (agar platforma javobdan keyingi ishni saqlab qolmasa, `?bgprobe=1` shuni ko‘rsatadi va shu rejim qo‘yiladi);
- vaqt tugaganda oxirgi halqa savolni o‘zi yopadi — hech kim javob yozmagan bo‘lsa ham. Tasmani kim birinchi bo‘lib yopsa (taymer yoki tugma), savolni shu tomon yechadi; ikkinchisi jim turadi, shuning uchun javob ikki marta ochilmaydi;
- `ZAKOVAT_LIVE_TIMER=0` qo‘yilsa taymer faqat «Vaqtni yangilash» tugmasi bilan yangilanadi.

## O‘yin mantig‘i

**Sozlamalar o‘yin boshlanishidan oldin so‘raladi.** `/solo` yoki `/duel` yozilgach bot panel yuboradi va tugmalar orqali uchta parametr tanlanadi:

- **raundlar soni** (1, 2, 3, 5, 10);
- **har raundda nechta savol** (5, 10, 15, 25 yoki „hammasi“);
- **har savolga necha sekund** (30, 45, 60, 90, 120).

Keyin **Boshlash** bosiladi. Standart qiymatlar `ZAKOVAT_ROUNDS`, `ZAKOVAT_PER_ROUND`, `ZAKOVAT_TIMER_SECONDS` orqali o‘zgartiriladi. «Boshqa paket» tugmasi boshqa tasodifiy paket tanlaydi.

**Jonli vaqt.** Har savoldan oldin alohida „hisoblagich tasmasi“ yuboriladi:

```
Savol 3 / 30 · 1-raund
QOLGAN VAQT  00:45
▰▰▰▰▰▰▰▱▱▱
```

Hisoblagich **bir marta yozib qo‘yilgan son emas**: sekundlar `00:45` ko‘rinishida, ustun har sakrashda qisqarib boradi, tasma **o‘zi yangilanib turadi** (har `ZAKOVAT_TICK_SECONDS` sekundda) va vaqt tugaganda savolni o‘zi yopadi. Ostidagi tugmalar:

- **00:45 · Vaqtni yangilash** — tugmaning o‘zi ham jonli: yorlig‘idagi soat har sakrashda yangilanadi, bosilganda qolgan vaqtni darhol ko‘rsatadi; vaqt tugagan bo‘lsa javobni ochib, ochko Qarshi tarafga yozadi;
- **O‘tkazish** — faqat admin;
- **Hisob**;
- **To‘xtatish**.

Javob berilgach tasma o‘chiriladi (botda xabar o‘chirish ruxsati bo‘lmasa, bir qatorli «Savol 3 / 30 — yakunlandi» ga yig‘iladi), chat toza qoladi. Polling rejimida (`python3 run_bot.py`) tasma job queue orqali yangilanadi.

**Ko‘rinmas holat.** Serverless xotirasiz, shuning uchun o‘yin holati (paket, savol, hisob, deadline, javobsizlik seriyasi) xabar ichida nol kenglikli belgilar ketma-ketligi sifatida yuradi: sentinel va nazorat yig‘indisi bilan tekshiriladi, buzilgan bo‘lsa e’tiborsiz qoldiriladi, xabar 4096 belgi chegarasidan oshmaydi. Ekranda na matn, na ochib o‘qiladigan spoiler qoladi; eski `#ZS`/`#ZS2` ko‘rinishidagi xabarlar ham o‘qilaveradi. Kerak bo‘lsa `ZAKOVAT_HIDDEN_STATE=spoiler` bilan eski usulga qaytish mumkin.

**Avto-to‘xtatish.** Ketma-ket **uch** savolga hech qanday taxmin yozilmasa (vaqt tugadi yoki admin o‘tkazdi), o‘yin o‘zini to‘xtatadi va yakuniy hisobni chiqaradi. Noto‘g‘ri javob ham faollik hisoblanadi — seriya uzilmaydi, to‘g‘ri javob esa uni noldan boshlaydi. Chegara `ZAKOVAT_IDLE_LIMIT` (0 = o‘chirilgan). Har bir javobsiz savoldan keyin ogohlantirish yoziladi.

**Uslub.** Xabarlarda bezak emojisi yo‘q: sarlavhalar katta harfda (`SAVOL 3 / 30`, `O‘YIN YAKUNI`), ro‘yxatlar `•` bilan, hisoblagich `▰▱` belgilarida, tugmalar faqat matn.

**Buyruqlar**

- `/start` — tanishtiruv, `/help` — barcha buyruqlar, `/rules` — qoidalar;
- `/solo [id|qidiruv]` — yakka o‘yin (javobni faqat boshlagan kishi yozadi);
- `/duel [id|qidiruv]` — guruh o‘yini (hamma javob yozadi);
- `/packages` — **barcha 1266 paket sahifalab** (`|‹ Boshi`, `‹ Orqaga`, `Oldinga ›`, `Oxiri ›|` tugmalari); `/packages Zakovat` — sarlavhadan qidirish; `/packages sahifa 5` — aniq sahifa;
- `/score` — hisob va joriy raund; `/stop` — tugatish;
- `/skip` — **faqat admin**: joriy savolni o‘tkazadi va javobini darhol ko‘rsatadi («hech kim javobni topmadi»);
- `/accept` — **faqat admin**: oxirgi noto‘g‘ri javobni to‘g‘ri deb qabul qiladi va bot uni o‘rganadi.

Admin = guruhda creator/administrator yoki o‘yinni boshlagan kishi; shaxsiy chatda har doim ruxsat.

**Hisob:** to‘g‘ri javob → **Bilimdonlar** +1; javob yo‘q / vaqt tugadi / o‘tkazildi → **Qarshi taraf** +1. 10 savoldan 4 tasiga javob berilsa hisob **4–6** bo‘ladi.

**Rasmlar.** Ba’zi savollarda rasm bor (2912 ta savol). Bot rasmni shu tartibda yuboradi: asl `cdn.zakovatklubi.uz` havolasi → yuklanmasa `data/images/` dagi generatsiya qilingan zaxira nusxa → u ham bo‘lmasa havola matn bilan. Javob rasmi (`answer_attachments`) ham javobdan keyin ko‘rsatiladi.

Guruhga qo‘shilganda bot o‘zini tanishtiradi; o‘yin yo‘q paytda `salom`, `yordam`, `qoidalar`, `paketlar`, `hisob`, `rahmat` kabi xabarlarga javob beradi.

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
| `bot/archive.py` | manifest va shardlardan paket/savol olish, qidiruv, sahifalab ro‘yxat |
| `bot/quiz.py` | sof o‘yin logikasi: raundlar, playlist, hisob, javobsizlik seriyasi (Telegramga bog‘liq emas) |
| `bot/settings.py` | raund/savol/vaqt sozlamalari va inline tugma ma’lumotlari (64 bayt ichida) |
| `bot/messages.py` | barcha matnlar (emojisiz): tanishtiruv, yordam, qoidalar, hisoblagich, avto-to‘xtatish |
| `bot/answers.py` | javob normalizatsiyasi, moslik va o‘rganish |
| `bot/webhook.py` | Vercel serverless webhook: handlerlar, jonli taymer (`tick`), diagnostika, self-test |
| `bot/selftest.py` | Telegram’ga chiqmasdan butun o‘yin oqimini sinash |
| `bot/app.py` | polling varianti (job queue bilan jonli hisoblagich va avto-to‘xtatish) |
| `api/index.py` | Vercel entrypoint |
| `public/index.html` | bot haqidagi sahifa (Mini App emas) |
| `data/images/` | rasmli savollar uchun zaxira tasvirlar |
| `run_bot.py` | polling kirish nuqtasi |
| `scripts/export_all.py` | butun bazani API’dan eksport qilish |

## Sinov

```bash
python3 -m unittest discover -s tests -v
```

## Xavfsizlik va foydalanish

- Savollar faqat yopiq guruh / shaxsiy o‘yin uchun. Sayt materiallarining litsenziyasi aniq emas; ommaviy tarqatish tavsiya etilmaydi.
- Ilgari suhbatda paydo bo‘lgan Telegram bot tokeni, `vcp_...` va GitHub tokeni ishlatilmadi, faylga yozilmadi — ularni yangilash kerak.
- Ba’zi savollarda rasm bor: bot avval asl rasmni yuboradi, yuklanmasa `data/images/` dagi zaxira nusxani ishlatadi (generatsiya qilingan tasvirlar ilustrativ, asl nusxa emas).
