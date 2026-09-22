"""Device health reports only write what the device actually sent.

`/telemetry/` documents its body as "any of" battery/temp/disk/wifi, so a
device that reports one reading must not blank the others — the control page
shows them side by side and a wiped field reads as a broken sensor.

No database: the device is a stub and `save()` is recorded, like the other
robot tests.
"""

from unittest import mock

from rest_framework.test import APIRequestFactory

from robot import views


class _Device:
    def __init__(self):
        self.battery_percent = 80
        self.cpu_temp = 41.5
        self.free_disk_mb = 2048
        self.wifi_rssi = -55
        self.telemetry_at = None
        self.saved_fields = None

    def save(self, update_fields=None):
        self.saved_fields = list(update_fields or [])


def _post(body, device):
    request = APIRequestFactory().post("/api/robot/v1/telemetry/", body, format="json")
    with mock.patch.object(views, "_device_or_401", return_value=(device, None)), \
            mock.patch.object(views.services, "raise_low_battery_alert") as alert:
        response = views.telemetry(request)
    return response, alert


from django.test import SimpleTestCase


class PartialReportsDoNotClobberTests(SimpleTestCase):

    def test_a_battery_only_report_leaves_the_other_readings_alone(self):
        device = _Device()
        response, _ = _post({"battery_percent": 64}, device)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(device.battery_percent, 64)
        self.assertEqual(device.cpu_temp, 41.5)
        self.assertEqual(device.free_disk_mb, 2048)
        self.assertEqual(device.wifi_rssi, -55)

    def test_only_the_reported_field_is_written(self):
        device = _Device()
        _post({"wifi_rssi": -70}, device)
        self.assertEqual(sorted(device.saved_fields), ["telemetry_at", "wifi_rssi"])

    def test_an_empty_report_still_stamps_the_time(self):
        device = _Device()
        _post({}, device)
        self.assertEqual(device.saved_fields, ["telemetry_at"])
        self.assertIsNotNone(device.telemetry_at)

    def test_a_full_report_writes_everything(self):
        device = _Device()
        _post({"battery_percent": 10, "cpu_temp": 60.0,
               "free_disk_mb": 100, "wifi_rssi": -80}, device)
        self.assertEqual(device.battery_percent, 10)
        self.assertEqual(device.cpu_temp, 60.0)
        self.assertEqual(device.free_disk_mb, 100)
        self.assertEqual(device.wifi_rssi, -80)


class GarbageReadingsAreDroppedTests(SimpleTestCase):

    def test_a_non_numeric_reading_becomes_none_instead_of_a_500(self):
        device = _Device()
        response, _ = _post({"battery_percent": "half"}, device)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(device.battery_percent)

    def test_a_low_battery_raises_one_alert(self):
        device = _Device()
        _, alert = _post({"battery_percent": 9}, device)
        alert.assert_called_once()

    def test_a_healthy_battery_raises_nothing(self):
        device = _Device()
        _, alert = _post({"battery_percent": 55}, device)
        alert.assert_not_called()
