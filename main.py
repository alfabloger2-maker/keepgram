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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

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
APP_VERSION = "1.3.0"
TERMS_VERSION = "1.0"
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
            "auto_manifest_enabled",
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
            """SELECT count(f.id)::int AS records,
                      COALESCE(sum(f.item_count),0)::int AS files,
                      COALESCE(sum(f.file_size),0)::bigint AS total_size
               FROM users u LEFT JOIN files f ON f.user_id=u.id AND f.deleted_at IS NULL
               WHERE u.telegram_id=$1 GROUP BY u.id""",
            telegram_id,
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
            return row

    async def file_by_id(
        self, telegram_id: int, file_id: UUID | str
    ) -> asyncpg.Record | None:
        parsed = safe_uuid(file_id)
        if not parsed:
            return None
        return await self.ready().fetchrow(
            """SELECT f.*,s.telegram_channel_id,
                      COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                FROM file_parts fp WHERE fp.file_id=f.id),
                               ARRAY[f.channel_message_id]) AS channel_message_ids
               FROM files f
               JOIN users u ON u.id=f.user_id JOIN storage_channels s ON s.id=f.channel_id
               WHERE f.id=$2 AND u.telegram_id=$1 AND f.deleted_at IS NULL""",
            telegram_id,
            parsed,
        )

    async def file_by_code(self, telegram_id: int, code: str) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            """SELECT f.*,s.telegram_channel_id,
                      COALESCE((SELECT array_agg(fp.channel_message_id ORDER BY fp.position)
                                FROM file_parts fp WHERE fp.file_id=f.id),
                               ARRAY[f.channel_message_id]) AS channel_message_ids
               FROM files f
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
        parsed = parse_search_query(query)
        filters = ["u.telegram_id=$1", "f.deleted_at IS NULL"]
        args: list[Any] = [telegram_id]

        def add(value: Any) -> str:
            args.append(value)
            return f"${len(args)}"

        if parsed["file_kind"]:
            filters.append(f"{add(parsed['file_kind'])}=ANY(f.file_kinds)")
        if parsed["date_start"]:
            filters.append(f"f.created_at>={add(parsed['date_start'])}")
            filters.append(f"f.created_at<{add(parsed['date_end'])}")
        if parsed["catalog"]:
            filters.append(f"f.catalog ILIKE {add(parsed['catalog'])}")
        if parsed["tag"]:
            filters.append(f"{add(parsed['tag'])}=ANY(f.tags)")
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
            return row, list(target["old_message_ids"] or [])

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
        if result.endswith("1"):
            await self.mark_manifest_dirty(telegram_id)
        return result.endswith("1")

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
        rows = await self.ready().fetch(
            """DELETE FROM files f USING users u WHERE f.user_id=u.id
               AND u.telegram_id=$1 AND f.id=ANY($2::uuid[]) RETURNING f.id""",
            telegram_id,
            parsed,
        )
        if rows:
            await self.mark_manifest_dirty(telegram_id)
        return len(rows)

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
        return await self.ready().fetchrow(
            "SELECT * FROM app_settings WHERE singleton=true"
        )

    async def set_super_backup_config(
        self, enabled: bool, channel_id: int | None
    ) -> asyncpg.Record:
        return await self.ready().fetchrow(
            """UPDATE app_settings SET super_backup_enabled=$1,
                      super_backup_channel_id=$2,updated_at=now()
               WHERE singleton=true RETURNING *""",
            enabled,
            channel_id,
        )

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
        if not parsed or new_status not in {"deleted", "replaced", "missing"}:
            return []
        return list(
            await self.ready().fetch(
                """UPDATE backup_assets SET status=$3,updated_at=now()
                   WHERE file_id=$2 AND owner_telegram_id=$1 AND status='active'
                   RETURNING *""",
                telegram_id,
                parsed,
                new_status,
            )
        )

    async def backup_asset(self, backup_id: UUID) -> asyncpg.Record | None:
        return await self.ready().fetchrow(
            "SELECT * FROM backup_assets WHERE id=$1", backup_id
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


db = Database(settings.database_url.get_secret_value())
bot = Bot(
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
        [KeyboardButton(text="📚 Barcha saqlanganlar")],
        [KeyboardButton(text="🗂 Kataloglar"), KeyboardButton(text="🏷 Teglar")],
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


async def send_terms(message: Message) -> None:
    await message.answer(
        "📜 <b>KeepGram foydalanish shartlari</b>\n\n"
        "KeepGram’dan foydalanish uchun quyidagilarga rozilik berishingiz kerak:\n\n"
        "• Ismingiz, tasdiqlangan telefon raqamingiz bazada saqlanadi.\n"
        "• Botga yuborgan fayllaringiz o‘zingiz ulagan Telegram kanaliga nusxalanadi.\n"
        f"Shartlar versiyasi: <code>{TERMS_VERSION}</code>\n"
        "Quyidagi tugmani bosish orqali ushbu shartlarga rozilik bildirasiz.",
        reply_markup=ikb([[('✅ Roziman va davom etaman', 'terms:accept')]]),
    )


def file_actions(file_id: Any, favorite: bool = False) -> InlineKeyboardMarkup:
    fid = str(file_id)
    return ikb(
        [
            [("📤 Olish", f"f:get:{fid}"), ("♻️ Almashtirish", f"f:replace:{fid}")],
            [("✏️ Nom", f"f:ren:{fid}"), ("🏷 Teglar", f"f:tag:{fid}")],
            [("🗂 Katalog", f"f:cat:{fid}")],
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
    return (
        f"{file_emoji(row['file_type'])} <b>{esc(row['title'])}</b>\n\n"
        f"🔢 Kod: <code>{esc(row['code'])}</code>\n"
        f"🧩 Turi: {esc(type_line)}\n"
        f"📦 Tarkib: {item_count} ta\n"
        f"🗂 Katalog: {esc(row['catalog'])}\n"
        f"🏷 Teglar: {tags}\n"
        f"📅 {created}"
    )


async def actor_user(
    event: Message | CallbackQuery, *, allow_incomplete: bool = False
) -> asyncpg.Record | None:
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
    if not await db.ping():
        await message.answer(
            "⚠️ Katalog bazasi vaqtincha ishlamayapti. Fayl saqlanmadi; keyinroq qayta yuboring."
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
    user = await actor_user(message, allow_incomplete=True)
    if not user:
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
@router.message(F.text == "ℹ️ Yordam", F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message) -> None:
    if not await actor_user(message):
        return
    await message.answer(
        "<b>KeepGram yordam</b>\n\n"
        "1. /channel orqali shaxsiy kanalingizni ulang.\n"
        "2. Fayl, rasm, video yoki audioni botga yuboring. Bir martada tanlangan albom bitta to‘plam bo‘lib saqlanadi.\n"
        "3. Bot bergan 6 belgili kodni saqlab qo‘ying.\n"
        "4. Kodni yuboring yoki 🔎 Qidirish orqali faylni toping.\n\n"
        "/recent — oxirgilari\n/all — barcha saqlanganlar menyusi\n/catalogs — kataloglar\n/tags — teglar\n/settings — sozlamalar\n"
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
@router.message(F.text == "🕘 Oxirgilari", F.chat.type == ChatType.PRIVATE)
@router.message(Command("fayllarim"), F.chat.type == ChatType.PRIVATE)
async def recent_files(message: Message) -> None:
    if not await actor_user(message):
        return
    await show_files(message)


@router.message(Command("all"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == "📚 Barcha saqlanganlar", F.chat.type == ChatType.PRIVATE)
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
                [("🧾 Faqat indeks", "bulk:delete:index")],
                [("🗑 Kanal + indeks", "bulk:delete:all")],
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
        f"✅ {changed} ta yozuv o‘chirildi."
        + (f" ⚠️ {failed} tasini kanaldan o‘chirib bo‘lmadi." if failed else "")
    )


@router.callback_query(F.data == "bulk:done")
async def bulk_done(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Tanlash tugatildi")
    if await actor_user(callback):
        await show_inventory(callback.message, callback.from_user.id, edit=True)


@router.message(F.text == "⭐ Sevimlilar", F.chat.type == ChatType.PRIVATE)
async def favorite_files(message: Message) -> None:
    if not await actor_user(message):
        return
    await show_files(message, favorite=True)


@router.message(Command("stats"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == "📊 Statistika", F.chat.type == ChatType.PRIVATE)
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
    file_id = callback.data.rsplit(":", 1)[1]
    if not await flush_pending_super_backup(callback.from_user.id, file_id):
        await callback.answer(
            "Backup nusxasi yaratilmagani uchun o‘chirish to‘xtatildi.", show_alert=True
        )
        return
    await update_super_backup_status(callback.from_user.id, file_id, "deleted")
    removed = await db.delete_file(callback.from_user.id, file_id)
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
    await message.answer(
        "Fayl nomi, #teg, catalog:Nomi, type:pdf, type:excel, date:2026-09 yoki 6 belgili kodni yuboring."
    )


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
    manifest_on = bool(setting and setting["auto_manifest_enabled"])
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
                [
                    (
                        f"🛟 Avto-manifest: {'ON' if manifest_on else 'OFF'}",
                        "settings:manifest",
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
    F.data.in_({"settings:index", "settings:favorite", "settings:manifest"})
)
async def setting_toggle(callback: CallbackQuery) -> None:
    if not await actor_user(callback):
        return
    row = await db.setting(callback.from_user.id)
    field = {
        "settings:index": "index_message_enabled",
        "settings:favorite": "default_favorite",
        "settings:manifest": "auto_manifest_enabled",
    }[callback.data]
    value = not bool(row and row[field])
    await db.update_setting(callback.from_user.id, field, value)
    if field == "auto_manifest_enabled" and value:
        await db.mark_manifest_dirty(callback.from_user.id)
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
    await state.clear()
    await message.answer(
        "✅ KeepGram’dagi metadata hisobingiz o‘chirildi. Telegram kanal fayllariga tegilmadi.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
@router.message(Command("bekor"), F.chat.type == ChatType.PRIVATE)
@router.message(F.text == "❌ Bekor", F.chat.type == ChatType.PRIVATE)
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
    sent = await bot.send_document(
        channel_id,
        BufferedInputFile(payload, filename="keepgram_restore_manifest.json"),
        caption=(
            "🛟 <b>KeepGram avtomatik tiklash manifesti</b>\n"
            "Bu fayl faqat indeks metadata va kanaldagi xabar IDlarini saqlaydi. "
            "Tiklash uchun botga /restore yuboring."
        ),
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
    while True:
        try:
            for row in await db.pending_manifest_users():
                try:
                    await publish_user_manifest(int(row["telegram_id"]))
                except (TelegramBadRequest, TelegramForbiddenError):
                    log.warning(
                        "Manifest backup channel unavailable for telegram_id=%s",
                        row["telegram_id"],
                    )
                except Exception:
                    log.exception(
                        "Manifest backup failed for telegram_id=%s", row["telegram_id"]
                    )
            await asyncio.sleep(15)
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
    while True:
        try:
            await db.requeue_stale_super_backups()
            for candidate in await db.pending_super_backups(TERMS_VERSION):
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
                    log.exception("Super backup worker failed for asset=%s", row["id"])
            await asyncio.sleep(1)
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
    await db.connect()
    if await db.ensure_schema():
        log.info("Fresh database detected; KeepGram schema installed automatically")
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="KeepGram’ni boshlash"),
            BotCommand(command="menu", description="Asosiy menyu"),
            BotCommand(command="search", description="Fayl qidirish"),
            BotCommand(command="recent", description="Oxirgi fayllar"),
            BotCommand(command="all", description="Barcha saqlanganlar"),
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
        ]
    )
    await db.ready().execute(
        """UPDATE storage_channels s SET manifest_dirty_at=now()
           FROM user_settings us WHERE us.user_id=s.user_id
             AND us.auto_manifest_enabled AND s.manifest_message_id IS NULL"""
    )
    manifest_worker_task = asyncio.create_task(manifest_backup_worker())
    super_backup_worker_task = asyncio.create_task(super_backup_worker())
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
                  (SELECT count(*) FROM users WHERE is_blocked)::int blocked"""
    )
    recent_users = await db.ready().fetch(
        """SELECT telegram_id,username,COALESCE(display_name,first_name) AS first_name,
                  created_at FROM users ORDER BY created_at DESC LIMIT 7"""
    )
    recent_files = await db.ready().fetch(
        "SELECT title,code,file_type,file_kinds,item_count,created_at FROM files WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 7"
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
               OR COALESCE(u.display_name,'') ILIKE '%'||$1||'%'
               OR COALESCE(u.first_name,'') ILIKE '%'||$1||'%' OR COALESCE(u.last_name,'') ILIKE '%'||$1||'%'
               OR COALESCE(u.phone,'') ILIKE '%'||$1||'%'"""
    total = await db.ready().fetchval(
        f"SELECT count(*) FROM users u WHERE {where}", search
    )
    rows = await db.ready().fetch(
        f"""SELECT u.id,u.telegram_id,u.username,COALESCE(u.display_name,u.first_name) AS first_name,
                   u.last_name,u.phone,u.onboarding_completed,u.terms_accepted_at,
                   u.terms_version,u.is_blocked,
                   u.created_at,u.last_seen_at,s.channel_title,s.telegram_channel_id,
                   COALESCE(sum(f.item_count) FILTER(WHERE f.deleted_at IS NULL),0)::int file_count
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
                  COALESCE(sum(f.item_count) FILTER(WHERE f.deleted_at IS NULL),0)::int file_count,
                  count(f.id) FILTER(WHERE f.is_favorite AND f.deleted_at IS NULL)::int favorite_count
           FROM users u LEFT JOIN storage_channels s ON s.user_id=u.id LEFT JOIN files f ON f.user_id=u.id
           WHERE u.id=$1 GROUP BY u.id,s.id""",
        user_id,
    )
    if not user:
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    files = await db.ready().fetch(
        """SELECT id,title,code,file_type,file_kinds,item_count,catalog,tags,
                  is_favorite,is_missing,channel_message_id,created_at
           FROM files WHERE user_id=$1 AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 100""",
        user_id,
    )
    user_payload = jsonable(user)
    user_payload["first_name"] = user_payload.get("display_name") or user_payload.get(
        "first_name"
    )
    return {"user": user_payload, "files": jsonable(files)}


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
                  u.telegram_id,u.username,COALESCE(sum(f.item_count),0)::int file_count
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
        f"""SELECT f.id,f.title,f.code,f.file_type,f.file_kinds,f.item_count,
                   f.catalog,f.tags,f.is_favorite,f.is_missing,
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
    limit: int = Query(25, ge=1, le=100),
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
                  count(*) FILTER(WHERE status='failed')::int failed
           FROM backup_assets"""
    )
    return {
        "items": jsonable(rows),
        "stats": jsonable(stats),
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
        "redis": await redis_healthy(),
        "webhook_url": info.url,
        "pending_updates": info.pending_update_count,
        "last_error": info.last_error_message,
    }


@app.exception_handler(asyncpg.PostgresError)
async def database_error(_: Request, exc: asyncpg.PostgresError) -> JSONResponse:
    log.error("Database error: %s", exc.__class__.__name__)
    return JSONResponse({"detail": "Ma’lumotlar bazasi xatosi"}, status_code=503)
