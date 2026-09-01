# KeepGram

KeepGram — foydalanuvchining o‘z shaxsiy Telegram kanalini fayl ombori sifatida ishlatadigan bot. Bot fayl baytlarini Render serveriga yuklamaydi: xabarni Telegram ichida `copyMessage` bilan kanalga nusxalaydi va Supabase PostgreSQL’da faqat kichik indeks metadata saqlaydi.

## Tayyor imkoniyatlar

- bir foydalanuvchi = bitta kanal, bir kanal = bitta foydalanuvchi;
- 15 daqiqalik bir martalik `LINK-XXXXXXXX` token bilan xavfsiz kanal ulash;
- document, photo, video, audio, voice, GIF, sticker, video-note, kontakt, lokatsiya va matn saqlash;
- fayl serverga yuklanmasdan Telegram ichida nusxalanishi;
- chalkash belgilar olib tashlangan 6 belgili kod;
- kod, nom, teg va katalog bo‘yicha owner-scoped qidiruv;
- kataloglar, teglar, sevimlilar, oxirgi fayllar, nomni tahrirlash;
- faqat indeksdan yoki kanal va indeksdan o‘chirish;
- ixtiyoriy telefon ulash, JSON metadata eksporti va foydalanuvchi metadata hisobini o‘chirish;
- metadata-only responsive admin panel, bloklash/ochish, kanal uzish va audit jurnali;
- Telegram secret header bilan himoyalangan webhook, HttpOnly admin sessiyasi, CSRF, login rate-limit va xavfsizlik headerlari;
- GitHub commitidan Render auto-deploy va avtomatik Telegram webhook sozlash.

## Ixcham tuzilma

```text
KeepGram/
├─ main.py          # bot + database qatlam + webhook + admin API
├─ admin.html       # butun responsive admin panel (HTML/CSS/JS)
├─ schema.sql       # Supabase sxemasi, indekslar va RLS
├─ assets/logo.png  # loyiha logosi
├─ tests.py         # asosiy xavfsiz utilitalar testi
├─ requirements.txt
├─ render.yaml
└─ .env.example
```

## 1. Telegram bot yaratish

1. Telegram’da `@BotFather` bilan `/newbot` yuboring.
2. Bot nomini **KeepGram** deb belgilang va tokenni oling.
3. Ixtiyoriy: `/setuserpic` orqali `assets/logo.png` faylini bot rasmi qiling.
4. Tokenni hech qachon GitHub’ga yozmang.

Storage kanalni ulashda botga kamida **Post Messages** huquqi kerak. “Kanal + indeksdan o‘chirish” ishlashi uchun **Delete Messages** huquqini ham bering.

## 2. Supabase tayyorlash

1. Supabase’da yangi project yarating.
2. KeepGram birinchi ishga tushishda [schema.sql](schema.sql) sxemasini avtomatik yaratadi. Agar hosting DB roli DDL yaratishga ruxsat bermasa, SQL Editor’da fayl ichidagi SQL’ni qo‘lda to‘liq ishga tushiring.
3. Project Settings → Database → Connection string → URI’dan server ulanish satrini oling.
4. Render uchun Transaction pooler (odatda `:6543`) URI qulay. `[YOUR-PASSWORD]` qismini haqiqiy DB paroliga almashtiring. Paroldagi maxsus belgilar URL-encode qilinishi kerak.

KeepGram Supabase browser SDK’dan foydalanmaydi. `DATABASE_URL` faqat server environmentida turadi; service-role yoki anon key brauzerga berilmaydi.

## 3. Maxfiy qiymatlarni yaratish

Avval `bcrypt` o‘rnating:

```powershell
python -m pip install bcrypt
```

Admin parolini terminal tarixiga yozmasdan bcrypt hash yarating:

```powershell
python -c "import bcrypt,getpass; print(bcrypt.hashpw(getpass.getpass('Admin parol: ').encode(), bcrypt.gensalt(12)).decode())"
```

Webhook va session kalitlarini yarating:

```powershell
python -c "import secrets; print('WEBHOOK_SECRET='+secrets.token_urlsafe(32)); print('SESSION_SECRET='+secrets.token_urlsafe(48))"
```

Lokal ishlash uchun `.env.example` nusxasini `.env` deb saqlab, barcha qiymatlarni kiriting. `WEBHOOK_SECRET` ichida faqat harf, raqam, `_` va `-` ishlatiladi.

## 4. Lokal tekshirish

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python tests.py
uvicorn main:app --reload
```

Telegram webhook lokal `localhost`’ga kela olmaydi. Lokal end-to-end sinov uchun HTTPS tunnel manzilini `APP_BASE_URL` sifatida yozing; production’da Render HTTPS manzili ishlatiladi.

## 5. GitHub va Render orqali ishga tushirish

1. Papkada Git repository yarating va fayllarni GitHub repository’ga push qiling.
2. Render Dashboard → **New +** → **Blueprint** ni tanlab, GitHub repository’ni ulang.
3. Render `render.yaml` ni o‘qib `keepgram` web service yaratadi.
4. Quyidagi secret qiymatlarni Render’da kiriting:

| Environment variable | Qiymat |
|---|---|
| `BOT_TOKEN` | BotFather tokeni |
| `DATABASE_URL` | Supabase PostgreSQL URI |
| `APP_BASE_URL` | Render bergan to‘liq HTTPS URL, masalan `https://keepgram-abcd.onrender.com` |
| `ADMIN_PASSWORD_HASH` | Yuqorida yaratilgan bcrypt hash |
| `ADMIN_TELEGRAM_IDS` | Ixtiyoriy, `/admin` buyrug‘i uchun IDlar: `123,456` |

`WEBHOOK_SECRET` va `SESSION_SECRET` Blueprint tomonidan avtomatik yaratiladi. Agar servisni Blueprint’siz qo‘lda yaratsangiz, ularni ham o‘zingiz kiriting.

5. Deploy tugagach `https://SIZNING-SERVIS.onrender.com/health` manzilida `status: ok`, `database: true` va `schema: true` ko‘rinishi kerak.
6. Botga `/start` yuboring. Webhook ilova ishga tushganida avtomatik o‘rnatiladi.
7. Admin panel: `https://SIZNING-SERVIS.onrender.com/admin`.

Keyingi GitHub commitlari Render’da avtomatik deploy bo‘ladi.

## Kanal ulash oqimi

1. Foydalanuvchi `/start` → **Kanalni ulash** tugmasini bosadi.
2. Bot bir martalik `LINK-XXXXXXXX` token beradi.
3. Foydalanuvchi botni shaxsiy kanalga admin qiladi va tokenni kanalda yuboradi.
4. KeepGram botning kanal adminligini va token muddatini tekshiradi.
5. Database’dagi ikkita `UNIQUE` cheklov bir userga ikki kanal yoki bir kanalga ikki user ulanishini to‘xtatadi.

Token usuli forward metadata cheklovlariga bog‘liq emas. Tokenni bilgan odam kanalga yoza olishi va botni admin qila olishi kerak; token 15 daqiqada eskiradi va muvaffaqiyatli ulanishdan keyin darhol o‘chadi.

## Xavfsizlik va maxfiylik

- Har bir file query `telegram_id/user_id` bilan owner-scoped; callback ichidagi user ID’ga ishonilmaydi.
- Admin panel real fayl, Telegram captioni yoki private message matnini ko‘rsatmaydi.
- Bot `getFile` chaqirmaydi, media bytes o‘qimaydi, OCR/AI/antivirus tahlili qilmaydi.
- Saqlash tartibi: DB tekshiruvi → Telegram copy → DB indeks. DB insert yiqilsa, nusxalangan orphan xabarni o‘chirishga urinish qilinadi.
- Kanalda bot huquqi yo‘qolsa storage `inactive` bo‘ladi; foydalanuvchi qayta ulaydi.
- `schema.sql` anon/authenticated rollardan jadvallarni yopadi. Admin UI faqat FastAPI orqali ishlaydi.
- Admin session cookie production’da `Secure`, `HttpOnly`, `SameSite=Lax`; mutatsiyalar CSRF header bilan himoyalangan.
- Loglarga tokenlar, fayl kontenti va foydalanuvchi xabar matni yozilmaydi.

Texnik haqiqat: bot tokeniga ega operator bot admin bo‘lgan kanallarda Telegram API orqali amal bajarish imkoniga ega bo‘lishi mumkin. Shu sabab bot tokeni qat’iy himoyalanishi, admin panel esa metadata-only bo‘lib qolishi kerak.

## Muhim operatsion eslatmalar

- Supabase bazasi indeksdir. Uni yo‘qotsangiz, Telegram kanaldagi fayllar qoladi, lekin KeepGram eski kod va qidiruv bilan ularni topolmaydi. Database backup yoqing.
- Foydalanuvchi faylni kanaldan qo‘lda o‘chirsa, keyingi olishda KeepGram indeksni `missing` deb belgilaydi.
- Kanal almashtirilganda eski kanal fayllari qoladi, eski kanalga tegishli indeks tozalanadi.
- Memory FSM faqat juda qisqa rename/tag/qidiruv dialoglari uchun ishlatiladi; media kelishi bilan darhol saqlanadi.
- `/health` endpoint Render health-check uchun, `/ping` esa tashqi uptime tekshiruvi uchun tayyor.

## Asosiy buyruqlar

`/start`, `/menu`, `/search`, `/recent`, `/catalogs`, `/tags`, `/settings`, `/channel`, `/disconnect`, `/mydata`, `/delete_my_data`, `/privacy`, `/help`, `/cancel`, `/admin`.

## Litsenziya va foydalanish

Deploy qilishdan oldin foydalanuvchilarga maxfiylik siyosati va foydalanish shartlarini ochiq ko‘rsating. Foydalanuvchi o‘z kanali, Telegram hisobi va saqlayotgan kontentining qonuniyligi uchun javobgar; kanal yoki database o‘chirilsa tiklash kafolatlanmaydi.
