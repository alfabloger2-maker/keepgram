from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "KeepGram"
APP_VERSION = "2.0.0"
TERMS_VERSION = "2.0"
BASE_DIR = Path(__file__).resolve().parent
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{6}$", re.IGNORECASE)
LINK_RE = re.compile(r"^LINK-[A-HJ-NP-Z2-9]{8}$", re.IGNORECASE)
SUPPORTED_CONTENT = {
    "document",
    "photo",
    "video",
    "audio",
    "voice",
    "animation",
    "sticker",
    "video_note",
    "contact",
    "location",
    "venue",
    "text",
}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic", "heif", "bmp", "tif", "tiff"}
WORD_EXTENSIONS = {"doc", "docx", "odt", "rtf"}
EXCEL_EXTENSIONS = {"xls", "xlsx", "xlsm", "csv", "ods"}

SUPPORTED_LANGUAGES = {"uz", "en", "ru"}
current_language: ContextVar[str] = ContextVar("keepgram_language", default="uz")

# Source strings stay Uzbek in handlers; this central catalogue localizes every
# outgoing bot text and keyboard without scattering language conditionals.
UI_PHRASES: list[tuple[str, str, str]] = [
    ("📥 Saqlash", "📥 Save", "📥 Сохранить"),
    ("🔎 Qidirish", "🔎 Search", "🔎 Поиск"),
    ("📚 Barcha saqlanganlar", "📚 All saved items", "📚 Все сохранённые"),
    ("🗂 Kataloglar", "🗂 Folders", "🗂 Папки"),
    ("🏷 Teglar", "🏷 Tags", "🏷 Теги"),
    ("🧠 Saqlangan qidiruvlar", "🧠 Saved searches", "🧠 Сохранённые поиски"),
    ("🗑 Savat", "🗑 Trash", "🗑 Корзина"),
    ("🔢 Kod bo‘yicha", "🔢 By code", "🔢 По коду"),
    ("🕘 Oxirgilari", "🕘 Recent", "🕘 Недавние"),
    ("⭐ Sevimlilar", "⭐ Favorites", "⭐ Избранное"),
    ("📊 Statistika", "📊 Statistics", "📊 Статистика"),
    ("⚙️ Sozlamalar", "⚙️ Settings", "⚙️ Настройки"),
    ("ℹ️ Yordam", "ℹ️ Help", "ℹ️ Помощь"),
    ("📱 Telefon raqamimni yuborish", "📱 Share my phone number", "📱 Отправить мой номер"),
    ("📱 Telefon raqamimni ulashish", "📱 Share my phone number", "📱 Отправить мой номер"),
    ("✅ Roziman va davom etaman", "✅ I agree and want to continue", "✅ Я согласен и хочу продолжить"),
    ("📤 Olish", "📤 Get", "📤 Получить"),
    ("♻️ Almashtirish", "♻️ Replace", "♻️ Заменить"),
    ("✏️ Nom", "✏️ Rename", "✏️ Название"),
    ("🗂 Katalog", "🗂 Folder", "🗂 Папка"),
    ("🕘 Versiyalar", "🕘 Versions", "🕘 Версии"),
    ("⏰ Eslatma", "⏰ Reminder", "⏰ Напоминание"),
    ("🔗 Ulashish", "🔗 Share", "🔗 Поделиться"),
    ("⭐ Sevimli", "⭐ Favorite", "⭐ В избранное"),
    ("☆ Sevimlidan", "☆ Remove favorite", "☆ Убрать из избранного"),
    ("🗑 O‘chirish", "🗑 Delete", "🗑 Удалить"),
    ("🔗 Kanalni ulash", "🔗 Connect channel", "🔗 Подключить канал"),
    ("ℹ️ Qanday ishlaydi?", "ℹ️ How does it work?", "ℹ️ Как это работает?"),
    ("🔐 Maxfiylik", "🔐 Privacy", "🔐 Конфиденциальность"),
    ("🔄 Almashtirish", "🔄 Replace", "🔄 Заменить"),
    ("🔌 Uzish", "🔌 Disconnect", "🔌 Отключить"),
    ("❌ Bekor", "❌ Cancel", "❌ Отмена"),
    ("✅ Ha, uzilsin", "✅ Yes, disconnect", "✅ Да, отключить"),
    ("❌ Yo‘q", "❌ No", "❌ Нет"),
    ("⬅️ Oldingi", "⬅️ Previous", "⬅️ Назад"),
    ("Keyingi ➡️", "Next ➡️", "Далее ➡️"),
    ("☑️ Bir nechtasini tanlash", "☑️ Select multiple", "☑️ Выбрать несколько"),
    ("🏷 Teg qo‘shish", "🏷 Add tags", "🏷 Добавить теги"),
    ("✅ Tugatish", "✅ Done", "✅ Готово"),
    ("🗑 Ommaviy o‘chirish", "🗑 Bulk delete", "🗑 Массовое удаление"),
    ("📂 Avvalgi yozuvni ochish", "📂 Open existing item", "📂 Открыть существующую запись"),
    ("↩️ Darhol qaytarish", "↩️ Undo now", "↩️ Восстановить сейчас"),
    ("🗑 Savatga (qaytarish mumkin)", "🗑 Move to trash (undo available)", "🗑 В корзину (можно восстановить)"),
    ("⚠️ Kanal fayli + savat", "⚠️ Channel file + trash", "⚠️ Файл канала + корзина"),
    ("➕ Yangi katalog", "➕ New folder", "➕ Новая папка"),
    ("🗑 Ma’lumotlarni o‘chirish", "🗑 Delete my data", "🗑 Удалить мои данные"),
    ("🔗 Kanal", "🔗 Channel", "🔗 Канал"),
    ("📱 Telefon", "📱 Phone", "📱 Телефон"),
    ("🛟 Avto-manifest", "🛟 Auto manifest", "🛟 Автоманифест"),
    ("⭐ Avto-sevimli", "⭐ Auto favorite", "⭐ Автоизбранное"),
    ("🪪 Ixcham kartalar", "🪪 Compact cards", "🪪 Компактные карточки"),
    ("🗂 Standart katalog", "🗂 Default folder", "🗂 Папка по умолчанию"),
    ("#️⃣ Kanal indeksi", "#️⃣ Channel index", "#️⃣ Индекс канала"),
    ("✅ faol", "✅ active", "✅ активно"),
    ("⚠️ aloqa yo‘q", "⚠️ unavailable", "⚠️ нет связи"),
    ("Yuborilmoqda…", "Sending…", "Отправка…"),
    ("Topilmadi", "Not found", "Не найдено"),
    ("Fayl topilmadi.", "File not found.", "Файл не найден."),
    ("Natija yo‘q", "No results", "Нет результатов"),
    ("O‘chirildi", "Deleted", "Удалено"),
    ("O‘chirib bo‘lmadi", "Could not delete", "Не удалось удалить"),
    ("Katalog yo‘q", "No folders", "Нет папок"),
    ("Savatga ko‘chirildi", "Moved to trash", "Перемещено в корзину"),
    ("Eslatma bekor qilindi", "Reminder cancelled", "Напоминание отменено"),
    ("Sevimlilar yangilandi", "Favorites updated", "Избранное обновлено"),
    ("Standart katalog yangilandi.", "Default folder updated.", "Папка по умолчанию обновлена."),
    ("Yoqildi", "Enabled", "Включено"),
    ("O‘chirib qo‘yildi", "Disabled", "Выключено"),
    ("📭 Bu bo‘limda hozircha hech narsa yo‘q.", "📭 There is nothing here yet.", "📭 Здесь пока ничего нет."),
    ("📭 Hozircha saqlangan fayllar yo‘q.", "📭 No saved files yet.", "📭 Сохранённых файлов пока нет."),
    ("📭 Tanlash uchun yozuv yo‘q.", "📭 There are no items to select.", "📭 Нет записей для выбора."),
    ("🚫 Hisobingiz vaqtincha bloklangan. Administrator bilan bog‘laning.", "🚫 Your account is temporarily blocked. Contact the administrator.", "🚫 Ваш аккаунт временно заблокирован. Свяжитесь с администратором."),
    ("Avval ro‘yxatdan o‘tishni yakunlang.", "Complete registration first.", "Сначала завершите регистрацию."),
    ("👤 Ro‘yxatdan o‘tishni boshlash uchun /start yuboring.", "👤 Send /start to begin registration.", "👤 Отправьте /start, чтобы начать регистрацию."),
    ("⚠️ Avval shaxsiy storage kanalingizni ulang.", "⚠️ Connect your private storage channel first.", "⚠️ Сначала подключите личный канал-хранилище."),
    ("⚠️ Avval kanalni ulang.", "⚠️ Connect a channel first.", "⚠️ Сначала подключите канал."),
    ("⚠️ Storage kanal bilan aloqa faol emas.", "⚠️ The storage channel is unavailable.", "⚠️ Канал-хранилище недоступен."),
    ("⚠️ Yuborilgan to‘plam ichida bir xil fayl takrorlangan.", "⚠️ The same file appears more than once in this batch.", "⚠️ В этой подборке один и тот же файл повторяется."),
    ("♻️ Bu fayl avval saqlangan.", "♻️ This file was saved before.", "♻️ Этот файл уже был сохранён."),
    ("⚠️ Kanal bilan aloqa uzildi.", "⚠️ Connection to the channel was lost.", "⚠️ Связь с каналом потеряна."),
    ("⚠️ Fayllar to‘plamini to‘liq nusxalab bo‘lmadi.", "⚠️ The whole batch could not be copied.", "⚠️ Не удалось полностью скопировать подборку."),
    ("⚠️ Indeksni yaratib bo‘lmadi.", "⚠️ The index could not be created.", "⚠️ Не удалось создать индекс."),
    ("✅ <b>Saqlandi</b>", "✅ <b>Saved</b>", "✅ <b>Сохранено</b>"),
    ("👋 <b>KeepGram’ga xush kelibsiz!</b>", "👋 <b>Welcome to KeepGram!</b>", "👋 <b>Добро пожаловать в KeepGram!</b>"),
    ("👤 <b>Ismingizni yozib yuboring:</b>", "👤 <b>Enter your name:</b>", "👤 <b>Введите ваше имя:</b>"),
    ("Asosiy menyu:", "Main menu:", "Главное меню:"),
    ("KeepGram asosiy menyusi:", "KeepGram main menu:", "Главное меню KeepGram:"),
    ("Boshlash uchun shaxsiy kanalingizni ulang", "Connect your private channel to get started", "Подключите личный канал, чтобы начать"),
    ("✅ Ulangan kanal", "✅ Connected channel", "✅ Подключённый канал"),
    ("Ism 2–80 belgidan iborat bo‘lsin", "The name must be 2–80 characters long", "Имя должно содержать от 2 до 80 символов"),
    ("✅ Rahmat", "✅ Thank you", "✅ Спасибо"),
    ("📱 Endi pastdagi tugmani bosib", "📱 Now use the button below to", "📱 Теперь нажмите кнопку ниже, чтобы"),
    ("<b>KeepGram yordam</b>", "<b>KeepGram Help</b>", "<b>Помощь KeepGram</b>"),
    ("🔐 <b>Maxfiylik va backup</b>", "🔐 <b>Privacy and backup</b>", "🔐 <b>Конфиденциальность и резервное копирование</b>"),
    ("🔗 <b>Storage kanal</b>", "🔗 <b>Storage channel</b>", "🔗 <b>Канал-хранилище</b>"),
    ("Nomi:", "Name:", "Название:"),
    ("Holati:", "Status:", "Статус:"),
    ("🔗 <b>Kanalni ulash — 3 qadam</b>", "🔗 <b>Connect a channel — 3 steps</b>", "🔗 <b>Подключение канала — 3 шага</b>"),
    ("✅ <b>Kanal muvaffaqiyatli ulandi!</b>", "✅ <b>Channel connected successfully!</b>", "✅ <b>Канал успешно подключён!</b>"),
    ("Fayl, rasm, video, audio yoki saqlamoqchi bo‘lgan matnni yuboring.", "Send a file, photo, video, audio, or text you want to save.", "Отправьте файл, фото, видео, аудио или текст, который хотите сохранить."),
    ("⚠️ Manifestni tiklab bo‘lmadi.", "⚠️ The manifest could not be restored.", "⚠️ Не удалось восстановить манифест."),
    ("⚠️ Yangi fayl umumiy hajm limitidan oshib ketadi.", "⚠️ The new file exceeds your total storage limit.", "⚠️ Новый файл превышает общий лимит объёма."),
    ("✅ Telefon raqamingiz tasdiqlandi.", "✅ Your phone number has been verified.", "✅ Ваш номер телефона подтверждён."),
    ("✅ Telefon raqamingiz yangilandi.", "✅ Your phone number has been updated.", "✅ Ваш номер телефона обновлён."),
    ("⚠️ Faqat o‘zingizning telefon raqamingizni", "⚠️ Only send your own phone number", "⚠️ Отправьте только свой номер телефона"),
    ("✅ <b>Ro‘yxatdan o‘tish yakunlandi!</b>", "✅ <b>Registration complete!</b>", "✅ <b>Регистрация завершена!</b>"),
    ("🗑 Savat bo‘sh.", "🗑 Trash is empty.", "🗑 Корзина пуста."),
    ("30 kun ichida qaytarish mumkin.", "You can restore items within 30 days.", "Записи можно восстановить в течение 30 дней."),
    ("📊 <b>KeepGram statistikangiz</b>", "📊 <b>Your KeepGram statistics</b>", "📊 <b>Ваша статистика KeepGram</b>"),
    ("📦 Saqlangan fayllar", "📦 Saved files", "📦 Сохранённые файлы"),
    ("🗂 Yozuv va to‘plamlar", "🗂 Records and collections", "🗂 Записи и подборки"),
    ("💾 Umumiy hajm", "💾 Total size", "💾 Общий объём"),
    ("Yangi nomni yuboring", "Send the new name", "Отправьте новое название"),
    ("Teglarni vergul yoki bo‘shliq bilan yuboring.", "Send tags separated by commas or spaces.", "Отправьте теги через запятую или пробел."),
    ("🔗 <b>Vaqtinchalik ulashish havolasi</b>", "🔗 <b>Temporary sharing link</b>", "🔗 <b>Временная ссылка</b>"),
    ("Havola 24 soat ishlaydi va faqat bir marta foydalaniladi.", "The link is valid for 24 hours and can be used once.", "Ссылка действует 24 часа и используется один раз."),
    ("🕘 <b>Fayl versiyalari</b>", "🕘 <b>File versions</b>", "🕘 <b>Версии файла</b>"),
    ("⏰ Eslatma vaqtini Toshkent vaqti bilan yuboring", "⏰ Enter the reminder time in Tashkent time", "⏰ Укажите время напоминания по Ташкенту"),
    ("Izoh ixtiyoriy.", "The note is optional.", "Комментарий необязателен."),
    ("Format noto‘g‘ri.", "Invalid format.", "Неверный формат."),
    ("⏰ Faol eslatmalar yo‘q.", "⏰ There are no active reminders.", "⏰ Активных напоминаний нет."),
    ("⏰ <b>Faol eslatmalar</b>", "⏰ <b>Active reminders</b>", "⏰ <b>Активные напоминания</b>"),
    ("Qayerdan o‘chirilsin?", "What should be deleted?", "Откуда удалить?"),
    ("🧠 Saqlangan qidiruvlar yo‘q.", "🧠 There are no saved searches.", "🧠 Сохранённых поисков нет."),
    ("🧠 <b>Saqlangan qidiruvlar</b>", "🧠 <b>Saved searches</b>", "🧠 <b>Сохранённые поиски</b>"),
    ("🗂 Kataloglaringiz:", "🗂 Your folders:", "🗂 Ваши папки:"),
    ("Yangi katalog nomini yuboring", "Send the new folder name", "Отправьте название новой папки"),
    ("🏷 Hozircha teglar yo‘q.", "🏷 There are no tags yet.", "🏷 Тегов пока нет."),
    ("⚙️ <b>Sozlamalar</b>", "⚙️ <b>Settings</b>", "⚙️ <b>Настройки</b>"),
    ("Hisobga ulangan telefon raqamini yangilash uchun", "To update the phone number linked to your account", "Чтобы обновить номер телефона аккаунта"),
    ("📄 KeepGram siz haqingizda saqlayotgan metadata eksporti.", "📄 Export of metadata KeepGram stores about you.", "📄 Экспорт метаданных, которые KeepGram хранит о вас."),
    ("🛟 Imzolangan KeepGram tiklash manifesti.", "🛟 Signed KeepGram recovery manifest.", "🛟 Подписанный манифест восстановления KeepGram."),
    ("✅ KeepGram’dagi metadata hisobingiz o‘chirildi.", "✅ Your KeepGram metadata account has been deleted.", "✅ Ваши метаданные KeepGram удалены."),
    ("Amal bekor qilindi.", "Action cancelled.", "Действие отменено."),
    ("❌ Bunday kodli fayl topilmadi.", "❌ No file with this code was found.", "❌ Файл с таким кодом не найден."),
    ("⏰ <b>KeepGram eslatmasi</b>", "⏰ <b>KeepGram reminder</b>", "⏰ <b>Напоминание KeepGram</b>"),
    ("📂 Faylni ochish", "📂 Open file", "📂 Открыть файл"),
    ("🔢 Kod", "🔢 Code", "🔢 Код"),
    ("🧩 Turi", "🧩 Type", "🧩 Тип"),
    ("📦 Tarkib", "📦 Items", "📦 Содержимое"),
    ("💾 Hajm", "💾 Size", "💾 Размер"),
    ("🏷 Teglar", "🏷 Tags", "🏷 Теги"),
    ("📅", "📅", "📅"),
    ("yo‘q", "none", "нет"),
    ("Boshqa fayl", "Other file", "Другой файл"),
    ("Rasm", "Image", "Изображение"),
    ("Video", "Video", "Видео"),
    ("Audio", "Audio", "Аудио"),
    ("Matn", "Text", "Текст"),
    ("Kontakt", "Contact", "Контакт"),
    ("Joylashuv", "Location", "Местоположение"),
    ("To‘plam", "Collection", "Подборка"),
]

UI_PHRASES.extend([
    ("Ro‘yxatdan o‘tish uchun ismingiz va Telegram orqali tasdiqlangan telefon raqamingiz kerak. Bu ma’lumotlar hisobingizni aniqlash va xavfsiz boshqarish uchun saqlanadi.",
     "Registration requires your name and a phone number verified by Telegram. This information identifies and protects your account.",
     "Для регистрации нужны ваше имя и номер телефона, подтверждённый Telegram. Эти данные помогают идентифицировать и защищать аккаунт."),
    ("👋 <b>Assalomu alaykum! Men KeepGram — shaxsiy Telegram fayl omboringizman.</b>",
     "👋 <b>Hello! I’m KeepGram, your personal Telegram file vault.</b>",
     "👋 <b>Здравствуйте! Я KeepGram — ваше личное хранилище файлов в Telegram.</b>"),
    ("📦 Fayllarni o‘zingizning shaxsiy kanalingizda saqlayman", "📦 I store files in your private channel", "📦 Я храню файлы в вашем личном канале"),
    ("🔎 Nomi, katalogi yoki tegi orqali topaman", "🔎 I find them by name, folder, or tag", "🔎 Я нахожу их по названию, папке или тегу"),
    ("🔢 Maxsus kod bilan bir zumda qaytaraman", "🔢 I retrieve them instantly with a unique code", "🔢 Я мгновенно возвращаю их по уникальному коду"),
    ("1. /channel orqali shaxsiy kanalingizni ulang.", "1. Connect your private channel with /channel.", "1. Подключите личный канал командой /channel."),
    ("2. Fayl, rasm, video yoki audioni botga yuboring. Bir martada tanlangan albom bitta to‘plam bo‘lib saqlanadi.", "2. Send a file, image, video, or audio. An album selected at once is saved as one collection.", "2. Отправьте файл, фото, видео или аудио. Альбом, выбранный за один раз, сохраняется как одна подборка."),
    ("3. Bot bergan 6 belgili kodni saqlab qo‘ying.", "3. Keep the 6-character code generated by the bot.", "3. Сохраните шестизначный код, выданный ботом."),
    ("4. Kodni yuboring yoki 🔎 Qidirish orqali faylni toping.", "4. Send the code or use 🔎 Search to find the file.", "4. Отправьте код или найдите файл через 🔎 Поиск."),
    ("/recent — oxirgilari", "/recent — recent files", "/recent — недавние файлы"),
    ("/all — barcha saqlanganlar menyusi", "/all — all saved items", "/all — все сохранённые"),
    ("/catalogs — kataloglar", "/catalogs — folders", "/catalogs — папки"),
    ("/tags — teglar", "/tags — tags", "/tags — теги"),
    ("/settings — sozlamalar", "/settings — settings", "/settings — настройки"),
    ("/settings — sozlamalar va til", "/settings — settings and language", "/settings — настройки и язык"),
    ("/stats — fayllar soni va hajmi", "/stats — file count and size", "/stats — количество и объём файлов"),
    ("/backup — tiklash manifesti", "/backup — recovery manifest", "/backup — манифест восстановления"),
    ("/restore — manifestni tiklash", "/restore — restore a manifest", "/restore — восстановить манифест"),
    ("/mydata — saqlangan metadata", "/mydata — stored metadata", "/mydata — сохранённые метаданные"),
    ("/delete_my_data — metadata hisobini o‘chirish", "/delete_my_data — delete the metadata account", "/delete_my_data — удалить метаданные аккаунта"),
    ("/privacy — maxfiylik", "/privacy — privacy", "/privacy — конфиденциальность"),
    ("/cancel — amalni bekor qilish", "/cancel — cancel an action", "/cancel — отменить действие"),
    ("/trash — 30 kunlik savat", "/trash — 30-day trash", "/trash — корзина на 30 дней"),
    ("/views — saqlangan qidiruvlar", "/views — saved searches", "/views — сохранённые поиски"),
    ("/reminders — faol eslatmalar", "/reminders — active reminders", "/reminders — активные напоминания"),
    ("Fayllar siz ulagan Telegram kanalida saqlanadi. Bazaga nom, kod, katalog, teg, kanal ID va xabar ID kabi indeks metadata yoziladi.",
     "Files are stored in the Telegram channel you connect. The database contains index metadata such as the name, code, folder, tags, channel ID, and message IDs.",
     "Файлы хранятся в подключённом Telegram-канале. В базе записываются метаданные индекса: название, код, папка, теги, ID канала и сообщений."),
    ("Ism va telefon raqami ro‘yxatdan o‘tish uchun majburiy; telefon faqat o‘zingiz Telegram kontakt tugmasi orqali tasdiqlaganingizda saqlanadi.",
     "A name and phone number are required for registration; the phone number is stored only when you verify it with Telegram’s contact button.",
     "Имя и номер телефона обязательны; номер сохраняется только после подтверждения через кнопку контакта Telegram."),
    ("Foydalanish shartlariga rozilik berganingizdan keyin botga yuborgan fayllaringiz, admin backupni yoqqan bo‘lsa, avariya holatida tiklash uchun administrator boshqaradigan alohida Telegram backup kanaliga ham nusxalanadi.",
     "After you accept the Terms, and if backup is enabled, files sent to the bot are also copied to a separate administrator-managed Telegram backup channel for disaster recovery.",
     "После принятия условий и при включённом резервном копировании файлы также копируются в отдельный Telegram-канал администратора для аварийного восстановления."),
    ("Rozilik sanasi va shartlar versiyasi bazada qayd etiladi. Kanal va Telegram hisobingiz xavfsizligi sizning nazoratingizda.",
     "The acceptance date and Terms version are recorded. You remain responsible for the security of your channel and Telegram account.",
     "Дата согласия и версия условий фиксируются. Безопасность канала и Telegram-аккаунта остаётся под вашим контролем."),
    ("1️⃣ Shaxsiy kanal yarating.", "1️⃣ Create a private channel.", "1️⃣ Создайте личный канал."),
    ("2️⃣ Botni kanalga xabar joylash huquqi bilan admin qiling.", "2️⃣ Make the bot a channel administrator with permission to post messages.", "2️⃣ Назначьте бота администратором канала с правом публикации."),
    ("3️⃣ KeepGram bergan LINK kodini kanalga yuboring.", "3️⃣ Send the LINK code from KeepGram to the channel.", "3️⃣ Отправьте в канал LINK-код от KeepGram."),
    ("4️⃣ Keyin fayllarni botga yuboravering.", "4️⃣ Then send files to the bot.", "4️⃣ После этого отправляйте файлы боту."),
    ("Fayllar Telegram ichida nusxalanadi va kichik metadata indeksi ishlatiladi.", "Files are copied inside Telegram and a small metadata index is used.", "Файлы копируются внутри Telegram, а для поиска используется небольшой индекс метаданных."),
    ("Avariya holatida tiklash uchun", "For disaster recovery", "Для аварийного восстановления"),
    ("Botni kanalga qayta admin qilib, kanalni qayta ulang.", "Make the bot an administrator again and reconnect the channel.", "Снова назначьте бота администратором и переподключите канал."),
    ("Dublikatni olib tashlab qayta yuboring.", "Remove the duplicate and send the batch again.", "Удалите дубликат и отправьте подборку снова."),
    ("Qayta urinib ko‘ring.", "Please try again.", "Попробуйте ещё раз."),
    ("Keraksiz yozuvlarni o‘chirib, qayta urinib ko‘ring.", "Delete unneeded items and try again.", "Удалите ненужные записи и повторите попытку."),
    ("Bir martada bir nechta fayl tanlasangiz, ular bitta to‘plam bo‘lib saqlanadi.", "If you select several files at once, they are saved as one collection.", "Если выбрать несколько файлов сразу, они сохранятся как одна подборка."),
    ("/cancel — bekor qilish.", "/cancel — cancel.", "/cancel — отмена."),
    ("Boshqa kontakt yoki qo‘lda yozilgan raqam qabul qilinmaydi.", "Other contacts and manually typed numbers are not accepted.", "Чужие контакты и номера, введённые вручную, не принимаются."),
    ("⚠️ KeepGram yaratgan JSON manifest faylini yuboring.", "⚠️ Send the JSON manifest created by KeepGram.", "⚠️ Отправьте JSON-манифест, созданный KeepGram."),
    ("Nom, kod, teg va katalog saqlanib qoladi.", "The name, code, tags, and folder will be preserved.", "Название, код, теги и папка сохранятся."),
    ("Tozalash uchun <code>-</code> yuboring.", "Send <code>-</code> to clear the tags.", "Чтобы очистить теги, отправьте <code>-</code>."),
    ("Havolani faqat ishonchli odamga yuboring.", "Share the link only with someone you trust.", "Отправляйте ссылку только доверенному человеку."),
    ("Kerakli versiyani shaxsiy chatga olish uchun tanlang:", "Choose a version to receive it in your private chat:", "Выберите версию, чтобы получить её в личном чате:"),
    ("Vaqt kamida 1 daqiqa keyin va 10 yil ichida bo‘lsin.", "Choose a time at least 1 minute from now and within 10 years.", "Укажите время не раньше чем через 1 минуту и не позже чем через 10 лет."),
    ("Fayl nomi, #teg, catalog:Nomi, type:pdf yoki date:2026-09 yozing.", "Enter a file name, #tag, catalog:Name, type:pdf, or date:2026-09.", "Введите название, #тег, catalog:Имя, type:pdf или date:2026-09."),
    ("6 belgili kodni yuboring.", "Send the 6-character code.", "Отправьте шестизначный код."),
    ("bo‘yicha natija topilmadi.", "— no results found.", "— ничего не найдено."),
    ("qidiruvi saqlandi.", "search saved.", "поиск сохранён."),
    ("O‘chiriladigan katalogni tanlang. Fayllar Umumiy katalogiga o‘tadi.", "Choose a folder to delete. Its files will move to General.", "Выберите папку для удаления. Файлы будут перемещены в «Общее»."),
    ("Fayl kartasidagi “Teglar” tugmasidan qo‘shing.", "Add them with the “Tags” button on a file card.", "Добавьте их кнопкой «Теги» в карточке файла."),
    ("Barcha KeepGram metadata, indeks, kanal bog‘lanishi va sozlamalar o‘chadi.", "All KeepGram metadata, indexes, channel links, and settings will be deleted.", "Все метаданные, индексы, привязка канала и настройки KeepGram будут удалены."),
    ("Telegram kanaldagi fayllar qoladi.", "Files in the Telegram channel will remain.", "Файлы в Telegram-канале останутся."),
    ("Tasdiqlash uchun aynan <code>DELETE</code> deb yozing.", "Type exactly <code>DELETE</code> to confirm.", "Для подтверждения введите точно <code>DELETE</code>."),
    ("Ro‘yxatdan o‘tish majburiy.", "Registration is required.", "Регистрация обязательна."),
    ("Telefon raqamingizni tasdiqlang:", "Verify your phone number:", "Подтвердите номер телефона:"),
    ("Noma’lum tur. Masalan: type:pdf yoki type:excel", "Unknown type. Example: type:pdf or type:excel", "Неизвестный тип. Например: type:pdf или type:excel"),
    ("Sana YYYY-MM ko‘rinishida bo‘lsin. Masalan: date:2026-09", "Use YYYY-MM for the date. Example: date:2026-09", "Укажите дату в формате YYYY-MM. Например: date:2026-09"),
    ("Oy 01-12 oralig‘ida bo‘lsin", "The month must be between 01 and 12", "Месяц должен быть от 01 до 12"),
    ("Manifest JSON formati noto‘g‘ri", "Invalid manifest JSON format", "Неверный формат JSON-манифеста"),
    ("Manifest imzosi noto‘g‘ri yoki fayl o‘zgartirilgan", "The manifest signature is invalid or the file was modified", "Подпись манифеста неверна или файл был изменён"),
    ("Manifest versiyasi qo‘llab-quvvatlanmaydi", "This manifest version is not supported", "Эта версия манифеста не поддерживается"),
    ("Noto‘g‘ri sahifa", "Invalid page", "Неверная страница"),
    ("Bir vaqtda ko‘pi bilan 50 ta tanlang", "Select no more than 50 items at once", "Выберите не более 50 записей за раз"),
    ("Kamida bitta to‘g‘ri teg kiriting.", "Enter at least one valid tag.", "Введите хотя бы один корректный тег."),
    ("Ommaviy o‘chirish yakunlandi", "Bulk deletion completed", "Массовое удаление завершено"),
    ("Backup nusxasi yaratilmagani uchun o‘chirish to‘xtatildi.", "Deletion was stopped because a backup copy could not be created.", "Удаление остановлено, потому что резервная копия не создана."),
    ("Botda o‘chirish huquqi yo‘q yoki Telegram rad etdi.", "The bot lacks deletion permission or Telegram rejected the request.", "У бота нет права удаления или Telegram отклонил запрос."),
    ("Umumiy", "General", "Общее"),
    ("Noma’lum", "Unknown", "Неизвестно"),
    ("Fayl, kod yoki qidiruv matnini yuboring", "Send a file, code, or search text", "Отправьте файл, код или поисковый запрос"),
    ("Telefon raqamingizni Telegram orqali yuboring", "Send your phone number through Telegram", "Отправьте номер телефона через Telegram"),
    ("📱 KeepGram’dan foydalanish uchun telefon raqamingizni pastdagi tugma orqali yuboring.", "📱 To use KeepGram, send your phone number with the button below.", "📱 Чтобы пользоваться KeepGram, отправьте номер кнопкой ниже."),
    ("📂 Fayllaringiz", "📂 Your files", "📂 Ваши файлы"),
    ("📚 <b>Barcha saqlanganlar</b>", "📚 <b>All saved items</b>", "📚 <b>Все сохранённые</b>"),
    ("tegsiz", "no tags", "без тегов"),
    ("Yozuvlarni belgilang, so‘ng amalni tanlang.", "Select items, then choose an action.", "Отметьте записи, затем выберите действие."),
    ("⚠️ Fayl storage kanaldan o‘chirilgan yoki bot kanalga kira olmayapti.", "⚠️ The file was removed from the storage channel or the bot cannot access it.", "⚠️ Файл удалён из канала-хранилища или бот не имеет доступа."),
    ("/channel orqali bot huquqlarini qayta tekshiring yoki kanalni almashtiring.", "Check the bot permissions with /channel or replace the channel.", "Проверьте права бота через /channel или замените канал."),
    ("⚠️ Fayllar limiti tugagan:", "⚠️ File limit reached:", "⚠️ Достигнут лимит файлов:"),
    ("Operatsiya bekor qilindi;", "The operation was cancelled;", "Операция отменена;"),
    ("⚠️ Fayllar to‘plamini saqlashda xatolik.", "⚠️ Error while saving the file collection.", "⚠️ Ошибка при сохранении подборки файлов."),
    ("🔐 Ulashilgan faylni olish uchun ro‘yxatdan o‘tishni yakunlang, so‘ng havolani yana oching.", "🔐 Complete registration to receive the shared file, then open the link again.", "🔐 Завершите регистрацию, затем снова откройте ссылку, чтобы получить файл."),
    ("⚠️ Bu ulashish havolasi eskirgan, bekor qilingan yoki ishlatib bo‘lingan.", "⚠️ This sharing link expired, was revoked, or has already been used.", "⚠️ Ссылка истекла, отозвана или уже использована."),
    ("yuborildi", "sent", "отправлено"),
    ("⚠️ Faylni yuborib bo‘lmadi. Havola egasidan yangisini so‘rang.", "⚠️ The file could not be sent. Ask the link owner for a new one.", "⚠️ Не удалось отправить файл. Попросите владельца создать новую ссылку."),
    ("Ro‘yxatdan o‘tish uchun ismingizni yozib yuboring:", "Enter your name to register:", "Для регистрации введите ваше имя:"),
    ("Ro‘yxatdan o‘tishda xatolik. /start orqali qayta urinib ko‘ring.", "Registration failed. Try again with /start.", "Ошибка регистрации. Повторите через /start."),
    ("o‘zingizning Telegram telefon raqamingizni yuboring.", "send your own Telegram phone number.", "отправить свой номер телефона Telegram."),
    ("Foydalanish shartlariga rozilik bergan foydalanuvchining fayllari, backup yoqilgan bo‘lsa, tiklash uchun administrator boshqaradigan alohida Telegram kanaliga ham nusxalanadi.", "If backup is enabled, files belonging to a user who accepted the Terms are also copied to a separate administrator-managed Telegram channel for recovery.", "Если резервное копирование включено, файлы пользователя, принявшего условия, также копируются в отдельный канал администратора для восстановления."),
    ("Sizda hozir kanal ulanmagan.", "No channel is currently connected.", "Сейчас канал не подключён."),
    ("Nomsiz kanal", "Unnamed channel", "Канал без названия"),
    ("Avval mavjud kanalni uzing yoki almashtiring.", "Disconnect or replace the current channel first.", "Сначала отключите или замените текущий канал."),
    ("1️⃣ Yopiq shaxsiy Telegram kanal yarating.", "1️⃣ Create a private Telegram channel.", "1️⃣ Создайте закрытый личный Telegram-канал."),
    ("botini kanalga <b>ADMIN</b> qiling va xabar joylash huquqini bering.", "bot an <b>ADMIN</b> of the channel and allow it to post messages.", "бота <b>АДМИНИСТРАТОРОМ</b> канала и дайте право публиковать сообщения."),
    ("Quyidagi bir martalik kodni kanalga oddiy xabar sifatida yuboring:", "Send this one-time code to the channel as a normal message:", "Отправьте этот одноразовый код в канал обычным сообщением:"),
    ("⏳ Kod 15 daqiqa amal qiladi. Uni boshqa odamga bermang.", "⏳ The code is valid for 15 minutes. Do not share it.", "⏳ Код действует 15 минут. Не передавайте его другим."),
    ("⚠️ Eski kanal bilan bog‘lanish va uning KeepGram indeksi o‘chadi. Fayllarning o‘zi Telegram kanalida qoladi.", "⚠️ The old channel connection and KeepGram index will be deleted. The Telegram files will remain.", "⚠️ Привязка старого канала и индекс KeepGram будут удалены. Файлы в Telegram останутся."),
    ("Bog‘lanish va metadata indeksi o‘chadi;", "The connection and metadata index will be deleted;", "Привязка и индекс метаданных будут удалены;"),
    ("Davom etilsinmi?", "Continue?", "Продолжить?"),
    ("Kanal topilmadi", "Channel not found", "Канал не найден"),
    ("🔌 Kanal bog‘lanishi uzildi. Telegram kanaldagi fayllaringiz o‘chirilmagan.", "🔌 Channel disconnected. Files in the Telegram channel were not deleted.", "🔌 Канал отключён. Файлы в Telegram-канале не удалены."),
    ("Sizda ulangan kanal yo‘q.", "You have no connected channel.", "У вас нет подключённого канала."),
    ("⚠️ Bot kanalga admin qilinmagan.", "⚠️ The bot is not a channel administrator.", "⚠️ Бот не является администратором канала."),
    ("⚠️ Botga kanalda xabar joylash huquqini bering.", "⚠️ Give the bot permission to post in the channel.", "⚠️ Дайте боту право публиковать сообщения в канале."),
    ("📡 Kanal:", "📡 Channel:", "📡 Канал:"),
    ("Endi botga fayl yuborishingiz mumkin.", "You can now send files to the bot.", "Теперь можно отправлять файлы боту."),
    ("🔐 KeepGram ulandi. Bu kanal sizning shaxsiy fayl omboringiz sifatida ishlatiladi.", "🔐 KeepGram connected. This channel is now your private file vault.", "🔐 KeepGram подключён. Этот канал будет вашим личным хранилищем."),
    ("⚠️ Kanalni ulashda xatolik. Bot admin huquqlarini tekshiring.", "⚠️ Could not connect the channel. Check the bot’s admin permissions.", "⚠️ Не удалось подключить канал. Проверьте права администратора у бота."),
    ("⚠️ Manifest 2 MB dan katta bo‘lmasligi kerak.", "⚠️ The manifest must not exceed 2 MB.", "⚠️ Размер манифеста не должен превышать 2 МБ."),
    ("Manifest tiklanganda fayllar limiti oshib ketadi", "Restoring this manifest would exceed the file limit", "Восстановление манифеста превысит лимит файлов"),
    ("ta mavjud yoki yaroqsiz yozuv o‘tkazib yuborildi.", "existing or invalid items skipped.", "существующих или недействительных записей пропущено."),
    ("ta yozuv qo‘shildi,", "items added,", "записей добавлено,"),
    ("⚠️ Almashtirishda hozircha faqat bitta yangi fayl yuboring.", "⚠️ Send only one new file when replacing content.", "⚠️ Для замены отправьте только один новый файл."),
    ("Almashtiriladigan yozuv topilmadi.", "The item to replace was not found.", "Запись для замены не найдена."),
    ("♻️ Bu fayl boshqa yozuvda mavjud:", "♻️ This file exists in another item:", "♻️ Этот файл уже есть в другой записи:"),
    ("⚠️ Storage kanal faol emas. /channel orqali tekshiring.", "⚠️ The storage channel is inactive. Check it with /channel.", "⚠️ Канал-хранилище неактивен. Проверьте его через /channel."),
    ("⚠️ Yangi faylni storage kanalga nusxalab bo‘lmadi.", "⚠️ The new file could not be copied to the storage channel.", "⚠️ Не удалось скопировать новый файл в канал-хранилище."),
    ("⚠️ Faylni almashtirib bo‘lmadi.", "⚠️ The file could not be replaced.", "⚠️ Не удалось заменить файл."),
    ("faylining tarkibi almashtirildi. Kod o‘zgarmadi:", "file content was replaced. The code remains:", "содержимое файла заменено. Код не изменился:"),
    ("Avval ismingizni yozib yuboring:", "Enter your name first:", "Сначала введите ваше имя:"),
    ("Ro‘yxatdan o‘tishni yakunlab bo‘lmadi. /start orqali qayta urinib ko‘ring.", "Registration could not be completed. Try again with /start.", "Не удалось завершить регистрацию. Повторите через /start."),
    ("Endi foydalanish shartlarini o‘qib chiqing.", "Now read the Terms of Use.", "Теперь прочитайте условия использования."),
    ("pastdagi maxsus tugma orqali yuboring.", "using the special button below.", "с помощью специальной кнопки ниже."),
    ("Siz shartlarga allaqachon rozilik bergansiz.", "You have already accepted the Terms.", "Вы уже приняли условия."),
    ("Rozilikni saqlab bo‘lmadi.", "Your consent could not be saved.", "Не удалось сохранить согласие."),
    ("Roziligingiz saqlandi.", "Your consent has been saved.", "Ваше согласие сохранено."),
    ("Boshlash uchun shaxsiy Telegram kanalingizni ulang:", "Connect your private Telegram channel to begin:", "Для начала подключите личный Telegram-канал:"),
    ("O‘chirilgan yozuvlar 30 kun shu yerda saqlanadi.", "Deleted items stay here for 30 days.", "Удалённые записи хранятся здесь 30 дней."),
    ("Faylni qaytarish uchun ustiga bosing.", "Tap an item to restore it.", "Нажмите на запись, чтобы восстановить её."),
    ("Yozuv topilmadi.", "Item not found.", "Запись не найдена."),
    ("Avval kamida bitta yozuvni tanlang", "Select at least one item first", "Сначала выберите хотя бы одну запись"),
    ("Tanlangan yozuvlarning barchasiga qo‘shiladigan teglarni yuboring. Mavjud teglar saqlanadi.", "Send tags to add to every selected item. Existing tags are preserved.", "Отправьте теги для всех выбранных записей. Существующие теги сохранятся."),
    ("katalogni tanlang:", "choose a folder:", "выберите папку:"),
    ("ta yozuv yangilandi", "items updated", "записей обновлено"),
    ("ta tanlangan yozuv qayerdan o‘chirilsin?", "selected items: what should be deleted?", "выбранных записей: откуда удалить?"),
    ("ta yozuv 30 kunlik savatga ko‘chirildi.", "items moved to the 30-day trash.", "записей перемещено в корзину на 30 дней."),
    ("tasini kanaldan o‘chirib bo‘lmadi.", "could not be deleted from the channel.", "не удалось удалить из канала."),
    ("Tanlash tugatildi", "Selection finished", "Выбор завершён"),
    ("Fayllar Render serverida emas, Telegram kanalingizda saqlanadi. Limitlar bot va indeksni tez saqlash uchun qo‘yilgan.", "Files are stored in your Telegram channel, not on Render. Limits keep the bot and index fast.", "Файлы хранятся в Telegram-канале, а не на Render. Лимиты поддерживают скорость бота и индекса."),
    ("o‘rniga qo‘yiladigan bitta yangi faylni yuboring.", "send one new replacement file.", "отправьте один новый файл для замены."),
    ("Nom 1–120 belgi bo‘lishi kerak.", "The name must be 1–120 characters.", "Название должно содержать 1–120 символов."),
    ("✅ Nomi yangilandi:", "✅ Name updated:", "✅ Название обновлено:"),
    ("✅ Teglar yangilandi:", "✅ Tags updated:", "✅ Теги обновлены:"),
    ("Versiya topilmadi.", "Version not found.", "Версия не найдена."),
    ("tiklash nusxasi yuborildi.", "recovery copy sent.", "копия для восстановления отправлена."),
    ("⚠️ Backup versiyasini yuborib bo‘lmadi.", "⚠️ The backup version could not be sent.", "⚠️ Не удалось отправить резервную версию."),
    ("✅ Eslatma o‘rnatildi:", "✅ Reminder set:", "✅ Напоминание установлено:"),
    ("🗑 Yozuv 30 kunlik savatga ko‘chirildi.", "🗑 Item moved to the 30-day trash.", "🗑 Запись перемещена в корзину на 30 дней."),
    ("Bot kanalda admin emas.", "The bot is not a channel administrator.", "Бот не является администратором канала."),
    ("Kanal fayli o‘chirildi, indeks savatda", "Channel file deleted; index moved to trash", "Файл канала удалён, индекс помещён в корзину"),
    ("Katalogni tanlang:", "Choose a folder:", "Выберите папку:"),
    ("Katalog yangilandi", "Folder updated", "Папка обновлена"),
    ("ta natija:", "results:", "результатов:"),
    ("Saqlash formati:", "Save format:", "Формат сохранения:"),
    ("Qidiruv topilmadi.", "Saved search not found.", "Сохранённый поиск не найден."),
    ("katalogi bo‘sh.", "folder is empty.", "папка пуста."),
    ("Boshqa nom kiriting. Nom 1–16 bayt bo‘lsin va ':' belgisini ishlatmang.", "Enter another name. Use 1–16 bytes and do not use ':'.", "Введите другое название: 1–16 байт, без символа ':'."),
    ("Bu nomdagi katalog mavjud.", "A folder with this name already exists.", "Папка с таким названием уже существует."),
    ("✅ Katalog yaratildi.", "✅ Folder created.", "✅ Папка создана."),
    ("ta fayl:", "files:", "файлов:"),
    ("o‘zingizning kontaktingizni yuboring:", "send your own contact:", "отправьте свой контакт:"),
    ("Yangi fayllar uchun standart katalogni tanlang:", "Choose the default folder for new files:", "Выберите папку по умолчанию для новых файлов:"),
    ("Katalog topilmadi.", "Folder not found.", "Папка не найдена."),
    ("⚠️ Backup uchun avval storage kanalni ulang.", "⚠️ Connect a storage channel before creating a backup.", "⚠️ Перед созданием резервной копии подключите канал-хранилище."),
    ("Uni xavfsiz joyda saqlang.", "Keep it in a safe place.", "Храните его в безопасном месте."),
    ("⚠️ Avval manifestdagi eski storage kanalni qayta ulang.", "⚠️ Reconnect the old storage channel from the manifest first.", "⚠️ Сначала переподключите старый канал-хранилище из манифеста."),
    ("Faqat o‘zingizga va hozir ulangan kanalga tegishli imzolangan manifest qabul qilinadi.", "Only a signed manifest belonging to you and the currently connected channel is accepted.", "Принимается только подписанный манифест, принадлежащий вам и текущему каналу."),
    ("Barcha metadata o‘chadi, kanaldagi fayllar qoladi.", "All metadata will be deleted; channel files will remain.", "Все метаданные будут удалены; файлы в канале останутся."),
    ("Bekor qilindi. O‘chirish uchun buyruqni qayta boshlang.", "Cancelled. Restart the command if you still want to delete the data.", "Отменено. Запустите команду снова, если хотите удалить данные."),
    ("Telegram kanal fayllariga tegilmadi.", "Telegram channel files were not changed.", "Файлы Telegram-канала не изменены."),
    ("Davom etish uchun ismingizni yozing:", "Enter your name to continue:", "Введите имя, чтобы продолжить:"),
    ("🛟 <b>KeepGram avtomatik tiklash manifesti</b>", "🛟 <b>KeepGram automatic recovery manifest</b>", "🛟 <b>Автоматический манифест восстановления KeepGram</b>"),
    ("Bu fayl faqat indeks metadata va kanaldagi xabar IDlarini saqlaydi.", "This file contains only index metadata and channel message IDs.", "Этот файл содержит только метаданные индекса и ID сообщений канала."),
    ("Tiklash uchun botga /restore yuboring.", "Send /restore to the bot to recover the index.", "Для восстановления отправьте боту /restore."),
    ("🗑 Asl yozuv o‘chirilgan", "🗑 Original item deleted", "🗑 Исходная запись удалена"),
    ("⚠️ Asl kanalda topilmadi", "⚠️ Missing from original channel", "⚠️ Нет в исходном канале"),
    ("🕓 Qabul qilindi", "🕓 Received", "🕓 Получено"),
    ("📡 Asl kanal", "📡 Original channel", "📡 Исходный канал"),
    ("Juda tez yuboryapsiz. Bir oz kuting.", "You are sending requests too quickly. Please wait.", "Слишком много запросов. Подождите немного."),
    ("⏳ Juda ko‘p so‘rov yuborildi. Bir necha soniya kuting.", "⏳ Too many requests. Wait a few seconds.", "⏳ Слишком много запросов. Подождите несколько секунд."),
    ("qayta urinib ko‘ring", "try again", "попробуйте ещё раз"),
    ("botini yangi kanalga admin qilib, shu kodni kanalga yuboring:", "bot an administrator of the new channel and send this code there:", "бота администратором нового канала и отправьте туда этот код:"),
    ("Yozuv topilmadi", "Item not found", "Запись не найдена"),
    ("ta tanlandi", "selected", "выбрано"),
    ("ta yozuvga", "items:", "записям:"),
    ("⚠️ Kanal + savat", "⚠️ Channel + trash", "⚠️ Канал + корзина"),
    ("Fayl topilmadi", "File not found", "Файл не найден"),
    ("Fayl nomi, #teg, catalog:Nomi, type:pdf, type:excel, date:2026-09 yoki 6 belgili kodni yuboring.", "Send a file name, #tag, catalog:Name, type:pdf, type:excel, date:2026-09, or a 6-character code.", "Отправьте название, #тег, catalog:Имя, type:pdf, type:excel, date:2026-09 или шестизначный код."),
    ("bo‘yicha", "for", "по запросу"),
    ("Katalog o‘chirildi", "Folder deleted", "Папка удалена"),
    ("🛟 KeepGram yaratgan <code>keepgram_restore_manifest.json</code> faylini yuboring.", "🛟 Send the <code>keepgram_restore_manifest.json</code> file created by KeepGram.", "🛟 Отправьте файл <code>keepgram_restore_manifest.json</code>, созданный KeepGram."),
    (" ta ·", " items ·", " шт. ·"),
    (" ta\n", " items\n", " шт.\n"),
])


def localize_text(text: str | None, language: str | None = None) -> str | None:
    if text is None:
        return None
    lang = language or current_language.get()
    if lang == "uz" or lang not in SUPPORTED_LANGUAGES:
        return text
    index = 1 if lang == "en" else 2
    result = text
    for phrase in sorted(UI_PHRASES, key=lambda item: len(item[0]), reverse=True):
        result = result.replace(phrase[0], phrase[index])
    return result


def menu_variants(source: str) -> set[str]:
    variants = {source}
    for uz, en, ru in UI_PHRASES:
        if uz == source:
            variants.update((en, ru))
    return variants


def localized_markup(markup: Any, language: str) -> Any:
    if not isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
        return markup
    copied = markup.model_copy(deep=True)
    rows = copied.inline_keyboard if isinstance(copied, InlineKeyboardMarkup) else copied.keyboard
    for row in rows:
        for button in row:
            button.text = localize_text(button.text, language) or button.text
    return copied


class LocalizedBot(Bot):
    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        lang = current_language.get()
        if len(args) > 1:
            args = (args[0], localize_text(args[1], lang), *args[2:])
        elif "text" in kwargs:
            kwargs["text"] = localize_text(kwargs["text"], lang)
        if "reply_markup" in kwargs:
            kwargs["reply_markup"] = localized_markup(kwargs["reply_markup"], lang)
        return await super().send_message(*args, **kwargs)

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        lang = current_language.get()
        if args:
            args = (localize_text(args[0], lang), *args[1:])
        elif "text" in kwargs:
            kwargs["text"] = localize_text(kwargs["text"], lang)
        if "reply_markup" in kwargs:
            kwargs["reply_markup"] = localized_markup(kwargs["reply_markup"], lang)
        return await super().edit_message_text(*args, **kwargs)

    async def send_document(self, *args: Any, **kwargs: Any) -> Any:
        lang = current_language.get()
        if "caption" in kwargs:
            kwargs["caption"] = localize_text(kwargs["caption"], lang)
        if "reply_markup" in kwargs:
            kwargs["reply_markup"] = localized_markup(kwargs["reply_markup"], lang)
        return await super().send_document(*args, **kwargs)

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> Any:
        lang = current_language.get()
        if len(args) > 1:
            args = (args[0], localize_text(args[1], lang), *args[2:])
        elif "text" in kwargs:
            kwargs["text"] = localize_text(kwargs["text"], lang)
        return await super().answer_callback_query(*args, **kwargs)

    async def edit_message_reply_markup(self, *args: Any, **kwargs: Any) -> Any:
        if "reply_markup" in kwargs:
            kwargs["reply_markup"] = localized_markup(
                kwargs["reply_markup"], current_language.get()
            )
        return await super().edit_message_reply_markup(*args, **kwargs)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: SecretStr
    database_url: SecretStr
    app_base_url: str
    webhook_secret: SecretStr
    admin_username: str = "admin"
    admin_password: SecretStr
    admin_telegram_ids: str = ""
    session_secret: SecretStr
    redis_url: SecretStr | None = None
    max_files_per_user: int = 5_000
    max_total_size_mb: int = 51_200
    app_env: str = "production"
    log_level: str = "INFO"
    webhook_drop_pending_updates: bool = False
    trash_retention_days: int = 30

    @field_validator("redis_url", mode="before")
    @classmethod
    def normalize_optional_redis_url(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("app_base_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("APP_BASE_URL productionda HTTPS manzil bo‘lishi kerak")
        return value

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", raw):
            raise ValueError(
                "WEBHOOK_SECRET 16-128 ta A-Z, a-z, 0-9, _ yoki - belgilardan iborat bo‘lsin"
            )
        return value

    @field_validator("admin_username")
    @classmethod
    def normalize_admin_username(cls, value: str) -> str:
        value = value.strip()
        if not 3 <= len(value) <= 64:
            raise ValueError("ADMIN_USERNAME 3-64 belgi bo‘lishi kerak")
        return value

    @field_validator("admin_password")
    @classmethod
    def validate_admin_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 4 <= len(raw) <= 128:
            raise ValueError("ADMIN_PASSWORD uzunligi 4-128 ta belgi bo‘lsin")
        return SecretStr(raw)

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET kamida 32 belgi bo‘lishi kerak")
        return value

    @field_validator("max_files_per_user")
    @classmethod
    def validate_file_limit(cls, value: int) -> int:
        if not 100 <= value <= 1_000_000:
            raise ValueError("MAX_FILES_PER_USER 100-1000000 oralig‘ida bo‘lsin")
        return value

    @field_validator("max_total_size_mb")
    @classmethod
    def validate_size_limit(cls, value: int) -> int:
        if not 100 <= value <= 10_000_000:
            raise ValueError("MAX_TOTAL_SIZE_MB 100-10000000 oralig‘ida bo‘lsin")
        return value

    @field_validator("trash_retention_days")
    @classmethod
    def validate_trash_retention(cls, value: int) -> int:
        if not 1 <= value <= 365:
            raise ValueError("TRASH_RETENTION_DAYS 1-365 oralig‘ida bo‘lsin")
        return value


settings = Settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("keepgram")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, asyncpg.Record):
        return {key: jsonable(item) for key, item in dict(value).items()}
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def build_manifest_bytes(payload: dict[str, Any]) -> bytes:
    manifest = {
        "schema_version": 1,
        "app_version": APP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "data": jsonable(payload),
    }
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(
        settings.session_secret.get_secret_value().encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(
        {"manifest": manifest, "signature": signature},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def verify_manifest_bytes(raw: bytes, telegram_id: int) -> dict[str, Any]:
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Manifest hajmi 2 MB dan katta")
    try:
        envelope = json.loads(raw.decode("utf-8"))
        manifest = envelope["manifest"]
        supplied = str(envelope["signature"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("Manifest JSON formati noto‘g‘ri") from exc
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected = hmac.new(
        settings.session_secret.get_secret_value().encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("Manifest imzosi noto‘g‘ri yoki fayl o‘zgartirilgan")
    if int(manifest.get("schema_version", 0)) != 1:
        raise ValueError("Manifest versiyasi qo‘llab-quvvatlanmaydi")
    data = manifest.get("data")
    if not isinstance(data, dict) or int(data.get("owner_telegram_id", 0)) != telegram_id:
        raise ValueError("Manifest boshqa foydalanuvchiga tegishli")
    return data


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def make_code(length: int = 6) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def make_link_token() -> str:
    return f"LINK-{make_code(8)}"


def safe_uuid(value: UUID | str) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def normalize_tags(raw: str) -> list[str]:
    result: list[str] = []
    for item in re.split(r"[,\s]+", raw.strip()):
        tag = re.sub(r"[^\w-]", "", item.lstrip("#"), flags=re.UNICODE).lower()[:32]
        if tag and tag not in result:
            result.append(tag)
    return result[:10]


SEARCH_TYPE_ALIASES = {
    "rasm": "image",
    "image": "image",
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "pdf": "pdf",
    "word": "word",
    "doc": "word",
    "docx": "word",
    "excel": "excel",
    "xls": "excel",
    "xlsx": "excel",
    "csv": "excel",
    "video": "video",
    "audio": "audio",
    "matn": "text",
    "text": "text",
    "boshqa": "other",
    "other": "other",
}


def parse_search_query(query: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "text": "",
        "file_kind": None,
        "date_start": None,
        "date_end": None,
        "catalog": None,
        "tag": None,
    }
    text_parts: list[str] = []
    for token in query.strip().split():
        lowered = token.casefold()
        if lowered.startswith("type:"):
            requested = lowered.split(":", 1)[1]
            kind = SEARCH_TYPE_ALIASES.get(requested)
            if not kind:
                raise ValueError("Noma’lum tur. Masalan: type:pdf yoki type:excel")
            result["file_kind"] = kind
        elif lowered.startswith("date:"):
            raw_date = lowered.split(":", 1)[1]
            if not re.fullmatch(r"\d{4}-\d{2}", raw_date):
                raise ValueError("Sana YYYY-MM ko‘rinishida bo‘lsin. Masalan: date:2026-09")
            year, month = (int(part) for part in raw_date.split("-"))
            if not 1 <= month <= 12:
                raise ValueError("Oy 01-12 oralig‘ida bo‘lsin")
            result["date_start"] = datetime(year, month, 1, tzinfo=timezone.utc)
            result["date_end"] = (
                datetime(year + 1, 1, 1, tzinfo=timezone.utc)
                if month == 12
                else datetime(year, month + 1, 1, tzinfo=timezone.utc)
            )
        elif lowered.startswith("catalog:"):
            result["catalog"] = token.split(":", 1)[1][:40]
        elif token.startswith("#") and len(token) > 1:
            tags = normalize_tags(token)
            result["tag"] = tags[0] if tags else None
        else:
            text_parts.append(token)
    result["text"] = " ".join(text_parts)[:100]
    return result


def file_extension(file_name: str | None) -> str | None:
    if not file_name or "." not in file_name:
        return None
    extension = file_name.rsplit(".", 1)[1].lower()
    extension = re.sub(r"[^a-z0-9]", "", extension)[:16]
    return extension or None


def classify_file_kind(
    content_type: str, file_name: str | None = None, mime_type: str | None = None
) -> str:
    extension = file_extension(file_name)
    mime = (mime_type or "").lower()
    if content_type == "document":
        if extension in IMAGE_EXTENSIONS or mime.startswith("image/"):
            return "image"
        if extension == "pdf" or mime == "application/pdf":
            return "pdf"
        if (
            extension in WORD_EXTENSIONS
            or "wordprocessingml" in mime
            or mime == "application/msword"
        ):
            return "word"
        if (
            extension in EXCEL_EXTENSIONS
            or "spreadsheetml" in mime
            or "ms-excel" in mime
        ):
            return "excel"
        return "other"
    return {
        "photo": "image",
        "video": "video",
        "animation": "video",
        "video_note": "video",
        "audio": "audio",
        "voice": "audio",
        "text": "text",
        "sticker": "sticker",
        "contact": "contact",
        "location": "location",
        "venue": "location",
    }.get(content_type, "other")


def file_kind_label(file_kind: str) -> str:
    return {
        "image": "Rasm",
        "pdf": "PDF",
        "word": "Word",
        "excel": "Excel",
        "video": "Video",
        "audio": "Audio",
        "text": "Matn",
        "sticker": "Stiker",
        "contact": "Kontakt",
        "location": "Joylashuv",
        "collection": "To‘plam",
        "photo": "Rasm",
        "document": "Boshqa fayl",
        "animation": "Video",
        "voice": "Audio",
        "video_note": "Video",
        "venue": "Joylashuv",
    }.get(file_kind, "Boshqa fayl")


def clean_title(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f]", " ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or fallback)[:180]


def content_metadata(message: Message) -> dict[str, Any]:
    stamp = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    content_type = message.content_type
    file_unique_id: str | None = None
    file_size: int | None = None
    file_name: str | None = None
    mime_type: str | None = None
    if message.document:
        file_name = message.document.file_name
        mime_type = message.document.mime_type
        title = file_name or f"Fayl_{stamp}_{message.message_id}"
        file_unique_id, file_size = (
            message.document.file_unique_id,
            message.document.file_size,
        )
    elif message.photo:
        file_name, mime_type = None, "image/jpeg"
        title = clean_title(message.caption, f"Rasm_{stamp}_{message.message_id}")
        file_unique_id, file_size = (
            message.photo[-1].file_unique_id,
            message.photo[-1].file_size,
        )
    elif message.video:
        file_name, mime_type = message.video.file_name, message.video.mime_type
        title = file_name or clean_title(
            message.caption, f"Video_{stamp}_{message.message_id}"
        )
        file_unique_id, file_size = (
            message.video.file_unique_id,
            message.video.file_size,
        )
    elif message.audio:
        file_name, mime_type = message.audio.file_name, message.audio.mime_type
        title = (
            file_name or message.audio.title or f"Audio_{stamp}_{message.message_id}"
        )
        file_unique_id, file_size = (
            message.audio.file_unique_id,
            message.audio.file_size,
        )
    elif message.voice:
        mime_type = message.voice.mime_type
        title = f"Ovoz_{stamp}_{message.message_id}"
        file_unique_id, file_size = (
            message.voice.file_unique_id,
            message.voice.file_size,
        )
    elif message.animation:
        file_name, mime_type = message.animation.file_name, message.animation.mime_type
        title = file_name or f"GIF_{stamp}_{message.message_id}"
        file_unique_id, file_size = (
            message.animation.file_unique_id,
            message.animation.file_size,
        )
    elif message.sticker:
        mime_type = "image/webp"
        title = f"Sticker_{stamp}_{message.message_id}"
        file_unique_id, file_size = (
            message.sticker.file_unique_id,
            message.sticker.file_size,
        )
    elif message.video_note:
        title = f"Video_xabar_{stamp}_{message.message_id}"
        file_unique_id, file_size = (
            message.video_note.file_unique_id,
            message.video_note.file_size,
        )
    elif message.contact:
        title = f"Kontakt_{message.contact.first_name}_{stamp}_{message.message_id}"
    elif message.location or message.venue:
        title = f"Joylashuv_{stamp}_{message.message_id}"
    else:
        title = clean_title(message.text, f"Matn_{stamp}_{message.message_id}")
    title = clean_title(title, f"Fayl_{stamp}_{message.message_id}")
    return {
        "content_type": content_type,
        "file_kind": classify_file_kind(content_type, file_name, mime_type),
        "title": title,
        "file_name": file_name,
        "file_extension": file_extension(file_name),
        "mime_type": mime_type,
        "file_unique_id": file_unique_id,
        "file_size": file_size,
    }


def collection_title(messages: list[Message], parts: list[dict[str, Any]]) -> str:
    if len(parts) == 1:
        return str(parts[0]["title"])
    caption = next((message.caption for message in messages if message.caption), None)
    if caption:
        return clean_title(caption, "Fayllar to‘plami")
    kinds = list(dict.fromkeys(str(part["file_kind"]) for part in parts))
    base = f"{file_kind_label(kinds[0])}lar" if len(kinds) == 1 else "Fayllar_to‘plami"
    stamp = messages[0].date.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    return clean_title(None, f"{base}_{stamp}_{messages[0].message_id}")


def title_with_suffix(title: str, number: int, max_length: int = 180) -> str:
    if number <= 1:
        return title[:max_length]
    suffix = f" ({number})"
    if "." in title and not title.startswith("."):
        stem, extension = title.rsplit(".", 1)
        if 1 <= len(extension) <= 16 and re.fullmatch(r"[A-Za-z0-9]+", extension):
            ending = f"{suffix}.{extension}"
            return f"{stem[: max_length - len(ending)]}{ending}"
    return f"{title[: max_length - len(suffix)]}{suffix}"


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self._backup_config_cache: tuple[float, asyncpg.Record] | None = None

    async def connect(self) -> None:
        host = (urlparse(self.dsn).hostname or "").lower()
        ssl: str | None = (
            None if host in {"localhost", "127.0.0.1", "::1"} else "require"
        )
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=5,
            command_timeout=20,
            statement_cache_size=0,
            ssl=ssl,
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def ready(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("Database ulanmagan")
        return self.pool

    async def ping(self) -> bool:
        try:
            return await self.ready().fetchval("SELECT 1") == 1
        except (asyncpg.PostgresError, OSError, RuntimeError):
            return False

    async def schema_ready(self) -> bool:
        """Check that every table required by the bot and admin panel exists."""
        try:
            return bool(
                await self.ready().fetchval(
                    """
                    SELECT to_regclass('public.users') IS NOT NULL
                       AND to_regclass('public.storage_channels') IS NOT NULL
                       AND to_regclass('public.user_settings') IS NOT NULL
                       AND to_regclass('public.catalogs') IS NOT NULL
                       AND to_regclass('public.files') IS NOT NULL
                       AND to_regclass('public.file_parts') IS NOT NULL
                       AND to_regclass('public.channel_link_tokens') IS NOT NULL
                       AND to_regclass('public.audit_logs') IS NOT NULL
                       AND to_regclass('public.app_settings') IS NOT NULL
                       AND to_regclass('public.backup_assets') IS NOT NULL
                       AND to_regclass('public.user_counters') IS NOT NULL
                       AND to_regclass('public.processed_updates') IS NOT NULL
                       AND to_regclass('public.saved_views') IS NOT NULL
                       AND to_regclass('public.reminders') IS NOT NULL
                       AND to_regclass('public.share_tokens') IS NOT NULL
                    """
                )
            )
        except (asyncpg.PostgresError, OSError, RuntimeError):
            return False

    async def ensure_schema(self) -> bool:
        """Install or migrate the idempotent Supabase schema."""
        was_ready = await self.schema_ready()
        schema_path = BASE_DIR / "schema.sql"
        if not schema_path.is_file():
            raise RuntimeError("schema.sql topilmadi")
        schema_sql = schema_path.read_text(encoding="utf-8")
        async with self.ready().acquire() as conn, conn.transaction():
            await conn.execute(schema_sql)
        if not await self.schema_ready():
            raise RuntimeError("KeepGram database sxemasi to‘liq yaratilmadi")
        return not was_ready

    async def upsert_user(self, tg_user: Any) -> asyncpg.Record:
        return await self.ready().fetchrow(
            """
            WITH changed AS (
              INSERT INTO users (telegram_id,username,first_name,last_name,language_code)
              VALUES ($1,$2,$3,$4,$5)
              ON CONFLICT (telegram_id) DO UPDATE SET
                username=EXCLUDED.username,first_name=EXCLUDED.first_name,
                last_name=EXCLUDED.last_name,language_code=EXCLUDED.language_code,
                last_seen_at=now()
              WHERE users.username IS DISTINCT FROM EXCLUDED.username
                 OR users.first_name IS DISTINCT FROM EXCLUDED.first_name
                 OR users.last_name IS DISTINCT FROM EXCLUDED.last_name
                 OR users.language_code IS DISTINCT FROM EXCLUDED.language_code
                 OR users.last_seen_at < now()-interval '5 minutes'
              RETURNING *
            )
            SELECT * FROM changed UNION ALL
            SELECT * FROM users WHERE telegram_id=$1 LIMIT 1
            """,
            tg_user.id,
            tg_user.username,
            tg_user.first_name,
            tg_user.last_name,
            tg_user.language_code,
        )

    async def user_by_tg(self, telegram_id: int) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            "SELECT * FROM users WHERE telegram_id=$1", telegram_id
        )

    async def user_language(self, telegram_id: int) -> str | None:
        return await self.ready().fetchval(
            "SELECT preferred_language FROM users WHERE telegram_id=$1", telegram_id
        )

    async def set_user_language(self, telegram_id: int, language: str) -> asyncpg.Record | None:
        if language not in {"uz", "en", "ru"}:
            return None
        async with self.ready().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE users SET preferred_language=$2,last_seen_at=now() WHERE telegram_id=$1 RETURNING *",
                telegram_id, language,
            )
            if row:
                await conn.execute(
                    """INSERT INTO user_settings(user_id,language) VALUES($1,$2)
                       ON CONFLICT(user_id) DO UPDATE SET language=EXCLUDED.language,updated_at=now()""",
                    row["id"], language,
                )
            return row

    async def update_phone(self, telegram_id: int, phone: str) -> None:
        await self.ready().execute(
            "UPDATE users SET phone=$2,last_seen_at=now() WHERE telegram_id=$1",
            telegram_id,
            phone[:32],
        )

    async def update_onboarding_name(
        self, telegram_id: int, display_name: str
    ) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """UPDATE users SET display_name=$2,last_seen_at=now()
               WHERE telegram_id=$1 RETURNING *""",
            telegram_id,
            display_name[:80],
        )

    async def save_onboarding_phone(
        self, telegram_id: int, phone: str
    ) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """UPDATE users SET phone=$2,last_seen_at=now()
               WHERE telegram_id=$1 AND display_name IS NOT NULL RETURNING *""",
            telegram_id,
            phone[:32],
        )

    async def accept_terms(
        self, telegram_id: int, terms_version: str
    ) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """UPDATE users SET terms_accepted_at=now(),terms_version=$2,
                      onboarding_completed=true,
                      onboarded_at=COALESCE(onboarded_at,now()),last_seen_at=now()
               WHERE telegram_id=$1 AND display_name IS NOT NULL AND phone IS NOT NULL
               RETURNING *""",
            telegram_id,
            terms_version,
        )

    async def storage_by_tg(self, telegram_id: int) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """SELECT s.*,u.telegram_id,u.is_blocked,us.default_catalog,
                      us.index_message_enabled,us.default_favorite
               FROM users u JOIN storage_channels s ON s.user_id=u.id
               LEFT JOIN user_settings us ON us.user_id=u.id
               WHERE u.telegram_id=$1""",
            telegram_id,
        )

    async def create_link_token(self, telegram_id: int) -> str:
        token = make_link_token()
        await self.ready().execute(
            "DELETE FROM channel_link_tokens WHERE expires_at<=now()"
        )
        await self.ready().execute(
            """INSERT INTO channel_link_tokens(user_id,token,expires_at)
               SELECT id,$2,now()+interval '15 minutes' FROM users WHERE telegram_id=$1
               ON CONFLICT(user_id) DO UPDATE SET token=EXCLUDED.token,
               expires_at=EXCLUDED.expires_at,created_at=now()""",
            telegram_id,
            token,
        )
        return token

    async def token_owner(self, token: str) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """SELECT t.*,u.telegram_id FROM channel_link_tokens t
               JOIN users u ON u.id=t.user_id
               WHERE upper(t.token)=upper($1) AND t.expires_at>now()""",
            token,
        )

    async def attach_channel(
        self, token: str, channel_id: int, title: str, username: str | None
    ) -> int:
        async with self.ready().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT t.user_id,u.telegram_id FROM channel_link_tokens t
                       JOIN users u ON u.id=t.user_id
                       WHERE upper(t.token)=upper($1) AND t.expires_at>now() FOR UPDATE""",
                token,
            )
            if not row:
                raise ValueError("Token yaroqsiz yoki muddati tugagan")
            if await conn.fetchval(
                "SELECT 1 FROM storage_channels WHERE user_id=$1", row["user_id"]
            ):
                raise ValueError("Sizda allaqachon kanal ulangan")
            if await conn.fetchval(
                "SELECT 1 FROM storage_channels WHERE telegram_channel_id=$1",
                channel_id,
            ):
                raise ValueError("Bu kanal boshqa hisobga ulangan")
            await conn.execute(
                """INSERT INTO storage_channels(user_id,telegram_channel_id,channel_title,channel_username)
                       VALUES($1,$2,$3,$4)""",
                row["user_id"],
                channel_id,
                title[:200],
                username,
            )
            await conn.execute(
                "INSERT INTO user_settings(user_id) VALUES($1) ON CONFLICT DO NOTHING",
                row["user_id"],
            )
            await conn.execute(
                "DELETE FROM channel_link_tokens WHERE user_id=$1", row["user_id"]
            )
            return row["telegram_id"]

    async def refresh_channel(
        self, telegram_id: int, title: str, username: str | None, active: bool = True
    ) -> None:
        await self.ready().execute(
            """UPDATE storage_channels s SET channel_title=$2,channel_username=$3,is_active=$4
               FROM users u WHERE s.user_id=u.id AND u.telegram_id=$1""",
            telegram_id,
            title[:200],
            username,
            active,
        )

    async def mark_channel_inactive(self, telegram_id: int) -> None:
        await self.ready().execute(
            """UPDATE storage_channels s SET is_active=false FROM users u
               WHERE s.user_id=u.id AND u.telegram_id=$1""",
            telegram_id,
        )

    async def disconnect_channel(self, telegram_id: int) -> bool:
        async with self.ready().acquire() as conn, conn.transaction():
            user_id = await conn.fetchval("SELECT id FROM users WHERE telegram_id=$1", telegram_id)
            if not user_id:
                return False
            result = await conn.execute("DELETE FROM storage_channels WHERE user_id=$1", user_id)
            if result.endswith("1"):
                await conn.execute(
                    """UPDATE user_counters SET record_count=0,item_count=0,total_size=0,
                       trash_count=0,updated_at=now() WHERE user_id=$1""", user_id
                )
            return result.endswith("1")

    async def disconnect_channel_by_id(self, channel_id: UUID) -> bool:
        async with self.ready().acquire() as conn, conn.transaction():
            user_id = await conn.fetchval("SELECT user_id FROM storage_channels WHERE id=$1", channel_id)
            if not user_id:
                return False
            await conn.execute("DELETE FROM storage_channels WHERE id=$1", channel_id)
            await conn.execute(
                """UPDATE user_counters SET record_count=0,item_count=0,total_size=0,
                   trash_count=0,updated_at=now() WHERE user_id=$1""", user_id
            )
            return True

    async def setting(self, telegram_id: int) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """SELECT us.* FROM user_settings us JOIN users u ON u.id=us.user_id
               WHERE u.telegram_id=$1""",
            telegram_id,
        )

    async def update_setting(self, telegram_id: int, field: str, value: Any) -> None:
        allowed = {
            "default_catalog",
            "index_message_enabled",
            "default_favorite",
            "auto_manifest_enabled",
            "compact_cards",
            "language",
        }
        if field not in allowed:
            raise ValueError("Noto‘g‘ri sozlama")
        await self.ready().execute(
            f"""UPDATE user_settings us SET {field}=$2,updated_at=now() FROM users u
                WHERE us.user_id=u.id AND u.telegram_id=$1""",
            telegram_id,
            value,
        )

    async def user_usage(self, telegram_id: int) -> asyncpg.Record:
        return await self.ready().fetchrow(
            """SELECT COALESCE(c.record_count,0)::int AS records,
                      COALESCE(c.item_count,0)::int AS files,
                      COALESCE(c.total_size,0)::bigint AS total_size,
                      COALESCE(c.trash_count,0)::int AS trash
               FROM users u LEFT JOIN user_counters c ON c.user_id=u.id
               WHERE u.telegram_id=$1""",
            telegram_id,
        )

    async def claim_update(self, update_id: int) -> bool:
        """Atomically claim a Telegram update; stale/failed claims may be retried."""
        row = await self.ready().fetchrow(
            """INSERT INTO processed_updates(update_id) VALUES($1)
               ON CONFLICT(update_id) DO UPDATE SET status='processing',attempts=processed_updates.attempts+1,
                 claimed_at=now(),error_message=NULL
               WHERE processed_updates.status='failed'
                  OR (processed_updates.status='processing' AND processed_updates.claimed_at<now()-interval '5 minutes')
               RETURNING update_id""",
            update_id,
        )
        return bool(row)

    async def finish_update(self, update_id: int, error: str | None = None) -> None:
        await self.ready().execute(
            """UPDATE processed_updates SET status=$2,completed_at=now(),error_message=$3
               WHERE update_id=$1""",
            update_id,
            "failed" if error else "done",
            error[:300] if error else None,
        )

    async def purge_operational_history(self) -> None:
        await self.ready().execute(
            "DELETE FROM processed_updates WHERE completed_at<now()-interval '7 days'"
        )
        await self.ready().execute(
            "DELETE FROM job_failures WHERE created_at<now()-interval '30 days'"
        )

    async def mark_manifest_dirty(self, telegram_id: int) -> None:
        await self.ready().execute(
            """UPDATE storage_channels s SET manifest_dirty_at=now() FROM users u
               WHERE s.user_id=u.id AND u.telegram_id=$1""",
            telegram_id,
        )

    async def find_duplicate(
        self, telegram_id: int, unique_ids: list[str]
    ) -> asyncpg.Record | None:
        if not unique_ids:
            return None
        return await self.ready().fetchrow(
            """SELECT f.id,f.title,f.code,fp.file_name
               FROM file_parts fp JOIN files f ON f.id=fp.file_id
               JOIN users u ON u.id=f.user_id
               WHERE u.telegram_id=$1 AND f.deleted_at IS NULL
                 AND fp.telegram_file_unique_id=ANY($2::text[])
               ORDER BY f.created_at DESC LIMIT 1""",
            telegram_id,
            unique_ids,
        )

    async def unique_file_title(
        self,
        conn: asyncpg.Connection,
        user_id: UUID,
        requested: str,
        exclude_file_id: UUID | None = None,
    ) -> str:
        base = clean_title(requested, "Nomsiz fayl")
        for number in range(1, 10_001):
            candidate = title_with_suffix(base, number)
            exists = await conn.fetchval(
                """SELECT EXISTS(SELECT 1 FROM files
                   WHERE user_id=$1 AND lower(title)=lower($2) AND deleted_at IS NULL
                     AND ($3::uuid IS NULL OR id<>$3))""",
                user_id,
                candidate,
                exclude_file_id,
            )
            if not exists:
                return candidate
        raise RuntimeError("Noyob fayl nomini yaratib bo‘lmadi")

    async def create_file(
        self,
        telegram_id: int,
        title: str,
        parts: list[dict[str, Any]],
        preferred_code: str | None = None,
    ) -> asyncpg.Record:
        if not parts:
            raise ValueError("Kamida bitta fayl qismi kerak")
        async with self.ready().acquire() as conn, conn.transaction():
            context = await conn.fetchrow(
                """SELECT u.id AS user_id,s.id AS channel_id,
                          COALESCE(us.default_catalog,'Umumiy') AS catalog,
                          COALESCE(us.default_favorite,false) AS is_favorite
                   FROM users u JOIN storage_channels s ON s.user_id=u.id AND s.is_active=true
                   LEFT JOIN user_settings us ON us.user_id=u.id
                   WHERE u.telegram_id=$1 AND u.is_blocked=false FOR UPDATE OF u""",
                telegram_id,
            )
            if not context:
                raise PermissionError("Faol kanal topilmadi")
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", str(context["user_id"])
            )
            unique_title = await self.unique_file_title(conn, context["user_id"], title)
            kinds = list(dict.fromkeys(str(part["file_kind"]) for part in parts))
            total_size = sum(int(part["file_size"] or 0) for part in parts) or None
            first = parts[0]
            row: asyncpg.Record | None = None
            for attempt in range(12):
                code = (
                    preferred_code.upper()
                    if attempt == 0
                    and preferred_code
                    and CODE_RE.fullmatch(preferred_code)
                    else make_code()
                )
                row = await conn.fetchrow(
                    """INSERT INTO files(
                           user_id,channel_id,channel_message_id,code,title,file_type,
                           catalog,tags,is_favorite,telegram_file_unique_id,file_size,
                           item_count,file_kinds)
                       VALUES($1,$2,$3,$4,$5,$6,$7,'{}'::text[],$8,$9,$10,$11,$12)
                       ON CONFLICT (user_id,code) DO NOTHING RETURNING *""",
                    context["user_id"],
                    context["channel_id"],
                    first["channel_message_id"],
                    code,
                    unique_title,
                    kinds[0] if len(kinds) == 1 else "collection",
                    context["catalog"],
                    context["is_favorite"],
                    first["file_unique_id"],
                    total_size,
                    len(parts),
                    kinds,
                )
                if row:
                    break
            if not row:
                raise RuntimeError("Noyob kod yaratib bo‘lmadi")
            await conn.executemany(
                """INSERT INTO file_parts(
                       file_id,channel_message_id,position,content_type,file_kind,
                       file_name,file_extension,mime_type,telegram_file_unique_id,file_size)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                [
                    (
                        row["id"],
                        part["channel_message_id"],
                        position,
                        part["content_type"],
                        part["file_kind"],
                        part["file_name"],
                        part["file_extension"],
                        part["mime_type"],
                        part["file_unique_id"],
                        part["file_size"],
                    )
                    for position, part in enumerate(parts)
                ],
            )
            await conn.execute(
                "UPDATE storage_channels SET manifest_dirty_at=now() WHERE id=$1",
                context["channel_id"],
            )
            await conn.execute(
                """INSERT INTO user_counters(user_id,record_count,item_count,total_size)
                   VALUES($1,1,$2,$3) ON CONFLICT(user_id) DO UPDATE SET
                   record_count=user_counters.record_count+1,
                   item_count=user_counters.item_count+EXCLUDED.item_count,
                   total_size=user_counters.total_size+EXCLUDED.total_size,updated_at=now()""",
                context["user_id"], len(parts), total_size or 0,
            )
            return row

    async def file_by_id(
        self, telegram_id: int, file_id: UUID | str
    ) -> asyncpg.Record | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        return await self.ready().fetchrow(
            """SELECT f.*,s.telegram_channel_id,COALESCE(us.compact_cards,true) AS compact_cards,
                      COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                FROM file_parts fp WHERE fp.file_id=f.id),
                               ARRAY[f.channel_message_id]) AS channel_message_ids
               FROM files f
               JOIN users u ON u.id=f.user_id JOIN storage_channels s ON s.id=f.channel_id
               LEFT JOIN user_settings us ON us.user_id=u.id
               WHERE f.id=$2 AND u.telegram_id=$1 AND f.deleted_at IS NULL""",
            telegram_id,
            parsed,
        )

    async def file_by_code(self, telegram_id: int, code: str) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """SELECT f.*,s.telegram_channel_id,COALESCE(us.compact_cards,true) AS compact_cards,
                      COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                FROM file_parts fp WHERE fp.file_id=f.id),
                               ARRAY[f.channel_message_id]) AS channel_message_ids
               FROM files f
               JOIN users u ON u.id=f.user_id JOIN storage_channels s ON s.id=f.channel_id
               LEFT JOIN user_settings us ON us.user_id=u.id
               WHERE u.telegram_id=$1 AND upper(f.code)=upper($2) AND f.deleted_at IS NULL""",
            telegram_id,
            code,
        )

    async def files_page(
        self,
        telegram_id: int,
        page: int = 1,
        limit: int = 8,
        *,
        favorite: bool = False,
        catalog: str | None = None,
        tag: str | None = None,
    ) -> tuple[list[asyncpg.Record], int]:
        filters = ["u.telegram_id=$1", "f.deleted_at IS NULL"]
        args: list[Any] = [telegram_id]
        if favorite:
            filters.append("f.is_favorite=true")
        if catalog:
            args.append(catalog)
            filters.append(f"f.catalog=${len(args)}")
        if tag:
            args.append(tag)
            filters.append(f"f.tags @> ARRAY[${len(args)}]::text[]")
        where = " AND ".join(filters)
        total = await self.ready().fetchval(
            f"SELECT count(*) FROM files f JOIN users u ON u.id=f.user_id WHERE {where}",
            *args,
        )
        args.extend([limit, (page - 1) * limit])
        rows = await self.ready().fetch(
            f"""SELECT f.* FROM files f JOIN users u ON u.id=f.user_id WHERE {where}
                 ORDER BY f.created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
            *args,
        )
        return list(rows), int(total)

    async def search_files(
        self, telegram_id: int, query: str, limit: int = 10
    ) -> list[asyncpg.Record]:
        query = query.strip()[:100]
        if CODE_RE.fullmatch(query):
            row = await self.file_by_code(telegram_id, query.upper())
            return [row] if row else []
        parsed = parse_search_query(query)
        filters = ["u.telegram_id=$1", "f.deleted_at IS NULL"]
        args: list[Any] = [telegram_id]

        def add(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        if parsed["file_kind"]:
            filters.append(f"f.file_kinds @> ARRAY[{add(parsed['file_kind'])}]::text[]")
        if parsed["date_start"]:
            filters.append(f"f.created_at>={add(parsed['date_start'])}")
            filters.append(f"f.created_at<{add(parsed['date_end'])}")
        if parsed["catalog"]:
            filters.append(f"f.catalog ILIKE {add(parsed['catalog'])}")
        if parsed["tag"]:
            filters.append(f"f.tags @> ARRAY[{add(parsed['tag'])}]::text[]")
        if parsed["text"]:
            placeholder = add(parsed["text"])
            filters.append(
                f"""(lower(f.title) LIKE '%'||lower({placeholder})||'%' OR EXISTS(
                    SELECT 1 FROM unnest(f.tags) t WHERE t ILIKE '%'||{placeholder}||'%')
                    OR f.catalog ILIKE '%'||{placeholder}||'%')"""
            )
        args.append(limit)
        where = " AND ".join(filters)
        return list(
            await self.ready().fetch(
                f"""SELECT f.* FROM files f JOIN users u ON u.id=f.user_id
                    WHERE {where} ORDER BY f.created_at DESC LIMIT ${len(args)}""",
                *args,
            )
        )

    async def update_file(
        self, telegram_id: int, file_id: str, field: str, value: Any
    ) -> asyncpg.Record | None:
        allowed = {"title", "tags", "catalog", "is_favorite", "is_missing"}
        if field not in allowed:
            raise ValueError("Noto‘g‘ri maydon")
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        if field == "title":
            async with self.ready().acquire() as conn, conn.transaction():
                target = await conn.fetchrow(
                    """SELECT f.id,f.user_id FROM files f JOIN users u ON u.id=f.user_id
                       WHERE u.telegram_id=$1 AND f.id=$2 AND f.deleted_at IS NULL
                       FOR UPDATE OF f""",
                    telegram_id,
                    parsed,
                )
                if not target:
                    return None
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    str(target["user_id"]),
                )
                unique_title = await self.unique_file_title(
                    conn, target["user_id"], str(value), parsed
                )
                row = await conn.fetchrow(
                    "UPDATE files SET title=$2,updated_at=now() WHERE id=$1 RETURNING *",
                    parsed,
                    unique_title,
                )
                await conn.execute(
                    "UPDATE storage_channels SET manifest_dirty_at=now() WHERE user_id=$1",
                    target["user_id"],
                )
                return row
        row = await self.ready().fetchrow(
            f"""UPDATE files f SET {field}=$3,updated_at=now() FROM users u
                WHERE f.user_id=u.id AND u.telegram_id=$1 AND f.id=$2 AND f.deleted_at IS NULL
                RETURNING f.*""",
            telegram_id,
            parsed,
            value,
        )
        if row:
            await self.mark_manifest_dirty(telegram_id)
        return row

    async def replace_file_content(
        self, telegram_id: int, file_id: str, parts: list[dict[str, Any]]
    ) -> tuple[asyncpg.Record | None, list[int]]:
        parsed = safe_uuid(file_id)
        if not parsed or not parts:
            return None, []
        async with self.ready().acquire() as conn, conn.transaction():
            target = await conn.fetchrow(
                """SELECT f.*,
                          COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                    FROM file_parts fp WHERE fp.file_id=f.id),
                                   ARRAY[f.channel_message_id]) AS old_message_ids
                   FROM files f JOIN users u ON u.id=f.user_id
                   WHERE u.telegram_id=$1 AND f.id=$2 AND f.deleted_at IS NULL
                   FOR UPDATE OF f""",
                telegram_id,
                parsed,
            )
            if not target:
                return None, []
            kinds = list(dict.fromkeys(str(part["file_kind"]) for part in parts))
            total_size = sum(int(part["file_size"] or 0) for part in parts) or None
            first = parts[0]
            row = await conn.fetchrow(
                """UPDATE files SET channel_message_id=$2,file_type=$3,
                          telegram_file_unique_id=$4,file_size=$5,item_count=$6,
                          file_kinds=$7,is_missing=false,updated_at=now()
                   WHERE id=$1 RETURNING *""",
                parsed,
                first["channel_message_id"],
                kinds[0] if len(kinds) == 1 else "collection",
                first["file_unique_id"],
                total_size,
                len(parts),
                kinds,
            )
            await conn.execute("DELETE FROM file_parts WHERE file_id=$1", parsed)
            await conn.executemany(
                """INSERT INTO file_parts(
                       file_id,channel_message_id,position,content_type,file_kind,
                       file_name,file_extension,mime_type,telegram_file_unique_id,file_size)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                [
                    (
                        parsed,
                        part["channel_message_id"],
                        position,
                        part["content_type"],
                        part["file_kind"],
                        part["file_name"],
                        part["file_extension"],
                        part["mime_type"],
                        part["file_unique_id"],
                        part["file_size"],
                    )
                    for position, part in enumerate(parts)
                ],
            )
            await conn.execute(
                "UPDATE storage_channels SET manifest_dirty_at=now() WHERE user_id=$1",
                target["user_id"],
            )
            await conn.execute(
                """UPDATE user_counters SET
                     item_count=GREATEST(0,item_count-$2+$3),
                     total_size=GREATEST(0,total_size-$4+$5),updated_at=now()
                   WHERE user_id=$1""",
                target["user_id"], int(target["item_count"] or 0), len(parts),
                int(target["file_size"] or 0), total_size or 0,
            )
            return row, list(target["old_message_ids"] or [])

    async def delete_file(self, telegram_id: int, file_id: str) -> bool:
        parsed = safe_uuid(file_id)
        if not parsed:
            return False
        async with self.ready().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """UPDATE files f SET deleted_at=now() FROM users u
                   WHERE f.user_id=u.id AND u.telegram_id=$1 AND f.id=$2
                     AND f.deleted_at IS NULL RETURNING f.*""", telegram_id, parsed
            )
            if not row:
                return False
            await conn.execute(
                """UPDATE user_counters SET record_count=GREATEST(0,record_count-1),
                     item_count=GREATEST(0,item_count-$2),total_size=GREATEST(0,total_size-$3),
                     trash_count=trash_count+1,updated_at=now() WHERE user_id=$1""",
                row["user_id"], int(row["item_count"] or 0), int(row["file_size"] or 0),
            )
            await conn.execute(
                "UPDATE storage_channels SET manifest_dirty_at=now() WHERE user_id=$1", row["user_id"]
            )
            return True

    async def files_by_ids(
        self, telegram_id: int, file_ids: list[str]
    ) -> list[asyncpg.Record]:
        parsed = [value for item in file_ids if (value := safe_uuid(item))]
        if not parsed:
            return []
        return list(
            await self.ready().fetch(
                """SELECT f.*,s.telegram_channel_id,
                          COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                    FROM file_parts fp WHERE fp.file_id=f.id),
                                   ARRAY[f.channel_message_id]) AS channel_message_ids
                   FROM files f JOIN users u ON u.id=f.user_id
                   JOIN storage_channels s ON s.id=f.channel_id
                   WHERE u.telegram_id=$1 AND f.id=ANY($2::uuid[])
                     AND f.deleted_at IS NULL ORDER BY f.created_at DESC""",
                telegram_id,
                parsed,
            )
        )

    async def bulk_add_tags(
        self, telegram_id: int, file_ids: list[str], tags: list[str]
    ) -> int:
        parsed = [value for item in file_ids if (value := safe_uuid(item))]
        if not parsed or not tags:
            return 0
        rows = await self.ready().fetch(
            """UPDATE files f SET tags=(
                     SELECT array_agg(tag ORDER BY first_seen) FROM (
                       SELECT tag,min(position) AS first_seen
                       FROM unnest(f.tags || $3::text[]) WITH ORDINALITY AS value(tag,position)
                       GROUP BY tag ORDER BY first_seen LIMIT 10
                     ) selected_tags
                   ),updated_at=now()
               FROM users u WHERE f.user_id=u.id AND u.telegram_id=$1
                 AND f.id=ANY($2::uuid[]) AND f.deleted_at IS NULL RETURNING f.id""",
            telegram_id,
            parsed,
            tags,
        )
        if rows:
            await self.mark_manifest_dirty(telegram_id)
        return len(rows)

    async def bulk_set_catalog(
        self, telegram_id: int, file_ids: list[str], catalog: str
    ) -> int:
        parsed = [value for item in file_ids if (value := safe_uuid(item))]
        if not parsed:
            return 0
        valid = await self.ready().fetchval(
            """SELECT $2='Umumiy' OR EXISTS(
                   SELECT 1 FROM catalogs c JOIN users u ON u.id=c.user_id
                   WHERE u.telegram_id=$1 AND c.name=$2)""",
            telegram_id,
            catalog,
        )
        if not valid:
            return 0
        rows = await self.ready().fetch(
            """UPDATE files f SET catalog=$3,updated_at=now() FROM users u
               WHERE f.user_id=u.id AND u.telegram_id=$1 AND f.id=ANY($2::uuid[])
                 AND f.deleted_at IS NULL RETURNING f.id""",
            telegram_id,
            parsed,
            catalog,
        )
        if rows:
            await self.mark_manifest_dirty(telegram_id)
        return len(rows)

    async def bulk_delete_files(self, telegram_id: int, file_ids: list[str]) -> int:
        parsed = [value for item in file_ids if (value := safe_uuid(item))]
        if not parsed:
            return 0
        changed = 0
        for file_id in parsed:
            changed += int(await self.delete_file(telegram_id, str(file_id)))
        return changed

    async def trash_page(
        self, telegram_id: int, page: int = 1, limit: int = 8
    ) -> tuple[list[asyncpg.Record], int]:
        total = await self.ready().fetchval(
            """SELECT count(*) FROM files f JOIN users u ON u.id=f.user_id
               WHERE u.telegram_id=$1 AND f.deleted_at IS NOT NULL""", telegram_id
        )
        rows = await self.ready().fetch(
            """SELECT f.* FROM files f JOIN users u ON u.id=f.user_id
               WHERE u.telegram_id=$1 AND f.deleted_at IS NOT NULL
               ORDER BY f.deleted_at DESC LIMIT $2 OFFSET $3""",
            telegram_id, limit, (page - 1) * limit,
        )
        return list(rows), int(total)

    async def restore_from_trash(self, telegram_id: int, file_id: str) -> asyncpg.Record | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        async with self.ready().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """UPDATE files f SET deleted_at=NULL,updated_at=now() FROM users u
                   WHERE f.user_id=u.id AND u.telegram_id=$1 AND f.id=$2
                     AND f.deleted_at IS NOT NULL
                   RETURNING f.*""", telegram_id, parsed
            )
            if not row:
                return None
            await conn.execute(
                """UPDATE user_counters SET record_count=record_count+1,
                     item_count=item_count+$2,total_size=total_size+$3,
                     trash_count=GREATEST(0,trash_count-1),updated_at=now()
                   WHERE user_id=$1""",
                row["user_id"], int(row["item_count"] or 0), int(row["file_size"] or 0),
            )
            await conn.execute(
                "UPDATE storage_channels SET manifest_dirty_at=now() WHERE user_id=$1", row["user_id"]
            )
            return row

    async def purge_trash(self, retention_days: int) -> int:
        async with self.ready().acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """DELETE FROM files WHERE deleted_at < now()-($1::text||' days')::interval
                   RETURNING user_id""", retention_days
            )
            counts: dict[UUID, int] = {}
            for row in rows:
                counts[row["user_id"]] = counts.get(row["user_id"], 0) + 1
            for user_id, count in counts.items():
                await conn.execute(
                    "UPDATE user_counters SET trash_count=GREATEST(0,trash_count-$2),updated_at=now() WHERE user_id=$1",
                    user_id, count,
                )
            return len(rows)

    async def save_view(self, telegram_id: int, name: str, query: str) -> bool:
        result = await self.ready().execute(
            """INSERT INTO saved_views(user_id,name,query)
               SELECT id,$2,$3 FROM users WHERE telegram_id=$1
               ON CONFLICT(user_id,name) DO UPDATE SET query=EXCLUDED.query,created_at=now()""",
            telegram_id, name[:32], query[:120],
        )
        return not result.endswith("0")

    async def saved_views(self, telegram_id: int) -> list[asyncpg.Record]:
        return list(await self.ready().fetch(
            """SELECT v.* FROM saved_views v JOIN users u ON u.id=v.user_id
               WHERE u.telegram_id=$1 ORDER BY v.created_at DESC LIMIT 30""", telegram_id
        ))

    async def delete_view(self, telegram_id: int, view_id: str) -> bool:
        parsed = safe_uuid(view_id)
        if not parsed:
            return False
        result = await self.ready().execute(
            """DELETE FROM saved_views v USING users u WHERE v.user_id=u.id
               AND u.telegram_id=$1 AND v.id=$2""", telegram_id, parsed
        )
        return result.endswith("1")

    async def add_reminder(
        self, telegram_id: int, file_id: str, remind_at: datetime, note: str
    ) -> asyncpg.Record | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        return await self.ready().fetchrow(
            """INSERT INTO reminders(user_id,file_id,remind_at,note)
               SELECT u.id,f.id,$3,$4 FROM users u JOIN files f ON f.user_id=u.id
               WHERE u.telegram_id=$1 AND f.id=$2 AND f.deleted_at IS NULL RETURNING *""",
            telegram_id, parsed, remind_at, note[:200] or None,
        )

    async def due_reminders(self, limit: int = 20) -> list[asyncpg.Record]:
        return list(await self.ready().fetch(
            """WITH due AS (
                 SELECT r.id FROM reminders r JOIN users u ON u.id=r.user_id
                 WHERE r.status='pending' AND r.remind_at<=now() AND u.is_blocked=false
                 ORDER BY r.remind_at LIMIT $1 FOR UPDATE OF r SKIP LOCKED
               )
               UPDATE reminders r SET status='processing',attempts=attempts+1,updated_at=now()
               FROM due,users u,files f WHERE r.id=due.id AND r.user_id=u.id AND r.file_id=f.id
               RETURNING r.*,u.telegram_id,u.preferred_language,f.title,f.code""", limit
        ))

    async def finish_reminder(self, reminder_id: UUID, success: bool) -> None:
        await self.ready().execute(
            """UPDATE reminders SET
                 status=CASE WHEN $2 THEN 'sent' WHEN attempts<3 THEN 'pending' ELSE 'failed' END,
                 remind_at=CASE WHEN NOT $2 AND attempts<3 THEN now()+interval '5 minutes' ELSE remind_at END,
                 updated_at=now() WHERE id=$1""",
            reminder_id, success,
        )

    async def user_reminders(self, telegram_id: int) -> list[asyncpg.Record]:
        return list(await self.ready().fetch(
            """SELECT r.*,f.title,f.code FROM reminders r JOIN users u ON u.id=r.user_id
               JOIN files f ON f.id=r.file_id WHERE u.telegram_id=$1 AND r.status='pending'
               ORDER BY r.remind_at LIMIT 30""", telegram_id
        ))

    async def cancel_reminder(self, telegram_id: int, reminder_id: str) -> bool:
        parsed = safe_uuid(reminder_id)
        if not parsed:
            return False
        result = await self.ready().execute(
            """UPDATE reminders r SET status='cancelled',updated_at=now() FROM users u
               WHERE r.user_id=u.id AND u.telegram_id=$1 AND r.id=$2 AND r.status='pending'""",
            telegram_id, parsed,
        )
        return result.endswith("1")

    async def create_share(
        self, telegram_id: int, file_id: str, expires_hours: int = 24, max_uses: int = 1
    ) -> str | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        token = secrets.token_urlsafe(18).replace("-", "A").replace("_", "B")
        row = await self.ready().fetchrow(
            """INSERT INTO share_tokens(file_id,owner_user_id,token,expires_at,max_uses)
               SELECT f.id,u.id,$3,now()+($4::text||' hours')::interval,$5
               FROM users u JOIN files f ON f.user_id=u.id
               WHERE u.telegram_id=$1 AND f.id=$2 AND f.deleted_at IS NULL RETURNING token""",
            telegram_id, parsed, token, expires_hours, max_uses,
        )
        return str(row["token"]) if row else None

    async def consume_share(self, token: str) -> asyncpg.Record | None:
        async with self.ready().acquire() as conn, conn.transaction():
            share = await conn.fetchrow(
                """SELECT st.*,f.title,f.code,s.telegram_channel_id,
                          COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                    FROM file_parts fp WHERE fp.file_id=f.id),ARRAY[f.channel_message_id]) message_ids
                   FROM share_tokens st JOIN files f ON f.id=st.file_id
                   JOIN storage_channels s ON s.id=f.channel_id
                   WHERE st.token=$1 AND st.revoked_at IS NULL AND st.expires_at>now()
                     AND st.use_count<st.max_uses AND f.deleted_at IS NULL FOR UPDATE OF st""", token
            )
            if not share:
                return None
            await conn.execute("UPDATE share_tokens SET use_count=use_count+1 WHERE id=$1", share["id"])
            return share

    async def refund_share(self, share_id: UUID) -> None:
        await self.ready().execute(
            "UPDATE share_tokens SET use_count=GREATEST(0,use_count-1) WHERE id=$1", share_id
        )

    async def catalogs(self, telegram_id: int) -> list[asyncpg.Record]:
        return list(
            await self.ready().fetch(
                """SELECT name,created_at FROM catalogs c JOIN users u ON u.id=c.user_id
               WHERE u.telegram_id=$1 UNION ALL
               SELECT 'Umumiy',to_timestamp(0) ORDER BY created_at""",
                telegram_id,
            )
        )

    async def add_catalog(self, telegram_id: int, name: str) -> bool:
        result = await self.ready().execute(
            """INSERT INTO catalogs(user_id,name)
               SELECT id,$2 FROM users WHERE telegram_id=$1
               ON CONFLICT DO NOTHING""",
            telegram_id,
            name,
        )
        return result.endswith("1")

    async def delete_catalog(self, telegram_id: int, name: str) -> bool:
        if name.casefold() == "umumiy":
            return False
        async with self.ready().acquire() as conn, conn.transaction():
            user_id = await conn.fetchval(
                "SELECT id FROM users WHERE telegram_id=$1", telegram_id
            )
            if not user_id:
                return False
            await conn.execute(
                "UPDATE files SET catalog='Umumiy' WHERE user_id=$1 AND catalog=$2",
                user_id,
                name,
            )
            result = await conn.execute(
                "DELETE FROM catalogs WHERE user_id=$1 AND name=$2", user_id, name
            )
            await conn.execute(
                "UPDATE user_settings SET default_catalog='Umumiy' WHERE user_id=$1 AND default_catalog=$2",
                user_id,
                name,
            )
            return result.endswith("1")

    async def tags(self, telegram_id: int) -> list[asyncpg.Record]:
        return list(
            await self.ready().fetch(
                """SELECT tag,count(*)::int count FROM files f JOIN users u ON u.id=f.user_id,
               unnest(f.tags) tag WHERE u.telegram_id=$1 AND f.deleted_at IS NULL
               GROUP BY tag ORDER BY count DESC,tag LIMIT 50""",
                telegram_id,
            )
        )

    async def export_user(self, telegram_id: int) -> dict[str, Any] | None:
        user = await self.ready().fetchrow(
            "SELECT * FROM users WHERE telegram_id=$1", telegram_id
        )
        if not user:
            return None
        channel = await self.storage_by_tg(telegram_id)
        files = await self.ready().fetch(
            """SELECT f.code,f.title,f.file_type,f.file_kinds,f.item_count,
                      f.catalog,f.tags,f.is_favorite,f.created_at
               FROM files f JOIN users u ON u.id=f.user_id
               WHERE u.telegram_id=$1 AND f.deleted_at IS NULL ORDER BY f.created_at""",
            telegram_id,
        )
        return {
            "user": jsonable(user),
            "channel": jsonable(channel),
            "files": jsonable(files),
        }

    async def export_manifest(self, telegram_id: int) -> dict[str, Any] | None:
        channel = await self.storage_by_tg(telegram_id)
        if not channel:
            return None
        files = await self.ready().fetch(
            """SELECT f.id,f.code,f.title,f.file_type,f.file_kinds,f.item_count,
                      f.catalog,f.tags,f.is_favorite,f.file_size,f.created_at,
                      COALESCE(jsonb_agg(jsonb_build_object(
                        'channel_message_id',fp.channel_message_id,
                        'position',fp.position,'content_type',fp.content_type,
                        'file_kind',fp.file_kind,'file_name',fp.file_name,
                        'file_extension',fp.file_extension,'mime_type',fp.mime_type,
                        'file_unique_id',fp.telegram_file_unique_id,
                        'file_size',fp.file_size
                      ) ORDER BY fp.position) FILTER (WHERE fp.id IS NOT NULL),'[]'::jsonb) AS parts
               FROM files f JOIN users u ON u.id=f.user_id
               LEFT JOIN file_parts fp ON fp.file_id=f.id
               WHERE u.telegram_id=$1 AND f.deleted_at IS NULL
               GROUP BY f.id ORDER BY f.created_at""",
            telegram_id,
        )
        return {
            "owner_telegram_id": telegram_id,
            "telegram_channel_id": channel["telegram_channel_id"],
            "channel_title": channel["channel_title"],
            "files": jsonable(files),
        }

    async def pending_manifest_users(self, limit: int = 3) -> list[asyncpg.Record]:
        return list(
            await self.ready().fetch(
                """SELECT u.telegram_id,s.telegram_channel_id,s.manifest_message_id
                   FROM storage_channels s JOIN users u ON u.id=s.user_id
                   JOIN user_settings us ON us.user_id=u.id
                   WHERE s.is_active AND us.auto_manifest_enabled
                     AND s.manifest_dirty_at IS NOT NULL
                     AND s.manifest_dirty_at < now()-interval '30 seconds'
                   ORDER BY s.manifest_dirty_at LIMIT $1""",
                limit,
            )
        )

    async def complete_manifest_backup(
        self, telegram_id: int, message_id: int
    ) -> int | None:
        async with self.ready().acquire() as conn, conn.transaction():
            old_message_id = await conn.fetchval(
                """SELECT s.manifest_message_id FROM storage_channels s
                   JOIN users u ON u.id=s.user_id WHERE u.telegram_id=$1
                   FOR UPDATE OF s""",
                telegram_id,
            )
            await conn.execute(
                """UPDATE storage_channels s SET manifest_message_id=$2,
                          manifest_dirty_at=NULL,manifest_updated_at=now()
                   FROM users u WHERE s.user_id=u.id AND u.telegram_id=$1""",
                telegram_id,
                message_id,
            )
            return int(old_message_id) if old_message_id else None

    async def restore_manifest_files(
        self, telegram_id: int, payload: dict[str, Any]
    ) -> tuple[int, int]:
        storage = await self.storage_by_tg(telegram_id)
        if not storage or int(payload.get("telegram_channel_id", 0)) != int(
            storage["telegram_channel_id"]
        ):
            raise ValueError("Manifest boshqa storage kanalga tegishli")
        restored = 0
        skipped = 0
        for saved in list(payload.get("files") or [])[:1000]:
            raw_parts = list(saved.get("parts") or [])[:100]
            parts: list[dict[str, Any]] = []
            for raw in raw_parts:
                try:
                    channel_message_id = int(raw["channel_message_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                parts.append(
                    {
                        "channel_message_id": channel_message_id,
                        "content_type": str(raw.get("content_type") or "document")[:32],
                        "file_kind": str(raw.get("file_kind") or "other")[:32],
                        "file_name": clean_title(raw.get("file_name"), "") or None,
                        "file_extension": str(raw.get("file_extension") or "")[:16]
                        or None,
                        "mime_type": str(raw.get("mime_type") or "")[:100] or None,
                        "file_unique_id": str(raw.get("file_unique_id") or "")[:200]
                        or None,
                        "file_size": max(0, int(raw.get("file_size") or 0)) or None,
                    }
                )
            if not parts:
                skipped += 1
                continue
            exists = await self.ready().fetchval(
                """SELECT EXISTS(SELECT 1 FROM files f
                   JOIN users u ON u.id=f.user_id
                   WHERE u.telegram_id=$1 AND f.channel_message_id=$2)""",
                telegram_id,
                parts[0]["channel_message_id"],
            )
            if exists:
                skipped += 1
                continue
            try:
                row = await self.create_file(
                    telegram_id,
                    clean_title(saved.get("title"), "Tiklangan fayl"),
                    parts,
                    preferred_code=str(saved.get("code") or ""),
                )
                tags = normalize_tags(" ".join(str(tag) for tag in saved.get("tags", [])))
                catalog = clean_title(saved.get("catalog"), "Umumiy")[:16]
                if catalog.casefold() != "umumiy":
                    await self.add_catalog(telegram_id, catalog)
                await self.ready().execute(
                    """UPDATE files SET tags=$2,catalog=$3,is_favorite=$4
                       WHERE id=$1""",
                    row["id"],
                    tags,
                    catalog,
                    bool(saved.get("is_favorite")),
                )
                restored += 1
            except (asyncpg.PostgresError, PermissionError, RuntimeError, ValueError):
                skipped += 1
        return restored, skipped

    async def delete_user(self, telegram_id: int) -> bool:
        result = await self.ready().execute(
            "DELETE FROM users WHERE telegram_id=$1", telegram_id
        )
        return result.endswith("1")

    async def super_backup_config(self) -> asyncpg.Record:
        if self._backup_config_cache and time.monotonic() - self._backup_config_cache[0] < 15:
            return self._backup_config_cache[1]
        row = await self.ready().fetchrow(
            "SELECT * FROM app_settings WHERE singleton=true"
        )
        self._backup_config_cache = (time.monotonic(), row)
        return row

    async def set_super_backup_config(
        self, enabled: bool, channel_id: int | None
    ) -> asyncpg.Record:
        row = await self.ready().fetchrow(
            """UPDATE app_settings SET super_backup_enabled=$1,
                      super_backup_channel_id=$2,updated_at=now()
               WHERE singleton=true RETURNING *""",
            enabled,
            channel_id,
        )
        self._backup_config_cache = (time.monotonic(), row)
        return row

    async def enqueue_super_backup(
        self, telegram_id: int, file_id: UUID | str
    ) -> asyncpg.Record | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        return await self.ready().fetchrow(
            """INSERT INTO backup_assets(
                   file_id,owner_telegram_id,owner_name,owner_username,
                   version,status,title,code,file_kinds,item_count,
                   source_channel_id,source_channel_title,source_message_ids)
               SELECT f.id,u.telegram_id,COALESCE(u.display_name,u.first_name),u.username,
                      COALESCE((SELECT max(version)+1 FROM backup_assets WHERE file_id=f.id),1),
                      'pending',f.title,f.code,f.file_kinds,f.item_count,
                      s.telegram_channel_id,s.channel_title,
                      COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                FROM file_parts fp WHERE fp.file_id=f.id),ARRAY[f.channel_message_id])
               FROM files f JOIN users u ON u.id=f.user_id
               JOIN storage_channels s ON s.id=f.channel_id
               WHERE f.id=$1 AND u.telegram_id=$2
               ON CONFLICT (file_id,version) DO NOTHING RETURNING *""",
            parsed,
            telegram_id,
        )

    async def enqueue_existing_consented_backups(
        self, terms_version: str, target_channel_id: int
    ) -> int:
        rows = await self.ready().fetch(
            """INSERT INTO backup_assets(
                   file_id,owner_telegram_id,owner_name,owner_username,
                   version,status,title,code,file_kinds,item_count,
                   source_channel_id,source_channel_title,source_message_ids)
               SELECT f.id,u.telegram_id,COALESCE(u.display_name,u.first_name),u.username,
                      COALESCE((SELECT max(bv.version)+1 FROM backup_assets bv
                                WHERE bv.file_id=f.id),1),
                      'pending',f.title,f.code,f.file_kinds,f.item_count,
                      s.telegram_channel_id,s.channel_title,
                      COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                FROM file_parts fp WHERE fp.file_id=f.id),ARRAY[f.channel_message_id])
               FROM files f JOIN users u ON u.id=f.user_id
               JOIN storage_channels s ON s.id=f.channel_id
               WHERE u.onboarding_completed AND u.terms_accepted_at IS NOT NULL
                 AND u.terms_version=$1
                 AND f.deleted_at IS NULL
                 AND NOT EXISTS(
                   SELECT 1 FROM backup_assets existing WHERE existing.file_id=f.id
                     AND (existing.status IN ('pending','processing') OR
                          (existing.status='active' AND existing.backup_channel_id=$2))
                 )
               ON CONFLICT (file_id,version) DO NOTHING
               RETURNING id""",
            terms_version,
            target_channel_id,
        )
        return len(rows)

    async def pending_super_backups(
        self, terms_version: str, limit: int = 5
    ) -> list[asyncpg.Record]:
        return list(
            await self.ready().fetch(
                """SELECT b.*,a.super_backup_channel_id FROM backup_assets b
                   JOIN users u ON u.telegram_id=b.owner_telegram_id
                   CROSS JOIN app_settings a WHERE a.singleton=true
                     AND a.super_backup_enabled AND a.super_backup_channel_id IS NOT NULL
                     AND u.onboarding_completed AND u.terms_accepted_at IS NOT NULL
                     AND u.terms_version=$1 AND b.status='pending'
                   ORDER BY b.created_at LIMIT $2""",
                terms_version,
                limit,
            )
        )

    async def latest_super_backup_for_file(
        self, telegram_id: int, file_id: UUID | str
    ) -> asyncpg.Record | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        return await self.ready().fetchrow(
            """SELECT * FROM backup_assets
               WHERE file_id=$1 AND owner_telegram_id=$2
               ORDER BY version DESC LIMIT 1""",
            parsed,
            telegram_id,
        )

    async def claim_super_backup(
        self, backup_id: UUID, terms_version: str
    ) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """UPDATE backup_assets b SET status='processing',updated_at=now()
               FROM app_settings a,users u
               WHERE b.id=$1 AND b.status='pending' AND a.singleton=true
                 AND a.super_backup_enabled AND a.super_backup_channel_id IS NOT NULL
                 AND u.telegram_id=b.owner_telegram_id AND u.onboarding_completed
                 AND u.terms_accepted_at IS NOT NULL AND u.terms_version=$2
               RETURNING b.*,a.super_backup_channel_id""",
            backup_id,
            terms_version,
        )

    async def requeue_stale_super_backups(self) -> None:
        await self.ready().execute(
            """UPDATE backup_assets SET status='pending',updated_at=now(),
                      error_message='stale processing lease retried'
               WHERE status='processing' AND updated_at < now()-interval '5 minutes'"""
        )

    async def complete_super_backup(
        self,
        backup_id: UUID,
        channel_id: int,
        message_ids: list[int],
        index_message_id: int,
    ) -> None:
        await self.ready().execute(
            """UPDATE backup_assets SET status='active',backup_channel_id=$2,
                      backup_message_ids=$3,index_message_id=$4,error_message=NULL,
                      updated_at=now() WHERE id=$1""",
            backup_id,
            channel_id,
            message_ids,
            index_message_id,
        )

    async def fail_super_backup(self, backup_id: UUID, error: str) -> None:
        await self.ready().execute(
            """UPDATE backup_assets SET status='failed',error_message=$2,
                      updated_at=now() WHERE id=$1""",
            backup_id,
            error[:300],
        )

    async def retry_super_backup(self, backup_id: UUID) -> bool:
        result = await self.ready().execute(
            """UPDATE backup_assets SET status='pending',error_message=NULL,
                      updated_at=now() WHERE id=$1 AND status='failed'""",
            backup_id,
        )
        return result.endswith("1")

    async def set_super_backup_status(
        self, telegram_id: int, file_id: UUID | str, new_status: str
    ) -> list[asyncpg.Record]:
        parsed = safe_uuid(file_id)
        if not parsed or new_status not in {"active", "deleted", "replaced", "missing"}:
            return []
        eligible = ["deleted", "missing"] if new_status == "active" else ["active"]
        return list(
            await self.ready().fetch(
                """UPDATE backup_assets SET status=$3,updated_at=now()
                   WHERE file_id=$2 AND owner_telegram_id=$1 AND status=ANY($4::text[])
                   RETURNING *""",
                telegram_id,
                parsed,
                new_status,
                eligible,
            )
        )

    async def backup_asset(self, backup_id: UUID) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            "SELECT * FROM backup_assets WHERE id=$1", backup_id
        )

    async def file_versions(self, telegram_id: int, file_id: str) -> list[asyncpg.Record]:
        parsed = safe_uuid(file_id)
        if not parsed:
            return []
        return list(await self.ready().fetch(
            """SELECT id,version,status,title,created_at,backup_channel_id,backup_message_ids
               FROM backup_assets WHERE owner_telegram_id=$1 AND file_id=$2
                 AND backup_channel_id IS NOT NULL AND backup_message_ids IS NOT NULL
               ORDER BY version DESC LIMIT 20""", telegram_id, parsed
        ))

    async def user_backup_version(self, telegram_id: int, backup_id: str) -> asyncpg.Record | None:
        parsed = safe_uuid(backup_id)
        if not parsed:
            return None
        return await self.ready().fetchrow(
            """SELECT * FROM backup_assets WHERE id=$2 AND owner_telegram_id=$1
               AND backup_channel_id IS NOT NULL AND backup_message_ids IS NOT NULL""",
            telegram_id, parsed,
        )

    async def audit(
        self,
        actor_type: str,
        actor_id: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.ready().execute(
            """INSERT INTO audit_logs(actor_type,actor_id,action,target_type,target_id,metadata)
               VALUES($1,$2,$3,$4,$5,$6::jsonb)""",
            actor_type,
            actor_id,
            action,
            target_type,
            target_id,
            json.dumps(metadata or {}, ensure_ascii=False),
        )

    async def job_failure(self, worker: str, target_id: Any, exc: Exception) -> None:
        await self.ready().execute(
            """INSERT INTO job_failures(worker,target_id,error_type,error_message)
               VALUES($1,$2,$3,$4)""",
            worker[:32], str(target_id)[:200], exc.__class__.__name__, str(exc)[:500],
        )


db = Database(settings.database_url.get_secret_value())
bot = LocalizedBot(
    settings.bot_token.get_secret_value(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
router = Router(name="keepgram")
redis_client = (
    Redis.from_url(
        settings.redis_url.get_secret_value(),
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
        health_check_interval=30,
    )
    if settings.redis_url
    else None
)


async def redis_healthy() -> bool | None:
    if not redis_client:
        return None
    try:
        return bool(await redis_client.ping())
    except Exception:  # noqa: BLE001 - health endpoint must return a status, not crash
        return False


fsm_storage = (
    RedisStorage(redis_client, state_ttl=86_400, data_ttl=86_400)
    if redis_client
    else MemoryStorage()
)
dp = Dispatcher(storage=fsm_storage)
dp.include_router(router)


class Flow(StatesGroup):
    onboarding_name = State()
    search = State()
    rename = State()
    tags = State()
    catalog_name = State()
    save_text = State()
    bulk_tags = State()
    replace_file = State()
    restore_manifest = State()
    delete_account = State()
    reminder = State()


class BotRateLimitMiddleware(BaseMiddleware):
    """Small in-process token bucket; allows albums but rejects sustained floods."""

    def __init__(self, capacity: float = 12, refill_per_second: float = 3) -> None:
        self.capacity = capacity
        self.refill = refill_per_second
        self.buckets: dict[int, tuple[float, float, float]] = {}

    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        now = time.monotonic()
        tokens, updated, warned = self.buckets.get(user.id, (self.capacity, now, 0.0))
        tokens = min(self.capacity, tokens + (now - updated) * self.refill)
        if tokens < 1:
            self.buckets[user.id] = (tokens, now, warned)
            if now - warned > 3:
                self.buckets[user.id] = (tokens, now, now)
                if isinstance(event, CallbackQuery):
                    await event.answer(
                        "Juda tez yuboryapsiz. Bir oz kuting.", show_alert=True
                    )
                elif isinstance(event, Message):
                    await event.answer(
                        "⏳ Juda ko‘p so‘rov yuborildi. Bir necha soniya kuting."
                    )
            return None
        self.buckets[user.id] = (tokens - 1, now, warned)
        if len(self.buckets) > 10_000:
            self.buckets = {
                key: value
                for key, value in self.buckets.items()
                if now - value[1] < 3600
            }
        return await handler(event, data)


language_cache: dict[int, tuple[float, str]] = {}


class LanguageMiddleware(BaseMiddleware):
    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        language = "uz"
        if user:
            cached = language_cache.get(user.id)
            if cached and time.monotonic() - cached[0] < 60:
                language = cached[1]
            else:
                try:
                    language = await db.user_language(user.id) or "uz"
                except (asyncpg.PostgresError, RuntimeError):
                    language = "uz"
                language_cache[user.id] = (time.monotonic(), language)
        token = current_language.set(language)
        try:
            return await handler(event, data)
        finally:
            current_language.reset(token)


bot_rate_limiter = BotRateLimitMiddleware()
router.message.outer_middleware(LanguageMiddleware())
router.callback_query.outer_middleware(LanguageMiddleware())
router.message.outer_middleware(bot_rate_limiter)
router.callback_query.outer_middleware(bot_rate_limiter)


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Saqlash"), KeyboardButton(text="🔎 Qidirish")],
        [KeyboardButton(text="📚 Barcha saqlanganlar")],
        [KeyboardButton(text="🗂 Kataloglar"), KeyboardButton(text="🏷 Teglar")],
        [KeyboardButton(text="🧠 Saqlangan qidiruvlar"), KeyboardButton(text="🗑 Savat")],
        [KeyboardButton(text="🔢 Kod bo‘yicha"), KeyboardButton(text="🕘 Oxirgilari")],
        [KeyboardButton(text="⭐ Sevimlilar"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="⚙️ Sozlamalar"), KeyboardButton(text="ℹ️ Yordam")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Fayl, kod yoki qidiruv matnini yuboring",
)

ONBOARDING_PHONE_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Telefon raqamimni yuborish", request_contact=True)]
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
    input_field_placeholder="Telefon raqamingizni Telegram orqali yuboring",
)


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def file_emoji(file_type: str) -> str:
    return {
        "image": "🖼",
        "pdf": "📕",
        "word": "📘",
        "excel": "📗",
        "other": "📦",
        "collection": "🗃",
        "document": "📄",
        "photo": "🖼",
        "video": "🎥",
        "audio": "🎵",
        "voice": "🎙",
        "animation": "🎞",
        "text": "📝",
        "sticker": "✨",
        "video_note": "🎬",
        "contact": "👤",
        "location": "📍",
        "venue": "📍",
    }.get(file_type, "📦")


def record_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def row_file_kinds(row: Any) -> list[str]:
    kinds = record_value(row, "file_kinds")
    return list(kinds or [row["file_type"]])


def kinds_text(kinds: list[str]) -> str:
    return ", ".join(file_kind_label(kind) for kind in kinds) or "Boshqa fayl"


def human_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def configured_admin_telegram_ids() -> set[int]:
    return {
        int(item.strip())
        for item in settings.admin_telegram_ids.split(",")
        if item.strip().isdigit()
    }


def terms_are_current(user: Any) -> bool:
    return bool(
        record_value(user, "terms_accepted_at")
        and record_value(user, "terms_version") == TERMS_VERSION
    )


def terms_text(language: str) -> str:
    texts = {
        "uz": (
            "📜 <b>KeepGram foydalanish shartlari</b>\n\n"
            "KeepGram sizning fayllaringizni qulay saqlash va qayta topishga yordam beradi. Davom etishdan oldin quyidagilarni o‘qing:\n\n"
            "👤 <b>Hisob ma’lumotlari</b>\nIsmingiz, tasdiqlangan telefon raqamingiz va tanlangan til bazada saqlanadi.\n\n"
            "📁 <b>Fayllarni saqlash</b>\nBotga yuborgan fayllaringiz siz ulagan shaxsiy Telegram kanaliga nusxalanadi. Bazada faqat qidiruv uchun nom, kod, tur, teg va xabar ID kabi metadata saqlanadi.\n\n"
            "🛡 <b>Avariya backupi</b>\nYo‘qolgan ma’lumotni tiklash uchun fayllar administrator boshqaradigan alohida backup kanaliga ham nusxalanishi mumkin. Backupda egasi, sana, asl kanal va fayl ma’lumotlari ko‘rsatiladi.\n\n"
            "🗑 <b>O‘chirish va tiklash</b>\nIndeksni yoki asl faylni o‘chirsangiz ham, avariya backupi tiklash maqsadida saqlanib qolishi mumkin. Administrator sizning so‘rovingiz bo‘yicha backupdan faylni tiklab yuborishi mumkin.\n\n"
            "🔐 <b>Xavfsizlik</b>\nKanal va Telegram hisobingiz xavfsizligi sizning nazoratingizda. Maxfiy havolalarni begonalarga bermang.\n\n"
            f"📌 Shartlar versiyasi: <code>{TERMS_VERSION}</code>\n\n"
            "Quyidagi tugmani bosib, ushbu shartlarni o‘qiganingizni va roziligingizni tasdiqlaysiz."
        ),
        "en": (
            "📜 <b>KeepGram Terms of Use</b>\n\n"
            "KeepGram helps you store and find your files. Please read the following before continuing:\n\n"
            "👤 <b>Account information</b>\nYour name, verified phone number, and selected language are stored in the database.\n\n"
            "📁 <b>File storage</b>\nFiles sent to the bot are copied to the private Telegram channel you connect. The database stores only searchable metadata such as the name, code, type, tags, and message IDs.\n\n"
            "🛡 <b>Disaster backup</b>\nTo recover lost data, files may also be copied to a separate backup channel managed by the administrator. The backup includes the owner, date, original channel, and file details.\n\n"
            "🗑 <b>Deletion and recovery</b>\nIf you delete an index or the original file, the disaster backup may remain for recovery. At your request, the administrator may send your file back from the backup.\n\n"
            "🔐 <b>Security</b>\nYou are responsible for the security of your Telegram account and channel. Never share private links with strangers.\n\n"
            f"📌 Terms version: <code>{TERMS_VERSION}</code>\n\n"
            "By pressing the button below, you confirm that you have read and accepted these terms."
        ),
        "ru": (
            "📜 <b>Условия использования KeepGram</b>\n\n"
            "KeepGram помогает удобно хранить и находить файлы. Перед продолжением прочитайте следующее:\n\n"
            "👤 <b>Данные аккаунта</b>\nВаше имя, подтверждённый номер телефона и выбранный язык сохраняются в базе данных.\n\n"
            "📁 <b>Хранение файлов</b>\nФайлы, отправленные боту, копируются в подключённый вами личный Telegram-канал. В базе сохраняются только метаданные для поиска: название, код, тип, теги и ID сообщений.\n\n"
            "🛡 <b>Аварийная копия</b>\nДля восстановления утерянных данных файлы также могут копироваться в отдельный канал, которым управляет администратор. В копии указываются владелец, дата, исходный канал и данные файла.\n\n"
            "🗑 <b>Удаление и восстановление</b>\nПосле удаления индекса или исходного файла аварийная копия может сохраниться для восстановления. По вашему запросу администратор может вернуть файл из резервной копии.\n\n"
            "🔐 <b>Безопасность</b>\nБезопасность вашего Telegram-аккаунта и канала находится под вашим контролем. Не передавайте приватные ссылки посторонним.\n\n"
            f"📌 Версия условий: <code>{TERMS_VERSION}</code>\n\n"
            "Нажимая кнопку ниже, вы подтверждаете, что прочитали и принимаете эти условия."
        ),
    }
    return texts.get(language, texts["uz"])


async def send_terms(message: Message) -> None:
    await message.answer(
        terms_text(current_language.get()),
        reply_markup=ikb([[('✅ Roziman va davom etaman', 'terms:accept')]]),
    )


async def send_language_picker(message: Message, *, changing: bool = False) -> None:
    title = (
        "🌐 <b>Tilni tanlang / Choose a language / Выберите язык</b>\n\n"
        "Botdagi barcha menyu va xabarlar tanlagan tilingizda ko‘rsatiladi.\n"
        "All menus and messages will use your selected language.\n"
        "Все меню и сообщения будут показаны на выбранном языке."
    )
    await message.answer(
        title,
        reply_markup=ikb([
            [("🇺🇿 O‘zbekcha", "lang:set:uz")],
            [("🇬🇧 English", "lang:set:en")],
            [("🇷🇺 Русский", "lang:set:ru")],
            *(([[('❌ Bekor', 'noop')]] if changing else [])),
        ]),
    )


def file_actions(file_id: Any, favorite: bool = False) -> InlineKeyboardMarkup:
    fid = str(file_id)
    return ikb(
        [
            [("📤 Olish", f"f:get:{fid}"), ("♻️ Almashtirish", f"f:replace:{fid}")],
            [("✏️ Nom", f"f:ren:{fid}"), ("🏷 Teglar", f"f:tag:{fid}")],
            [("🗂 Katalog", f"f:cat:{fid}"), ("🕘 Versiyalar", f"f:versions:{fid}")],
            [("⏰ Eslatma", f"f:rem:{fid}"), ("🔗 Ulashish", f"f:share:{fid}")],
            [
                ("☆ Sevimlidan" if favorite else "⭐ Sevimli", f"f:fav:{fid}"),
                ("🗑 O‘chirish", f"f:del:{fid}"),
            ],
        ]
    )


def file_card(row: asyncpg.Record) -> str:
    tags = " ".join(f"#{esc(tag)}" for tag in (row["tags"] or [])) or "yo‘q"
    created = row["created_at"].astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    item_count = int(record_value(row, "item_count", 1))
    type_line = kinds_text(row_file_kinds(row))
    size = human_size(int(record_value(row, "file_size", 0) or 0))
    compact = bool(record_value(row, "compact_cards", True))
    if compact:
        return (
            f"{file_emoji(row['file_type'])} <b>{esc(row['title'])}</b> · {esc(type_line)} · {size}\n"
            f"<code>{esc(row['code'])}</code> · {esc(row['catalog'])} · {tags}\n"
            f"📦 {item_count} ta · 📅 {created}"
        )
    return (
        f"{file_emoji(row['file_type'])} <b>{esc(row['title'])}</b> · {esc(type_line)} · {size}\n"
        f"\n🔢 Kod: <code>{esc(row['code'])}</code>\n🧩 Turi: {esc(type_line)}\n"
        f"📦 Tarkib: {item_count} ta\n💾 Hajm: {size}\n🗂 Katalog: {esc(row['catalog'])}\n"
        f"🏷 Teglar: {tags}\n📅 {created}"
    )


actor_user_cache: dict[int, tuple[float, asyncpg.Record]] = {}


async def actor_user(
    event: Message | CallbackQuery, *, allow_incomplete: bool = False,
    allow_language_missing: bool = False,
) -> asyncpg.Record | None:
    tg_user = event.from_user
    if not tg_user:
        return None
    cached = actor_user_cache.get(tg_user.id)
    if cached and time.monotonic() - cached[0] < 15 and cached[1]["onboarding_completed"]:
        user = cached[1]
    else:
        user = await db.upsert_user(tg_user)
        if user["onboarding_completed"]:
            actor_user_cache[tg_user.id] = (time.monotonic(), user)
        if len(actor_user_cache) > 10_000:
            actor_user_cache.clear()
    if user["is_blocked"]:
        if isinstance(event, CallbackQuery):
            await event.answer("Hisobingiz bloklangan.", show_alert=True)
        else:
            await event.answer(
                "🚫 Hisobingiz vaqtincha bloklangan. Administrator bilan bog‘laning."
            )
        return None
    if not user["preferred_language"] and not allow_language_missing:
        target = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(event, CallbackQuery):
            await event.answer("Tilni tanlang / Choose a language / Выберите язык", show_alert=True)
        await send_language_picker(target)
        return None
    if (
        not user["onboarding_completed"] or not terms_are_current(user)
    ) and not allow_incomplete:
        target = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(event, CallbackQuery):
            await event.answer("Avval ro‘yxatdan o‘tishni yakunlang.", show_alert=True)
        if not user["display_name"]:
            await target.answer(
                "👤 Ro‘yxatdan o‘tishni boshlash uchun /start yuboring.",
                reply_markup=ReplyKeyboardRemove(),
            )
        elif not user["phone"]:
            await target.answer(
                "📱 KeepGram’dan foydalanish uchun telefon raqamingizni pastdagi tugma orqali yuboring.",
                reply_markup=ONBOARDING_PHONE_KEYBOARD,
            )
        else:
            await send_terms(target)
        return None
    return user


async def show_files(
    message: Message,
    page: int = 1,
    *,
    favorite: bool = False,
    catalog: str | None = None,
    tag: str | None = None,
) -> None:
    rows, total = await db.files_page(
        message.chat.id, page, favorite=favorite, catalog=catalog, tag=tag
    )
    if not rows:
        await message.answer("📭 Bu bo‘limda hozircha hech narsa yo‘q.")
        return
    pages = max(1, (total + 7) // 8)
    buttons = [
        [(f"{file_emoji(r['file_type'])} {r['title'][:38]}", f"f:open:{r['id']}")]
        for r in rows
    ]
    mode = "fav" if favorite else "all"
    nav: list[tuple[str, str]] = []
    if page > 1:
        nav.append(("⬅️", f"list:{mode}:{page - 1}"))
    nav.append((f"{page}/{pages}", "noop"))
    if page < pages:
        nav.append(("➡️", f"list:{mode}:{page + 1}"))
    buttons.append(nav)
    title = "⭐ Sevimlilar" if favorite else "📂 Fayllaringiz"
    await message.answer(f"{title} — jami {total} ta:", reply_markup=ikb(buttons))


INVENTORY_PAGE_SIZE = 20


def inventory_page_text(rows: list[Any], total: int, page: int, pages: int) -> str:
    lines = [f"📚 <b>Barcha saqlanganlar</b> · {total} ta · {page}/{pages}\n"]
    for position, row in enumerate(rows, start=(page - 1) * INVENTORY_PAGE_SIZE + 1):
        tags = " ".join(f"#{esc(tag)}" for tag in (row["tags"] or [])[:3]) or "tegsiz"
        title = clean_title(str(row["title"]), "Nomsiz")[:55]
        item_count = int(record_value(row, "item_count", 1))
        count = f" · {item_count} ta" if item_count > 1 else ""
        lines.append(
            f"{position}. {file_emoji(row['file_type'])} <b>{esc(title)}</b> · "
            f"<code>{esc(row['code'])}</code>\n"
            f"   {esc(kinds_text(row_file_kinds(row)))}{count} · {tags}"
        )
    return "\n".join(lines)


async def show_inventory(
    message: Message, telegram_id: int, page: int = 1, *, edit: bool = False
) -> None:
    rows, total = await db.files_page(telegram_id, page=page, limit=INVENTORY_PAGE_SIZE)
    if not rows:
        await message.answer("📭 Hozircha saqlangan fayllar yo‘q.")
        return
    pages = max(1, (total + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
    page = min(page, pages)
    buttons = [
        [(f"{index}. {str(row['title'])[:32]}", f"f:open:{row['id']}")]
        for index, row in enumerate(rows, start=(page - 1) * INVENTORY_PAGE_SIZE + 1)
    ]
    navigation: list[tuple[str, str]] = []
    if page > 1:
        navigation.append(("⬅️ Oldingi", f"inventory:{page - 1}"))
    if page < pages:
        navigation.append(("Keyingi ➡️", f"inventory:{page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([("☑️ Bir nechtasini tanlash", f"bulk:start:{page}")])
    text = inventory_page_text(rows, total, page, pages)
    markup = ikb(buttons)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def show_bulk_inventory(
    message: Message, telegram_id: int, state: FSMContext, page: int
) -> None:
    rows, total = await db.files_page(telegram_id, page=page, limit=INVENTORY_PAGE_SIZE)
    if not rows:
        await message.edit_text("📭 Tanlash uchun yozuv yo‘q.")
        return
    data = await state.get_data()
    selected = set(data.get("bulk_selected", []))
    pages = max(1, (total + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE)
    buttons = [
        [
            (
                f"{'✅' if str(row['id']) in selected else '⬜'} {str(row['title'])[:30]}",
                f"bulk:toggle:{page}:{row['id']}",
            )
        ]
        for row in rows
    ]
    navigation: list[tuple[str, str]] = []
    if page > 1:
        navigation.append(("⬅️", f"bulk:page:{page - 1}"))
    navigation.append((f"{page}/{pages}", "noop"))
    if page < pages:
        navigation.append(("➡️", f"bulk:page:{page + 1}"))
    buttons.extend(
        [
            navigation,
            [("🏷 Teg qo‘shish", "bulk:tags"), ("🗂 Katalog", "bulk:catalog")],
            [("🗑 Ommaviy o‘chirish", "bulk:delete"), ("✅ Tugatish", "bulk:done")],
        ]
    )
    await message.edit_text(
        f"☑️ <b>Ommaviy boshqaruv</b> · {len(selected)} ta tanlandi\n"
        "Yozuvlarni belgilang, so‘ng amalni tanlang.",
        reply_markup=ikb(buttons),
    )


async def send_stored_file(chat_id: int, row: asyncpg.Record) -> bool:
    try:
        message_ids = list(row["channel_message_ids"] or [row["channel_message_id"]])
        if len(message_ids) == 1:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=row["telegram_channel_id"],
                message_id=message_ids[0],
            )
        else:
            await bot.copy_messages(
                chat_id=chat_id,
                from_chat_id=row["telegram_channel_id"],
                message_ids=message_ids,
            )
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        await db.update_file(chat_id, str(row["id"]), "is_missing", True)
        await update_super_backup_status(chat_id, row["id"], "missing")
        await bot.send_message(
            chat_id,
            "⚠️ Fayl storage kanaldan o‘chirilgan yoki bot kanalga kira olmayapti.",
        )
        return False


async def save_messages(messages: list[Message]) -> None:
    messages = sorted(messages, key=lambda item: item.message_id)
    message = messages[0]
    user = await actor_user(message)
    if not user:
        return
    storage = await db.storage_by_tg(message.from_user.id)
    if not storage:
        await message.answer(
            "⚠️ Avval shaxsiy storage kanalingizni ulang.",
            reply_markup=ikb([[("🔗 Kanalni ulash", "channel:link")]]),
        )
        return
    if not storage["is_active"]:
        await message.answer(
            "⚠️ Storage kanal bilan aloqa faol emas. /channel orqali bot huquqlarini qayta tekshiring yoki kanalni almashtiring."
        )
        return
    parts = [content_metadata(item) for item in messages]
    unique_ids = [str(part["file_unique_id"]) for part in parts if part["file_unique_id"]]
    if len(unique_ids) != len(set(unique_ids)):
        await message.answer(
            "⚠️ Yuborilgan to‘plam ichida bir xil fayl takrorlangan. Dublikatni olib tashlab qayta yuboring."
        )
        return
    duplicate = await db.find_duplicate(message.from_user.id, unique_ids)
    if duplicate:
        await message.answer(
            "♻️ Bu fayl avval saqlangan.\n\n"
            f"📝 {esc(duplicate['title'])}\n🔢 Kod: <code>{duplicate['code']}</code>",
            reply_markup=ikb(
                [[("📂 Avvalgi yozuvni ochish", f"f:open:{duplicate['id']}")]]
            ),
        )
        return
    usage = await db.user_usage(message.from_user.id)
    current_files = int(usage["files"] if usage else 0)
    current_size = int(usage["total_size"] if usage else 0)
    incoming_size = sum(int(part["file_size"] or 0) for part in parts)
    size_limit = settings.max_total_size_mb * 1024 * 1024
    if current_files + len(parts) > settings.max_files_per_user:
        await message.answer(
            f"⚠️ Fayllar limiti tugagan: {current_files}/{settings.max_files_per_user}. "
            "Keraksiz yozuvlarni o‘chirib, qayta urinib ko‘ring."
        )
        return
    if current_size + incoming_size > size_limit:
        await message.answer(
            f"⚠️ Umumiy hajm limiti tugagan: {settings.max_total_size_mb} MB. "
            "Keraksiz yozuvlarni o‘chirib, qayta urinib ko‘ring."
        )
        return
    title = collection_title(messages, parts)
    copied_ids: list[int] = []
    try:
        if len(messages) == 1:
            copied = await bot.copy_message(
                chat_id=storage["telegram_channel_id"],
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            copied_ids = [copied.message_id]
        else:
            copied = await bot.copy_messages(
                chat_id=storage["telegram_channel_id"],
                from_chat_id=message.chat.id,
                message_ids=[item.message_id for item in messages],
            )
            copied_ids = [item.message_id for item in copied]
    except (TelegramBadRequest, TelegramForbiddenError):
        await db.mark_channel_inactive(message.from_user.id)
        await message.answer(
            "⚠️ Kanal bilan aloqa uzildi. Botni kanalga qayta admin qilib, kanalni qayta ulang."
        )
        return
    if len(copied_ids) != len(parts):
        log.error(
            "Telegram copied %s of %s grouped messages", len(copied_ids), len(parts)
        )
        for copied_id in copied_ids:
            try:
                await bot.delete_message(storage["telegram_channel_id"], copied_id)
            except Exception:  # noqa: BLE001 - rollback is best effort
                log.warning(
                    "Incomplete group rollback failed for channel_message_id=%s",
                    copied_id,
                )
        await message.answer(
            "⚠️ Fayllar to‘plamini to‘liq nusxalab bo‘lmadi. Qayta urinib ko‘ring."
        )
        return
    for part, copied_id in zip(parts, copied_ids, strict=True):
        part["channel_message_id"] = copied_id
    try:
        row = await db.create_file(message.from_user.id, title, parts)
    except Exception:
        log.exception("File index insert failed; attempting orphan cleanup")
        for copied_id in copied_ids:
            try:
                await bot.delete_message(storage["telegram_channel_id"], copied_id)
            except Exception:  # noqa: BLE001 - cleanup failure must not hide the original DB failure
                log.warning(
                    "Orphan cleanup failed for channel_message_id=%s", copied_id
                )
        await message.answer(
            "⚠️ Indeksni yaratib bo‘lmadi. Operatsiya bekor qilindi; qayta urinib ko‘ring."
        )
        return
    if await super_backup_allowed(message.from_user.id):
        backup_config = await db.super_backup_config()
        if backup_config["super_backup_enabled"] and backup_config[
            "super_backup_channel_id"
        ]:
            await db.enqueue_super_backup(message.from_user.id, row["id"])
    if storage["index_message_enabled"]:
        tags = " ".join(f"#T_{tag}" for tag in row["tags"])
        type_summary = kinds_text(row_file_kinds(row))
        try:
            await bot.send_message(
                storage["telegram_channel_id"],
                f"🗂 KEEPGRAM INDEX\n#C_{row['code']}\n📝 {esc(row['title'])}\n"
                f"🧩 {esc(type_summary)} · {row['item_count']} ta\n"
                f"#K_{esc(row['catalog'])} {tags}",
            )
        except Exception:  # noqa: BLE001 - optional index must never fail a successful save
            log.warning("Optional channel index message failed")
    await message.answer(
        f"✅ <b>Saqlandi</b>\n\n📝 {esc(row['title'])}\n🔢 Kod: <code>{row['code']}</code>\n"
        f"🧩 Turi: {esc(kinds_text(row_file_kinds(row)))}\n📦 Tarkib: {row['item_count']} ta\n"
        f"🗂 Katalog: {esc(row['catalog'])}\n🏷 Teglar: yo‘q",
        reply_markup=file_actions(row["id"], row["is_favorite"]),
    )


MEDIA_GROUP_SETTLE_SECONDS = 2.0
REDIS_ALBUM_QUEUE = "keepgram:albums:due"
album_buffers: dict[tuple[int, str], list[Message]] = {}
album_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
album_lock = asyncio.Lock()
redis_album_worker_task: asyncio.Task[None] | None = None


async def flush_media_group(key: tuple[int, str]) -> None:
    messages: list[Message] = []
    try:
        await asyncio.sleep(MEDIA_GROUP_SETTLE_SECONDS)
        async with album_lock:
            if album_tasks.get(key) is not asyncio.current_task():
                return
            messages = album_buffers.pop(key, [])
            album_tasks.pop(key, None)
        if messages:
            await save_messages(messages)
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("Media group save failed")
        messages = messages or album_buffers.pop(key, [])
        album_tasks.pop(key, None)
        if messages:
            await messages[0].answer(
                "⚠️ Fayllar to‘plamini saqlashda xatolik. Qayta urinib ko‘ring."
            )


async def queue_media_group(message: Message) -> None:
    if redis_client:
        album_key = (
            f"keepgram:album:{message.from_user.id}:{str(message.media_group_id)}"
        )
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(
                album_key,
                str(message.message_id),
                message.model_dump_json(exclude_none=True),
            )
            pipe.expire(album_key, 900)
            pipe.zadd(
                REDIS_ALBUM_QUEUE,
                {album_key: time.time() + MEDIA_GROUP_SETTLE_SECONDS},
            )
            await pipe.execute()
        return
    key = (message.from_user.id, str(message.media_group_id))
    async with album_lock:
        buffered = album_buffers.setdefault(key, [])
        if all(item.message_id != message.message_id for item in buffered):
            buffered.append(message)
        previous = album_tasks.get(key)
        if previous:
            previous.cancel()
        album_tasks[key] = asyncio.create_task(flush_media_group(key))


async def redis_album_worker() -> None:
    if not redis_client:
        return
    while True:
        try:
            due_keys = await redis_client.zrangebyscore(
                REDIS_ALBUM_QUEUE, min="-inf", max=time.time(), start=0, num=10
            )
            for album_key in due_keys:
                lock_key = f"{album_key}:lock"
                lock_token = secrets.token_urlsafe(16)
                acquired = await redis_client.set(lock_key, lock_token, nx=True, ex=60)
                if not acquired:
                    continue
                try:
                    score = await redis_client.zscore(REDIS_ALBUM_QUEUE, album_key)
                    if score is None or float(score) > time.time():
                        continue
                    serialized = list((await redis_client.hgetall(album_key)).values())
                    messages = [
                        Message.model_validate_json(item, context={"bot": bot})
                        for item in serialized
                    ]
                    if messages:
                        await save_messages(messages)
                    async with redis_client.pipeline(transaction=True) as pipe:
                        pipe.delete(album_key)
                        pipe.zrem(REDIS_ALBUM_QUEUE, album_key)
                        await pipe.execute()
                finally:
                    await redis_client.eval(
                        "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",
                        1,
                        lock_key,
                        lock_token,
                    )
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Redis album worker iteration failed")
            await asyncio.sleep(2)


async def save_message(message: Message) -> None:
    if message.media_group_id:
        await queue_media_group(message)
        return
    await save_messages([message])


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    start_arg = ""
    if message.text and len(message.text.split(maxsplit=1)) == 2:
        start_arg = message.text.split(maxsplit=1)[1].strip()
    user = await actor_user(
        message, allow_incomplete=True, allow_language_missing=True
    )
    if not user:
        if start_arg.startswith("share_"):
            await message.answer(
                "🔐 Ulashilgan faylni olish uchun ro‘yxatdan o‘tishni yakunlang, so‘ng havolani yana oching."
            )
        return
    if not user["preferred_language"]:
        await state.update_data(pending_start=start_arg)
        await send_language_picker(message)
        return
    if start_arg.startswith("share_"):
        shared = await db.consume_share(start_arg[6:])
        if not shared:
            await message.answer("⚠️ Bu ulashish havolasi eskirgan, bekor qilingan yoki ishlatib bo‘lingan.")
            return
        try:
            await bot.copy_messages(
                chat_id=message.chat.id,
                from_chat_id=shared["telegram_channel_id"],
                message_ids=list(shared["message_ids"]),
            )
            await message.answer(
                f"🔗 <b>{esc(shared['title'])}</b> yuborildi · <code>{esc(shared['code'])}</code>"
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            await db.refund_share(shared["id"])
            await message.answer("⚠️ Faylni yuborib bo‘lmadi. Havola egasidan yangisini so‘rang.")
        return
    if not user["onboarding_completed"] or not terms_are_current(user):
        if not user["display_name"]:
            await state.set_state(Flow.onboarding_name)
            await message.answer(
                "👋 <b>KeepGram’ga xush kelibsiz!</b>\n\n"
                "Ro‘yxatdan o‘tish uchun ismingiz va Telegram orqali tasdiqlangan telefon raqamingiz kerak. "
                "Bu ma’lumotlar hisobingizni aniqlash va xavfsiz boshqarish uchun saqlanadi.\n\n"
                "👤 <b>Ismingizni yozib yuboring:</b>",
                reply_markup=ReplyKeyboardRemove(),
            )
        elif not user["phone"]:
            await message.answer(
                f"Salom, <b>{esc(user['display_name'])}</b>! Endi telefon raqamingizni tasdiqlang.",
                reply_markup=ONBOARDING_PHONE_KEYBOARD,
            )
        else:
            await send_terms(message)
        return
    storage = await db.storage_by_tg(message.from_user.id)
    text = (
        "👋 <b>Assalomu alaykum! Men KeepGram — shaxsiy Telegram fayl omboringizman.</b>\n\n"
        "📦 Fayllarni o‘zingizning shaxsiy kanalingizda saqlayman\n"
        "🔎 Nomi, katalogi yoki tegi orqali topaman\n"
        "🔢 Maxsus kod bilan bir zumda qaytaraman"
    )
    rows = [[("ℹ️ Qanday ishlaydi?", "info:how"), ("🔐 Maxfiylik", "info:privacy")]]
    if not storage:
        text += "\n\nBoshlash uchun shaxsiy kanalingizni ulang 👇"
        rows.insert(0, [("🔗 Kanalni ulash", "channel:link")])
    else:
        text += f"\n\n✅ Ulangan kanal: <b>{esc(storage['channel_title'])}</b>"
    await message.answer(text, reply_markup=ikb(rows))
    await message.answer("Asosiy menyu:", reply_markup=MAIN_MENU)


@router.callback_query(F.data.startswith("lang:set:"))
async def language_set(callback: CallbackQuery, state: FSMContext) -> None:
    language = callback.data.rsplit(":", 1)[1]
    if language not in SUPPORTED_LANGUAGES:
        await callback.answer("Invalid language", show_alert=True)
        return
    user = await db.set_user_language(callback.from_user.id, language)
    if not user:
        await db.upsert_user(callback.from_user)
        user = await db.set_user_language(callback.from_user.id, language)
    if not user:
        await callback.answer("Could not save language", show_alert=True)
        return
    language_cache[callback.from_user.id] = (time.monotonic(), language)
    actor_user_cache.pop(callback.from_user.id, None)
    token = current_language.set(language)
    try:
        labels = {
            "uz": "✅ Til O‘zbekchaga o‘zgartirildi.",
            "en": "✅ Language changed to English.",
            "ru": "✅ Язык изменён на русский.",
        }
        await callback.answer(labels[language], show_alert=True)
        if not user["display_name"]:
            await state.set_state(Flow.onboarding_name)
            prompts = {
                "uz": "👋 <b>KeepGram’ga xush kelibsiz!</b>\n\n👤 Ro‘yxatdan o‘tish uchun ismingizni yozib yuboring:",
                "en": "👋 <b>Welcome to KeepGram!</b>\n\n👤 Enter your name to register:",
                "ru": "👋 <b>Добро пожаловать в KeepGram!</b>\n\n👤 Для регистрации введите ваше имя:",
            }
            await callback.message.answer(
                prompts[language], reply_markup=ReplyKeyboardRemove()
            )
        elif not user["phone"]:
            prompt = {
                "uz": "📱 Endi telefon raqamingizni tasdiqlang.",
                "en": "📱 Now verify your phone number.",
                "ru": "📱 Теперь подтвердите номер телефона.",
            }[language]
            await callback.message.answer(
                prompt, reply_markup=ONBOARDING_PHONE_KEYBOARD
            )
        elif not user["onboarding_completed"] or not terms_are_current(user):
            await send_terms(callback.message)
        else:
            await callback.message.answer(labels[language], reply_markup=MAIN_MENU)
    finally:
        current_language.reset(token)


@router.message(
    Flow.onboarding_name,
    F.text,
    ~F.text.startswith("/"),
    F.chat.type == ChatType.PRIVATE,
)
async def onboarding_name(message: Message, state: FSMContext) -> None:
    if not await actor_user(message, allow_incomplete=True):
        return
    name = re.sub(r"[\x00-\x1f]", "", message.text).strip()
    name = re.sub(r"\s+", " ", name)
    if not 2 <= len(name) <= 80 or not any(char.isalpha() for char in name):
        await message.answer(
            "Ism 2–80 belgidan iborat bo‘lsin va kamida bitta harf qatnashsin. Qayta kiriting:"
        )
        return
    user = await db.update_onboarding_name(message.from_user.id, name)
    if not user:
        await message.answer(
            "Ro‘yxatdan o‘tishda xatolik. /start orqali qayta urinib ko‘ring."
        )
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"✅ Rahmat, <b>{esc(name)}</b>.\n\n"
        "📱 Endi pastdagi tugmani bosib o‘zingizning Telegram telefon raqamingizni yuboring. "
        "Boshqa kontakt yoki qo‘lda yozilgan raqam qabul qilinmaydi.",
        reply_markup=ONBOARDING_PHONE_KEYBOARD,
    )


@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await actor_user(message):
        return
    await message.answer("KeepGram asosiy menyusi:", reply_markup=MAIN_MENU)


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("ℹ️ Yordam")), F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message) -> None:
    if not await actor_user(message):
        return
    await message.answer(
        "<b>KeepGram yordam</b>\n\n"
        "1. /channel orqali shaxsiy kanalingizni ulang.\n"
        "2. Fayl, rasm, video yoki audioni botga yuboring. Bir martada tanlangan albom bitta to‘plam bo‘lib saqlanadi.\n"
        "3. Bot bergan 6 belgili kodni saqlab qo‘ying.\n"
        "4. Kodni yuboring yoki 🔎 Qidirish orqali faylni toping.\n\n"
        "/recent — oxirgilari\n/all — barcha saqlanganlar menyusi\n/trash — 30 kunlik savat\n"
        "/catalogs — kataloglar\n/tags — teglar\n/views — saqlangan qidiruvlar\n"
        "/reminders — faol eslatmalar\n/settings — sozlamalar va til\n"
        "/stats — fayllar soni va hajmi\n/backup — tiklash manifesti\n/restore — manifestni tiklash\n"
        "/mydata — saqlangan metadata\n/delete_my_data — metadata hisobini o‘chirish\n/privacy — maxfiylik\n/cancel — amalni bekor qilish"
    )


@router.message(Command("privacy"), F.chat.type == ChatType.PRIVATE)
async def cmd_privacy(message: Message) -> None:
    if not await actor_user(message, allow_incomplete=True):
        return
    await message.answer(
        "🔐 <b>Maxfiylik va backup</b>\n\nFayllar siz ulagan Telegram kanalida saqlanadi. "
        "Bazaga nom, kod, katalog, teg, kanal ID va xabar ID kabi indeks metadata yoziladi. "
        "Ism va telefon raqami ro‘yxatdan o‘tish uchun majburiy; telefon faqat o‘zingiz Telegram kontakt tugmasi orqali tasdiqlaganingizda saqlanadi. "
        "Foydalanish shartlariga rozilik berganingizdan keyin botga yuborgan fayllaringiz, admin backupni yoqqan bo‘lsa, "
        "avariya holatida tiklash uchun administrator boshqaradigan alohida Telegram backup kanaliga ham nusxalanadi. "
        "Rozilik sanasi va shartlar versiyasi bazada qayd etiladi. Kanal va Telegram hisobingiz xavfsizligi sizning nazoratingizda."
    )


@router.callback_query(F.data == "info:how")
async def info_how(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await callback.answer()
    await callback.message.answer(
        "1️⃣ Shaxsiy kanal yarating.\n2️⃣ Botni kanalga xabar joylash huquqi bilan admin qiling.\n"
        "3️⃣ KeepGram bergan LINK kodini kanalga yuboring.\n4️⃣ Keyin fayllarni botga yuboravering."
    )


@router.callback_query(F.data == "info:privacy")
async def info_privacy(callback: CallbackQuery) -> None:
    if not await actor_user(callback, allow_incomplete=True):
        return
    await callback.answer()
    await callback.message.answer(
        "Fayllar Telegram ichida nusxalanadi va kichik metadata indeksi ishlatiladi. "
        "Foydalanish shartlariga rozilik bergan foydalanuvchining fayllari, backup yoqilgan bo‘lsa, "
        "tiklash uchun administrator boshqaradigan alohida Telegram kanaliga ham nusxalanadi."
    )


@router.message(Command("channel"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("kanal"), F.chat.type == ChatType.PRIVATE)
async def cmd_channel(message: Message) -> None:
    if not await actor_user(message):
        return
    storage = await db.storage_by_tg(message.from_user.id)
    if not storage:
        await message.answer(
            "Sizda hozir kanal ulanmagan.",
            reply_markup=ikb([[("🔗 Kanalni ulash", "channel:link")]]),
        )
        return
    try:
        chat = await bot.get_chat(storage["telegram_channel_id"])
        member = await bot.get_chat_member(storage["telegram_channel_id"], bot.id)
        active = (
            member.status
            in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }
            and getattr(member, "can_post_messages", True) is not False
        )
        await db.refresh_channel(
            message.from_user.id, chat.title or "Nomsiz kanal", chat.username, active
        )
    except Exception:  # noqa: BLE001 - Telegram transport/API failures all invalidate the channel
        active = False
        await db.mark_channel_inactive(message.from_user.id)
    await message.answer(
        f"🔗 <b>Storage kanal</b>\n\nNomi: {esc(storage['channel_title'])}\n"
        f"ID: <code>{storage['telegram_channel_id']}</code>\nHolati: {'✅ faol' if active else '⚠️ aloqa yo‘q'}\n"
        f"Ulangan: {storage['linked_at'].strftime('%d.%m.%Y')}",
        reply_markup=ikb(
            [
                [
                    ("🔄 Almashtirish", "channel:replace"),
                    ("🔌 Uzish", "channel:disconnect"),
                ]
            ]
        ),
    )


@router.callback_query(F.data == "channel:link")
async def channel_link(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    if await db.storage_by_tg(callback.from_user.id):
        await callback.answer(
            "Avval mavjud kanalni uzing yoki almashtiring.", show_alert=True
        )
        return
    token = await db.create_link_token(callback.from_user.id)
    me = await bot.get_me()
    await callback.answer()
    await callback.message.answer(
        "🔗 <b>Kanalni ulash — 3 qadam</b>\n\n"
        "1️⃣ Yopiq shaxsiy Telegram kanal yarating.\n"
        f"2️⃣ @{esc(me.username)} botini kanalga <b>ADMIN</b> qiling va xabar joylash huquqini bering.\n"
        "3️⃣ Quyidagi bir martalik kodni kanalga oddiy xabar sifatida yuboring:\n\n"
        f"<code>{token}</code>\n\n⏳ Kod 15 daqiqa amal qiladi. Uni boshqa odamga bermang."
    )


@router.callback_query(F.data == "channel:replace")
async def channel_replace(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await callback.answer()
    await callback.message.answer(
        "⚠️ Eski kanal bilan bog‘lanish va uning KeepGram indeksi o‘chadi. Fayllarning o‘zi Telegram kanalida qoladi.",
        reply_markup=ikb(
            [[("✅ Almashtirish", "channel:replace_yes"), ("❌ Bekor", "noop")]]
        ),
    )


@router.callback_query(F.data == "channel:replace_yes")
async def channel_replace_yes(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await db.disconnect_channel(callback.from_user.id)
    token = await db.create_link_token(callback.from_user.id)
    me = await bot.get_me()
    await callback.answer()
    await callback.message.answer(
        f"Eski bog‘lanish uzildi. @{esc(me.username)} botini yangi kanalga admin qilib, shu kodni kanalga yuboring:\n\n<code>{token}</code>"
    )


@router.callback_query(F.data == "channel:disconnect")
async def channel_disconnect(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await callback.answer()
    await callback.message.answer(
        "Bog‘lanish va metadata indeksi o‘chadi; Telegram kanaldagi fayllar qoladi. Davom etilsinmi?",
        reply_markup=ikb(
            [[("✅ Ha, uzilsin", "channel:disconnect_yes"), ("❌ Yo‘q", "noop")]]
        ),
    )


@router.callback_query(F.data == "channel:disconnect_yes")
async def channel_disconnect_yes(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    removed = await db.disconnect_channel(callback.from_user.id)
    await callback.answer("Uzildi" if removed else "Kanal topilmadi", show_alert=True)
    await callback.message.answer(
        "🔌 Kanal bog‘lanishi uzildi. Telegram kanaldagi fayllaringiz o‘chirilmagan."
    )


@router.message(Command("disconnect"), F.chat.type == ChatType.PRIVATE)
async def disconnect_command(message: Message) -> None:
    if not await actor_user(message):
        return
    if not await db.storage_by_tg(message.from_user.id):
        await message.answer("Sizda ulangan kanal yo‘q.")
        return
    await message.answer(
        "Bog‘lanish va metadata indeksi o‘chadi; Telegram kanaldagi fayllar qoladi. Davom etilsinmi?",
        reply_markup=ikb(
            [[("✅ Ha, uzilsin", "channel:disconnect_yes"), ("❌ Yo‘q", "noop")]]
        ),
    )


@router.channel_post(F.text.regexp(LINK_RE))
async def channel_post_link(message: Message) -> None:
    token = (message.text or "").strip().upper()
    owner = await db.token_owner(token)
    if not owner:
        return
    try:
        member = await bot.get_chat_member(message.chat.id, bot.id)
        if member.status not in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            await bot.send_message(
                owner["telegram_id"], "⚠️ Bot kanalga admin qilinmagan."
            )
            return
        can_post = getattr(member, "can_post_messages", True)
        if can_post is False:
            await bot.send_message(
                owner["telegram_id"], "⚠️ Botga kanalda xabar joylash huquqini bering."
            )
            return
        telegram_id = await db.attach_channel(
            token,
            message.chat.id,
            message.chat.title or "Nomsiz kanal",
            message.chat.username,
        )
        await bot.send_message(
            telegram_id,
            f"✅ <b>Kanal muvaffaqiyatli ulandi!</b>\n\n📡 Kanal: {esc(message.chat.title)}\nEndi botga fayl yuborishingiz mumkin.",
            reply_markup=MAIN_MENU,
        )
        await message.answer(
            "🔐 KeepGram ulandi. Bu kanal sizning shaxsiy fayl omboringiz sifatida ishlatiladi."
        )
        if getattr(member, "can_delete_messages", False):
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception as exc:  # noqa: BLE001 - token is already consumed; deletion is best effort
                log.debug(
                    "Consumed link token message could not be deleted: %s",
                    exc.__class__.__name__,
                )
    except ValueError as exc:
        await bot.send_message(owner["telegram_id"], f"⚠️ {esc(exc)}")
    except Exception:
        log.exception("Channel link failed")
        await bot.send_message(
            owner["telegram_id"],
            "⚠️ Kanalni ulashda xatolik. Bot admin huquqlarini tekshiring.",
        )


@router.message(F.text.in_(menu_variants("📥 Saqlash")), F.chat.type == ChatType.PRIVATE)
async def begin_save(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    if not await db.storage_by_tg(message.from_user.id):
        await message.answer(
            "⚠️ Avval kanalni ulang.",
            reply_markup=ikb([[("🔗 Kanalni ulash", "channel:link")]]),
        )
        return
    await state.set_state(Flow.save_text)
    await message.answer(
        "Fayl, rasm, video, audio yoki saqlamoqchi bo‘lgan matnni yuboring. "
        "Bir martada bir nechta fayl tanlasangiz, ular bitta to‘plam bo‘lib saqlanadi. /cancel — bekor qilish."
    )


@router.message(
    Flow.save_text, F.text, ~F.text.startswith("/"), F.chat.type == ChatType.PRIVATE
)
async def save_text_state(message: Message, state: FSMContext) -> None:
    await state.clear()
    await save_message(message)


@router.message(
    Flow.restore_manifest,
    F.document,
    F.chat.type == ChatType.PRIVATE,
)
async def restore_manifest_finish(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    if (message.document.file_size or 0) > 2 * 1024 * 1024:
        await message.answer("⚠️ Manifest 2 MB dan katta bo‘lmasligi kerak.")
        return
    if not (message.document.file_name or "").lower().endswith(".json"):
        await message.answer("⚠️ KeepGram yaratgan JSON manifest faylini yuboring.")
        return
    destination = io.BytesIO()
    try:
        await bot.download(message.document, destination=destination)
        payload = verify_manifest_bytes(destination.getvalue(), message.from_user.id)
        incoming_files = sum(
            len(saved.get("parts") or []) for saved in payload.get("files", [])[:1000]
        )
        incoming_size = sum(
            int(part.get("file_size") or 0)
            for saved in payload.get("files", [])[:1000]
            for part in (saved.get("parts") or [])[:100]
        )
        usage = await db.user_usage(message.from_user.id)
        if int(usage["files"] if usage else 0) + incoming_files > settings.max_files_per_user:
            raise ValueError("Manifest tiklanganda fayllar limiti oshib ketadi")
        if (
            int(usage["total_size"] if usage else 0) + incoming_size
            > settings.max_total_size_mb * 1024 * 1024
        ):
            raise ValueError("Manifest tiklanganda umumiy hajm limiti oshib ketadi")
        restored, skipped = await db.restore_manifest_files(
            message.from_user.id, payload
        )
    except ValueError as exc:
        await message.answer(f"⚠️ {esc(exc)}")
        return
    except Exception:
        log.exception("Manifest restore failed")
        await message.answer("⚠️ Manifestni tiklab bo‘lmadi.")
        return
    await state.clear()
    await message.answer(
        f"✅ Manifest tiklandi: {restored} ta yozuv qo‘shildi, {skipped} ta mavjud yoki yaroqsiz yozuv o‘tkazib yuborildi."
    )


@router.message(
    Flow.replace_file,
    F.chat.type == ChatType.PRIVATE,
    F.content_type.in_(
        {
            "document",
            "photo",
            "video",
            "audio",
            "voice",
            "animation",
            "sticker",
            "video_note",
        }
    ),
)
async def replace_file_finish(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    if message.media_group_id:
        await message.answer(
            "⚠️ Almashtirishda hozircha faqat bitta yangi fayl yuboring. /cancel — bekor qilish."
        )
        return
    data = await state.get_data()
    target = await db.file_by_id(message.from_user.id, data.get("replace_file_id", ""))
    if not target:
        await state.clear()
        await message.answer("Almashtiriladigan yozuv topilmadi.")
        return
    part = content_metadata(message)
    unique_id = str(part["file_unique_id"] or "")
    duplicate = await db.find_duplicate(
        message.from_user.id, [unique_id] if unique_id else []
    )
    if duplicate and str(duplicate["id"]) != str(target["id"]):
        await message.answer(
            f"♻️ Bu fayl boshqa yozuvda mavjud: <b>{esc(duplicate['title'])}</b> · "
            f"<code>{duplicate['code']}</code>"
        )
        return
    usage = await db.user_usage(message.from_user.id)
    new_total = (
        int(usage["total_size"] if usage else 0)
        - int(target["file_size"] or 0)
        + int(part["file_size"] or 0)
    )
    if new_total > settings.max_total_size_mb * 1024 * 1024:
        await message.answer("⚠️ Yangi fayl umumiy hajm limitidan oshib ketadi.")
        return
    storage = await db.storage_by_tg(message.from_user.id)
    if not storage or not storage["is_active"]:
        await message.answer("⚠️ Storage kanal faol emas. /channel orqali tekshiring.")
        return
    try:
        copied = await bot.copy_message(
            chat_id=storage["telegram_channel_id"],
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await db.mark_channel_inactive(message.from_user.id)
        await message.answer("⚠️ Yangi faylni storage kanalga nusxalab bo‘lmadi.")
        return
    part["channel_message_id"] = copied.message_id
    try:
        updated, old_message_ids = await db.replace_file_content(
            message.from_user.id, str(target["id"]), [part]
        )
    except Exception:
        log.exception("File replacement failed")
        try:
            await bot.delete_message(storage["telegram_channel_id"], copied.message_id)
        except Exception:  # noqa: BLE001 - replacement rollback is best effort
            log.warning("Replacement rollback failed")
        await message.answer("⚠️ Faylni almashtirib bo‘lmadi. Qayta urinib ko‘ring.")
        return
    await update_super_backup_status(message.from_user.id, target["id"], "replaced")
    if await super_backup_allowed(message.from_user.id):
        backup_config = await db.super_backup_config()
        if backup_config["super_backup_enabled"] and backup_config[
            "super_backup_channel_id"
        ]:
            await db.enqueue_super_backup(message.from_user.id, updated["id"])
            await flush_pending_super_backup(message.from_user.id, updated["id"])
    for old_message_id in old_message_ids:
        try:
            await bot.delete_message(storage["telegram_channel_id"], old_message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            log.warning("Old replaced message could not be removed: %s", old_message_id)
    await state.clear()
    await message.answer(
        f"✅ <b>{esc(updated['title'])}</b> faylining tarkibi almashtirildi. "
        f"Kod o‘zgarmadi: <code>{updated['code']}</code>"
    )


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.content_type.in_(
        {
            "document",
            "photo",
            "video",
            "audio",
            "voice",
            "animation",
            "sticker",
            "video_note",
            "location",
            "venue",
        }
    ),
)
async def save_media(message: Message, state: FSMContext) -> None:
    await state.clear()
    await save_message(message)


@router.message(F.contact, F.chat.type == ChatType.PRIVATE)
async def save_contact_or_phone(message: Message, state: FSMContext) -> None:
    user = await actor_user(message, allow_incomplete=True)
    if not user:
        return
    if message.contact and message.contact.user_id == message.from_user.id:
        if not user["display_name"]:
            await state.set_state(Flow.onboarding_name)
            await message.answer(
                "Avval ismingizni yozib yuboring:", reply_markup=ReplyKeyboardRemove()
            )
            return
        if not user["onboarding_completed"] or not terms_are_current(user):
            saved = await db.save_onboarding_phone(
                message.from_user.id, message.contact.phone_number
            )
            if not saved:
                await message.answer(
                    "Ro‘yxatdan o‘tishni yakunlab bo‘lmadi. /start orqali qayta urinib ko‘ring."
                )
                return
            await state.clear()
            await message.answer(
                "✅ Telefon raqamingiz tasdiqlandi. Endi foydalanish shartlarini o‘qib chiqing.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await send_terms(message)
            return
        await db.update_phone(message.from_user.id, message.contact.phone_number)
        await state.clear()
        await message.answer(
            "✅ Telefon raqamingiz yangilandi.",
            reply_markup=MAIN_MENU,
        )
    else:
        if not user["onboarding_completed"] or not terms_are_current(user):
            await message.answer(
                "⚠️ Faqat o‘zingizning telefon raqamingizni pastdagi maxsus tugma orqali yuboring.",
                reply_markup=ONBOARDING_PHONE_KEYBOARD,
            )
            return
        await state.clear()
        await save_message(message)


@router.callback_query(F.data == "terms:accept")
async def accept_terms(callback: CallbackQuery, state: FSMContext) -> None:
    user = await actor_user(callback, allow_incomplete=True)
    if not user:
        return
    if user["onboarding_completed"] and terms_are_current(user):
        await callback.answer("Siz shartlarga allaqachon rozilik bergansiz.", show_alert=True)
        return
    if not user["display_name"] or not user["phone"]:
        await callback.answer(
            "Avval ism va telefon raqamingizni kiriting.", show_alert=True
        )
        return
    accepted = await db.accept_terms(callback.from_user.id, TERMS_VERSION)
    if not accepted:
        await callback.answer("Rozilikni saqlab bo‘lmadi.", show_alert=True)
        return
    await db.audit(
        "user",
        str(callback.from_user.id),
        "accept_terms",
        "terms",
        TERMS_VERSION,
        {"backup_consent": True},
    )
    backup_config = await db.super_backup_config()
    if backup_config["super_backup_enabled"] and backup_config[
        "super_backup_channel_id"
    ]:
        await db.enqueue_existing_consented_backups(
            TERMS_VERSION, int(backup_config["super_backup_channel_id"])
        )
    await state.clear()
    await callback.answer("Roziligingiz saqlandi.", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.message.answer(
        "✅ <b>Ro‘yxatdan o‘tish yakunlandi!</b>\n\nEndi KeepGram’dan foydalanishingiz mumkin.",
        reply_markup=MAIN_MENU,
    )
    if not await db.storage_by_tg(callback.from_user.id):
        await callback.message.answer(
            "Boshlash uchun shaxsiy Telegram kanalingizni ulang:",
            reply_markup=ikb([[('🔗 Kanalni ulash', 'channel:link')]]),
        )


@router.message(Command("recent"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("🕘 Oxirgilari")), F.chat.type == ChatType.PRIVATE)
@router.message(Command("fayllarim"), F.chat.type == ChatType.PRIVATE)
async def recent_files(message: Message) -> None:
    if not await actor_user(message):
        return
    await show_files(message)


async def show_trash(message: Message, telegram_id: int, page: int = 1, *, edit: bool = False) -> None:
    rows, total = await db.trash_page(telegram_id, page)
    if not rows:
        text = "🗑 Savat bo‘sh. O‘chirilgan yozuvlar 30 kun shu yerda saqlanadi."
        if edit:
            try:
                await message.edit_text(text)
                return
            except TelegramBadRequest:
                pass
        await message.answer(text)
        return
    pages = max(1, (total + 7) // 8)
    page = min(page, pages)
    buttons = [
        [(f"↩️ {str(row['title'])[:35]}", f"trash:restore:{row['id']}")]
        for row in rows
    ]
    nav: list[tuple[str, str]] = []
    if page > 1:
        nav.append(("⬅️", f"trash:page:{page-1}"))
    nav.append((f"{page}/{pages}", "noop"))
    if page < pages:
        nav.append(("➡️", f"trash:page:{page+1}"))
    buttons.append(nav)
    text = f"🗑 <b>Savat</b> · {total} ta\n30 kun ichida qaytarish mumkin. Faylni qaytarish uchun ustiga bosing."
    if edit:
        try:
            await message.edit_text(text, reply_markup=ikb(buttons))
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=ikb(buttons))


@router.message(Command("trash"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("🗑 Savat")), F.chat.type == ChatType.PRIVATE)
async def trash_menu(message: Message) -> None:
    if await actor_user(message):
        await show_trash(message, message.from_user.id)


@router.callback_query(F.data.startswith("trash:page:"))
async def trash_page_callback(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await callback.answer()
    await show_trash(callback.message, callback.from_user.id, max(1, int(callback.data.rsplit(":",1)[1])), edit=True)


@router.callback_query(F.data.startswith("trash:restore:"))
async def trash_restore(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    row = await db.restore_from_trash(callback.from_user.id, callback.data.rsplit(":",1)[1])
    if not row:
        await callback.answer("Yozuv topilmadi.", show_alert=True)
        return
    await update_super_backup_status(callback.from_user.id, row["id"], "active")
    await callback.answer("Savatdan qaytarildi", show_alert=True)
    await callback.message.answer(file_card(row), reply_markup=file_actions(row["id"], row["is_favorite"]))


@router.message(Command("all"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("📚 Barcha saqlanganlar")), F.chat.type == ChatType.PRIVATE)
async def all_files_inventory(message: Message) -> None:
    if not await actor_user(message):
        return
    await show_inventory(message, message.from_user.id)


@router.callback_query(F.data.startswith("inventory:"))
async def inventory_callback(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    try:
        page = max(1, int(callback.data.split(":", 1)[1]))
    except ValueError:
        await callback.answer("Noto‘g‘ri sahifa", show_alert=True)
        return
    await callback.answer()
    await show_inventory(callback.message, callback.from_user.id, page, edit=True)


@router.callback_query(F.data.startswith("bulk:start:"))
@router.callback_query(F.data.startswith("bulk:page:"))
async def bulk_start_or_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    page = max(1, int(callback.data.rsplit(":", 1)[1]))
    if callback.data.startswith("bulk:start:"):
        await state.update_data(bulk_selected=[])
    await callback.answer()
    await show_bulk_inventory(callback.message, callback.from_user.id, state, page)


@router.callback_query(F.data.startswith("bulk:toggle:"))
async def bulk_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    _, _, page_raw, file_id = callback.data.split(":", 3)
    if not await db.file_by_id(callback.from_user.id, file_id):
        await callback.answer("Yozuv topilmadi", show_alert=True)
        return
    data = await state.get_data()
    selected = list(dict.fromkeys(data.get("bulk_selected", [])))
    if file_id in selected:
        selected.remove(file_id)
    elif len(selected) < 50:
        selected.append(file_id)
    else:
        await callback.answer("Bir vaqtda ko‘pi bilan 50 ta tanlang", show_alert=True)
        return
    await state.update_data(bulk_selected=selected)
    await callback.answer(f"{len(selected)} ta tanlandi")
    await show_bulk_inventory(
        callback.message, callback.from_user.id, state, max(1, int(page_raw))
    )


async def selected_bulk_ids(
    callback: CallbackQuery, state: FSMContext
) -> list[str] | None:
    selected = list(dict.fromkeys((await state.get_data()).get("bulk_selected", [])))
    if not selected:
        await callback.answer("Avval kamida bitta yozuvni tanlang", show_alert=True)
        return None
    return selected


@router.callback_query(F.data == "bulk:tags")
async def bulk_tags_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback) or not await selected_bulk_ids(callback, state):
        return
    await state.set_state(Flow.bulk_tags)
    await callback.answer()
    await callback.message.answer(
        "Tanlangan yozuvlarning barchasiga qo‘shiladigan teglarni yuboring. "
        "Mavjud teglar saqlanadi. /cancel — bekor qilish."
    )


@router.message(Flow.bulk_tags, F.text, ~F.text.startswith("/"))
async def bulk_tags_finish(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    tags = normalize_tags(message.text)
    if not tags:
        await message.answer("Kamida bitta to‘g‘ri teg kiriting.")
        return
    selected = list((await state.get_data()).get("bulk_selected", []))
    changed = await db.bulk_add_tags(message.from_user.id, selected, tags)
    await state.clear()
    await message.answer(
        f"✅ {changed} ta yozuvga {' '.join('#' + tag for tag in tags)} qo‘shildi."
    )


@router.callback_query(F.data == "bulk:catalog")
async def bulk_catalog_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback) or not await selected_bulk_ids(callback, state):
        return
    catalogs = await db.catalogs(callback.from_user.id)
    buttons = [
        [(f"📁 {row['name']}", f"bulk:setcat:{row['name']}")]
        for row in catalogs
        if len(str(row["name"]).encode("utf-8")) <= 32
    ]
    await callback.answer()
    await callback.message.answer("Umumiy katalogni tanlang:", reply_markup=ikb(buttons))


@router.callback_query(F.data.startswith("bulk:setcat:"))
async def bulk_catalog_finish(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    selected = await selected_bulk_ids(callback, state)
    if not selected:
        return
    catalog = callback.data.split(":", 2)[2]
    changed = await db.bulk_set_catalog(callback.from_user.id, selected, catalog)
    await state.clear()
    await callback.answer(f"{changed} ta yozuv yangilandi", show_alert=True)


@router.callback_query(F.data == "bulk:delete")
async def bulk_delete_choose(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    selected = await selected_bulk_ids(callback, state)
    if not selected:
        return
    await callback.answer()
    await callback.message.answer(
        f"⚠️ {len(selected)} ta tanlangan yozuv qayerdan o‘chirilsin?",
        reply_markup=ikb(
            [
                [("🗑 30 kunlik savatga", "bulk:delete:index")],
                [("⚠️ Kanal + savat", "bulk:delete:all")],
                [("❌ Bekor", "bulk:done")],
            ]
        ),
    )


@router.callback_query(F.data.in_({"bulk:delete:index", "bulk:delete:all"}))
async def bulk_delete_finish(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    selected = await selected_bulk_ids(callback, state)
    if not selected:
        return
    deleted_ids: list[str] = []
    failed = 0
    delete_from_channel = callback.data.endswith(":all")
    for row in await db.files_by_ids(callback.from_user.id, selected):
        if not await flush_pending_super_backup(callback.from_user.id, row["id"]):
            failed += 1
            continue
        if delete_from_channel:
            try:
                for message_id in row["channel_message_ids"]:
                    await bot.delete_message(row["telegram_channel_id"], message_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                failed += 1
                continue
        deleted_ids.append(str(row["id"]))
    for file_id in deleted_ids:
        await update_super_backup_status(callback.from_user.id, file_id, "deleted")
    changed = await db.bulk_delete_files(callback.from_user.id, deleted_ids)
    await state.clear()
    await callback.answer("Ommaviy o‘chirish yakunlandi", show_alert=True)
    await callback.message.answer(
        f"✅ {changed} ta yozuv 30 kunlik savatga ko‘chirildi."
        + (f" ⚠️ {failed} tasini kanaldan o‘chirib bo‘lmadi." if failed else "")
    )


@router.callback_query(F.data == "bulk:done")
async def bulk_done(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Tanlash tugatildi")
    if await actor_user(callback):
        await show_inventory(callback.message, callback.from_user.id, edit=True)


@router.message(F.text.in_(menu_variants("⭐ Sevimlilar")), F.chat.type == ChatType.PRIVATE)
async def favorite_files(message: Message) -> None:
    if not await actor_user(message):
        return
    await show_files(message, favorite=True)


@router.message(Command("stats"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("📊 Statistika")), F.chat.type == ChatType.PRIVATE)
async def user_statistics(message: Message) -> None:
    if not await actor_user(message):
        return
    usage = await db.user_usage(message.from_user.id)
    files = int(usage["files"] if usage else 0)
    records = int(usage["records"] if usage else 0)
    total_size = int(usage["total_size"] if usage else 0)
    await message.answer(
        "📊 <b>KeepGram statistikangiz</b>\n\n"
        f"📦 Saqlangan fayllar: {files}/{settings.max_files_per_user}\n"
        f"🗂 Yozuv va to‘plamlar: {records}\n"
        f"💾 Umumiy hajm: {human_size(total_size)} / {settings.max_total_size_mb} MB\n\n"
        "Fayllar Render serverida emas, Telegram kanalingizda saqlanadi. Limitlar bot va indeksni tez saqlash uchun qo‘yilgan."
    )


@router.callback_query(F.data.startswith("list:"))
async def list_callback(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    _, mode, page_raw = callback.data.split(":", 2)
    await callback.answer()
    await show_files(callback.message, max(1, int(page_raw)), favorite=mode == "fav")


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("f:open:"))
async def open_file(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    row = await db.file_by_id(callback.from_user.id, callback.data.rsplit(":", 1)[1])
    if not row:
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        file_card(row), reply_markup=file_actions(row["id"], row["is_favorite"])
    )


@router.callback_query(F.data.startswith("f:get:"))
async def get_file(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    row = await db.file_by_id(callback.from_user.id, callback.data.rsplit(":", 1)[1])
    if not row:
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    await callback.answer("Yuborilmoqda…")
    await send_stored_file(callback.from_user.id, row)


@router.callback_query(F.data.startswith("f:replace:"))
async def replace_file_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    row = await db.file_by_id(callback.from_user.id, file_id)
    if not row:
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    await state.set_state(Flow.replace_file)
    await state.update_data(replace_file_id=file_id)
    await callback.answer()
    await callback.message.answer(
        f"♻️ <b>{esc(row['title'])}</b> o‘rniga qo‘yiladigan bitta yangi faylni yuboring. "
        "Nom, kod, teg va katalog saqlanib qoladi. /cancel — bekor qilish."
    )


@router.callback_query(F.data.startswith("f:ren:"))
async def rename_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    if not await db.file_by_id(callback.from_user.id, file_id):
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    await state.set_state(Flow.rename)
    await state.update_data(file_id=file_id)
    await callback.answer()
    await callback.message.answer(
        "Yangi nomni yuboring (1–120 belgi). /cancel — bekor qilish."
    )


@router.message(Flow.rename, F.text, ~F.text.startswith("/"))
async def rename_finish(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    title = message.text.strip()
    if not 1 <= len(title) <= 120:
        await message.answer("Nom 1–120 belgi bo‘lishi kerak.")
        return
    data = await state.get_data()
    row = await db.update_file(message.from_user.id, data["file_id"], "title", title)
    await state.clear()
    await message.answer(
        f"✅ Nomi yangilandi: <b>{esc(row['title'])}</b>" if row else "Fayl topilmadi."
    )


@router.callback_query(F.data.startswith("f:tag:"))
async def tags_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    row = await db.file_by_id(callback.from_user.id, file_id)
    if not row:
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    await state.set_state(Flow.tags)
    await state.update_data(file_id=file_id)
    await callback.answer()
    await callback.message.answer(
        "Teglarni vergul yoki bo‘shliq bilan yuboring. Tozalash uchun <code>-</code> yuboring."
    )


@router.message(Flow.tags, F.text, ~F.text.startswith("/"))
async def tags_finish(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    tags = [] if message.text.strip() == "-" else normalize_tags(message.text)
    data = await state.get_data()
    row = await db.update_file(message.from_user.id, data["file_id"], "tags", tags)
    await state.clear()
    await message.answer(
        f"✅ Teglar yangilandi: {' '.join('#' + t for t in tags) or 'yo‘q'}"
        if row
        else "Fayl topilmadi."
    )


@router.callback_query(F.data.startswith("f:fav:"))
async def favorite_toggle(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    row = await db.file_by_id(callback.from_user.id, file_id)
    if not row:
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    updated = await db.update_file(
        callback.from_user.id, file_id, "is_favorite", not row["is_favorite"]
    )
    await callback.answer("Sevimlilar yangilandi")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=file_actions(file_id, updated["is_favorite"])
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("f:share:"))
async def share_file(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    token = await db.create_share(callback.from_user.id, callback.data.rsplit(":", 1)[1])
    if not token:
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=share_{token}"
    await callback.answer()
    await callback.message.answer(
        "🔗 <b>Vaqtinchalik ulashish havolasi</b>\n\n"
        f"<code>{esc(link)}</code>\n\n"
        "Havola 24 soat ishlaydi va faqat bir marta foydalaniladi. Havolani faqat ishonchli odamga yuboring."
    )


@router.callback_query(F.data.startswith("f:versions:"))
async def file_versions_menu(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    rows = await db.file_versions(callback.from_user.id, callback.data.rsplit(":", 1)[1])
    if not rows:
        await callback.answer("Hali tayyor backup versiyasi yo‘q.", show_alert=True)
        return
    buttons = [[(f"📤 v{row['version']} · {row['status']}", f"ver:get:{row['id']}")] for row in rows]
    await callback.answer()
    await callback.message.answer(
        "🕘 <b>Fayl versiyalari</b>\nKerakli versiyani shaxsiy chatga olish uchun tanlang:",
        reply_markup=ikb(buttons),
    )


@router.callback_query(F.data.startswith("ver:get:"))
async def file_version_get(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    row = await db.user_backup_version(callback.from_user.id, callback.data.rsplit(":", 1)[1])
    if not row:
        await callback.answer("Versiya topilmadi.", show_alert=True)
        return
    await callback.answer("Yuborilmoqda…")
    try:
        await bot.copy_messages(
            chat_id=callback.from_user.id,
            from_chat_id=row["backup_channel_id"],
            message_ids=list(row["backup_message_ids"]),
        )
        await callback.message.answer(
            f"✅ <b>{esc(row['title'])}</b> · v{row['version']} tiklash nusxasi yuborildi."
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await callback.message.answer("⚠️ Backup versiyasini yuborib bo‘lmadi.")


@router.callback_query(F.data.startswith("f:rem:"))
async def reminder_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    if not await db.file_by_id(callback.from_user.id, file_id):
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    await state.set_state(Flow.reminder)
    await state.update_data(reminder_file_id=file_id)
    await callback.answer()
    await callback.message.answer(
        "⏰ Eslatma vaqtini Toshkent vaqti bilan yuboring:\n"
        "<code>2026-09-15 09:30 | Pasport muddatini tekshirish</code>\n\n"
        "Izoh ixtiyoriy. /cancel — bekor qilish."
    )


@router.message(Flow.reminder, F.text, ~F.text.startswith("/"))
async def reminder_finish(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    raw_time, _, note = message.text.partition("|")
    try:
        local_time = datetime.strptime(raw_time.strip(), "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("Asia/Tashkent")
        )
        remind_at = local_time.astimezone(timezone.utc)
    except ValueError:
        await message.answer("Format noto‘g‘ri. Masalan: <code>2026-09-15 09:30 | Izoh</code>")
        return
    if remind_at <= utcnow() + timedelta(minutes=1) or remind_at > utcnow() + timedelta(days=3650):
        await message.answer("Vaqt kamida 1 daqiqa keyin va 10 yil ichida bo‘lsin.")
        return
    data = await state.get_data()
    row = await db.add_reminder(message.from_user.id, data.get("reminder_file_id", ""), remind_at, note.strip())
    if not row:
        await message.answer("Fayl topilmadi.")
        await state.clear()
        return
    await state.clear()
    await message.answer(f"✅ Eslatma o‘rnatildi: {local_time.strftime('%d.%m.%Y %H:%M')} (Toshkent)")


@router.message(Command("reminders"), F.chat.type == ChatType.PRIVATE)
async def reminders_menu(message: Message) -> None:
    if not await actor_user(message):
        return
    rows = await db.user_reminders(message.from_user.id)
    if not rows:
        await message.answer("⏰ Faol eslatmalar yo‘q.")
        return
    lines = ["⏰ <b>Faol eslatmalar</b>"]
    buttons = []
    for row in rows:
        local = row["remind_at"].astimezone(ZoneInfo("Asia/Tashkent")).strftime("%d.%m.%Y %H:%M")
        lines.append(f"• <b>{esc(row['title'])}</b> · <code>{row['code']}</code> · {local}")
        buttons.append([(f"❌ {str(row['title'])[:30]}", f"rem:cancel:{row['id']}")])
    await message.answer("\n".join(lines), reply_markup=ikb(buttons))


@router.callback_query(F.data.startswith("rem:cancel:"))
async def reminder_cancel(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    removed = await db.cancel_reminder(callback.from_user.id, callback.data.rsplit(":", 1)[1])
    await callback.answer("Eslatma bekor qilindi" if removed else "Topilmadi", show_alert=True)


@router.callback_query(F.data.startswith("f:del:"))
async def delete_choose(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    if not await db.file_by_id(callback.from_user.id, file_id):
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(
        "Qayerdan o‘chirilsin?",
        reply_markup=ikb(
            [
                [("🗑 Savatga (qaytarish mumkin)", f"f:dx:{file_id}")],
                [("⚠️ Kanal fayli + savat", f"f:da:{file_id}")],
                [("❌ Bekor", "noop")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("f:dx:"))
async def delete_index(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    if not await flush_pending_super_backup(callback.from_user.id, file_id):
        await callback.answer(
            "Backup nusxasi yaratilmagani uchun o‘chirish to‘xtatildi.", show_alert=True
        )
        return
    await update_super_backup_status(callback.from_user.id, file_id, "deleted")
    removed = await db.delete_file(callback.from_user.id, file_id)
    await callback.answer("Savatga ko‘chirildi" if removed else "Topilmadi", show_alert=True)
    if removed:
        await callback.message.answer(
            "🗑 Yozuv 30 kunlik savatga ko‘chirildi.",
            reply_markup=ikb([[("↩️ Darhol qaytarish", f"trash:restore:{file_id}")]]),
        )


@router.callback_query(F.data.startswith("f:da:"))
async def delete_all(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    row = await db.file_by_id(callback.from_user.id, file_id)
    if not row:
        await callback.answer("Topilmadi", show_alert=True)
        return
    if not await flush_pending_super_backup(callback.from_user.id, file_id):
        await callback.answer(
            "Backup nusxasi yaratilmagani uchun o‘chirish to‘xtatildi.", show_alert=True
        )
        return
    for message_id in list(row["channel_message_ids"] or [row["channel_message_id"]]):
        try:
            await bot.delete_message(row["telegram_channel_id"], message_id)
        except TelegramBadRequest as exc:
            if "message to delete not found" not in str(exc).lower():
                await callback.answer(
                    "Botda o‘chirish huquqi yo‘q yoki Telegram rad etdi.",
                    show_alert=True,
                )
                return
        except TelegramForbiddenError:
            await callback.answer("Bot kanalda admin emas.", show_alert=True)
            return
    await update_super_backup_status(callback.from_user.id, file_id, "deleted")
    await db.delete_file(callback.from_user.id, file_id)
    await callback.answer("Kanal fayli o‘chirildi, indeks savatda", show_alert=True)


@router.callback_query(F.data.startswith("f:cat:"))
async def catalog_choose(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    file_id = callback.data.rsplit(":", 1)[1]
    if not await db.file_by_id(callback.from_user.id, file_id):
        await callback.answer("Fayl topilmadi.", show_alert=True)
        return
    rows = await db.catalogs(callback.from_user.id)
    buttons = [
        [(f"📁 {row['name']}", f"f:setcat:{file_id}:{row['name']}")]
        for row in rows
        if len(str(row["name"]).encode()) <= 16
    ]
    await callback.answer()
    await callback.message.answer("Katalogni tanlang:", reply_markup=ikb(buttons))


@router.callback_query(F.data.startswith("f:setcat:"))
async def catalog_set(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    _, _, file_id, catalog = callback.data.split(":", 3)
    row = await db.update_file(callback.from_user.id, file_id, "catalog", catalog)
    await callback.answer(
        "Katalog yangilandi" if row else "Fayl topilmadi", show_alert=True
    )


@router.message(Command("search"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("qidir"), F.chat.type == ChatType.PRIVATE)
async def command_search(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        await run_search(message, parts[1])
        return
    await state.set_state(Flow.search)
    await message.answer(
        "Fayl nomi, #teg, catalog:Nomi, type:pdf, type:excel, date:2026-09 yoki 6 belgili kodni yuboring."
    )


@router.message(
    F.text.in_(menu_variants("🔎 Qidirish") | menu_variants("🔢 Kod bo‘yicha")), F.chat.type == ChatType.PRIVATE
)
async def begin_search(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    await state.set_state(Flow.search)
    await message.answer(
        "6 belgili kodni yuboring."
        if message.text.startswith("🔢")
        else "Fayl nomi, #teg, catalog:Nomi, type:pdf yoki date:2026-09 yozing."
    )


async def run_search(message: Message, query: str) -> None:
    try:
        rows = await db.search_files(message.from_user.id, query)
    except ValueError as exc:
        await message.answer(f"⚠️ {esc(exc)}")
        return
    if not rows:
        await message.answer(f"🔎 “{esc(query[:100])}” bo‘yicha natija topilmadi.")
        return
    if len(rows) == 1 and CODE_RE.fullmatch(query.strip()):
        await send_stored_file(message.from_user.id, rows[0])
        return
    buttons = [
        [(f"{file_emoji(row['file_type'])} {row['title'][:40]}", f"f:open:{row['id']}")]
        for row in rows
    ]
    await message.answer(
        f"🔎 “{esc(query[:100])}” bo‘yicha {len(rows)} ta natija:",
        reply_markup=ikb(buttons),
    )


@router.message(Flow.search, F.text, ~F.text.startswith("/"))
async def search_state(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    await state.clear()
    await run_search(message, message.text)


@router.message(Command("saveview"), F.chat.type == ChatType.PRIVATE)
async def save_view_command(message: Message) -> None:
    if not await actor_user(message):
        return
    raw = (message.text or "").partition(" ")[2].strip()
    name, separator, query = raw.partition("|")
    name = re.sub(r"[^\w\- ]", "", name, flags=re.UNICODE).strip()[:32]
    query = query.strip()[:120]
    if not separator or not name or not query:
        await message.answer(
            "Saqlash formati:\n<code>/saveview PDF hujjatlar | type:pdf #muhim</code>"
        )
        return
    await db.save_view(message.from_user.id, name, query)
    await message.answer(f"✅ “{esc(name)}” qidiruvi saqlandi.")


@router.message(Command("views"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("🧠 Saqlangan qidiruvlar")), F.chat.type == ChatType.PRIVATE)
async def saved_views_menu(message: Message) -> None:
    if not await actor_user(message):
        return
    rows = await db.saved_views(message.from_user.id)
    if not rows:
        await message.answer(
            "🧠 Saqlangan qidiruvlar yo‘q. Masalan:\n"
            "<code>/saveview PDF hujjatlar | type:pdf #muhim</code>"
        )
        return
    buttons = [[(f"🔎 {row['name']}", f"view:run:{row['id']}"), ("🗑", f"view:del:{row['id']}")] for row in rows]
    await message.answer("🧠 <b>Saqlangan qidiruvlar</b>", reply_markup=ikb(buttons))


@router.callback_query(F.data.startswith("view:run:"))
async def saved_view_run(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    view_id = callback.data.rsplit(":", 1)[1]
    row = next((item for item in await db.saved_views(callback.from_user.id) if str(item["id"]) == view_id), None)
    if not row:
        await callback.answer("Qidiruv topilmadi.", show_alert=True)
        return
    await callback.answer()
    await run_search(callback.message, row["query"])


@router.callback_query(F.data.startswith("view:del:"))
async def saved_view_delete(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    removed = await db.delete_view(callback.from_user.id, callback.data.rsplit(":", 1)[1])
    await callback.answer("O‘chirildi" if removed else "Topilmadi", show_alert=True)


@router.message(Command("catalogs"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("🗂 Kataloglar")), F.chat.type == ChatType.PRIVATE)
async def catalogs_menu(message: Message) -> None:
    if not await actor_user(message):
        return
    rows = await db.catalogs(message.from_user.id)
    buttons = [
        [(f"📁 {r['name']}", f"catalog:view:{r['name']}")]
        for r in rows
        if len(str(r["name"]).encode()) <= 32
    ]
    buttons += [
        [("➕ Yangi katalog", "catalog:add"), ("🗑 O‘chirish", "catalog:delete")]
    ]
    await message.answer("🗂 Kataloglaringiz:", reply_markup=ikb(buttons))


@router.callback_query(F.data.startswith("catalog:view:"))
async def catalog_view(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    name = callback.data.split(":", 2)[2]
    await callback.answer()
    rows, total = await db.files_page(callback.from_user.id, catalog=name)
    if not rows:
        await callback.message.answer(f"📁 {esc(name)} katalogi bo‘sh.")
        return
    await callback.message.answer(
        f"📁 {esc(name)} — {total} ta:",
        reply_markup=ikb(
            [
                [
                    (
                        f"{file_emoji(r['file_type'])} {r['title'][:40]}",
                        f"f:open:{r['id']}",
                    )
                ]
                for r in rows
            ]
        ),
    )


@router.callback_query(F.data == "catalog:add")
async def catalog_add_begin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    await state.set_state(Flow.catalog_name)
    await state.update_data(catalog_action="add")
    await callback.answer()
    await callback.message.answer("Yangi katalog nomini yuboring (1–40 belgi).")


@router.callback_query(F.data == "catalog:delete")
async def catalog_delete_menu(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    rows = await db.catalogs(callback.from_user.id)
    buttons = [
        [(f"🗑 {r['name']}", f"catalog:del:{r['name']}")]
        for r in rows
        if r["name"] != "Umumiy" and len(str(r["name"]).encode()) <= 32
    ]
    await callback.answer()
    await callback.message.answer(
        "O‘chiriladigan katalogni tanlang. Fayllar Umumiy katalogiga o‘tadi.",
        reply_markup=ikb(buttons or [[("Katalog yo‘q", "noop")]]),
    )


@router.callback_query(F.data.startswith("catalog:del:"))
async def catalog_delete(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    name = callback.data.split(":", 2)[2]
    removed = await db.delete_catalog(callback.from_user.id, name)
    await callback.answer(
        "Katalog o‘chirildi" if removed else "O‘chirib bo‘lmadi", show_alert=True
    )


@router.message(Flow.catalog_name, F.text, ~F.text.startswith("/"))
async def catalog_add_finish(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    name = re.sub(r"[\x00-\x1f]", "", message.text).strip()[:16]
    if (
        not name
        or name.casefold() == "umumiy"
        or ":" in name
        or len(name.encode("utf-8")) > 16
    ):
        await message.answer(
            "Boshqa nom kiriting. Nom 1–16 bayt bo‘lsin va ':' belgisini ishlatmang."
        )
        return
    added = await db.add_catalog(message.from_user.id, name)
    await state.clear()
    await message.answer(
        "✅ Katalog yaratildi." if added else "Bu nomdagi katalog mavjud."
    )


@router.message(Command("tags"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("🏷 Teglar")), F.chat.type == ChatType.PRIVATE)
async def tags_menu(message: Message) -> None:
    if not await actor_user(message):
        return
    rows = await db.tags(message.from_user.id)
    if not rows:
        await message.answer(
            "🏷 Hozircha teglar yo‘q. Fayl kartasidagi “Teglar” tugmasidan qo‘shing."
        )
        return
    await message.answer(
        "🏷 Teglar:",
        reply_markup=ikb(
            [
                [(f"#{r['tag']} ({r['count']})", f"tag:view:{r['tag']}")]
                for r in rows
                if len(str(r["tag"]).encode()) < 40
            ]
        ),
    )


@router.callback_query(F.data.startswith("tag:view:"))
async def tag_view(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    tag = callback.data.split(":", 2)[2]
    rows, total = await db.files_page(callback.from_user.id, tag=tag)
    await callback.answer()
    buttons = [
        [(f"{file_emoji(r['file_type'])} {r['title'][:40]}", f"f:open:{r['id']}")]
        for r in rows
    ]
    await callback.message.answer(
        f"#{esc(tag)} — {total} ta fayl:",
        reply_markup=ikb(buttons or [[("Natija yo‘q", "noop")]]),
    )


@router.message(Command("settings"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("sozlamalar"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("⚙️ Sozlamalar")), F.chat.type == ChatType.PRIVATE)
async def settings_menu(message: Message) -> None:
    if not await actor_user(message):
        return
    setting = await db.setting(message.from_user.id)
    index_on = bool(setting and setting["index_message_enabled"])
    fav_on = bool(setting and setting["default_favorite"])
    manifest_on = bool(setting and setting["auto_manifest_enabled"])
    compact_on = bool(not setting or setting["compact_cards"])
    default_catalog = setting["default_catalog"] if setting else "Umumiy"
    language_name = {"uz": "O‘zbekcha", "en": "English", "ru": "Русский"}.get(
        current_language.get(), "O‘zbekcha"
    )
    await message.answer(
        "⚙️ <b>Sozlamalar</b>",
        reply_markup=ikb(
            [
                [("🔗 Kanal", "settings:channel"), ("📱 Telefon", "settings:phone")],
                [(f"#️⃣ Kanal indeksi: {'ON' if index_on else 'OFF'}", "settings:index")],
                [(f"🗂 Standart katalog: {default_catalog}", "settings:catalog")],
                [
                    (
                        f"⭐ Avto-sevimli: {'ON' if fav_on else 'OFF'}",
                        "settings:favorite",
                    )
                ],
                [
                    (
                        f"🛟 Avto-manifest: {'ON' if manifest_on else 'OFF'}",
                        "settings:manifest",
                    )
                ],
                [(f"🪪 Ixcham kartalar: {'ON' if compact_on else 'OFF'}", "settings:cards")],
                [(f"🌐 Til / Language: {language_name}", "settings:language")],
                [("🗑 Ma’lumotlarni o‘chirish", "settings:delete")],
            ]
        ),
    )


@router.callback_query(F.data == "settings:language")
async def settings_language(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await callback.answer()
    await send_language_picker(callback.message, changing=True)


@router.callback_query(F.data == "settings:channel")
async def settings_channel(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await callback.answer()
    storage = await db.storage_by_tg(callback.from_user.id)
    if not storage:
        await callback.message.answer(
            "Sizda hozir kanal ulanmagan.",
            reply_markup=ikb([[("🔗 Kanalni ulash", "channel:link")]]),
        )
        return
    await callback.message.answer(
        f"🔗 <b>Storage kanal</b>\n\nNomi: {esc(storage['channel_title'])}\n"
        f"ID: <code>{storage['telegram_channel_id']}</code>\n"
        f"Holati: {'✅ faol' if storage['is_active'] else '⚠️ aloqa yo‘q'}",
        reply_markup=ikb(
            [
                [
                    ("🔄 Almashtirish", "channel:replace"),
                    ("🔌 Uzish", "channel:disconnect"),
                ]
            ]
        ),
    )


@router.callback_query(F.data == "settings:phone")
async def settings_phone(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    await callback.answer()
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 Telefon raqamimni ulashish", request_contact=True
                )
            ],
            [KeyboardButton(text="❌ Bekor")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await callback.message.answer(
        "Hisobga ulangan telefon raqamini yangilash uchun o‘zingizning kontaktingizni yuboring:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "settings:catalog")
async def settings_catalog(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    rows = await db.catalogs(callback.from_user.id)
    buttons = [
        [(f"📁 {row['name']}", f"settings:setcat:{row['name']}")]
        for row in rows
        if len(str(row["name"]).encode("utf-8")) <= 16
    ]
    await callback.answer()
    await callback.message.answer(
        "Yangi fayllar uchun standart katalogni tanlang:",
        reply_markup=ikb(buttons),
    )


@router.callback_query(F.data.startswith("settings:setcat:"))
async def settings_catalog_set(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    catalog = callback.data.split(":", 2)[2]
    allowed = {row["name"] for row in await db.catalogs(callback.from_user.id)}
    if catalog not in allowed:
        await callback.answer("Katalog topilmadi.", show_alert=True)
        return
    await db.update_setting(callback.from_user.id, "default_catalog", catalog)
    await callback.answer("Standart katalog yangilandi.", show_alert=True)


@router.callback_query(
    F.data.in_({"settings:index", "settings:favorite", "settings:manifest", "settings:cards"})
)
async def setting_toggle(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    row = await db.setting(callback.from_user.id)
    field = {
        "settings:index": "index_message_enabled",
        "settings:favorite": "default_favorite",
        "settings:manifest": "auto_manifest_enabled",
        "settings:cards": "compact_cards",
    }[callback.data]
    value = not bool(row and row[field])
    await db.update_setting(callback.from_user.id, field, value)
    if field == "auto_manifest_enabled" and value:
        await db.mark_manifest_dirty(callback.from_user.id)
    await callback.answer(
        f"{'Yoqildi' if value else 'O‘chirib qo‘yildi'}", show_alert=True
    )


@router.message(Command("mydata"), F.chat.type == ChatType.PRIVATE)
async def my_data(message: Message) -> None:
    if not await actor_user(message):
        return
    data = await db.export_user(message.from_user.id)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    await message.answer_document(
        BufferedInputFile(payload, filename="keepgram_metadata.json"),
        caption="📄 KeepGram siz haqingizda saqlayotgan metadata eksporti.",
    )


@router.message(Command("backup"), F.chat.type == ChatType.PRIVATE)
async def manual_manifest_backup(message: Message) -> None:
    if not await actor_user(message):
        return
    payload = await publish_user_manifest(message.from_user.id)
    if not payload:
        await message.answer("⚠️ Backup uchun avval storage kanalni ulang.")
        return
    await message.answer_document(
        BufferedInputFile(payload, filename="keepgram_restore_manifest.json"),
        caption="🛟 Imzolangan KeepGram tiklash manifesti. Uni xavfsiz joyda saqlang.",
    )


@router.message(Command("restore"), F.chat.type == ChatType.PRIVATE)
async def restore_manifest_begin(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    if not await db.storage_by_tg(message.from_user.id):
        await message.answer("⚠️ Avval manifestdagi eski storage kanalni qayta ulang.")
        return
    await state.set_state(Flow.restore_manifest)
    await message.answer(
        "🛟 KeepGram yaratgan <code>keepgram_restore_manifest.json</code> faylini yuboring. "
        "Faqat o‘zingizga va hozir ulangan kanalga tegishli imzolangan manifest qabul qilinadi."
    )


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def telegram_admin(message: Message) -> None:
    allowed = {
        int(item.strip())
        for item in settings.admin_telegram_ids.split(",")
        if item.strip().isdigit()
    }
    if message.from_user.id not in allowed:
        return
    await message.answer(
        f"🔐 KeepGram admin paneli: {esc(settings.app_base_url)}/admin"
    )


@router.message(Command("delete_my_data"), F.chat.type == ChatType.PRIVATE)
async def delete_my_data_begin(message: Message, state: FSMContext) -> None:
    if not await actor_user(message, allow_incomplete=True):
        return
    await state.set_state(Flow.delete_account)
    await message.answer(
        "⚠️ Barcha KeepGram metadata, indeks, kanal bog‘lanishi va sozlamalar o‘chadi. Telegram kanaldagi fayllar qoladi.\n\n"
        "Tasdiqlash uchun aynan <code>DELETE</code> deb yozing. /cancel — bekor qilish."
    )


@router.callback_query(F.data == "settings:delete")
async def delete_from_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if not await actor_user(callback):
        return
    await state.set_state(Flow.delete_account)
    await callback.answer()
    await callback.message.answer(
        "Barcha metadata o‘chadi, kanaldagi fayllar qoladi. Tasdiqlash uchun aynan <code>DELETE</code> deb yozing."
    )


@router.message(Flow.delete_account, F.text, ~F.text.startswith("/"))
async def delete_my_data_finish(message: Message, state: FSMContext) -> None:
    if message.text.strip() != "DELETE":
        await message.answer("Bekor qilindi. O‘chirish uchun buyruqni qayta boshlang.")
        await state.clear()
        return
    await db.audit(
        "user",
        str(message.from_user.id),
        "delete_own_metadata",
        "user",
        str(message.from_user.id),
    )
    await db.delete_user(message.from_user.id)
    actor_user_cache.pop(message.from_user.id, None)
    await state.clear()
    await message.answer(
        "✅ KeepGram’dagi metadata hisobingiz o‘chirildi. Telegram kanal fayllariga tegilmadi.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("bekor"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text.in_(menu_variants("❌ Bekor")), F.chat.type == ChatType.PRIVATE)
async def cancel(message: Message, state: FSMContext) -> None:
    user = await actor_user(message, allow_incomplete=True)
    if not user:
        return
    if not user["onboarding_completed"] or not terms_are_current(user):
        if not user["display_name"]:
            await state.set_state(Flow.onboarding_name)
            await message.answer(
                "Ro‘yxatdan o‘tish majburiy. Davom etish uchun ismingizni yozing:",
                reply_markup=ReplyKeyboardRemove(),
            )
        elif not user["phone"]:
            await state.clear()
            await message.answer(
                "Ro‘yxatdan o‘tish majburiy. Telefon raqamingizni tasdiqlang:",
                reply_markup=ONBOARDING_PHONE_KEYBOARD,
            )
        else:
            await state.clear()
            await send_terms(message)
        return
    await state.clear()
    await message.answer("Amal bekor qilindi.", reply_markup=MAIN_MENU)


@router.message(F.text, F.chat.type == ChatType.PRIVATE)
async def plain_text(message: Message) -> None:
    if not await actor_user(message):
        return
    text = message.text.strip()
    if CODE_RE.fullmatch(text):
        row = await db.file_by_code(message.from_user.id, text.upper())
        if row:
            await send_stored_file(message.from_user.id, row)
        else:
            await message.answer("❌ Bunday kodli fayl topilmadi.")
        return
    await run_search(message, text)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class BackupSettingsBody(BaseModel):
    enabled: bool
    channel_id: int | None = None


class BackupSendBody(BaseModel):
    target_chat_id: int


login_attempts: dict[str, list[float]] = {}


def session_fingerprint(request: Request) -> str:
    user_agent = request.headers.get("user-agent", "")[:512]
    return hmac.new(
        settings.session_secret.get_secret_value().encode("utf-8"),
        user_agent.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def session_admin(request: Request) -> str:
    username = request.session.get("admin")
    fingerprint = str(request.session.get("fingerprint", ""))
    if (
        not username
        or not fingerprint
        or not hmac.compare_digest(fingerprint, session_fingerprint(request))
    ):
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Kirish talab qilinadi"
        )
    return str(username)


def csrf_admin(request: Request, admin: str = Depends(session_admin)) -> str:
    expected = str(request.session.get("csrf", ""))
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF tekshiruvi muvaffaqiyatsiz",
        )
    return admin


async def publish_user_manifest(telegram_id: int) -> bytes | None:
    data = await db.export_manifest(telegram_id)
    if not data:
        return None
    payload = build_manifest_bytes(data)
    channel_id = int(data["telegram_channel_id"])
    language = await db.user_language(telegram_id) or "uz"
    sent = await bot.send_document(
        channel_id,
        BufferedInputFile(payload, filename="keepgram_restore_manifest.json"),
        caption=localize_text((
            "🛟 <b>KeepGram avtomatik tiklash manifesti</b>\n"
            "Bu fayl faqat indeks metadata va kanaldagi xabar IDlarini saqlaydi. "
            "Tiklash uchun botga /restore yuboring."
        ), language),
        disable_notification=True,
    )
    old_message_id = await db.complete_manifest_backup(telegram_id, sent.message_id)
    if old_message_id and old_message_id != sent.message_id:
        try:
            await bot.delete_message(channel_id, old_message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            log.warning("Old manifest message could not be removed: %s", old_message_id)
    return payload


async def manifest_backup_worker() -> None:
    idle_delay = 5.0
    while True:
        try:
            pending = await db.pending_manifest_users()
            for row in pending:
                try:
                    await publish_user_manifest(int(row["telegram_id"]))
                except (TelegramBadRequest, TelegramForbiddenError):
                    log.warning(
                        "Manifest backup channel unavailable for telegram_id=%s",
                        row["telegram_id"],
                    )
                except Exception as exc:
                    log.exception(
                        "Manifest backup failed for telegram_id=%s", row["telegram_id"]
                    )
                    await db.job_failure("manifest", row["telegram_id"], exc)
            idle_delay = 5.0 if pending else min(60.0, idle_delay * 1.6)
            await asyncio.sleep(idle_delay)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Manifest worker iteration failed")
            await asyncio.sleep(15)


manifest_worker_task: asyncio.Task[None] | None = None


def backup_asset_card(row: Any) -> str:
    status_label = {
        "pending": "⏳ Kutilmoqda",
        "processing": "🔄 Nusxalanmoqda",
        "active": "✅ Faol",
        "deleted": "🗑 Asl yozuv o‘chirilgan",
        "replaced": "♻️ Yangi versiya bilan almashtirilgan",
        "missing": "⚠️ Asl kanalda topilmadi",
        "failed": "❌ Backup xatosi",
    }.get(str(row["status"]), str(row["status"]))
    created = row["created_at"].astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return (
        "🛡 <b>KEEPGRAM CONSENTED BACKUP</b>\n\n"
        f"👤 Egasi: {esc(row['owner_name'] or 'Noma’lum')}"
        f"{(' (@' + esc(row['owner_username']) + ')') if row['owner_username'] else ''}\n"
        f"🆔 Telegram ID: <code>{row['owner_telegram_id']}</code>\n"
        f"🕓 Qabul qilindi: {created}\n"
        f"📡 Asl kanal: {esc(row['source_channel_title'] or 'Noma’lum')} "
        f"(<code>{row['source_channel_id']}</code>)\n"
        f"📝 Nomi: {esc(row['title'])}\n"
        f"🔢 Kod: <code>{row['code']}</code>\n"
        f"🧩 Turi: {esc(kinds_text(list(row['file_kinds'] or [])))}\n"
        f"📦 Tarkib: {row['item_count']} ta\n"
        f"🔄 Versiya: {row['version']}\n"
        f"📌 Holati: <b>{status_label}</b>"
    )


async def super_backup_allowed(telegram_id: int) -> bool:
    user = await db.user_by_tg(telegram_id)
    return bool(user and user["onboarding_completed"] and terms_are_current(user))


async def process_super_backup_asset(row: asyncpg.Record) -> None:
    backup_channel_id = int(row["super_backup_channel_id"])
    copied = await bot.copy_messages(
        chat_id=backup_channel_id,
        from_chat_id=int(row["source_channel_id"]),
        message_ids=list(row["source_message_ids"]),
        disable_notification=True,
    )
    copied_ids = [item.message_id for item in copied]
    try:
        card_row = dict(row)
        card_row["status"] = "active"
        index_message = await bot.send_message(
            backup_channel_id,
            backup_asset_card(card_row),
            disable_notification=True,
        )
    except Exception:
        for message_id in copied_ids:
            try:
                await bot.delete_message(backup_channel_id, message_id)
            except Exception:  # noqa: BLE001 - backup rollback is best effort
                log.warning("Backup rollback failed for message_id=%s", message_id)
        raise
    await db.complete_super_backup(
        row["id"], backup_channel_id, copied_ids, index_message.message_id
    )


async def super_backup_worker() -> None:
    idle_delay = 2.0
    while True:
        try:
            await db.requeue_stale_super_backups()
            pending = await db.pending_super_backups(TERMS_VERSION)
            for candidate in pending:
                row = await db.claim_super_backup(candidate["id"], TERMS_VERSION)
                if not row:
                    continue
                try:
                    await process_super_backup_asset(row)
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    await db.fail_super_backup(row["id"], exc.__class__.__name__)
                    log.warning("Super backup failed for asset=%s", row["id"])
                except Exception as exc:
                    await db.fail_super_backup(row["id"], exc.__class__.__name__)
                    await db.job_failure("backup", row["id"], exc)
                    log.exception("Super backup worker failed for asset=%s", row["id"])
            idle_delay = 2.0 if pending else min(30.0, idle_delay * 1.7)
            await asyncio.sleep(idle_delay)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Super backup worker iteration failed")
            await asyncio.sleep(3)


async def update_super_backup_status(
    telegram_id: int, file_id: UUID | str, new_status: str
) -> None:
    for row in await db.set_super_backup_status(telegram_id, file_id, new_status):
        if not row["backup_channel_id"] or not row["index_message_id"]:
            continue
        try:
            await bot.edit_message_text(
                backup_asset_card(row),
                chat_id=row["backup_channel_id"],
                message_id=row["index_message_id"],
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            log.warning("Backup status message could not be updated for asset=%s", row["id"])


async def flush_pending_super_backup(
    telegram_id: int, file_id: UUID | str
) -> bool:
    if not await super_backup_allowed(telegram_id):
        return True
    config = await db.super_backup_config()
    if not config["super_backup_enabled"] or not config["super_backup_channel_id"]:
        return True
    latest = await db.latest_super_backup_for_file(telegram_id, file_id)
    if not latest or latest["status"] in {"deleted", "replaced", "missing"}:
        await db.enqueue_super_backup(telegram_id, file_id)
    for _ in range(50):
        latest = await db.latest_super_backup_for_file(telegram_id, file_id)
        if not latest:
            return False
        if latest["status"] == "active":
            return True
        if latest["status"] == "failed":
            return False
        if latest["status"] == "pending":
            row = await db.claim_super_backup(latest["id"], TERMS_VERSION)
            if row:
                try:
                    await process_super_backup_asset(row)
                except Exception as exc:
                    await db.fail_super_backup(row["id"], exc.__class__.__name__)
                    log.exception("Urgent super backup failed for asset=%s", row["id"])
                    return False
                return True
        await asyncio.sleep(0.2)
    log.warning("Timed out waiting for super backup file_id=%s", file_id)
    return False


super_backup_worker_task: asyncio.Task[None] | None = None
reminder_worker_task: asyncio.Task[None] | None = None
maintenance_worker_task: asyncio.Task[None] | None = None


async def reminder_worker() -> None:
    idle_delay = 10.0
    while True:
        try:
            rows = await db.due_reminders()
            for row in rows:
                success = False
                language_token = current_language.set(row["preferred_language"] or "uz")
                try:
                    note = f"\n📝 {esc(row['note'])}" if row["note"] else ""
                    await bot.send_message(
                        row["telegram_id"],
                        f"⏰ <b>KeepGram eslatmasi</b>\n\n{esc(row['title'])} · <code>{row['code']}</code>{note}",
                        reply_markup=ikb([[("📂 Faylni ochish", f"f:open:{row['file_id']}")]]),
                    )
                    success = True
                except (TelegramBadRequest, TelegramForbiddenError):
                    log.warning("Reminder delivery failed reminder_id=%s", row["id"])
                finally:
                    current_language.reset(language_token)
                await db.finish_reminder(row["id"], success)
            idle_delay = 10.0 if rows else min(60.0, idle_delay * 1.5)
            await asyncio.sleep(idle_delay)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Reminder worker iteration failed")
            await asyncio.sleep(15)


async def maintenance_worker() -> None:
    while True:
        try:
            purged = await db.purge_trash(settings.trash_retention_days)
            await db.purge_operational_history()
            if purged:
                log.info("Purged %s expired trash records", purged)
            await asyncio.sleep(6 * 60 * 60)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Maintenance worker iteration failed")
            await asyncio.sleep(15 * 60)


async def admin_log(
    admin: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await db.audit("admin", admin, action, target_type, target_id, metadata)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global manifest_worker_task, redis_album_worker_task, super_backup_worker_task
    global reminder_worker_task, maintenance_worker_task
    await db.connect()
    if await db.ensure_schema():
        log.info("Fresh database detected; KeepGram schema installed automatically")
    command_sets = {
        "uz": [
            BotCommand(command="start", description="KeepGram’ni boshlash"),
            BotCommand(command="menu", description="Asosiy menyu"),
            BotCommand(command="search", description="Fayl qidirish"),
            BotCommand(command="recent", description="Oxirgi fayllar"),
            BotCommand(command="all", description="Barcha saqlanganlar"),
            BotCommand(command="trash", description="30 kunlik savat"),
            BotCommand(command="views", description="Saqlangan qidiruvlar"),
            BotCommand(command="reminders", description="Faol eslatmalar"),
            BotCommand(command="stats", description="Fayl statistikasi"),
            BotCommand(command="catalogs", description="Kataloglar"),
            BotCommand(command="tags", description="Teglar"),
            BotCommand(command="settings", description="Sozlamalar"),
            BotCommand(command="channel", description="Storage kanal"),
            BotCommand(command="mydata", description="Metadata eksporti"),
            BotCommand(command="backup", description="Tiklash manifesti"),
            BotCommand(command="restore", description="Manifestdan tiklash"),
            BotCommand(command="privacy", description="Maxfiylik"),
            BotCommand(command="help", description="Yordam"),
        ],
        "en": [
            BotCommand(command="start", description="Start KeepGram"),
            BotCommand(command="menu", description="Main menu"),
            BotCommand(command="search", description="Search files"),
            BotCommand(command="recent", description="Recent files"),
            BotCommand(command="all", description="All saved items"),
            BotCommand(command="trash", description="30-day trash"),
            BotCommand(command="views", description="Saved searches"),
            BotCommand(command="reminders", description="Active reminders"),
            BotCommand(command="stats", description="File statistics"),
            BotCommand(command="catalogs", description="Folders"),
            BotCommand(command="tags", description="Tags"),
            BotCommand(command="settings", description="Settings"),
            BotCommand(command="channel", description="Storage channel"),
            BotCommand(command="mydata", description="Export my metadata"),
            BotCommand(command="backup", description="Recovery manifest"),
            BotCommand(command="restore", description="Restore manifest"),
            BotCommand(command="privacy", description="Privacy"),
            BotCommand(command="help", description="Help"),
        ],
        "ru": [
            BotCommand(command="start", description="Запустить KeepGram"),
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="search", description="Поиск файлов"),
            BotCommand(command="recent", description="Недавние файлы"),
            BotCommand(command="all", description="Все сохранённые"),
            BotCommand(command="trash", description="Корзина на 30 дней"),
            BotCommand(command="views", description="Сохранённые поиски"),
            BotCommand(command="reminders", description="Активные напоминания"),
            BotCommand(command="stats", description="Статистика файлов"),
            BotCommand(command="catalogs", description="Папки"),
            BotCommand(command="tags", description="Теги"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="channel", description="Канал-хранилище"),
            BotCommand(command="mydata", description="Экспорт метаданных"),
            BotCommand(command="backup", description="Манифест восстановления"),
            BotCommand(command="restore", description="Восстановить манифест"),
            BotCommand(command="privacy", description="Конфиденциальность"),
            BotCommand(command="help", description="Помощь"),
        ],
    }
    await bot.set_my_commands(command_sets["uz"])
    await bot.set_my_commands(command_sets["uz"], language_code="uz")
    await bot.set_my_commands(command_sets["en"], language_code="en")
    await bot.set_my_commands(command_sets["ru"], language_code="ru")
    await db.ready().execute(
        """UPDATE storage_channels s SET manifest_dirty_at=now()
           FROM user_settings us WHERE us.user_id=s.user_id
             AND us.auto_manifest_enabled AND s.manifest_message_id IS NULL"""
    )
    manifest_worker_task = asyncio.create_task(manifest_backup_worker())
    super_backup_worker_task = asyncio.create_task(super_backup_worker())
    reminder_worker_task = asyncio.create_task(reminder_worker())
    maintenance_worker_task = asyncio.create_task(maintenance_worker())
    if redis_client:
        await redis_client.ping()
        redis_album_worker_task = asyncio.create_task(redis_album_worker())
        log.info("Redis connected; persistent FSM and album queue enabled")
    else:
        log.warning("REDIS_URL is not set; using in-memory FSM and album queue")
    webhook_secret = settings.webhook_secret.get_secret_value()
    webhook_url = f"{settings.app_base_url}/telegram/webhook"
    await bot.set_webhook(
        webhook_url,
        secret_token=webhook_secret,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=settings.webhook_drop_pending_updates,
    )
    log.info("KeepGram started; webhook configured")
    try:
        yield
    finally:
        if manifest_worker_task:
            manifest_worker_task.cancel()
            await asyncio.gather(manifest_worker_task, return_exceptions=True)
        if super_backup_worker_task:
            super_backup_worker_task.cancel()
            await asyncio.gather(super_backup_worker_task, return_exceptions=True)
        if reminder_worker_task:
            reminder_worker_task.cancel()
            await asyncio.gather(reminder_worker_task, return_exceptions=True)
        if maintenance_worker_task:
            maintenance_worker_task.cancel()
            await asyncio.gather(maintenance_worker_task, return_exceptions=True)
        if redis_album_worker_task:
            redis_album_worker_task.cancel()
            await asyncio.gather(redis_album_worker_task, return_exceptions=True)
        pending_albums = list(album_tasks.values())
        if pending_albums:
            await asyncio.gather(*pending_albums, return_exceptions=True)
        await bot.session.close()
        await dp.storage.close()
        await db.close()


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret.get_secret_value(),
    session_cookie="keepgram_admin",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=settings.app_env.lower() == "production",
)


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/admin") or request.url.path.startswith(
        "/api/admin"
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return "<h1>KeepGram</h1><p>Telegram personal vault bot is running.</p>"


@app.get("/health")
@app.get("/ping")
async def health() -> JSONResponse:
    connected = await db.ping()
    schema = connected and await db.schema_ready()
    redis_status = await redis_healthy()
    ok = connected and schema and redis_status is not False
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "app": APP_NAME,
            "version": APP_VERSION,
            "database": connected,
            "schema": schema,
            "redis": redis_status,
        },
        status_code=200 if ok else 503,
    )


@app.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request) -> Response:
    expected = settings.webhook_secret.get_secret_value()
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(header, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
        if not await db.claim_update(update.update_id):
            return Response(status_code=200)
        try:
            await dp.feed_update(bot, update)
        except Exception as exc:
            await db.finish_update(update.update_id, exc.__class__.__name__)
            raise
        await db.finish_update(update.update_id)
    except Exception:
        log.exception("Telegram update processing failed")
        return Response(status_code=500)
    return Response(status_code=200)


@app.get("/assets/logo.png", include_in_schema=False)
async def logo() -> FileResponse:
    path = BASE_DIR / "assets" / "logo.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(BASE_DIR / "admin.html", media_type="text/html")


@app.api_route(
    "/admin/{unexpected_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def reject_unknown_admin_path(unexpected_path: str) -> None:
    raise HTTPException(status_code=404, detail="Sahifa topilmadi")


@app.post("/api/admin/login")
async def admin_login(body: LoginBody, request: Request) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = [value for value in login_attempts.get(ip, []) if now - value < 600]
    if len(attempts) >= 5:
        raise HTTPException(429, "10 daqiqada juda ko‘p noto‘g‘ri urinish")
    submitted_username = body.username.strip()
    username_ok = hmac.compare_digest(submitted_username, settings.admin_username)
    password_ok = hmac.compare_digest(
        body.password.encode("utf-8"),
        settings.admin_password.get_secret_value().encode("utf-8"),
    )
    if not username_ok or not password_ok:
        attempts.append(now)
        login_attempts[ip] = attempts
        await db.audit(
            "admin",
            submitted_username[:100],
            "login_failed",
            metadata={"ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:16]},
        )
        raise HTTPException(401, "Login yoki parol noto‘g‘ri")
    login_attempts.pop(ip, None)
    request.session.clear()
    request.session["admin"] = settings.admin_username
    request.session["csrf"] = secrets.token_urlsafe(24)
    request.session["fingerprint"] = session_fingerprint(request)
    request.session["issued_at"] = int(time.time())
    await admin_log(settings.admin_username, "login")
    return {
        "ok": True,
        "csrf": request.session["csrf"],
        "username": settings.admin_username,
    }


@app.post("/api/admin/logout")
async def admin_logout(
    request: Request, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    await admin_log(admin, "logout")
    request.session.clear()
    return {"ok": True}


@app.get("/api/admin/session")
async def admin_session(request: Request) -> dict[str, Any]:
    try:
        admin = session_admin(request)
    except HTTPException:
        return {"authenticated": False}
    csrf = request.session.get("csrf") or secrets.token_urlsafe(24)
    request.session["csrf"] = csrf
    return {
        "authenticated": True,
        "username": admin,
        "csrf": csrf,
    }


@app.get("/api/admin/me")
async def admin_me(
    request: Request, admin: str = Depends(session_admin)
) -> dict[str, Any]:
    csrf = request.session.get("csrf") or secrets.token_urlsafe(24)
    request.session["csrf"] = csrf
    return {"username": admin, "csrf": csrf}


@app.get("/api/admin/stats")
async def admin_stats(_: str = Depends(session_admin)) -> dict[str, Any]:
    row = await db.ready().fetchrow(
        """SELECT (SELECT count(*) FROM users)::int users,
                  (SELECT count(*) FROM storage_channels WHERE is_active)::int channels,
                  (SELECT COALESCE(sum(item_count),0) FROM files WHERE deleted_at IS NULL)::int files,
                  (SELECT count(*) FROM users WHERE last_seen_at>now()-interval '24 hours')::int active_24h,
                  (SELECT count(*) FROM users WHERE is_blocked)::int blocked,
                  (SELECT count(*) FROM files WHERE deleted_at IS NOT NULL)::int trash,
                  (SELECT count(*) FROM reminders WHERE status='pending')::int reminders,
                  (SELECT count(*) FROM share_tokens WHERE revoked_at IS NULL AND expires_at>now())::int active_shares"""
    )
    recent_users = await db.ready().fetch(
        """SELECT telegram_id,username,COALESCE(display_name,first_name) AS first_name,
                  created_at FROM users ORDER BY created_at DESC LIMIT 7"""
    )
    recent_files = await db.ready().fetch(
        "SELECT title,code,file_type,file_kinds,item_count,created_at FROM files WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 7"
    )
    activity = await db.ready().fetch(
        """SELECT day::date,
                  (SELECT count(*) FROM users u WHERE u.created_at>=day AND u.created_at<day+interval '1 day')::int users,
                  (SELECT COALESCE(sum(f.item_count),0) FROM files f WHERE f.created_at>=day AND f.created_at<day+interval '1 day')::int files
           FROM generate_series(current_date-interval '6 days',current_date,interval '1 day') day"""
    )
    return {
        **jsonable(row),
        "recent_users": jsonable(recent_users),
        "recent_files": jsonable(recent_files),
        "activity": jsonable(activity),
    }


@app.get("/api/admin/users")
async def admin_users(
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: Literal[10, 15, 50] = Query(15),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    where = """$1='' OR u.telegram_id::text ILIKE '%'||$1||'%' OR COALESCE(u.username,'') ILIKE '%'||$1||'%'
               OR COALESCE(u.display_name,'') ILIKE '%'||$1||'%'
               OR COALESCE(u.first_name,'') ILIKE '%'||$1||'%' OR COALESCE(u.last_name,'') ILIKE '%'||$1||'%'
               OR COALESCE(u.phone,'') ILIKE '%'||$1||'%'"""
    total = await db.ready().fetchval(
        f"SELECT count(*) FROM users u WHERE {where}", search
    )
    rows = await db.ready().fetch(
        f"""SELECT u.id,u.telegram_id,u.username,COALESCE(u.display_name,u.first_name) AS first_name,
                   u.last_name,u.phone,u.onboarding_completed,u.terms_accepted_at,
                   u.terms_version,u.preferred_language,u.is_blocked,
                   u.created_at,u.last_seen_at,s.channel_title,s.telegram_channel_id,
                   COALESCE(c.item_count,0)::int file_count
            FROM users u LEFT JOIN storage_channels s ON s.user_id=u.id
            LEFT JOIN user_counters c ON c.user_id=u.id WHERE {where}
            ORDER BY u.created_at DESC LIMIT $2 OFFSET $3""",
        search,
        limit,
        (page - 1) * limit,
    )
    return {"items": jsonable(rows), "total": int(total), "page": page, "limit": limit}


@app.get("/api/admin/users/{user_id}")
async def admin_user_detail(
    user_id: UUID, _: str = Depends(session_admin)
) -> dict[str, Any]:
    user = await db.ready().fetchrow(
        """SELECT u.*,s.telegram_channel_id,s.channel_title,s.channel_username,s.is_active,s.linked_at,
                  COALESCE(c.item_count,0)::int file_count,COALESCE(c.trash_count,0)::int trash_count,
                  (SELECT count(*) FROM files f WHERE f.user_id=u.id AND f.is_favorite AND f.deleted_at IS NULL)::int favorite_count
           FROM users u LEFT JOIN storage_channels s ON s.user_id=u.id
           LEFT JOIN user_counters c ON c.user_id=u.id WHERE u.id=$1""",
        user_id,
    )
    if not user:
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    user_payload = jsonable(user)
    user_payload["first_name"] = user_payload.get("display_name") or user_payload.get(
        "first_name"
    )
    return {"user": user_payload}


@app.get("/api/admin/users/{user_id}/files")
async def admin_user_files(
    user_id: UUID,
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: Literal[10, 15, 50] = Query(15),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    where = """user_id=$1 AND deleted_at IS NULL AND
               ($2='' OR title ILIKE '%'||$2||'%' OR code ILIKE '%'||$2||'%'
                OR catalog ILIKE '%'||$2||'%' OR tags @> ARRAY[lower($2)]::text[])"""
    total = await db.ready().fetchval(f"SELECT count(*) FROM files WHERE {where}", user_id, search)
    rows = await db.ready().fetch(
        f"""SELECT id,title,code,file_type,file_kinds,item_count,catalog,tags,
                   is_favorite,is_missing,channel_message_id,created_at
            FROM files WHERE {where} ORDER BY created_at DESC LIMIT $3 OFFSET $4""",
        user_id, search, limit, (page - 1) * limit,
    )
    return {"items": jsonable(rows), "total": int(total), "page": page, "limit": limit}


@app.post("/api/admin/users/{user_id}/block")
async def admin_block_user(
    user_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    result = await db.ready().execute(
        "UPDATE users SET is_blocked=true WHERE id=$1", user_id
    )
    if result.endswith("0"):
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    telegram_id = await db.ready().fetchval("SELECT telegram_id FROM users WHERE id=$1", user_id)
    if telegram_id:
        actor_user_cache.pop(int(telegram_id), None)
    await admin_log(admin, "block_user", "user", str(user_id))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/unblock")
async def admin_unblock_user(
    user_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    result = await db.ready().execute(
        "UPDATE users SET is_blocked=false WHERE id=$1", user_id
    )
    if result.endswith("0"):
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    telegram_id = await db.ready().fetchval("SELECT telegram_id FROM users WHERE id=$1", user_id)
    if telegram_id:
        actor_user_cache.pop(int(telegram_id), None)
    await admin_log(admin, "unblock_user", "user", str(user_id))
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}/metadata")
async def admin_delete_user(
    user_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    telegram_id = await db.ready().fetchval("SELECT telegram_id FROM users WHERE id=$1", user_id)
    await admin_log(admin, "delete_user_metadata", "user", str(user_id))
    result = await db.ready().execute("DELETE FROM users WHERE id=$1", user_id)
    if result.endswith("0"):
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    if telegram_id:
        actor_user_cache.pop(int(telegram_id), None)
    return {"ok": True}


@app.get("/api/admin/channels")
async def admin_channels(
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: Literal[10, 15, 50] = Query(15),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    where = """$1='' OR s.channel_title ILIKE '%'||$1||'%' OR s.telegram_channel_id::text ILIKE '%'||$1||'%'
               OR u.telegram_id::text ILIKE '%'||$1||'%' OR COALESCE(u.username,'') ILIKE '%'||$1||'%'"""
    total = await db.ready().fetchval(
        f"SELECT count(*) FROM storage_channels s JOIN users u ON u.id=s.user_id WHERE {where}", search
    )
    rows = await db.ready().fetch(
        f"""SELECT s.id,s.telegram_channel_id,s.channel_title,s.channel_username,s.is_active,s.linked_at,
                  u.telegram_id,u.username,COALESCE(c.item_count,0)::int file_count
           FROM storage_channels s JOIN users u ON u.id=s.user_id
           LEFT JOIN user_counters c ON c.user_id=u.id
           WHERE {where} ORDER BY s.linked_at DESC LIMIT $2 OFFSET $3""",
        search, limit, (page - 1) * limit,
    )
    return {"items": jsonable(rows), "total": int(total), "page": page, "limit": limit}


@app.post("/api/admin/channels/{channel_id}/disconnect")
async def admin_disconnect_channel(
    channel_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    await admin_log(admin, "disconnect_channel", "channel", str(channel_id))
    if not await db.disconnect_channel_by_id(channel_id):
        raise HTTPException(404, "Kanal topilmadi")
    return {"ok": True}


@app.get("/api/admin/files")
async def admin_files(
    search: str = Query("", max_length=100),
    file_status: Literal["active", "trash", "all"] = Query("active", alias="status"),
    page: int = Query(1, ge=1),
    limit: Literal[10, 15, 50] = Query(15),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    where = "$1='' OR f.title ILIKE '%'||$1||'%' OR f.code ILIKE '%'||$1||'%' OR u.telegram_id::text ILIKE '%'||$1||'%'"
    status_where = {"active": "f.deleted_at IS NULL", "trash": "f.deleted_at IS NOT NULL", "all": "true"}[file_status]
    total = await db.ready().fetchval(
        f"SELECT count(*) FROM files f JOIN users u ON u.id=f.user_id WHERE {status_where} AND ({where})",
        search,
    )
    rows = await db.ready().fetch(
        f"""SELECT f.id,f.title,f.code,f.file_type,f.file_kinds,f.item_count,
                   f.catalog,f.tags,f.is_favorite,f.is_missing,
                   f.channel_message_id,f.created_at,f.deleted_at,u.telegram_id,u.username,s.channel_title
            FROM files f JOIN users u ON u.id=f.user_id JOIN storage_channels s ON s.id=f.channel_id
            WHERE {status_where} AND ({where}) ORDER BY f.created_at DESC LIMIT $2 OFFSET $3""",
        search,
        limit,
        (page - 1) * limit,
    )
    return {
        "items": jsonable(rows),
        "total": int(total),
        "page": page,
        "limit": limit,
        "metadata_only": True,
    }


@app.get("/api/admin/backup/settings")
async def admin_backup_settings(_: str = Depends(session_admin)) -> dict[str, Any]:
    config = await db.super_backup_config()
    consented_users = await db.ready().fetchval(
        """SELECT count(*) FROM users
           WHERE onboarding_completed AND terms_accepted_at IS NOT NULL
             AND terms_version=$1""",
        TERMS_VERSION,
    )
    channel_title: str | None = None
    if config["super_backup_channel_id"]:
        try:
            chat = await bot.get_chat(config["super_backup_channel_id"])
            channel_title = chat.title
        except (TelegramBadRequest, TelegramForbiddenError):
            channel_title = None
    return {
        **jsonable(config),
        "channel_title": channel_title,
        "terms_version": TERMS_VERSION,
        "consented_users": int(consented_users),
    }


@app.post("/api/admin/backup/settings")
async def admin_update_backup_settings(
    body: BackupSettingsBody, admin: str = Depends(csrf_admin)
) -> dict[str, Any]:
    channel_id = body.channel_id
    if body.enabled:
        if not channel_id:
            raise HTTPException(422, "Backup kanal ID majburiy")
        used = await db.ready().fetchval(
            "SELECT EXISTS(SELECT 1 FROM storage_channels WHERE telegram_channel_id=$1)",
            channel_id,
        )
        if used:
            raise HTTPException(409, "Backup kanal alohida kanal bo‘lishi kerak")
        try:
            member = await bot.get_chat_member(channel_id, bot.id)
            if member.status not in {
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            } or not all(
                (
                    getattr(member, "can_post_messages", False),
                    getattr(member, "can_edit_messages", False),
                    getattr(member, "can_delete_messages", False),
                )
            ):
                raise HTTPException(
                    422,
                    "Botga backup kanalda Post, Edit va Delete Messages huquqlarini bering",
                )
            await bot.get_chat(channel_id)
        except HTTPException:
            raise
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            raise HTTPException(
                422, "Backup kanal topilmadi yoki bot admin emas"
            ) from exc
    row = await db.set_super_backup_config(body.enabled, channel_id)
    queued = 0
    if body.enabled and channel_id:
        queued = await db.enqueue_existing_consented_backups(
            TERMS_VERSION, channel_id
        )
    await admin_log(
        admin,
        "update_super_backup",
        "channel",
        str(channel_id or "disabled"),
        {"enabled": body.enabled, "queued_existing": queued},
    )
    return {**jsonable(row), "queued_existing": queued}


@app.get("/api/admin/backups")
async def admin_backups(
    search: str = Query("", max_length=100),
    backup_status: str = Query("all", alias="status", max_length=16),
    telegram_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    limit: Literal[10, 15, 50] = Query(15),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    allowed_statuses = {
        "all", "pending", "processing", "active", "deleted", "replaced", "missing", "failed"
    }
    if backup_status not in allowed_statuses:
        raise HTTPException(422, "Noto‘g‘ri backup holati")
    filters = ["1=1"]
    args: list[Any] = []

    def add(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if backup_status != "all":
        filters.append(f"b.status={add(backup_status)}")
    if telegram_id is not None:
        filters.append(f"b.owner_telegram_id={add(telegram_id)}")
    if search:
        placeholder = add(search)
        filters.append(
            f"(b.title ILIKE '%'||{placeholder}||'%' OR b.code ILIKE '%'||{placeholder}||'%')"
        )
    where = " AND ".join(filters)
    total = await db.ready().fetchval(f"SELECT count(*) FROM backup_assets b WHERE {where}", *args)
    args.extend([limit, (page - 1) * limit])
    rows = await db.ready().fetch(
        f"""SELECT b.* FROM backup_assets b WHERE {where}
            ORDER BY b.created_at DESC LIMIT ${len(args)-1} OFFSET ${len(args)}""",
        *args,
    )
    stats = await db.ready().fetchrow(
        """SELECT count(*)::int total,COALESCE(sum(item_count),0)::int files,
                  count(*) FILTER(WHERE status='active')::int active,
                  count(*) FILTER(WHERE status='deleted')::int deleted,
                  count(*) FILTER(WHERE status='failed')::int failed,
                  count(*) FILTER(WHERE status='pending')::int pending,
                  count(*) FILTER(WHERE status='processing')::int processing,
                  count(DISTINCT file_id) FILTER(WHERE status IN ('active','deleted','replaced'))::int covered
           FROM backup_assets"""
    )
    active_files = await db.ready().fetchval("SELECT count(*) FROM files WHERE deleted_at IS NULL")
    stats_payload = jsonable(stats)
    stats_payload["active_files"] = int(active_files)
    stats_payload["coverage_percent"] = round(
        min(100.0, 100 * int(stats["covered"] or 0) / max(1, int(active_files))), 1
    )
    return {
        "items": jsonable(rows),
        "stats": stats_payload,
        "total": int(total),
        "page": page,
        "limit": limit,
    }


@app.post("/api/admin/backups/{backup_id}/send")
async def admin_send_backup(
    backup_id: UUID, body: BackupSendBody, admin: str = Depends(csrf_admin)
) -> dict[str, Any]:
    row = await db.backup_asset(backup_id)
    if not row or not row["backup_channel_id"] or not row["backup_message_ids"]:
        raise HTTPException(404, "Tayyor backup fayli topilmadi")
    try:
        sent = await bot.copy_messages(
            chat_id=body.target_chat_id,
            from_chat_id=row["backup_channel_id"],
            message_ids=list(row["backup_message_ids"]),
        )
        await bot.send_message(body.target_chat_id, backup_asset_card(row))
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        raise HTTPException(422, "Telegram manziliga yuborib bo‘lmadi") from exc
    await admin_log(
        admin,
        "send_backup",
        "backup_asset",
        str(backup_id),
        {"target_chat_id": body.target_chat_id},
    )
    return {"ok": True, "sent_messages": len(sent)}


@app.post("/api/admin/backups/{backup_id}/retry")
async def admin_retry_backup(
    backup_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    if not await db.retry_super_backup(backup_id):
        raise HTTPException(409, "Faqat failed holatidagi backup qayta navbatga olinadi")
    await admin_log(admin, "retry_backup", "backup_asset", str(backup_id))
    return {"ok": True}


@app.get("/api/admin/audit-logs")
async def admin_audit_logs(
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: Literal[10, 15, 50] = Query(15),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    where = "$1='' OR action ILIKE '%'||$1||'%' OR actor_id ILIKE '%'||$1||'%' OR target_id ILIKE '%'||$1||'%'"
    total = await db.ready().fetchval(f"SELECT count(*) FROM audit_logs WHERE {where}", search)
    rows = await db.ready().fetch(
        f"SELECT * FROM audit_logs WHERE {where} ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        search, limit, (page - 1) * limit,
    )
    return {"items": jsonable(rows), "total": int(total), "page": page, "limit": limit}


@app.get("/api/admin/system")
async def admin_system(_: str = Depends(session_admin)) -> dict[str, Any]:
    info = await bot.get_webhook_info()
    queues = await db.ready().fetchrow(
        """SELECT
             (SELECT count(*) FROM processed_updates WHERE status='failed')::int failed_updates,
             (SELECT count(*) FROM backup_assets WHERE status='pending')::int pending_backups,
             (SELECT count(*) FROM backup_assets WHERE status='failed')::int failed_backups,
             (SELECT count(*) FROM reminders WHERE status='pending')::int pending_reminders,
             (SELECT count(*) FROM job_failures WHERE created_at>now()-interval '24 hours')::int worker_errors_24h"""
    )
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "environment": settings.app_env,
        "database": await db.ping(),
        "redis": await redis_healthy(),
        "webhook_url": info.url,
        "pending_updates": info.pending_update_count,
        "last_error": info.last_error_message,
        "queues": jsonable(queues),
    }


@app.exception_handler(asyncpg.PostgresError)
async def database_error(_: Request, exc: asyncpg.PostgresError) -> JSONResponse:
    log.error("Database error: %s", exc.__class__.__name__)
    return JSONResponse({"detail": "Ma’lumotlar bazasi xatosi"}, status_code=503)
