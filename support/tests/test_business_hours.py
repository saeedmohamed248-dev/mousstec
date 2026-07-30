"""Business-hours gate — the live-chat availability window.

Cairo 09:00–17:00, closed Friday. Pure datetime logic — we pass explicit
aware datetimes so the assertions don't depend on the wall clock.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from support.services.business_hours import (
    get_offline_message, get_status_payload, is_business_hours,
)

CAIRO = ZoneInfo("Africa/Cairo")


def _cairo(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=CAIRO)


class BusinessHoursTests(SimpleTestCase):
    def test_open_midday_on_a_weekday(self):
        # 2026-07-30 is a Thursday
        self.assertTrue(is_business_hours(_cairo(2026, 7, 30, 12)))

    def test_closed_before_opening(self):
        self.assertFalse(is_business_hours(_cairo(2026, 7, 30, 8, 30)))

    def test_closed_at_and_after_five(self):
        self.assertFalse(is_business_hours(_cairo(2026, 7, 30, 17, 0)))
        self.assertFalse(is_business_hours(_cairo(2026, 7, 30, 19, 0)))

    def test_closed_all_day_friday(self):
        # 2026-07-31 is a Friday
        self.assertFalse(is_business_hours(_cairo(2026, 7, 31, 12)))

    def test_status_payload_shape(self):
        payload = get_status_payload()
        for key in ('is_open', 'cairo_time', 'work_start', 'work_end',
                    'closed_days', 'offline_message'):
            self.assertIn(key, payload)
        self.assertEqual(payload['closed_days'], ['Friday'])

    def test_offline_message_non_empty(self):
        self.assertTrue(get_offline_message().strip())
