"""The robot API is rate-limited per device, not by the anonymous-per-IP limit.

The endpoints authenticate by device token, so DRF treated every call as an
anonymous visitor and applied the global 30/minute-per-IP limit: the ESP32-CAM
(a frame every 0.2–1.5 s) plus heartbeats and command polling hit 429 within
seconds. Kiosk customer/invoice lookups keep a tight limit of their own.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from robot import views

_LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                       'LOCATION': 'robot-rate-limit-tests'}}


def _get(view, path, token, **params):
    request = APIRequestFactory().get(path, params, HTTP_X_ROBOT_TOKEN=token)
    with mock.patch.object(views, "_device_or_401", return_value=(mock.MagicMock(), None)):
        return view(request)


@override_settings(CACHES=_LOCMEM)
class RobotRateLimitTests(SimpleTestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_a_streaming_robot_is_not_cut_off_at_30_a_minute(self):
        codes = []
        with mock.patch.object(views, "RobotCommand"):
            for _ in range(60):
                codes.append(_get(views.commands_pending, "/api/robot/v1/commands/pending/",
                                  "device-a").status_code)
        self.assertNotIn(429, codes)

    def test_kiosk_customer_lookups_stay_slow(self):
        with mock.patch("robot.kiosk.customer_brief", return_value={"found": False}):
            codes = [_get(views.kiosk_customer, "/api/robot/v1/kiosk/customer/", "device-b",
                          phone=f"0100000{i:04d}").status_code for i in range(25)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[-1], 429)

    def test_one_device_does_not_spend_another_devices_budget(self):
        with mock.patch("robot.kiosk.customer_brief", return_value={"found": False}):
            for i in range(21):
                _get(views.kiosk_customer, "/api/robot/v1/kiosk/customer/", "device-c", phone=str(i))
            other = _get(views.kiosk_customer, "/api/robot/v1/kiosk/customer/", "device-d", phone="1")
        self.assertEqual(other.status_code, 200)
