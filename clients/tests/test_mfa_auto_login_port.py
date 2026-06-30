"""Auto-login redirect port-carry helper (`_with_request_port`).

Regression guard for the MFA challenge redirect bug: after a correct TOTP code
the cross-subdomain auto-login redirect must keep the request's non-standard
dev port (:8000), otherwise it drops to port 80 → ERR_CONNECTION_REFUSED. The
password-only login already used this helper; the MFA path now does too.
"""
from __future__ import annotations

import unittest

from clients.views.auth_views import _with_request_port


class _FakeRequest:
    def __init__(self, host: str) -> None:
        self._host = host

    def get_host(self) -> str:
        return self._host


class WithRequestPortTests(unittest.TestCase):
    def test_dev_port_is_carried_onto_bare_domain(self) -> None:
        req = _FakeRequest("auto-garage-test.localhost:8000")
        self.assertEqual(
            _with_request_port("shop.localhost", req), "shop.localhost:8000")

    def test_standard_http_port_not_appended(self) -> None:
        req = _FakeRequest("shop.example.com:80")
        self.assertEqual(
            _with_request_port("shop.example.com", req), "shop.example.com")

    def test_standard_https_port_not_appended(self) -> None:
        req = _FakeRequest("shop.example.com:443")
        self.assertEqual(
            _with_request_port("shop.example.com", req), "shop.example.com")

    def test_no_port_on_request_leaves_domain_unchanged(self) -> None:
        req = _FakeRequest("shop.example.com")
        self.assertEqual(
            _with_request_port("shop.example.com", req), "shop.example.com")

    def test_domain_that_already_has_a_port_is_untouched(self) -> None:
        req = _FakeRequest("auto-garage-test.localhost:8000")
        self.assertEqual(
            _with_request_port("shop.localhost:9000", req), "shop.localhost:9000")


if __name__ == "__main__":
    unittest.main()
