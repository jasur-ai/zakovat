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

## Nima uchun to‘liq baza hali yuklanmadi?

Bu ish muhitidan `api.zakovatklubi.uz` ga to‘g‘ridan tarmoq chiqishi bloklangan. Sahifalarni faqat o‘qish vositasi mavjud; u 28 ming savolni olib faylga yozishga imkon bermaydi. GitHub Actions va Codespaces ham ushbu avtorizatsiya bilan ishga tushmadi. Shu sabab hozircha faqat 959-paket arxivlangan. Yuqoridagi usullardan biri ishga tushganda to‘liq arxiv avtomatik yaratiladi.

## Xavfsizlik va foydalanish

- Suhbatda paydo bo‘lgan GitHub tokeni va Telegram bot tokeni **ishlatilmadi, faylga yozilmadi**. Ularni revoke qilib, yangilarini yaratish kerak.
- Savollar faqat yopiq guruh o‘yini uchun. Sayt materiallarining litsenziyasi aniq emas; ommaviy tarqatish tavsiya etilmaydi.
- Ba'zi savollarda rasm bor; ushbu JSON da asosan matn, javob va izoh saqlangan.
