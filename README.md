# Zakovat savollar bazasini JSON ga olish

**Xulosa: ha, olish mumkin.** `zakovatklubi.uz` sahifasi React bilan yasalgan bo‘lib, savollar dastlab HTML ichida ko‘rinmaydi. Lekin sahifa ochiq (login talab qilmaydigan) API ga murojaat qiladi. Savollar, javoblar va izohlar (`comment`) shu API orqali to‘liq keladi.

## Topilgan API

```
GET https://api.zakovatklubi.uz/v1/package/959?include=questions
```

- `959` — berilgan havoladagi paket id (`/quiz/5/10/959/0/0`).
- `include=questions` paket ichidagi savollarni ham qaytaradi.
- `filter[id]=959` bilan `/v1/package/questions` noto‘g‘ri natija beradi (boshqa savol), shu sabab `include=questions` ishlatiladi.
- Hech qanday token / BotFather kaliti kerak emas.

`GET https://api.zakovatklubi.uz/v1/package` orqali barcha paketlar ro‘yxati ham olinadi (`totalCount: 1266`). Shu yo‘l bilan boshqa eski paketlarni ham eksport qilish mumkin.

## Tayyor JSON fayllar

| Fayl | Tarkibi |
| --- | --- |
| `data/zakovat-959.json` | Metadata + 54 ta savol (javob va izoh bilan) |
| `data/zakovat-959.flat.json` | Faqat savolar massivi — Telegram botga yuklash uchun qulay |

Har bir savol:

```json
{
  "id": 22453,
  "round": 0,
  "number": 1,
  "text": "1-o‘rinni aniqlash uchun 5-qo‘shimcha savol. Kolibri – ...",
  "answer": "Peru",
  "comment": "Yuqoridagilar Naska platosidagi turli geogliflarning ..."
}
```

`round: 0` — asosiy 5 tur oldidan beriladigan qo‘shimcha savollar.

## Qayta eksport qilish

```bash
python3 scripts/export_zakovat.py \
  --url https://zakovatklubi.uz/quiz/5/10/959/0/0 \
  --out data/zakovat-959.json \
  --flat-out data/zakovat-959.flat.json
```

Skript URL dan `rounds / questions_per_round / package_id` ni ajratib oladi, API ga so‘rov yuboradi, HTML teglarni tozalaydi va JSON yozadi. Tarmoq cheklangan muhitda (masalan, sandbox) to‘g‘ridan API ga chiqish bloklanishi mumkin — shuning uchun tayyor JSON fayllar qo‘shib qo‘yildi.

## Savollar eski ekanligini tekshirish

- Paket sarlavhasi: `Intellektual Olimpiada doirasidagi “Zakovat” o‘yini savollari (19.12.2024–20.12.2024)`.
- API dagi `created_at = 1734959339`, `updated_at = 1735045999` (dekabr 2024).
- Havoladagi “Kolibri – 50 metr ...” savoli paket ichida topildi, javobi `Peru`, izohida Naska geogliflari tilga olinadi.
- Demak, bu **o‘tgan** 2024-yil 19–20 dekabr o‘yini savollari; kelgusi o‘yinlar savollari emas.

## Xavfsizlik va foydalanish

- Siz yuborgan Telegram bot tokeni va `vcp_...` tokeni fayllarga yozilmadi, commit qilinmadi. Ishlab chiqarishda ularni **almashtirish (rotate) kerak**, chunki ular suhbatda ochiq paydo bo‘lgan.
- Savollar faqat siz aytgan maqsadda — yopiq Telegram guruhida tartibli o‘yin o‘tkazish uchun ishlatilsin. Sayt materiallarining litsenziyasi aniq emas; ommaviy tarqatish / qayta nashr qilish tavsiya etilmaydi.
- Ba'zi savollarda rasm bor (API da `attachments` maydoni). Ushbu JSON da rasm URL lari kiritilmadi, faqat matn, javob va izoh saqlandi.
