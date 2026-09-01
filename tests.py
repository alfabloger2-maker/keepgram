import os
import unittest
from uuid import uuid4

os.environ.setdefault("BOT_TOKEN", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("APP_BASE_URL", "https://example.com")
os.environ.setdefault("WEBHOOK_SECRET", "abcdefghijklmnop")
os.environ.setdefault(
    "ADMIN_PASSWORD_HASH",
    "$2b$12$00000000000000000000000000000000000000000000000000000",
)
os.environ.setdefault("SESSION_SECRET", "01234567890123456789012345678901")

import main


class KeepGramCoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
