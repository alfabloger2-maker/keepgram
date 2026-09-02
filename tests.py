import os
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("BOT_TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_BASE_URL", "https://example.com")
os.environ.setdefault("WEBHOOK_SECRET", "abcdefghijklmnop")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "01234567890123456789012345678901")
os.environ.setdefault("MAX_FILES_PER_USER", "5000")
os.environ.setdefault("MAX_TOTAL_SIZE_MB", "51200")

import main


class KeepGramCoreTests(unittest.TestCase):
    def test_plain_admin_password_accepts_four_characters(self):
        configured = main.Settings(admin_password="1111")
        self.assertEqual(configured.admin_password.get_secret_value(), "1111")

    def test_codes_have_safe_six_character_alphabet(self):
        for _ in range(500):
            code = main.make_code()
            self.assertRegex(code, r"^[A-HJ-NP-Z2-9]{6}$")
            self.assertNotRegex(code, r"[OI01]")

    def test_link_token_format(self):
        self.assertRegex(main.make_link_token(), r"^LINK-[A-HJ-NP-Z2-9]{8}$")

    def test_tags_are_normalized_unique_and_limited(self):
        tags = main.normalize_tags("#Ish, 2026 ish, Bojxona!  juda-uzun")
        self.assertEqual(tags, ["ish", "2026", "bojxona", "juda-uzun"])
        self.assertEqual(
            len(main.normalize_tags(" ".join(f"t{i}" for i in range(20)))), 10
        )

    def test_document_types_are_classified(self):
        cases = {
            "scan.JPG": "image",
            "passport.pdf": "pdf",
            "contract.docx": "word",
            "budget.XLSX": "excel",
            "backup.bin": "other",
        }
        for file_name, expected in cases.items():
            with self.subTest(file_name=file_name):
                self.assertEqual(
                    main.classify_file_kind("document", file_name), expected
                )

    def test_duplicate_title_suffix_preserves_extension(self):
        self.assertEqual(main.title_with_suffix("passport.pdf", 2), "passport (2).pdf")
        self.assertEqual(main.title_with_suffix("Rasm", 3), "Rasm (3)")

    def test_inventory_contains_name_code_type_and_tags(self):
        text = main.inventory_page_text(
            [
                {
                    "title": "Passport",
                    "code": "ABC234",
                    "file_type": "pdf",
                    "file_kinds": ["pdf"],
                    "item_count": 1,
                    "tags": ["hujjat"],
                }
            ],
            total=1,
            page=1,
            pages=1,
        )
        self.assertIn("Passport", text)
        self.assertIn("ABC234", text)
        self.assertIn("PDF", text)
        self.assertIn("#hujjat", text)

    def test_code_detection_is_case_insensitive(self):
        self.assertTrue(main.CODE_RE.fullmatch("a2z7km"))
        self.assertFalse(main.CODE_RE.fullmatch("O0I1AA"))
        self.assertFalse(main.CODE_RE.fullmatch("ABCDE"))

    def test_uuid_parser_rejects_callback_garbage(self):
        value = uuid4()
        self.assertEqual(main.safe_uuid(str(value)), value)
        self.assertIsNone(main.safe_uuid("../../not-a-uuid"))

    def test_public_routes_exist(self):
        paths = {route.path for route in main.app.routes}
        self.assertIn("/health", paths)
        self.assertIn("/telegram/webhook", paths)
        self.assertIn("/admin", paths)
        self.assertIn("/admin/{unexpected_path:path}", paths)
        self.assertIn("/api/admin/session", paths)
        self.assertIn("/api/admin/backup/settings", paths)
        self.assertIn("/api/admin/backups", paths)
        self.assertIn("/api/admin/backups/{backup_id}/send", paths)
        self.assertIn("/api/admin/backups/{backup_id}/retry", paths)

    def test_advanced_search_filters(self):
        parsed = main.parse_search_query(
            "passport type:pdf date:2026-09 catalog:Hujjat #muhim"
        )
        self.assertEqual(parsed["text"], "passport")
        self.assertEqual(parsed["file_kind"], "pdf")
        self.assertEqual(parsed["date_start"].isoformat(), "2026-09-01T00:00:00+00:00")
        self.assertEqual(parsed["date_end"].isoformat(), "2026-10-01T00:00:00+00:00")
        self.assertEqual(parsed["catalog"], "Hujjat")
        self.assertEqual(parsed["tag"], "muhim")

    def test_advanced_search_rejects_invalid_month(self):
        with self.assertRaises(ValueError):
            main.parse_search_query("date:2026-13")

    def test_blank_redis_url_uses_memory_fallback(self):
        configured = main.Settings(redis_url="")
        self.assertIsNone(configured.redis_url)

    def test_signed_manifest_is_owner_scoped(self):
        raw = main.build_manifest_bytes({"owner_telegram_id": 123, "files": []})
        self.assertEqual(
            main.verify_manifest_bytes(raw, 123)["owner_telegram_id"], 123
        )
        with self.assertRaises(ValueError):
            main.verify_manifest_bytes(raw, 456)

    def test_terms_acceptance_is_one_time(self):
        self.assertTrue(
            main.terms_are_current(
                {"terms_accepted_at": "2026-09-01T00:00:00Z", "terms_version": main.TERMS_VERSION}
            )
        )
        self.assertTrue(
            main.terms_are_current(
                {"terms_accepted_at": "2026-09-01T00:00:00Z", "terms_version": "old"}
            )
        )
        self.assertFalse(
            main.terms_are_current({"terms_accepted_at": None, "terms_version": None})
        )

    def test_trash_retention_uses_typed_interval(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("make_interval(days => $1::int)", source)
        self.assertNotIn("($1::text||' days')::interval", source)

    def test_direct_code_handler_precedes_text_save_handler(self):
        source = Path("main.py").read_text(encoding="utf-8")
        direct = source.index("async def get_file_immediately_by_code")
        save_state = source.index("async def save_text_state")
        self.assertLess(direct, save_state)

    def test_schema_contains_mandatory_onboarding_fields(self):
        schema = Path("schema.sql").read_text(encoding="utf-8")
        self.assertIn("display_name text", schema)
        self.assertIn("onboarding_completed boolean not null default false", schema)
        self.assertIn("create table if not exists file_parts", schema)
        self.assertIn("file_kinds text[]", schema)
        self.assertIn("create table if not exists backup_assets", schema)
        self.assertIn("'processing'", schema)
        self.assertIn("manifest_message_id bigint", schema)
        self.assertIn("terms_accepted_at timestamptz", schema)
        self.assertIn("terms_version varchar(16)", schema)
        self.assertIn("preferred_language varchar(2)", schema)
        self.assertIn("create table if not exists processed_updates", schema)
        self.assertIn("create table if not exists user_counters", schema)
        self.assertIn("create table if not exists saved_views", schema)
        self.assertIn("create table if not exists reminders", schema)
        self.assertIn("create table if not exists share_tokens", schema)
        self.assertIn("files_trash_idx", schema)

    def test_file_card_supports_compact_and_detailed_modes(self):
        base = {
            "id": uuid4(), "title": "Passport.pdf", "file_type": "pdf",
            "file_kinds": ["pdf"], "tags": ["muhim"], "code": "ABC234",
            "catalog": "Hujjat", "item_count": 1, "file_size": 2048,
            "created_at": main.utcnow(),
        }
        compact = main.file_card({**base, "compact_cards": True})
        detailed = main.file_card({**base, "compact_cards": False})
        self.assertLess(len(compact), len(detailed))
        self.assertIn("2.0 KB", compact)
        self.assertIn("Hajm", detailed)

    def test_admin_uses_server_pagination_sizes(self):
        html = Path("admin.html").read_text(encoding="utf-8")
        self.assertIn("[10,15,50]", html)
        self.assertIn("/api/admin/users/${userFileId}/files", html)
        self.assertIn("channelSearch", html)

    def test_three_language_localization(self):
        self.assertEqual(main.localize_text("📥 Saqlash", "en"), "📥 Save")
        self.assertEqual(main.localize_text("📥 Saqlash", "ru"), "📥 Сохранить")
        self.assertIn("📥 Save", main.menu_variants("📥 Saqlash"))
        self.assertIn("📥 Сохранить", main.menu_variants("📥 Saqlash"))
        self.assertIn("KeepGram Terms of Use", main.terms_text("en"))
        self.assertIn("Условия использования KeepGram", main.terms_text("ru"))
        self.assertIn("KeepGram foydalanish shartlari", main.terms_text("uz"))

    def test_all_main_menu_buttons_have_three_variants(self):
        for row in main.MAIN_MENU.keyboard:
            for button in row:
                variants = main.menu_variants(button.text)
                self.assertGreaterEqual(len(variants), 3, button.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
