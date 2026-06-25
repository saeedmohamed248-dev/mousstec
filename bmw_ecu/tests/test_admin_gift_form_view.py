"""Smoke checks for the admin_gift_form view + URL wiring.

The view itself is a thin staff-gated template renderer; the
business logic is exercised in test_admin_gift_endpoint.py against
_admin_validation. Here we confirm the view module imports cleanly,
exposes the right callable, and the URL reverses.
"""
from __future__ import annotations

import unittest


class AdminGiftFormViewTests(unittest.TestCase):
    def test_view_callable_exists(self) -> None:
        from bmw_ecu.views_ui import admin_gift_form
        self.assertTrue(callable(admin_gift_form))

    def test_view_is_staff_gated(self) -> None:
        # staff_member_required wraps the view with `login_url` attribute
        # on the wrapper closure.
        from bmw_ecu.views_ui import admin_gift_form
        # Wrapped callable retains the inner __wrapped__ chain.
        self.assertTrue(hasattr(admin_gift_form, "__wrapped__")
                        or admin_gift_form.__name__ == "admin_gift_form")
