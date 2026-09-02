# KeepGram

KeepGram — foydalanuvchining o‘z shaxsiy Telegram kanalini fayl ombori sifatida ishlatadigan bot. Xabarlar Telegram kanallariga nusxalanadi, Supabase PostgreSQL esa qidiruv va boshqaruv indeksini saqlaydi.

## Tayyor imkoniyatlar

- bir foydalanuvchi = bitta kanal, bir kanal = bitta foydalanuvchi;
- 15 daqiqalik bir martalik `LINK-XXXXXXXX` token bilan xavfsiz kanal ulash;
- document, photo, video, audio, voice, GIF, sticker, video-note, kontakt, lokatsiya va matn saqlash;
- bir martada yuborilgan 2–10 ta media/faylni bitta kodli to‘plam sifatida saqlash va qaytarish;
- `file_unique_id` orqali takroriy faylni aniqlash;
- JPG/PNG va boshqa rasmlar, PDF, Word, Excel hamda boshqa fayllarni avtomatik turkumlash;
- takrorlangan nomlarni avtomatik `Nomi (2)`, `Nomi (3)` ko‘rinishida noyob qilish;
- nom, kod, tur va teglar ko‘rinadigan ixcham “Barcha saqlanganlar” menyusi;
- fayl serverga yuklanmasdan Telegram ichida nusxalanishi;
- chalkash belgilar olib tashlangan 6 belgili kod;
- kod, nom, teg va katalog bo‘yicha owner-scoped qidiruv, `type:pdf` va `date:2026-09` kabi filtrlar;
- kataloglar, teglar, sevimlilar, oxirgi fayllar, nomni tahrirlash;
- ko‘p yozuvni belgilab umumiy teg/katalog berish yoki ommaviy o‘chirish;
- fayl tarkibini kod, nom va teglarni saqlagan holda almashtirish;
- fayl soni, umumiy hajm statistikasi va sozlanadigan limitlar;
- imzolangan avtomatik JSON tiklash manifesti va `/restore` orqali indeksni qayta tiklash;
- Redis orqali restartga chidamli FSM va media-albom navbati;
- foydalanish shartlariga rozilik bergan barcha foydalanuvchilar uchun alohida Telegram backup kanali, versiya va holat kuzatuvi;
- faqat indeksdan yoki kanal va indeksdan o‘chirish;
- majburiy ism va Telegram kontakt onboarding, JSON metadata eksporti va foydalanuvchi metadata hisobini o‘chirish;
- metadata-only responsive admin panel, bloklash/ochish, kanal uzish va audit jurnali;
- Telegram secret header bilan himoyalangan webhook, HttpOnly admin sessiyasi, CSRF, login rate-limit va xavfsizlik headerlari;
- GitHub commitidan Render auto-deploy va avtomatik Telegram webhook sozlash.
- 30 kunlik savat va bir bosishda qaytarish; kanal faylini o‘chirish alohida xavfli amal.
- Fayl kartasidan eslatma, oldingi backup versiyasini olish va 24 soatlik bir martalik ulashish havolasi.
- Qidiruvni nom bilan saqlash (`/saveview`) va keyin menyudan qayta ishlatish.
- Webhook update-id idempotentligi, tez user-counterlar va adaptiv fon workerlar.
- Admin jadvallarida server-side qidiruv hamda 10/15/50 talik pagination.
- To‘liq uch tilli bot: O‘zbekcha, English va Русский. Birinchi kirishda til → ism → telefon → shartlarga rozilik tartibi; tilni keyin Sozlamalardan almashtirish mumkin.

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

Admin parolini Render Environment bo‘limida oddiy ko‘rinishda kiriting. Masalan:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=1111
```

`1111` ishlaydi, lekin haqiqiy bot uchun uzun va taxmin qilish qiyin parol tanlash tavsiya etiladi. Parolni `.env` fayli bilan GitHub’ga yuklamang.

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
| `ADMIN_PASSWORD` | Admin panelga kirish uchun oddiy parol (masalan, `1111`) |
| `TRASH_RETENTION_DAYS` | Savatdagi yozuv muddati; standart `30` |
| `ADMIN_TELEGRAM_IDS` | Telegram ichida `/admin` havolasini olishga ruxsat berilgan ID’lar, masalan `123456789` |
| `MAX_FILES_PER_USER` | Bitta egaga ruxsat etilgan maksimal fayl qismlari, standart `5000` |
| `MAX_TOTAL_SIZE_MB` | Metadata asosida umumiy hajm limiti, standart `51200` MB |

`WEBHOOK_SECRET` va `SESSION_SECRET` Blueprint tomonidan avtomatik yaratiladi. `keepgram-queue` Render Key Value ham yaratiladi va uning ichki `REDIS_URL` manzili web-servisga avtomatik ulanadi. Agar servisni Blueprint’siz qo‘lda yaratsangiz, ushbu qiymatlarni o‘zingiz kiriting.

5. Deploy tugagach `https://SIZNING-SERVIS.onrender.com/health` manzilida `status: ok`, `database: true` va `schema: true` ko‘rinishi kerak.
6. Botga `/start` yuboring. Webhook ilova ishga tushganida avtomatik o‘rnatiladi.
7. Admin panel: `https://SIZNING-SERVIS.onrender.com/admin`.

Admin panelga faqat aynan `/admin` manzili orqali kiring. `/admin/...` ko‘rinishidagi noma’lum yo‘llar yopiq. Login — `ADMIN_USERNAME`, parol — Render Environment’dagi `ADMIN_PASSWORD` qiymati. Login doim 401 qaytarsa, Render’dagi ikkala qiymatni tekshiring, saqlang va servisni qayta deploy qiling.

Admin paneldagi foydalanuvchilar, kanallar, fayllar, backup va audit sahifalari ma’lumotni bo‘lib yuklaydi. Har sahifada 10, 15 yoki 50 ta yozuv tanlanadi; qidiruv serverda bajariladi, shuning uchun brauzerga barcha baza birdan yuborilmaydi. Foydalanuvchi ichidagi fayllar oynasi ham alohida pagination va qidiruvga ega.

## Savat, eslatmalar va ulashish

- Oddiy `O‘chirish → Savatga` amali fayl metadata yozuvini 30 kun saqlaydi va Telegram storage kanalidagi asl xabarga tegmaydi. `/trash` yoki `🗑 Savat` orqali qaytariladi.
- `Kanal fayli + savat` amali storage nusxani ham o‘chiradi; metadata qaytarilsa ham asl fayl faqat backup versiyasidan olinishi mumkin.
- Fayl kartasidagi `⏰ Eslatma` Toshkent vaqti bo‘yicha sana qabul qiladi. `/reminders` faol eslatmalarni ko‘rsatadi.
- `🔗 Ulashish` 24 soatlik va bir martalik deep-link yaratadi. Link ishlatilganda fayl Telegram orqali yuboriladi; server fayl baytlarini saqlamaydi.
- `/saveview PDF hujjatlar | type:pdf #muhim` qidiruvni saqlaydi; `/views` yoki `🧠 Saqlangan qidiruvlar` orqali ishga tushiriladi.

## Til va foydalanish shartlari

Birinchi `/start` da foydalanuvchi `🇺🇿 O‘zbekcha`, `🇬🇧 English` yoki `🇷🇺 Русский` tilini tanlaydi. Tanlov `users.preferred_language` maydonida saqlanadi va menyu, inline tugmalar, ogohlantirishlar, fayl kartalari, qidiruv, savat, eslatma hamda Telegram command tavsiflariga qo‘llanadi. Tilni `⚙️ Sozlamalar → 🌐 Til / Language` orqali istalgan payt almashtirish mumkin.

Foydalanish shartlari 2.0 versiyada soddalashtirilgan va uch tilda alohida yozilgan. Unda hisob ma’lumotlari, shaxsiy storage kanal, administrator boshqaradigan avariya backupi, o‘chirish/tiklash va xavfsizlik aniq tushuntiriladi. Versiya o‘zgargani uchun avvalgi foydalanuvchilardan yangi matnga qayta rozilik olinadi.

## Rozilikka asoslangan umumiy backup kanalini yoqish

Yangi foydalanuvchi ism va telefonini tasdiqlagach, KeepGram foydalanish shartlarini ko‘rsatadi. Shartlarda qo‘shimcha administrator backup kanali, u yerda saqlanadigan metadata va tiklash imkoniyati aniq tushuntiriladi. `✅ Roziman va davom etaman` bosilmaguncha bot funksiyalari ochilmaydi; rozilik sanasi va shartlar versiyasi bazada saqlanadi.

1. Alohida private Telegram kanal yarating va botni **Post Messages**, **Edit Messages** hamda **Delete Messages** huquqlari bilan admin qiling.
2. Kanal ID sini oling (`-100...` ko‘rinishida).
3. `/admin` → **Backup mirror** sahifasida kanal ID sini kiriting va **Faol** ni yoqing.
4. KeepGram amaldagi shartlarga rozilik bergan foydalanuvchilarning avvalgi indekslangan fayllarini navbatga oladi; yangi fayllar avtomatik nusxalanadi.

Backup kanalidagi har bir yozuvda egasi, asl kanal, sana, kod, fayl turlari, versiya va `active/deleted/replaced/missing/failed` holati bor. Asl storage’dan yoki bot orqali o‘chirish backup nusxani o‘chirmaydi. Admin panelda nom, kod, Telegram ID va status bilan filtrlash hamda tanlangan backupni Telegram chatga yuborish mumkin.

Keyingi GitHub commitlari Render’da avtomatik deploy bo‘ladi.

## Kanal ulash oqimi

1. Foydalanuvchi `/start` → **Kanalni ulash** tugmasini bosadi.
2. Bot bir martalik `LINK-XXXXXXXX` token beradi.
3. Foydalanuvchi botni shaxsiy kanalga admin qiladi va tokenni kanalda yuboradi.
4. KeepGram botning kanal adminligini va token muddatini tekshiradi.
5. Database’dagi ikkita `UNIQUE` cheklov bir userga ikki kanal yoki bir kanalga ikki user ulanishini to‘xtatadi.

Token usuli forward metadata cheklovlariga bog‘liq emas. Tokenni bilgan odam kanalga yoza olishi va botni admin qila olishi kerak; token 15 daqiqada eskiradi va muvaffaqiyatli ulanishdan keyin darhol o‘chadi.

## Majburiy ro‘yxatdan o‘tish

Yangi foydalanuvchi `/start` yuborganda KeepGram avval foydalanuvchi kiritgan ismni, keyin Telegram `request_contact` tugmasi orqali aynan o‘z telefon raqamini oladi. Begona kontakt va qo‘lda yozilgan telefon qabul qilinmaydi. Telefon tasdiqlangach foydalanish va umumiy backup shartlari ko‘rsatiladi. Foydalanuvchi `✅ Roziman va davom etaman` tugmasini bosmaguncha kanal, fayl, qidiruv va sozlamalar funksiyalari ochilmaydi.

## Xavfsizlik va maxfiylik

- Har bir file query `telegram_id/user_id` bilan owner-scoped; callback ichidagi user ID’ga ishonilmaydi.
- Admin panel real fayl, Telegram captioni yoki private message matnini ko‘rsatmaydi.
- Backup faqat amaldagi foydalanish shartlariga aniq rozilik bergan foydalanuvchilarga ishlaydi; rozilik bermagan hisob botdan foydalana olmaydi va uning fayli backupga olinmaydi.
- Bot `getFile` chaqirmaydi, media bytes o‘qimaydi, OCR/AI/antivirus tahlili qilmaydi.
- Saqlash tartibi: DB tekshiruvi → Telegram copy → DB indeks. DB insert yiqilsa, nusxalangan orphan xabarni o‘chirishga urinish qilinadi.
- Kanalda bot huquqi yo‘qolsa storage `inactive` bo‘ladi; foydalanuvchi qayta ulaydi.
- `schema.sql` anon/authenticated rollardan jadvallarni yopadi. Admin UI faqat FastAPI orqali ishlaydi.
- Admin session cookie production’da `Secure`, `HttpOnly`, `SameSite=Lax`; mutatsiyalar CSRF header bilan himoyalangan.
- Admin sessiyasi imzolangan cookie va brauzer fingerprintiga bog‘langan; noma’lum `/admin/...` yo‘llari yopiq.
- Loglarga tokenlar, fayl kontenti va foydalanuvchi xabar matni yozilmaydi.

Texnik haqiqat: bot tokeniga ega operator bot admin bo‘lgan kanallarda Telegram API orqali amal bajarish imkoniga ega bo‘lishi mumkin. Shu sabab bot tokeni va admin panel login ma’lumotlari qat’iy himoyalanishi kerak.

## Muhim operatsion eslatmalar

- Supabase bazasi indeksdir. Avto-manifest har o‘zgarishdan keyin storage kanalga imzolangan tiklash faylini joylaydi; `SESSION_SECRET` ni almashtirmang, aks holda eski manifest imzosi tekshiruvdan o‘tmaydi. Supabase backupni ham yoqing.
- Foydalanuvchi faylni kanaldan qo‘lda o‘chirsa, keyingi olishda KeepGram indeksni `missing` deb belgilaydi.
- Kanal almashtirilganda eski kanal fayllari qoladi, eski kanalga tegishli indeks tozalanadi.
- Redis mavjud bo‘lsa FSM va media-albom navbati restartdan keyin ham davom etadi; bo‘lmasa xavfsiz memory fallback ishlaydi. Render free Key Value diskka doimiy yozmaydi, shu sabab eng kuchli kafolat uchun pullik persistence rejimi kerak.
- Web-servis bitta worker bilan ishlaydi, PostgreSQL pool 5 ulanish bilan cheklangan, backup/manifest fon ishlari kichik paketlarda bajariladi. Bu 512 MB Render servisida keskin yuklanishni kamaytiradi.
- `/health` endpoint Render health-check uchun, `/ping` esa tashqi uptime tekshiruvi uchun tayyor.

## Asosiy buyruqlar

`/start`, `/menu`, `/search`, `/recent`, `/all`, `/stats`, `/catalogs`, `/tags`, `/settings`, `/channel`, `/disconnect`, `/backup`, `/restore`, `/mydata`, `/delete_my_data`, `/privacy`, `/help`, `/cancel`, `/admin`.

## Litsenziya va foydalanish

Deploy qilishdan oldin foydalanuvchilarga maxfiylik siyosati va foydalanish shartlarini ochiq ko‘rsating. Foydalanuvchi o‘z kanali, Telegram hisobi va saqlayotgan kontentining qonuniyligi uchun javobgar; kanal yoki database o‘chirilsa tiklash kafolatlanmaydi.
