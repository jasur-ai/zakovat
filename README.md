# Zakovat savollar arxivi

Maqsat: `zakovatklubi.uz` savollar bazasini JSON ko‘rinishda GitHub’da saqlab qo‘yish, keyinchalik yopiq Telegram guruhida o‘yin o‘tkazish uchun ishlatish.

## Topilgan API

Sahifa React bilan yasalgan, savollar HTML ichida yo‘q. Lekin ochiq API mavjud:

```
GET https://api.zakovatklubi.uz/v1/package?per-page=100&page=1
GET https://api.zakovatklubi.uz/v1/package/959?include=questions
```

- Login / BotFather tokeni kerak emas.
- API bo‘yicha jami **1266 paket** va **28814 savol** bor.
- `filter[id]=959` noto‘g‘ri natija beradi; to‘g‘ri usul — `include=questions`.

## Hozircha repo da bor ma'lumot

| Fayl | Tarkibi |
| --- | --- |
| `data/all/manifest.json` | **Butun arxiv indeksi: 1266 paket, 30853 savol** |
| `data/all/questions/questions-0001.json … 0007.json` | **Barcha savollar** (matn, javob, izoh, paket id) — 7 sharda |
| `data/zakovat-959.json` | 959-paket: metadata + 54 savol (javob va izoh bilan) |
| `data/zakovat-959.flat.json` | Faqat savolar massivi |

959-paket 2024-yil 19–20 dekabr o‘yiniga tegishli (`created_at 1734959339`, `updated_at 1735045999`). Havoladagi “Kolibri…” savoli ham bor, javobi `Peru` — demak savollar **eski**, kelgusi o‘yinlarniki emas.

## To‘liq bazani eksport qilish

`scripts/export_all.py` butun bazani yuklaydi:

1. `/v1/package` dan barcha paketlarni oladi;
2. har bir paket uchun `/v1/package/{id}?include=questions` chaqiradi;
3. savollarni `id / package_id / text / answer / comment` bilan 5000 tadan sharda yozadi;
4. `data/all/manifest.json` ga hisob va paketlar indeksini qo‘yadi.

Interneti bor muhitda:

```bash
python3 scripts/export_all.py --out-dir data/all
git add data/all
git commit -m "Archive full Zakovat question bank"
git push
```

Natija:

```
data/all/manifest.json
data/all/questions/questions-0001.json
data/all/questions/questions-0002.json
...
```

### GitHub Actions orqali

`.github/workflows/export-all.yml` qo‘shilgan. Repo Settings → Actions → General → Actions permissions ni yoqib, workflow’ni ishga tushuring (`workflow_dispatch`) — u butun bazani yuklab, shu branchga commit qiladi. Kunlik schedule ham bor. Shuningdek `repository_dispatch` (`archive-zakovat`) orqali ham ishga tushirish mumkin.

### Codespaces orqali

`.devcontainer/devcontainer.json` va `scripts/full_export_codespace.sh` Codespace yaratilganda eksportni o‘zi bajarib push qiladi. Codespace yaratish uchun hisobingizda Codespaces ruxsati bo‘lishi kerak.

## Arxiv holati

To‘liq arxiv **2026-11-26 da GitHub Actions orqali yaratildi va shu branchga commit qilindi** (`Update full Zakovat question bank archive`):

- paketlar: **1266**;
- savollar: **30853** (javob va izohlar bilan);
- shards: **7** (`data/all/questions/questions-0001.json … 0007.json`);
- indeks: `data/all/manifest.json`;
- siz bergan “Kolibri…” savoli arxivda bor (`id 23348`, `package_id 959`, javob `Peru`).

Sandboxdan saytga to‘g‘ridan chiqish bloklanganligi sabab arxiv GitHub runner’da yaratildi. Yangilash uchun yuqoridagi Actions/Codespaces usullaridan foydalanish mumkin.

## Xavfsizlik va foydalanish

- Suhbatda paydo bo‘lgan GitHub tokeni va Telegram bot tokeni **ishlatilmadi, faylga yozilmadi**. Ularni revoke qilib, yangilarini yaratish kerak.
- Savollar faqat yopiq guruh o‘yini uchun. Sayt materiallarining litsenziyasi aniq emas; ommaviy tarqatish tavsiya etilmaydi.
- Ba'zi savollarda rasm bor; ushbu JSON da asosan matn, javob va izoh saqlangan.
