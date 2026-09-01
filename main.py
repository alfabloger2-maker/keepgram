from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
import bcrypt
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
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
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "KeepGram"
APP_VERSION = "1.0.0"
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: SecretStr
    database_url: SecretStr
    app_base_url: str
    webhook_secret: SecretStr
    admin_username: str = "admin"
    admin_password_hash: SecretStr
    admin_telegram_ids: str = ""
    session_secret: SecretStr
    app_env: str = "production"
    log_level: str = "INFO"
    webhook_drop_pending_updates: bool = False

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

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("SESSION_SECRET kamida 32 belgi bo‘lishi kerak")
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


def content_metadata(message: Message) -> tuple[str, str, str | None, int | None]:
    stamp = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    content_type = message.content_type
    file_unique_id: str | None = None
    file_size: int | None = None
    if message.document:
        title = message.document.file_name or f"Hujjat_{stamp}"
        file_unique_id, file_size = (
            message.document.file_unique_id,
            message.document.file_size,
        )
    elif message.photo:
        title = f"Rasm_{stamp}"
        file_unique_id, file_size = (
            message.photo[-1].file_unique_id,
            message.photo[-1].file_size,
        )
    elif message.video:
        title = message.video.file_name or f"Video_{stamp}"
        file_unique_id, file_size = (
            message.video.file_unique_id,
            message.video.file_size,
        )
    elif message.audio:
        title = message.audio.file_name or message.audio.title or f"Audio_{stamp}"
        file_unique_id, file_size = (
            message.audio.file_unique_id,
            message.audio.file_size,
        )
    elif message.voice:
        title = f"Ovoz_{stamp}"
        file_unique_id, file_size = (
            message.voice.file_unique_id,
            message.voice.file_size,
        )
    elif message.animation:
        title = message.animation.file_name or f"GIF_{stamp}"
        file_unique_id, file_size = (
            message.animation.file_unique_id,
            message.animation.file_size,
        )
    elif message.sticker:
        title = f"Sticker_{stamp}"
        file_unique_id, file_size = (
            message.sticker.file_unique_id,
            message.sticker.file_size,
        )
    elif message.video_note:
        title = f"Video_xabar_{stamp}"
        file_unique_id, file_size = (
            message.video_note.file_unique_id,
            message.video_note.file_size,
        )
    elif message.contact:
        title = f"Kontakt_{message.contact.first_name}_{stamp}"
    elif message.location or message.venue:
        title = f"Joylashuv_{stamp}"
    else:
        title = f"Matn_{stamp}"
    return content_type, title[:180], file_unique_id, file_size


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        host = (urlparse(self.dsn).hostname or "").lower()
        ssl: str | None = (
            None if host in {"localhost", "127.0.0.1", "::1"} else "require"
        )
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=8,
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

    async def upsert_user(self, tg_user: Any) -> asyncpg.Record:
        return await self.ready().fetchrow(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, language_code)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (telegram_id) DO UPDATE SET
              username=EXCLUDED.username, first_name=EXCLUDED.first_name,
              last_name=EXCLUDED.last_name, language_code=EXCLUDED.language_code,
              last_seen_at=now()
            RETURNING *
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

    async def update_phone(self, telegram_id: int, phone: str) -> None:
        await self.ready().execute(
            "UPDATE users SET phone=$2,last_seen_at=now() WHERE telegram_id=$1",
            telegram_id,
            phone[:32],
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
        result = await self.ready().execute(
            """DELETE FROM storage_channels s USING users u
               WHERE s.user_id=u.id AND u.telegram_id=$1""",
            telegram_id,
        )
        return result.endswith("1")

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

    async def create_file(
        self,
        telegram_id: int,
        channel_message_id: int,
        title: str,
        file_type: str,
        file_unique_id: str | None,
        file_size: int | None,
    ) -> asyncpg.Record:
        for _ in range(12):
            code = make_code()
            try:
                row = await self.ready().fetchrow(
                    """INSERT INTO files(user_id,channel_id,channel_message_id,code,title,file_type,
                       catalog,tags,is_favorite,telegram_file_unique_id,file_size)
                       SELECT u.id,s.id,$2,$3,$4,$5,COALESCE(us.default_catalog,'Umumiy'),'{}'::text[],
                              COALESCE(us.default_favorite,false),$6,$7
                       FROM users u JOIN storage_channels s ON s.user_id=u.id AND s.is_active=true
                       LEFT JOIN user_settings us ON us.user_id=u.id
                       WHERE u.telegram_id=$1 AND u.is_blocked=false RETURNING *""",
                    telegram_id,
                    channel_message_id,
                    code,
                    title,
                    file_type,
                    file_unique_id,
                    file_size,
                )
                if not row:
                    raise PermissionError("Faol kanal topilmadi")
                return row
            except asyncpg.UniqueViolationError:
                continue
        raise RuntimeError("Noyob kod yaratib bo‘lmadi")

    async def file_by_id(
        self, telegram_id: int, file_id: UUID | str
    ) -> asyncpg.Record | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        return await self.ready().fetchrow(
            """SELECT f.*,s.telegram_channel_id FROM files f
               JOIN users u ON u.id=f.user_id JOIN storage_channels s ON s.id=f.channel_id
               WHERE f.id=$2 AND u.telegram_id=$1 AND f.deleted_at IS NULL""",
            telegram_id,
            parsed,
        )

    async def file_by_code(self, telegram_id: int, code: str) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """SELECT f.*,s.telegram_channel_id FROM files f
               JOIN users u ON u.id=f.user_id JOIN storage_channels s ON s.id=f.channel_id
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
            filters.append(f"${len(args)}=ANY(f.tags)")
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
        if query.startswith("#"):
            tag = normalize_tags(query)[0] if normalize_tags(query) else ""
            return list(
                await self.ready().fetch(
                    """SELECT f.* FROM files f JOIN users u ON u.id=f.user_id
                   WHERE u.telegram_id=$1 AND $2=ANY(f.tags) AND f.deleted_at IS NULL
                   ORDER BY f.created_at DESC LIMIT $3""",
                    telegram_id,
                    tag,
                    limit,
                )
            )
        if query.lower().startswith("catalog:"):
            catalog = query.split(":", 1)[1].strip()
            return list(
                await self.ready().fetch(
                    """SELECT f.* FROM files f JOIN users u ON u.id=f.user_id
                   WHERE u.telegram_id=$1 AND f.catalog ILIKE $2 AND f.deleted_at IS NULL
                   ORDER BY f.created_at DESC LIMIT $3""",
                    telegram_id,
                    catalog,
                    limit,
                )
            )
        return list(
            await self.ready().fetch(
                """SELECT f.* FROM files f JOIN users u ON u.id=f.user_id
               WHERE u.telegram_id=$1 AND f.deleted_at IS NULL
                 AND (f.title ILIKE '%'||$2||'%' OR EXISTS(
                    SELECT 1 FROM unnest(f.tags) t WHERE t ILIKE '%'||$2||'%') OR f.catalog ILIKE '%'||$2||'%')
               ORDER BY CASE WHEN lower(f.title)=lower($2) THEN 0 ELSE 1 END,f.created_at DESC LIMIT $3""",
                telegram_id,
                query,
                limit,
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
        return await self.ready().fetchrow(
            f"""UPDATE files f SET {field}=$3,updated_at=now() FROM users u
                WHERE f.user_id=u.id AND u.telegram_id=$1 AND f.id=$2 AND f.deleted_at IS NULL
                RETURNING f.*""",
            telegram_id,
            parsed,
            value,
        )

    async def delete_file(self, telegram_id: int, file_id: str) -> bool:
        parsed = safe_uuid(file_id)
        if not parsed:
            return False
        result = await self.ready().execute(
            """DELETE FROM files f USING users u WHERE f.user_id=u.id
               AND u.telegram_id=$1 AND f.id=$2""",
            telegram_id,
            parsed,
        )
        return result.endswith("1")

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
            """SELECT f.code,f.title,f.file_type,f.catalog,f.tags,f.is_favorite,f.created_at
               FROM files f JOIN users u ON u.id=f.user_id
               WHERE u.telegram_id=$1 AND f.deleted_at IS NULL ORDER BY f.created_at""",
            telegram_id,
        )
        return {
            "user": jsonable(user),
            "channel": jsonable(channel),
            "files": jsonable(files),
        }

    async def delete_user(self, telegram_id: int) -> bool:
        result = await self.ready().execute(
            "DELETE FROM users WHERE telegram_id=$1", telegram_id
        )
        return result.endswith("1")

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


db = Database(settings.database_url.get_secret_value())
bot = Bot(
    settings.bot_token.get_secret_value(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
router = Router(name="keepgram")
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)


class Flow(StatesGroup):
    search = State()
    rename = State()
    tags = State()
    catalog_name = State()
    save_text = State()
    delete_account = State()


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


bot_rate_limiter = BotRateLimitMiddleware()
router.message.outer_middleware(bot_rate_limiter)
router.callback_query.outer_middleware(bot_rate_limiter)


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Saqlash"), KeyboardButton(text="🔎 Qidirish")],
        [KeyboardButton(text="🗂 Kataloglar"), KeyboardButton(text="🏷 Teglar")],
        [KeyboardButton(text="🔢 Kod bo‘yicha"), KeyboardButton(text="🕘 Oxirgilari")],
        [KeyboardButton(text="⭐ Sevimlilar"), KeyboardButton(text="⚙️ Sozlamalar")],
        [KeyboardButton(text="ℹ️ Yordam")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Fayl, kod yoki qidiruv matnini yuboring",
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


def file_actions(file_id: Any, favorite: bool = False) -> InlineKeyboardMarkup:
    fid = str(file_id)
    return ikb(
        [
            [("📤 Olish", f"f:get:{fid}"), ("✏️ Nom", f"f:ren:{fid}")],
            [("🏷 Teglar", f"f:tag:{fid}"), ("🗂 Katalog", f"f:cat:{fid}")],
            [
                ("☆ Sevimlidan" if favorite else "⭐ Sevimli", f"f:fav:{fid}"),
                ("🗑 O‘chirish", f"f:del:{fid}"),
            ],
        ]
    )


def file_card(row: asyncpg.Record) -> str:
    tags = " ".join(f"#{esc(tag)}" for tag in (row["tags"] or [])) or "yo‘q"
    created = row["created_at"].astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return (
        f"{file_emoji(row['file_type'])} <b>{esc(row['title'])}</b>\n\n"
        f"🔢 Kod: <code>{esc(row['code'])}</code>\n"
        f"🗂 Katalog: {esc(row['catalog'])}\n"
        f"🏷 Teglar: {tags}\n"
        f"📅 {created}"
    )


async def actor_user(event: Message | CallbackQuery) -> asyncpg.Record | None:
    tg_user = event.from_user
    if not tg_user:
        return None
    user = await db.upsert_user(tg_user)
    if user["is_blocked"]:
        if isinstance(event, CallbackQuery):
            await event.answer("Hisobingiz bloklangan.", show_alert=True)
        else:
            await event.answer(
                "🚫 Hisobingiz vaqtincha bloklangan. Administrator bilan bog‘laning."
            )
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


async def send_stored_file(chat_id: int, row: asyncpg.Record) -> bool:
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=row["telegram_channel_id"],
            message_id=row["channel_message_id"],
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        await db.update_file(chat_id, str(row["id"]), "is_missing", True)
        await bot.send_message(
            chat_id,
            "⚠️ Fayl storage kanaldan o‘chirilgan yoki bot kanalga kira olmayapti.",
        )
        return False


async def save_message(message: Message) -> None:
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
    if not await db.ping():
        await message.answer(
            "⚠️ Katalog bazasi vaqtincha ishlamayapti. Fayl saqlanmadi; keyinroq qayta yuboring."
        )
        return
    file_type, title, unique_id, size = content_metadata(message)
    try:
        copied = await bot.copy_message(
            chat_id=storage["telegram_channel_id"],
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await db.mark_channel_inactive(message.from_user.id)
        await message.answer(
            "⚠️ Kanal bilan aloqa uzildi. Botni kanalga qayta admin qilib, kanalni qayta ulang."
        )
        return
    try:
        row = await db.create_file(
            message.from_user.id, copied.message_id, title, file_type, unique_id, size
        )
    except Exception:
        log.exception("File index insert failed; attempting orphan cleanup")
        try:
            await bot.delete_message(storage["telegram_channel_id"], copied.message_id)
        except Exception:  # noqa: BLE001 - cleanup failure must not hide the original DB failure
            log.warning(
                "Orphan cleanup failed for channel_message_id=%s", copied.message_id
            )
        await message.answer(
            "⚠️ Indeksni yaratib bo‘lmadi. Operatsiya bekor qilindi; qayta urinib ko‘ring."
        )
        return
    if storage["index_message_enabled"]:
        tags = " ".join(f"#T_{tag}" for tag in row["tags"])
        try:
            await bot.send_message(
                storage["telegram_channel_id"],
                f"🗂 KEEPGRAM INDEX\n#C_{row['code']}\n📝 {esc(row['title'])}\n#K_{esc(row['catalog'])} {tags}",
            )
        except Exception:  # noqa: BLE001 - optional index must never fail a successful save
            log.warning("Optional channel index message failed")
    await message.answer(
        f"✅ <b>Saqlandi</b>\n\n📝 {esc(row['title'])}\n🔢 Kod: <code>{row['code']}</code>\n"
        f"🗂 Katalog: {esc(row['catalog'])}\n🏷 Teglar: yo‘q",
        reply_markup=file_actions(row["id"], row["is_favorite"]),
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await actor_user(message):
        return
    storage = await db.storage_by_tg(message.from_user.id)
    text = (
        "👋 <b>Assalomu alaykum! Men KeepGram — shaxsiy Telegram fayl omboringizman.</b>\n\n"
        "📦 Fayllarni o‘zingizning shaxsiy kanalingizda saqlayman\n"
        "🔎 Nomi, katalogi yoki tegi orqali topaman\n"
        "🔢 Maxsus kod bilan bir zumda qaytaraman\n\n"
        "Men faylni serverga yuklamayman — Telegram ichida nusxalayman."
    )
    rows = [[("ℹ️ Qanday ishlaydi?", "info:how"), ("🔐 Maxfiylik", "info:privacy")]]
    if not storage:
        text += "\n\nBoshlash uchun shaxsiy kanalingizni ulang 👇"
        rows.insert(0, [("🔗 Kanalni ulash", "channel:link")])
    else:
        text += f"\n\n✅ Ulangan kanal: <b>{esc(storage['channel_title'])}</b>"
    await message.answer(text, reply_markup=ikb(rows))
    await message.answer("Asosiy menyu:", reply_markup=MAIN_MENU)


@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await actor_user(message)
    await message.answer("KeepGram asosiy menyusi:", reply_markup=MAIN_MENU)


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == "ℹ️ Yordam", F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message) -> None:
    if not await actor_user(message):
        return
    await message.answer(
        "<b>KeepGram yordam</b>\n\n"
        "1. /channel orqali shaxsiy kanalingizni ulang.\n"
        "2. Fayl, rasm, video yoki audioni botga yuboring.\n"
        "3. Bot bergan 6 belgili kodni saqlab qo‘ying.\n"
        "4. Kodni yuboring yoki 🔎 Qidirish orqali faylni toping.\n\n"
        "/recent — oxirgilari\n/catalogs — kataloglar\n/tags — teglar\n/settings — sozlamalar\n"
        "/mydata — saqlangan metadata\n/delete_my_data — metadata hisobini o‘chirish\n/privacy — maxfiylik\n/cancel — amalni bekor qilish"
    )


@router.message(Command("privacy"), F.chat.type == ChatType.PRIVATE)
async def cmd_privacy(message: Message) -> None:
    if not await actor_user(message):
        return
    await message.answer(
        "🔐 <b>Maxfiylik</b>\n\nFayllar KeepGram serverida saqlanmaydi; ular siz ulagan Telegram kanalida qoladi. "
        "Bazaga faqat nom, kod, katalog, teg, kanal ID va xabar ID kabi indeks metadata yoziladi. "
        "Telefon raqamingiz faqat o‘zingiz kontakt tugmasi orqali ulashsangiz saqlanadi. "
        "Admin panel real faylni ko‘rsatmaydi. Kanal va Telegram hisobingiz xavfsizligi sizning nazoratingizda."
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
    if not await actor_user(callback):
        return
    await callback.answer()
    await callback.message.answer(
        "Fayl baytlari yuklab olinmaydi; faqat Telegram <code>copyMessage</code> amali va kichik metadata indeksi ishlatiladi."
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


@router.message(F.text == "📥 Saqlash", F.chat.type == ChatType.PRIVATE)
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
        "Fayl, rasm, video, audio yoki saqlamoqchi bo‘lgan matnni yuboring. /cancel — bekor qilish."
    )


@router.message(
    Flow.save_text, F.text, ~F.text.startswith("/"), F.chat.type == ChatType.PRIVATE
)
async def save_text_state(message: Message, state: FSMContext) -> None:
    await state.clear()
    await save_message(message)


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
    if not await actor_user(message):
        return
    if message.contact and message.contact.user_id == message.from_user.id:
        await db.update_phone(message.from_user.id, message.contact.phone_number)
        await state.clear()
        await message.answer(
            "✅ Telefon raqamingiz ixtiyoriy metadata sifatida saqlandi.",
            reply_markup=MAIN_MENU,
        )
    else:
        await state.clear()
        await save_message(message)


@router.message(Command("recent"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == "🕘 Oxirgilari", F.chat.type == ChatType.PRIVATE)
@router.message(Command("fayllarim"), F.chat.type == ChatType.PRIVATE)
async def recent_files(message: Message) -> None:
    if not await actor_user(message):
        return
    await show_files(message)


@router.message(F.text == "⭐ Sevimlilar", F.chat.type == ChatType.PRIVATE)
async def favorite_files(message: Message) -> None:
    if not await actor_user(message):
        return
    await show_files(message, favorite=True)


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
    await message.answer("✅ Nomi yangilandi." if row else "Fayl topilmadi.")


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
                [("🧾 Faqat indeks", f"f:dx:{file_id}")],
                [("🗑 Kanal + indeks", f"f:da:{file_id}")],
                [("❌ Bekor", "noop")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("f:dx:"))
async def delete_index(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    removed = await db.delete_file(
        callback.from_user.id, callback.data.rsplit(":", 1)[1]
    )
    await callback.answer(
        "Indeks o‘chirildi" if removed else "Topilmadi", show_alert=True
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
    try:
        await bot.delete_message(row["telegram_channel_id"], row["channel_message_id"])
    except TelegramBadRequest as exc:
        if "message to delete not found" not in str(exc).lower():
            await callback.answer(
                "Botda o‘chirish huquqi yo‘q yoki Telegram rad etdi.", show_alert=True
            )
            return
    except TelegramForbiddenError:
        await callback.answer("Bot kanalda admin emas.", show_alert=True)
        return
    await db.delete_file(callback.from_user.id, file_id)
    await callback.answer("Kanal va indeksdan o‘chirildi", show_alert=True)


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
    await message.answer("Fayl nomi, #teg, catalog:Nomi yoki 6 belgili kodni yuboring.")


@router.message(
    F.text.in_({"🔎 Qidirish", "🔢 Kod bo‘yicha"}), F.chat.type == ChatType.PRIVATE
)
async def begin_search(message: Message, state: FSMContext) -> None:
    if not await actor_user(message):
        return
    await state.set_state(Flow.search)
    await message.answer(
        "6 belgili kodni yuboring."
        if message.text.startswith("🔢")
        else "Fayl nomi, #teg yoki catalog:Nomi yozing."
    )


async def run_search(message: Message, query: str) -> None:
    rows = await db.search_files(message.from_user.id, query)
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


@router.message(Command("catalogs"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == "🗂 Kataloglar", F.chat.type == ChatType.PRIVATE)
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
@router.message(F.text == "🏷 Teglar", F.chat.type == ChatType.PRIVATE)
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
@router.message(F.text == "⚙️ Sozlamalar", F.chat.type == ChatType.PRIVATE)
async def settings_menu(message: Message) -> None:
    if not await actor_user(message):
        return
    setting = await db.setting(message.from_user.id)
    index_on = bool(setting and setting["index_message_enabled"])
    fav_on = bool(setting and setting["default_favorite"])
    default_catalog = setting["default_catalog"] if setting else "Umumiy"
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
                [("🗑 Ma’lumotlarni o‘chirish", "settings:delete")],
            ]
        ),
    )


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
        "Telefon ixtiyoriy. Faqat o‘zingiz xohlasangiz ulashing:", reply_markup=keyboard
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


@router.callback_query(F.data.in_({"settings:index", "settings:favorite"}))
async def setting_toggle(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    row = await db.setting(callback.from_user.id)
    field = (
        "index_message_enabled"
        if callback.data.endswith("index")
        else "default_favorite"
    )
    value = not bool(row and row[field])
    await db.update_setting(callback.from_user.id, field, value)
    await callback.answer(f"{'Yoqildi' if value else 'O‘chirildi'}", show_alert=True)


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
    if not await actor_user(message):
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
    await state.clear()
    await message.answer(
        "✅ KeepGram’dagi metadata hisobingiz o‘chirildi. Telegram kanal fayllariga tegilmadi.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("bekor"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == "❌ Bekor", F.chat.type == ChatType.PRIVATE)
async def cancel(message: Message, state: FSMContext) -> None:
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


login_attempts: dict[str, list[float]] = {}


def session_admin(request: Request) -> str:
    username = request.session.get("admin")
    if not username:
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
    await db.connect()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="KeepGram’ni boshlash"),
            BotCommand(command="menu", description="Asosiy menyu"),
            BotCommand(command="search", description="Fayl qidirish"),
            BotCommand(command="recent", description="Oxirgi fayllar"),
            BotCommand(command="catalogs", description="Kataloglar"),
            BotCommand(command="tags", description="Teglar"),
            BotCommand(command="settings", description="Sozlamalar"),
            BotCommand(command="channel", description="Storage kanal"),
            BotCommand(command="mydata", description="Metadata eksporti"),
            BotCommand(command="privacy", description="Maxfiylik"),
            BotCommand(command="help", description="Yordam"),
        ]
    )
    webhook_secret = settings.webhook_secret.get_secret_value()
    webhook_url = f"{settings.app_base_url}/telegram/webhook/{webhook_secret}"
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
        await bot.session.close()
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
    ok = await db.ping()
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "app": APP_NAME,
            "version": APP_VERSION,
            "database": ok,
        },
        status_code=200 if ok else 503,
    )


@app.post("/telegram/webhook/{path_secret}", include_in_schema=False)
async def telegram_webhook(path_secret: str, request: Request) -> Response:
    expected = settings.webhook_secret.get_secret_value()
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(path_secret, expected) or not hmac.compare_digest(
        header, expected
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Telegram update processing failed")
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


@app.post("/api/admin/login")
async def admin_login(body: LoginBody, request: Request) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = [value for value in login_attempts.get(ip, []) if now - value < 600]
    if len(attempts) >= 5:
        raise HTTPException(429, "10 daqiqada juda ko‘p noto‘g‘ri urinish")
    username_ok = hmac.compare_digest(body.username, settings.admin_username)
    try:
        password_ok = bcrypt.checkpw(
            body.password.encode(),
            settings.admin_password_hash.get_secret_value().encode(),
        )
    except ValueError:
        log.error("ADMIN_PASSWORD_HASH yaroqsiz bcrypt hash")
        password_ok = False
    if not username_ok or not password_ok:
        attempts.append(now)
        login_attempts[ip] = attempts
        await db.audit(
            "admin",
            body.username[:100],
            "login_failed",
            metadata={"ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:16]},
        )
        raise HTTPException(401, "Login yoki parol noto‘g‘ri")
    login_attempts.pop(ip, None)
    request.session.clear()
    request.session["admin"] = settings.admin_username
    request.session["csrf"] = secrets.token_urlsafe(24)
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
                  (SELECT count(*) FROM files WHERE deleted_at IS NULL)::int files,
                  (SELECT count(*) FROM users WHERE last_seen_at>now()-interval '24 hours')::int active_24h,
                  (SELECT count(*) FROM users WHERE is_blocked)::int blocked"""
    )
    recent_users = await db.ready().fetch(
        "SELECT telegram_id,username,first_name,created_at FROM users ORDER BY created_at DESC LIMIT 7"
    )
    recent_files = await db.ready().fetch(
        "SELECT title,code,file_type,created_at FROM files WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 7"
    )
    return {
        **jsonable(row),
        "recent_users": jsonable(recent_users),
        "recent_files": jsonable(recent_files),
    }


@app.get("/api/admin/users")
async def admin_users(
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    where = """$1='' OR u.telegram_id::text ILIKE '%'||$1||'%' OR COALESCE(u.username,'') ILIKE '%'||$1||'%'
               OR COALESCE(u.first_name,'') ILIKE '%'||$1||'%' OR COALESCE(u.last_name,'') ILIKE '%'||$1||'%'
               OR COALESCE(u.phone,'') ILIKE '%'||$1||'%'"""
    total = await db.ready().fetchval(
        f"SELECT count(*) FROM users u WHERE {where}", search
    )
    rows = await db.ready().fetch(
        f"""SELECT u.id,u.telegram_id,u.username,u.first_name,u.last_name,u.phone,u.is_blocked,
                   u.created_at,u.last_seen_at,s.channel_title,s.telegram_channel_id,
                   count(f.id) FILTER(WHERE f.deleted_at IS NULL)::int file_count
            FROM users u LEFT JOIN storage_channels s ON s.user_id=u.id
            LEFT JOIN files f ON f.user_id=u.id WHERE {where}
            GROUP BY u.id,s.id ORDER BY u.created_at DESC LIMIT $2 OFFSET $3""",
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
                  count(f.id) FILTER(WHERE f.deleted_at IS NULL)::int file_count,
                  count(f.id) FILTER(WHERE f.is_favorite AND f.deleted_at IS NULL)::int favorite_count
           FROM users u LEFT JOIN storage_channels s ON s.user_id=u.id LEFT JOIN files f ON f.user_id=u.id
           WHERE u.id=$1 GROUP BY u.id,s.id""",
        user_id,
    )
    if not user:
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    files = await db.ready().fetch(
        """SELECT id,title,code,file_type,catalog,tags,is_favorite,is_missing,channel_message_id,created_at
           FROM files WHERE user_id=$1 AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 100""",
        user_id,
    )
    return {"user": jsonable(user), "files": jsonable(files)}


@app.post("/api/admin/users/{user_id}/block")
async def admin_block_user(
    user_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    result = await db.ready().execute(
        "UPDATE users SET is_blocked=true WHERE id=$1", user_id
    )
    if result.endswith("0"):
        raise HTTPException(404, "Foydalanuvchi topilmadi")
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
    await admin_log(admin, "unblock_user", "user", str(user_id))
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}/metadata")
async def admin_delete_user(
    user_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    await admin_log(admin, "delete_user_metadata", "user", str(user_id))
    result = await db.ready().execute("DELETE FROM users WHERE id=$1", user_id)
    if result.endswith("0"):
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    return {"ok": True}


@app.get("/api/admin/channels")
async def admin_channels(
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    total = await db.ready().fetchval("SELECT count(*) FROM storage_channels")
    rows = await db.ready().fetch(
        """SELECT s.id,s.telegram_channel_id,s.channel_title,s.channel_username,s.is_active,s.linked_at,
                  u.telegram_id,u.username,count(f.id)::int file_count
           FROM storage_channels s JOIN users u ON u.id=s.user_id LEFT JOIN files f ON f.channel_id=s.id
           GROUP BY s.id,u.id ORDER BY s.linked_at DESC LIMIT $1 OFFSET $2""",
        limit,
        (page - 1) * limit,
    )
    return {"items": jsonable(rows), "total": int(total), "page": page, "limit": limit}


@app.post("/api/admin/channels/{channel_id}/disconnect")
async def admin_disconnect_channel(
    channel_id: UUID, admin: str = Depends(csrf_admin)
) -> dict[str, bool]:
    await admin_log(admin, "disconnect_channel", "channel", str(channel_id))
    result = await db.ready().execute(
        "DELETE FROM storage_channels WHERE id=$1", channel_id
    )
    if result.endswith("0"):
        raise HTTPException(404, "Kanal topilmadi")
    return {"ok": True}


@app.get("/api/admin/files")
async def admin_files(
    search: str = Query("", max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    where = "$1='' OR f.title ILIKE '%'||$1||'%' OR f.code ILIKE '%'||$1||'%' OR u.telegram_id::text ILIKE '%'||$1||'%'"
    total = await db.ready().fetchval(
        f"SELECT count(*) FROM files f JOIN users u ON u.id=f.user_id WHERE f.deleted_at IS NULL AND ({where})",
        search,
    )
    rows = await db.ready().fetch(
        f"""SELECT f.id,f.title,f.code,f.file_type,f.catalog,f.tags,f.is_favorite,f.is_missing,
                   f.channel_message_id,f.created_at,u.telegram_id,u.username,s.channel_title
            FROM files f JOIN users u ON u.id=f.user_id JOIN storage_channels s ON s.id=f.channel_id
            WHERE f.deleted_at IS NULL AND ({where}) ORDER BY f.created_at DESC LIMIT $2 OFFSET $3""",
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


@app.get("/api/admin/audit-logs")
async def admin_audit_logs(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    _: str = Depends(session_admin),
) -> dict[str, Any]:
    total = await db.ready().fetchval("SELECT count(*) FROM audit_logs")
    rows = await db.ready().fetch(
        "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        limit,
        (page - 1) * limit,
    )
    return {"items": jsonable(rows), "total": int(total), "page": page, "limit": limit}


@app.get("/api/admin/system")
async def admin_system(_: str = Depends(session_admin)) -> dict[str, Any]:
    info = await bot.get_webhook_info()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "environment": settings.app_env,
        "database": await db.ping(),
        "webhook_url": info.url,
        "pending_updates": info.pending_update_count,
        "last_error": info.last_error_message,
    }


@app.exception_handler(asyncpg.PostgresError)
async def database_error(_: Request, exc: asyncpg.PostgresError) -> JSONResponse:
    log.error("Database error: %s", exc.__class__.__name__)
    return JSONResponse({"detail": "Ma’lumotlar bazasi xatosi"}, status_code=503)
