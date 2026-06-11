"""
Tests for erp_core.ai._safety — the shared SSRF / secret-redaction / rate-limit
helpers that every AI caller must use. These were previously untested at the
erp_core level (only indirectly exercised via clients.tests).
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from django.core.cache import cache
from django.test import TestCase

from erp_core.ai._safety import (
    safe_log_text,
    safe_fetch_image,
    check_ai_rate_limit,
)


class SafeLogTextTests(TestCase):
    """Secrets must never reach the log handler in raw form."""

    def test_redacts_bearer_token(self):
        out = safe_log_text("Authorization: Bearer abcd1234efgh5678ijkl")
        self.assertNotIn("abcd1234efgh5678ijkl", out)
        self.assertIn("***REDACTED***", out)

    def test_redacts_openai_style_key(self):
        out = safe_log_text("key=sk-AAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertNotIn("sk-AAAAAAAAAAAAAAAAAAAAAAAA", out)

    def test_redacts_together_key(self):
        out = safe_log_text("tgp_v1_abcdefghijklmnopqrstuv")
        self.assertNotIn("tgp_v1_abcdefghijklmnopqrstuv", out)

    def test_redacts_google_key(self):
        out = safe_log_text("AIza0123456789ABCDEFGHIJKLMNOPQRSTUVWX")
        self.assertNotIn("AIza0123456789ABCDEFGHIJKLMNOPQRSTUVWX", out)

    def test_redacts_apikey_assignment(self):
        out = safe_log_text('api_key="MYSUPERSECRETKEY1234"')
        self.assertNotIn("MYSUPERSECRETKEY1234", out)

    def test_truncates_long_text(self):
        long = "x" * 1000
        out = safe_log_text(long, max_len=100)
        # 100 chars + the ellipsis sentinel
        self.assertLessEqual(len(out), 101)

    def test_none_safe(self):
        self.assertEqual(safe_log_text(None), "")


class SafeFetchImageTests(TestCase):
    """SSRF protection: scheme/host whitelist + private-IP block."""

    def test_blocks_non_http_scheme(self):
        self.assertIsNone(safe_fetch_image("file:///etc/passwd"))
        self.assertIsNone(safe_fetch_image("ftp://example.com/x.png"))

    def test_blocks_empty_url(self):
        self.assertIsNone(safe_fetch_image(""))
        self.assertIsNone(safe_fetch_image(None))

    def test_blocks_non_whitelisted_host(self):
        self.assertIsNone(safe_fetch_image("https://example.com/x.png"))
        self.assertIsNone(safe_fetch_image("https://evil.attacker.com/x.png"))

    @patch("erp_core.ai._safety._is_private_ip", return_value=False)
    @patch("erp_core.ai._safety.requests.get")
    def test_allows_whitelisted_host(self, mock_get, _mock_ip):
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [b"PNGDATA"]
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        mock_get.return_value = resp

        data = safe_fetch_image("https://api.together.xyz/img/x.png")
        self.assertEqual(data, b"PNGDATA")

    @patch("erp_core.ai._safety._is_private_ip", return_value=False)
    @patch("erp_core.ai._safety.requests.get")
    def test_allows_subdomain_of_whitelisted_host(self, mock_get, _mock_ip):
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [b"OK"]
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        mock_get.return_value = resp

        data = safe_fetch_image("https://pbxt.replicate.delivery/x/y.png")
        self.assertEqual(data, b"OK")

    @patch("erp_core.ai._safety._is_private_ip", return_value=False)
    @patch("erp_core.ai._safety.requests.get")
    def test_respects_extra_allowed_hosts(self, mock_get, _mock_ip):
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [b"OK"]
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        mock_get.return_value = resp

        data = safe_fetch_image(
            "https://my-cdn.example.com/x.png",
            extra_allowed_hosts={"my-cdn.example.com"},
        )
        self.assertEqual(data, b"OK")

    @patch("erp_core.ai._safety._is_private_ip", return_value=True)
    def test_blocks_private_ip(self, _mock_ip):
        # Host is whitelisted, but IP resolves to private → must still block.
        self.assertIsNone(safe_fetch_image("https://api.together.xyz/x.png"))

    @patch("erp_core.ai._safety._is_private_ip", return_value=False)
    @patch("erp_core.ai._safety.requests.get")
    def test_caps_response_size(self, mock_get, _mock_ip):
        # 2 chunks of 1 MB each → exceeds max_bytes=1 MB cap.
        big_chunk = b"x" * (1 * 1024 * 1024)
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content.return_value = [big_chunk, big_chunk]
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        mock_get.return_value = resp

        data = safe_fetch_image(
            "https://api.together.xyz/x.png",
            max_bytes=1 * 1024 * 1024,
        )
        self.assertIsNone(data)

    @patch("erp_core.ai._safety._is_private_ip", return_value=False)
    @patch("erp_core.ai._safety.requests.get")
    def test_non_200_returns_none(self, mock_get, _mock_ip):
        resp = MagicMock()
        resp.status_code = 404
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        mock_get.return_value = resp
        self.assertIsNone(safe_fetch_image("https://api.together.xyz/missing.png"))


class RateLimitTests(TestCase):
    """Per-tenant/user sliding bucket. Cache failures must fail OPEN."""

    def setUp(self):
        cache.clear()

    def test_under_limit_is_allowed(self):
        for _ in range(3):
            ok, msg = check_ai_rate_limit("test_user", per_minute=5, per_hour=100)
            self.assertTrue(ok)
            self.assertIsNone(msg)

    def test_per_minute_limit_blocks(self):
        for _ in range(5):
            check_ai_rate_limit("test_block", per_minute=5, per_hour=100)
        ok, msg = check_ai_rate_limit("test_block", per_minute=5, per_hour=100)
        self.assertFalse(ok)
        self.assertIn("دقيقة", msg)

    def test_per_hour_limit_blocks(self):
        for _ in range(10):
            check_ai_rate_limit("test_hour", per_minute=100, per_hour=10)
        ok, msg = check_ai_rate_limit("test_hour", per_minute=100, per_hour=10)
        self.assertFalse(ok)
        self.assertIn("ساعة", msg)

    def test_different_keys_isolated(self):
        for _ in range(5):
            check_ai_rate_limit("key_a", per_minute=5, per_hour=100)
        # key_a is now at limit, key_b must still be allowed
        ok, _ = check_ai_rate_limit("key_b", per_minute=5, per_hour=100)
        self.assertTrue(ok)

    @patch("erp_core.ai._safety.cache.get", side_effect=RuntimeError("cache down"))
    def test_cache_failure_fails_open(self, _broken):
        # If the cache backend explodes, legitimate users must NOT be blocked.
        ok, msg = check_ai_rate_limit("test_x", per_minute=5, per_hour=100)
        self.assertTrue(ok)
        self.assertIsNone(msg)
