# Zakovat savollar arxivi + Telegram bot

`zakovatklubi.uz` va `turnirlar.uz` savollar bazalari JSON ko‘rinishda GitHub’da saqlangan va yopiq Telegram guruhida / shaxsiy chatda o‘yin o‘tkazadigan bot mavjud. Jami **~419 000 savol** (8 670 to‘plam). Bot **tashqi AI API va API kalitisiz** ishlaydi.

## Arxiv holati

Bot **ikki manbani** bitta interfeys orqali o‘qiydi (`bot/archive.py`):

- `data/all/manifest.json` — **zakovatklubi.uz**: 1266 paket, **30853 savol**. Shardlari (`data/all/questions/questions-0001.json … 0007.json`) deploy bilan birga ketadi, shuning uchun diskdan o‘qiladi;
- `data/turnirlar/manifest.json` — **turnirlar.uz**: 7404 paket, **388 388 savol** (eng kattasi 1678 savolli). Shardlari (50 fayl, ~112 MB gzip, har biri ~2.3 MB) GitHub Release [`savollar-bazasi`](https://github.com/jasur-ai/zakovat/releases/tag/savollar-bazasi) da turadi va **kerak bo‘lganda** yuklanadi: `data/turnirlar/shards` `.gitignore` va `.vercelignore` ichida, ya’ni na repo, na deploy og‘irlashadi. Xotirada bir vaqtda 2 shard ushlab turiladi, qolgani `/tmp` keshida; shard yuklanmasa o‘yin buzilmaydi — bot mahalliy baza bilan ishlashni davom ettiradi;
- `data/zakovat-959.json`, `data/zakovat-959.flat.json` — sinov uchun 54 savollik 959-paket (2024-yil 19–20 dekabr).

Paket id’lari to‘qnashmaydi: turnirlar.uz paketlari `900000+`, savollari `900000000+` siljish bilan saqlanadi (7800-paket → `907800`).

Katalog'dagi 7 798 to‘plamning barchasi o‘qilgan: 7 404 tasi saqlangan, 394 tasining hamma savoli `data/all`’da allaqachon bor edi. Sayt sarlavhasidagi «559 214 savol» butun korpusni sanaydi (to‘plamga kirmagan savollarni ham) — `--mode verify` katalog bo‘ylab olingan namunada har to‘plamning o‘zi e’lon qilgan savollarining **100%** ini topadi (hisob `data/turnirlar/probe/digest.txt` da).

**Katta baza bilan o‘ynash.** `/solo katta` (yoki `butun`, `hamma`) — ikki manbadan tasodifiy to‘plam; `/solo 907785` — aniq to‘plam; `/packages` ro‘yxatida katta bazadagi qatorlar `*` bilan belgilanadi. To‘plam nomi bo‘yicha qidiruv (`/packages liga`) ikkala bazani qamrab oladi.

Ixtiyoriy o‘zgaruvchilar: `ZAKOVAT_TURNIRLAR_BASE` (shardlar manzili; sukut bo‘yicha manifest’dagi Release), `ZAKOVAT_TURNIRLAR_CACHE` (kesh katalogi; sukut bo‘yicha `/tmp/zakovat-turnirlar`).

### turnirlar.uz bazasi qanday yig‘iladi

Saytning JSON API’si yo‘q (faqat PDF/DOCX yuklab olish bor), shuning uchun `scripts/export_turnirlar.py` sahifalarni o‘qiydi:

- `/savollar/paketlar?page=N` — 30 ta `article.zk-catalog-card`: id, sarlavha, savol/tur soni, sana, toifa, mualliflar, manba;
- `/savollar/paket/<id>` — `section#tur-N` ichida `article#savol-N`; savol matni `p.zk-question-text`, javob bloki `Javob: Qabul: Izoh: Manba(lar): Muallif(lar): Muharrir:` yorliqlari bilan, rasm `figure.zk-material-block` ichida (havolalar mutlaq qilinadi), `Blits`/`Duplet`/`Rasmli` nishonlari ham olinadi;
- **60 savoldan katta to‘plam bir sahifaga sig‘maydi**: sayt uni turma-tur ko‘rsatadi va sahifa oxirida «Keyingi tur: 2-tur →» havolasi (`?tur=2`) qoldiradi. Parser sahifadagi `Savollar soni` ga yetgunicha shu havolalardan yuradi (`fetch_package`), savollarni sayt tartibida (`savol-N` raqami turlar bo‘ylab davom etadi) joylaydi va takrorlangan savolni bir marta saqlaydi;
- Next.js flight payload butun sahifani `<script>` ichida takrorlaydi — parser uni kesib tashlaydi.

Yig‘ish **GitHub Actions**’da ishlaydi (`.github/workflows/turnirlar-import.yml`), chunki ishlab chiqish muhitidan saytga chiqib bo‘lmaydi:

- ishga tushirish: `.turnirlar-run` faylini o‘zgartirib push qilish — yoki Actions → *Import turnirlar.uz question base* → *Run workflow*;
- variantlar: `source` (`all` / `zakovat-klubi` / `boshqa`), `start_page`, `pages`, `max_packages`, `workers`, `polite`, `dedupe`, `fresh`, `commit`, `release`;
- `fresh: true` saqlangan shardlarni tiklamay, bazani noldan yig‘adi — parser ko‘proq savol topadigan bo‘lganda (yuqoridagi tur sahifalari kabi) kerak;
- yig‘ish **davom etadigan**: Release’dagi shardlar avval qaytarib olinadi, saqlangan paketlar o‘tkazib yuboriladi; diskda yo‘q shard manifest’dan olib tashlanadi, ya’ni «saqlangan» deb hech narsa hisoblanmaydi;
- sayt yozuvi (`ö ç ş ğ ı`) standart o‘zbek lotiniga o‘tkaziladi (`o‘ ch sh g‘ i`); katta harfli so‘zlarda `Ş→SH`, bosh harfda `Ş→Sh`;
- `data/all`’da bor savollar takrorlanmaydi (matnning ortografiyaga befarq barmoq izi bo‘yicha) — saytning 1238 ta «Zakovat klubi» to‘plami asosan shu sabab tushib qoladi;
- `--mode probe` sahifa tuzilishini tekshiradi va hisobotni branchga commit qiladi (`.github/workflows/export-turnirlar.yml`), `--mode check` yuklab olingan fayllarni **tarmoqsiz** tahlil qiladi — parserlar shunday yozilgan;
- `--mode verify` katalog bo‘ylab tarqatilgan sahifalardan namuna oladi va har to‘plamda sayt aytgan son bilan parser qaytargan sonni solishtiradi: `short` nol bo‘lmasa, biror sahifa qisman o‘qilgan. Hisob `data/turnirlar/probe/digest.txt` da saqlanadi.

zakovatklubi.uz eksporti esa `.github/workflows/export-all.yml` orqali yangilanadi (`workflow_dispatch`, `repository_dispatch archive-zakovat` yoki har kuni 03:17).

## Botni ishga tushirish

```bash
python3 -m pip install -r requirements.txt
export BOT_TOKEN='...'          # BotFather tokeni; faylga yozmang, chatga yubormang
python3 run_bot.py
```

`.env.example` — faqat namuna. **Token hech qaerga commit qilinmaydi.** Suhbatda ilgari paydo bo‘lgan tokenlar kompromat hisoblanadi — ularni BotFather orqali yangilang.

## Vercel ga deploy qilish (webhook)

`api/index.py` — FastAPI ASGI entrypoint. Vercel Python funksiyasi `/api/index` manzilida ishlaydi: `POST /api/index` — Telegram webhook (xabar va tugma bosishlari) va ichki `tick` so‘rovlari (jonli taymer), `GET /api/index` — diagnostika, `?setup=1` — webhook’ni qo‘lda o‘rnatish (`&drop=1` — Telegram navbatini tozalash), `?test=1` — o‘z-o‘zini sinash. `/` — bot haqidagi oddiy sahifa (`public/index.html`), `/api/now` — server vaqti, `/app` — statistika paneli (Mini App, faqat ega va `/grant` olganlar), `/api/app` — shu panelning ma’lumotlari, `/api/browse` — savollar bazasi bo‘ylab qidirish (bo‘limlar, paketlar, savollar), `/api/tour` — turnirni panel ichida shakllantirish yoki rejasini bekor qilish. Uchala manzil bitta darvozadan o‘tadi (`panel_request`): initData imzosi va `stats.allowed(id)` tekshiriladi, aks holda `ok: false` va rad sababi. O‘yin Telegram ichida o‘ynaladi; hisoblagich xabarning o‘zida yuradi, Mini App faqat statistika paneli uchun. Serverless holatda o‘yin holati xabarning ichida **ko‘rinmas belgilarda** (nol kenglikli `U+200C`/`U+200D`) saqlanadi: ekranda hech narsa ko‘rinmaydi, ochib o‘qiladigan joy ham yo‘q — spoiler emas, javoblar **ForceReply** orqali qabul qilinadi.

**Eng oddiy usul — repo’ni Vercel’ga qo‘shish kifoya:** Vercel dashboard → *Add New → Project* → shu reponi tanlang → *Deploy*. `vercel.json`, `.python-version` va `requirements.txt` tayyor, build buyrug‘i kerak emas. Keyingi `push` lar avtomatik deploy bo‘ladi.

Deploy qilgach Project → Settings → Environment Variables ga kirib, ikkita o‘zgaruvchi qo‘ying:

- `BOT_TOKEN` — BotFather’dan olingan **yangi** token (chatga yuborilgani allaqachon oshkor bo‘lgan);
- `WEBHOOK_SECRET` — o‘zingiz tanlagan tasodifiy sir (masalan `openssl rand -hex 24`).

Ixtiyoriy: `ZAKOVAT_ROUNDS`, `ZAKOVAT_PER_ROUND`, `ZAKOVAT_TIMER_SECONDS`, `ZAKOVAT_IDLE_LIMIT`, `ZAKOVAT_TICK_SECONDS`, `ZAKOVAT_TICK_MODE`, `ZAKOVAT_LIVE_TIMER`, `ZAKOVAT_HIDDEN_STATE`, `ZAKOVAT_OWNER_ID`, `ZAKOVAT_STATS_URL`, `ZAKOVAT_STATS_TOKEN`, `ZAKOVAT_STATS_FLUSH`, `WEBHOOK_URL` (o‘z domeningiz bo‘lsa).

**Webhook o‘zi o‘rnatiladi:** saytga birinchi marta kiritilganda bot `/api/index` manzilini Telegram’ga ro‘yxatdan o‘tkazadi. Manzil quyidagi tartibda tanlanadi:

1. `WEBHOOK_URL` (o‘zingiz belgilasangiz);
2. `VERCEL_PROJECT_PRODUCTION_URL` — Vercel’ning **ochiq** production domeni (masalan `zakovat-peach.vercel.app`);
3. `VERCEL_URL` — oxirgi zaxira.

`VERCEL_URL` dan foydalanmang: bu Vercel’ning generatsiya qilingan deployment manzili (`zakovat-<hash>-<jamoa>.vercel.app`) va **Standard Deployment Protection** uni login sahifasi bilan himoya qiladi — Telegram u yerga kira olmaydi va bot umuman javob bermaydi. Shu sababli kod production domenini afzal ko‘radi. Har kuni 03:00 da `/api/index` cron webhook’ni qayta tasdiqlaydi.

**Diagnostika (token qiymatlari ko‘rsatilmaydi):**

- `https://<loyihangiz>.vercel.app/api/index` — muhit o‘zgaruvchilari bor-yo‘qligi, tanlangan webhook manzili, arxiv holati, Telegram’ning oxirgi xatoligi va so‘nggi handler xatolari;
- `.../api/index?setup=1` — webhook’ni qayta o‘rnatadi va natijani ko‘rsatadi;
- `.../api/index?test=1` — Telegram’ga chiqmasdan `/start`, `/rules`, `/solo 959`, bitta to‘g‘ri javob, **uchta javobsiz savolni** va `/stats` panelini haqiqiy handlerlar orqali o‘tkazib, bot yuboradigan xabarlarni (ogohlantirish, avto-to‘xtatish va statistika bilan) ko‘rsatadi. Sinov statistikani tashqi saqlashga **yozmaydi**. Deploy muvaffaqiyatli bo‘ldimi — shu sahifa aytib beradi;
- `.../api/index?tickprobe=1` — jonli taymer transportini tekshiradi: funksiya o‘ziga `tick` so‘rovini yuborib, natijani qaytaradi (Telegram’ga hech narsa ketmaydi) va ko‘rinmas holat kodini ham tekshirib ko‘rsatadi;
- `.../api/index?statsprobe=1` — statistika saqlashini tekshiradi: mavjud hisoblarni saqlab, sinov belgisini yozadi va qayta o‘qiydi (`ZAKOVAT_STATS_URL` berilmagan bo‘lsa, shuni aytadi);
- `.../api/index?statsprobe=1&create=1` — bepul JSON saqlashni o‘zi yaratishga urinadi va har bir xizmatning javobini ko‘rsatadi;
- `.../api/index?statsprobe=1&tg=1` — Telegram’ni saqlagich sifatida sinaydi: xabar yuboradi, pinlaydi, qayta o‘qiydi, o‘chiradi;
- `.../api/index?appprobe=1` — Mini App panelini tekshiradi: `initData` imzosi to‘rt xil sinovdan o‘tkaziladi (to‘g‘ri, buzilgan, eskirgan, begona token), keyin ruxsat darvozasi (ega kiradi, begona kirmaydi) va panel ma’lumotlari tekshiriladi. Hammasi `stats.freeze()` ichida bo‘lgani uchun **bitta haqiqiy hisob ham siljimaydi**. `&menu=1` qo‘shilsa, ega va ruxsat olganlarning chat menyusidagi tugma qayta o‘rnatiladi;
- `.../api/index?bgprobe=1` — platforma javob yuborilgandan **keyin** ham ish bajarishiga ruxsat berishini o‘zi tekshiradi: o‘ziga bir necha sekundlik fon ishi buyuradi, ichki javob qancha tez qaytganini o‘lchaydi va ish bajarildimi — ko‘radi. `xulosa` maydoni `bg` rejimi ishlaydimi yoki `ZAKOVAT_TICK_MODE=sleep` qo‘yish kerakligini aytadi. Jonli taymer aynan shu mexanizmga tayanadi.

`scripts/set_webhook.py` — qo‘lda bajarish uchun yana bir zaxira usul.

So‘ngra `/start`, `/help`, `/packages`, `/solo` bilan sinab ko‘ring.

Barchasini bir komandada bajarish uchun (tokenlar faqat lokal shell muhitida):

```bash
VERCEL_TOKEN=... VERCEL_ORG_ID=... VERCEL_PROJECT_ID=... \
BOT_TOKEN=... WEBHOOK_SECRET=... bash scripts/deploy_all.sh
```

GitHub Actions orqali ham deploy qilish mumkin: repo Settings → Secrets and variables → Actions da `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` secretlarini qo‘ying, keyin **Deploy Zakovat Agent to Vercel** workflow’sini ishga tushuring (`workflow_dispatch` yoki `repository_dispatch` `deploy-zakovat`). `BOT_TOKEN` va `WEBHOOK_SECRET` ni Vercel dashboard’ida qo‘yish shart — ular repo secretlariga yozilmaydi.

**Jonli taymer serverless’da ham ishlaydi.** Vercel’da doimiy jarayon yo‘q, shuning uchun taymer qisqa «sakrashlar» zanjiridan iborat: har savoldan keyin funksiya o‘ziga `tick` so‘rovi yuboradi, u `ZAKOVAT_TICK_SECONDS` (standart 5) sekund kutib, hisoblagichni qayta chizadi va navbatagi `tick` ni chaqiradi. Telegram’ning «flood» tanbehi kelsa zanjir o‘chmaydi — navbatagi sakrash shunchaki kechroqqa qo‘yiladi.

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

## Guruh turniri

Oddiy o‘yindan farqi: **jamoalar**, **yakuniy javob** va **jadval**. Admin `/turnir 959` yozadi (paket raqami ixtiyoriy — ko‘rsatilmasa tasodifiy paket olinadi) va guruhda **pinlangan karta** paydo bo‘ladi. Karta turnirning butun holatini o‘z ichida saqlaydi (ko‘rinmas belgilar bilan), shuning uchun Vercel funksiyasi har so‘rovda qayta ishga tushsa ham hech narsa yo‘qolmaydi: jamoalar, ochkolar, tanlangan savollar va navbatdagi savol kartaning ichida yuradi.

**Qoidalar**

- kamida **3 jamoa**, har birida **3–7 kishi**; har jamoadan kamida **2 kishi** «Men tayyorman» tugmasini bosadi;
- har savolga har jamoa **bitta yakuniy javob** beradi: tasmaga reply qilib `/yakuniy <matn>`. Vaqt tugaguncha o‘zgartirish mumkin — oxirgisi hisoblanadi;
- to‘g‘ri javob — jamoaga **+1**; bir savolda bir nechta jamoa ochko olishi mumkin, javob bermagan jamoa ochko yo‘qotmaydi;
- javoblar guruhda ko‘rinmaydi: bot xabarni o‘chiradi (o‘chirish ruxsati bo‘lsa) va natijani faqat o‘z jamoasiga shaxsiy xabar bilan ko‘rsatadi;
- `/pichoq matn` — o‘z jamoangizga maxfiy gap; `/pichoq @a’zo matn` — bitta a’zoga. **Faqat o‘z jamoangiz** ko‘radi: ro‘yxatda yo‘q odamga yoki boshqa jamoaga yozib bo‘lmaydi (kurator bundan mustasno — u istalgan jamoaga yoza oladi). Bot guruhdagi xabarni **avval** o‘chiradi, keyin yetkazadi: o‘chirish huquqi bo‘lmasa, xabar hech kimga yuborilmaydi va nima uchunligi shaxsiy chatda aytiladi.

**Buyruqlar.** Telegram menyusida faqat o‘yinchi yozadigan 16 ta buyruq turadi — qolgani ishlaydi, lekin menyuni to‘ldirmaydi; hammasi `/help` da bo‘limlarga ajratilgan (`/help turnir`, `/help kurator`, `/help o‘yin`, `/help ega` — bittadan bo‘lim).

| Buyruq | Kim | Nima qiladi |
| --- | --- | --- |
| `/turnir [paket] [vaqt=… jamoa=… tur=… savol=… sekund=… sarlavha=…]` | admin/kurator | yangi turnir va pinlangan karta — hammasi bir qatorda; `vaqt=` va `sarlavha=` bir necha so‘z bo‘lishi mumkin |
| `/turnir qoidalar` | har kim | qoidalar |
| `/jamoa Nom` | har kim | yangi jamoa (yozgan odam — kapitan) |
| `/qoshil Nom` · `/chiqish` | har kim | jamoaga qo‘shilish · chiqish (kapitan chiqsa, o‘rni keyingi a’zoga o‘tadi) |
| `/tayyor` | a’zo | «Men tayyorman» |
| `/azolar` | har kim | o‘z jamoangiz a’zolari va id’lari (kuratorga — barcha jamoalar) |
| `/help turnir` | har kim | faqat turnir buyruqlari |
| `/yakuniy <matn>` · `/bekor` | a’zo | jamoaning yakuniy javobi · uni bekor qilish |
| `/jadval` | har kim | ochkolar va o‘rinlar |
| `/tanlov [so‘z]` · `/olish <id…>` · `/chiqarish <id…>` | kurator | aniq savollarni olish yoki chiqarish |
| `/kurator <id> [olib]` | admin | kurator tayinlash · tayinlovni olish |
| `/savollar [sahifa]` | har kim | turnir savollari ro‘yxati (javoblar yashirin) |
| `/savol <id>` | har kim | bitta savol: kuratorga javobi bilan, qolganlarga javob e’lon qilingach |
| `/turnirstop` | kurator | turnirni to‘xtatish va yakuniy jadval |

**Reja va eslatma.** `/turnir 959 vaqt=ertaga 19:00 jamoa=6 tur=3 savol=10 sekund=60 sarlavha=Kuzgi liga` — karta darhol guruhga chiqadi va mahkamlanadi, bot esa belgilangan daqiqada guruhga **eslatma** va tayyor **«Boshlash»** tugmasini yuboradi. O‘yin o‘zi boshlanmaydi: kamida 3 jamoa va har biridan 2 kishi «Men tayyorman» degach, kurator yoki admin tugmani bosadi. Vaqtni `19:00`, `bugun 23:59`, `ertaga 10:30`, `28.11 19:00` yoki `+45` (daqiqa) ko‘rinishida yozsa bo‘ladi; o‘tmish va 30 kundan uzoq vaqt rad etiladi. Reja statistika hujjatida saqlanadi (12 tagacha, eng yaqinlari), shuning uchun funksiya qayta ishga tushsa ham eslatma yo‘qolmaydi: guruhga kelgan **har qanday xabar** muddati yaqin rejalarni uyg‘otadi va bot boshlanishgacha qisqa qadamlar («hop») bilan kutadi. Eslatma bir marta yuboriladi — karta allaqachon o‘yinni ochgan bo‘lsa, zanjir jim tugaydi. Daqiqasi o‘tib ketgan reja ham e’lon qilinadi (kechikkan eslatma hech qanday eslatmasizlikdan yaxshi), lekin ikki soatdan ko‘p eskirgani tushirib yuboriladi — hujjatda 12 tadan ko‘p reja saqlanmaydi. Bekor qilish: paneldagi **«Bekor»** (faqat reja o‘chadi, karta qoladi) yoki `/turnirstop` (butun turnir).

**Kurator** — bot egasi, kartani ochgan admin yoki `/kurator` bilan tayinlangan a’zo. «Sozlamalar» panelida raund, savol soni va vaqt tanlanadi; «Savol tanlash» ro‘yxatida savol ustiga bosib uni OLISH yoki CHIQARISH mumkin. So‘ralgan savollar to‘plamdagi savollardan ko‘p bo‘lsa, panel ogohlantiradi: **bir savol ikki marta berilmaydi** — shuncha savol o‘ynaladi.

**Tugma so‘raydi, guruh javob beradi.** Matn kerak bo‘lgan uch joyda bot «buyruq yozing» deb turmaydi — tugma bosilgach savol shu guruhga qaytadi va **keyingi xabar** javob bo‘ladi: «Yangi jamoa» → jamoa nomi (bir bosishda `/jamoa Nom`), «Qidirish» → qidiruv so‘zi, «Paketni almashtirish» → paket raqami yoki nomidagi so‘z. Savol faqat bitta kishiga beriladi (so‘rov yuborgan kuratorning o‘zi), boshqa a’zoning xabari o‘yin uchun qoladi; istalgan boshqa tugma savolni bekor qiladi. Paket almashtirishda nom faqat qat’iy tekshiriladi (so‘z sarlavhada bo‘lishi shart), shuning uchun tasodifiy gap turnirni boshqa paketga ko‘chirmaydi; topilmasa bot aynan shu xabarni qaytaradi va savol ochiq qoladi — xato uchun yana tugma bosish shart emas. O‘yin boshlangach paket almashtirib bo‘lmaydi (`/turnirstop` kerak).

Savol yopilgach guruhda shu ko‘rinish chiqadi:

```
SAVOL 3 / 30 YAKUNI
• Ochko oldi: Bilimdonlar, Zakovat
• To‘g‘ri javob: Parnas
• Izoh: Parnas — Yunonistondagi tog‘ tizmasi…

YAKUNIY JAVOBLAR
• Bilimdonlar: Parnas — to‘g‘ri
• Intellekt: — — javob bermadi

HISOB: Bilimdonlar 7 · Zakovat 5 · Intellekt 2
```

**Statistika (faqat bot egasi).** `ZAKOVAT_OWNER_ID` (standart `8004724563`) dagi foydalanuvchi `/stats` yozsa, boshqa hech kim ko‘rmaydigan panel ochiladi:

```
STATISTIKA · Zakovat Agent
Manba: store.example · yozildi

O‘YINLAR
• Boshlangan: 12 · Yakunlangan: 9
• Avto-to‘xtagan: 2 · To‘xtatilgan: 1
• Foydalanuvchilar: 7 · Chatlar: 5
• O‘ynalgan paketlar: 6

SAVOLLAR
• Berilgan: 214
• To‘g‘ri: 96 · Noto‘g‘ri: 61 · Javobsiz: 57
• Aniqlik: 45%
• Jonli soat oynasi: 34 marta

ARXIV
• Turkumlar (paketlar): 1266 · Savollar: 30853 · Bo‘limlar: 13
• Rasmi bor savollar: 2912 · Eng katta paket: … (140 savol)

BO‘LIMLAR
• Intellektual marafon — 305 paket, 3489 savol
• Professional va Birinchi liga — 285 paket, 9883 savol
…
```

Panel ostidagi tugmalar: **Mini App — to‘liq panel** (ochiq manzil bo‘lsa), **Yangilash**, **Bo‘limlar** (barcha bo‘limlar), **O‘yinlar** (oxirgi harakatlar), **Ishlatilgan savollar** (qaysi savol qachon va qaysi paketda berilgan — 120 tagacha, sahifalanadi), **Turnir rejalari** (kelgusi o‘yinlar va qolgan vaqt) va **Ruxsatlar** (kim `/grant` olgan). `/stats ishlatilgan`, `/stats reja` va `/stats ruxsat` shu ko‘rinishlarni to‘g‘ridan-to‘g‘ri ochadi. `/stats` buyrug‘i faqat eganing buyruq menyusida ko‘rinadi; kengaytirilgan panel quyida, «Mini App paneli» bo‘limida.

Serverless’da baza yo‘q, shuning uchun raqamlar bitta kichik JSON hujjatda saqlanadi. Qaysi saqlagich ishlayotganini `stats.backend()` aytadi; ustunlik tartibi shunday:

1. **Telegram — standart, hech narsa sozlash kerak emas.** Bot hujjatni egasi bilan bo‘lgan shaxsiy chatdagi **pinlangan xabar**da saqlaydi: yozish `editMessageText`, o‘qish `getChat().pinned_message`. Xabar boshida odam o‘qiy oladigan bir qator izoh turadi («Zakovat Agent — statistika hujjati. Bu xabarni o‘chirmang…»), ostida JSON. Xabar o‘chirilsa yoki pin yechilsa, bot yangisini yuborib pinlaydi. Hujjat 4096 belgiga sig‘ishi uchun uzun ro‘yxatlar qisqartiriladi — hisoblagichlar hech qachon tushirib qoldirilmaydi. Boshqa chat kerak bo‘lsa: `ZAKOVAT_STATS_CHAT=<chat id>`.
2. **Vercel Blob.** Vercel → Storage → Create Database → Blob → loyihaga ulang: Vercel `BLOB_READ_WRITE_TOKEN` ni o‘zi qo‘shadi va bot saqlashni o‘zi topadi (`zakovat-stats.json`, manzil barqaror bo‘lishi uchun tasodifiy qo‘shimchasiz yoziladi).
3. **Istalgan JSON manzil.** `ZAKOVAT_STATS_URL` — `GET` ga JSON qaytaradigan, `PUT`/`POST` ni qabul qiladigan joy; `ZAKOVAT_STATS_TOKEN` berilsa `Authorization: Bearer` sifatida yuboriladi. `.../api/index?statsprobe=1&create=1` bepul xizmatlarni o‘zi sinab ko‘radi, ammo amalda ular serverless manzillarini rad etadi: jsonblob 403 (Cloudflare), kvdb 500 («Unable to write to value log file»), npoint 500, extendsclass 404, pantry captcha. Hisobotda har birining sababi ko‘rinadi.
4. **Hech narsa.** Saqlagich topilmasa hisob shu nusxa ichida yuradi va panel buni ochiq aytadi (`Manba: faqat shu nusxa`).

Yozuvlar `ZAKOVAT_STATS_FLUSH` (standart 20) sekundda bir to‘planib, **javob yuborilgandan keyin** yoziladi — o‘yin sekinlashmaydi; o‘yin boshlanishi, yakuni, avto-to‘xtash va to‘xtatish darhol yoziladi. Har qanday xato yutiladi: saqlash ishlamasa ham o‘yin davom etadi. Ikki nusxa bir vaqtda yozsa, bir nechta hisob yo‘qolishi mumkin — dashboard uchun bu arzimas. Arxiv bo‘limi (turkumlar, bo‘limlar, savollar, rasmlar) saqlashni talab qilmaydi — u manifestdan hisoblanadi.

Tekshirish: `.../api/index?statsprobe=1` — amaldagi saqlagichni hujjatni **buzmasdan** (mavjud hisoblarni saqlab) sinaydi, `rejim` ni ko‘rsatadi; `"ok": true` bo‘lsa saqlash ishlaydi. `.../api/index?statsprobe=1&tg=1` — Telegram saqlagichini majburan sinaydi: pinlangan xabarni o‘qiydi, yozadi, qayta o‘qiydi (xabar o‘chirilmaydi, chunki u saqlagichning o‘zi).

**Mini App paneli (ega va u ruxsat berganlar).** `/stats` panelining katta versiyasi Telegram ichida ochiladigan oynada: `https://<loyihangiz>.vercel.app/app`. Sahifa butunlay o‘zi bilan yetarli (bitta HTML, ichida CSS va JS, tashqi kutubxona yo‘q — faqat Telegram’ning o‘z SDK’si), ko‘rinishi Telegram’niki bilan bir xil: yorug‘ rejimda yumshoq oq fon (`#eff1f4`) va oq kartochkalar, qora rejimda **qop-qora emas** — chuqur ko‘mirrang (`#15191d`) fon va `#1d2329` kartochkalar. Mavzu `Telegram.WebApp.colorScheme` va `themeParams` dan olinadi, brauzerda ochilsa tizim mavzusiga qaraydi.

Olti bo‘lim: **Umumiy** (hisoblagichlar, aniqlik, odamlar, ko‘p o‘ynalgan paketlar, arxiv, tizim holati), **Savollar** (ishlatilgan savollar indeksi va butun baza bo‘ylab brauzer: bo‘limlar → paketlar → savollar, qidiruv va sahifalash bilan; har savolda **«Tanlovga»** tugmasi), **Turnir** (shakllantirish: guruh, paket, sarlavha, raund/savol/sekund/jamoa, sana va soat yoki tayyor tugmalar — «30 daqiqadan so‘ng», «Bugun 19:00», «Ertaga 10:00»; tanlangan savollar va rejadagi turnirlar ro‘yxati), **Bo‘limlar** (13 liga ulushi bilan), **O‘yinlar** (oxirgi harakatlar tasmasi), **Ruxsatlar** (faqat egada ko‘rinadi).

«Turnir» bo‘limidagi **«Turnirni guruhga chiqarish»** tugmasi `/api/tour` ga yozadi: bot guruhni tekshiradi (so‘rov yuborgan odam o‘sha guruhning admini yoki bot egasi bo‘lishi kerak), kartani yuborib mahkamlaydi va vaqt belgilangan bo‘lsa eslatmani reja qiladi. Guruh tanlanmagan, paket topilmagan, vaqt xato yoki huquq yetishmasa — javob `ok: false` va **o‘zbekcha sabab** bilan qaytadi, panel uni ko‘rsatadi. Brauzer va kompozitor `/api/browse` va `/api/tour` dan oladi, ikkalasi ham `no-store`.

Kirish ikki qavatdan iborat:

1. **Tugma faqat ruxsat olganlarning chatida.** Bot `setChatMenuButton` bilan eganing va `/grant` orqali qo‘shilgan har bir foydalanuvchining shaxsiy chat menyusiga «Statistika» tugmasini qo‘yadi; ruxsat olinganda menyu oddiy holatiga qaytadi. Tugmasi yo‘q odam oynani Telegram’dan ocholmaydi.
2. **Server har bir so‘rovda imzoni tekshiradi.** Manzilni kimdir ulashib yuborishi mumkin, shuning uchun sahifa `WebApp.initData` ni yuboradi va `bot/webapp.py` uni Bot API hujjatidagidek tekshiradi: `HMAC-SHA256`, kalit `hmac(b"WebAppData", BOT_TOKEN)`, imzolangan qator — `hash` dan boshqa barcha maydonlarning kalit bo‘yicha saralangan `key=value` ro‘yxati. Imzo, `auth_date` yoshi (`ZAKOVAT_APP_TTL`, standart bir kun) va `user.id` mos kelmasa — javob `ok: false`. Keyin `stats.allowed(id)` qaralaydi: ega yoki `/grant` olgan id bo‘lsa raqamlar beriladi, aks holda rad javobi va o‘z `id` si qaytadi (uni egaga yuborish uchun).

Ruxsat ro‘yxati statistik hujjatning `"a"` maydonida saqlanadi (chegara 60 ta id), shuning uchun server qayta ishga tushsa ham yo‘qolmaydi; ro‘yxat to‘lganda eng eski ruxsat chiqarib yuboriladi, yangisi tushib qolmaydi.

Ega buyruqlari (faqat eganing menyusida ko‘rinadi):

```
/access                     — kimlar ruxsat olganini ko‘rish
/grant 123456789 Ism        — ruxsat berish (ism berilmasa Telegram’dan so‘raladi)
/revoke 123456789           — ruxsatni olish va menyuni qaytarish
```

Boshqa hech kim bu buyruqlarni ishlatolmaydi; begona urinishlar `app_denied` yozuvi bilan qayd etiladi. Foydalanuvchi o‘z `id` sini bilishi oson: `/stats` yoki Mini App ga urinib ko‘rsa, rad javobida `id` si chiqadi va uni egaga yuboradi.

Lokal ko‘rish uchun (Telegram’siz, brauzerda):

```bash
pip install -r requirements.txt uvicorn
ZAKOVAT_APP_DEMO=1 python3 scripts/preview_app.py      # http://127.0.0.1:8080/app
```

Skript bir nechta sinov hisobini oldindan to‘ldiradi va sahifani eganing imzolangan `initData` si bilan ochadi. `ZAKOVAT_APP_DEMO` faqat `VERCEL_ENV` bo‘lmaganda ishlaydi — Vercel’da bu rejim hech qachon yoqilmaydi.

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
| `bot/messages.py` | barcha matnlar (emojisiz): tanishtiruv, yordam, qoidalar, hisoblagich, turnir, maxfiy xabar, statistika paneli |
| `bot/stats.py` | statistika: hisoblagichlar, tashqi JSON saqlash, arxiv bo‘limlari |
| `bot/tournament.py` | guruh turniri: jamoalar, ro‘yxatdan o‘tish, yakuniy javob, jadval (Telegramga bog‘liq emas) |
| `bot/hidden.py` | ko‘rinmas holat kodlaygichi: v2 (1 bit/belgi) va v3 (2 bit/belgi + zlib), buzilishni tekshirish |
| `bot/webapp.py` | Mini App paneli: `initData` imzosini tekshirish, ruxsat darvozasi, panel ma’lumotlari |
| `bot/dashboard.py` | Mini App sahifasining o‘zi (bitta HTML: Telegram ko‘rinishi, yorug‘/qora mavzu) |
| `bot/answers.py` | javob normalizatsiyasi, moslik va o‘rganish |
| `bot/webhook.py` | Vercel serverless webhook: handlerlar, jonli taymer (`tick`), diagnostika, self-test |
| `bot/selftest.py` | Telegram’ga chiqmasdan butun o‘yin oqimini sinash |
| `bot/app.py` | polling varianti (job queue bilan jonli hisoblagich va avto-to‘xtatish) |
| `api/index.py` | Vercel entrypoint |
| `public/index.html` | bot haqidagi sahifa (Mini App emas) |
| `data/images/` | rasmli savollar uchun zaxira tasvirlar |
| `run_bot.py` | polling kirish nuqtasi |
| `scripts/export_all.py` | butun bazani API’dan eksport qilish |
| `scripts/preview_app.py` | Mini App panelini brauzerda ko‘rish (Telegram’siz, sinov hisoblari bilan) |

## Sinov

```bash
python3 -m unittest discover -s tests -v
```

## Xavfsizlik va foydalanish

- Savollar faqat yopiq guruh / shaxsiy o‘yin uchun. Sayt materiallarining litsenziyasi aniq emas; ommaviy tarqatish tavsiya etilmaydi.
- Ilgari suhbatda paydo bo‘lgan Telegram bot tokeni, `vcp_...` va GitHub tokeni ishlatilmadi, faylga yozilmadi — ularni yangilash kerak.
- Ba’zi savollarda rasm bor: bot avval asl rasmni yuboradi, yuklanmasa `data/images/` dagi zaxira nusxani ishlatadi (generatsiya qilingan tasvirlar ilustrativ, asl nusxa emas).
