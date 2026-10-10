"""Working through an internet outage, then catching up.

While the net is down the bridge records what people say on its SD card and
the camera keeps motion photos on its own card. When it's back, each clip is
sent to /voice/offline/ and each photo to /snapshot/ with the time it was
taken, and each board sends a summary of the outage.
"""

from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_product
from robot import audio, security, services, views
from robot.models import (
    RobotAlert, RobotDevice, RobotStockTakeLine, RobotStockTakeSession,
    RobotSyncEvent, RobotVoiceInteraction,
)


def _clip_request(uid="aabb-1", recorded_at=0, ptt="0"):
    data = {"audio": SimpleUploadedFile("q.wav", b"RIFF....WAVE", "audio/wav"),
            "client_uid": uid, "recorded_at": str(recorded_at), "ptt": ptt}
    return APIRequestFactory().post("/api/robot/v1/voice/offline/", data, format="multipart")


class OfflineVoiceTests(ERPTenantTestCase):

    def setUp(self):
        branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-offline", branch=branch)
        self.device.issue_token()
        self.device.save()
        self.product = make_product(name="فلتر زيت", part_number="11427953125")
        self.said = timezone.now() - timedelta(minutes=40)
        for target, kwargs in ((views, {"_device_or_401": mock.Mock(return_value=(self.device, None))}),
                               (security.RobotDeviceThrottle, {"allow_request": mock.Mock(return_value=True)})):
            patcher = mock.patch.multiple(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _send(self, transcript, **kwargs):
        stt = (mock.patch.object(audio, "transcribe", side_effect=transcript)
               if isinstance(transcript, Exception)
               else mock.patch.object(audio, "transcribe", return_value=transcript))
        with stt:
            return views.voice_offline(_clip_request(**kwargs))

    def test_a_question_is_listed_for_staff_at_the_time_it_was_said(self):
        resp = self._send("يا موس عندك طرمبة مية للـ X5؟", recorded_at=int(self.said.timestamp()))
        self.assertEqual(resp.data["result"], "logged")
        turn = RobotVoiceInteraction.objects.get()
        self.assertTrue(turn.payload["offline"] and turn.payload["unresolved"])
        self.assertLess(abs((turn.created_at - self.said).total_seconds()), 1)

    def test_chatter_is_dropped(self):
        resp = self._send("هات الشاي يا محمد")
        self.assertEqual(resp.data["result"], "not_addressed")
        self.assertFalse(RobotVoiceInteraction.objects.exists())

    def test_a_count_lands_in_the_stock_take_that_was_open(self):
        session = RobotStockTakeSession.objects.create(
            device=self.device, branch=self.device.branch, status="open")
        RobotStockTakeSession.objects.filter(pk=session.pk).update(
            created_at=self.said - timedelta(minutes=10))
        resp = self._send("يا موس فلتر زيت 7", recorded_at=int(self.said.timestamp()))
        self.assertEqual(resp.data["result"], "count")
        line = RobotStockTakeLine.objects.get(session=session)
        self.assertEqual((line.product_id, line.counted_qty), (self.product.id, 7))

    def test_a_resent_clip_is_applied_once(self):
        self._send("يا موس محتاج فلتر هوا", uid="aabb-9")
        resp = self._send("يا موس محتاج فلتر هوا", uid="aabb-9")
        self.assertEqual(resp.data["result"], "duplicate")
        self.assertEqual(RobotVoiceInteraction.objects.count(), 1)

    def test_speech_to_text_down_keeps_the_clip_for_later(self):
        resp = self._send(audio.SttUnavailable("down"), uid="aabb-3")
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(RobotSyncEvent.objects.filter(client_uid="aabb-3").exists())

    def test_the_outage_summary_becomes_an_alert(self):
        start = int((timezone.now() - timedelta(hours=1)).timestamp())
        services.apply_offline_events(self.device, [{
            "client_uid": "outage-1", "kind": "outage",
            "payload": {"from": start, "to": start + 1800, "clips": 3}}])
        alert = RobotAlert.objects.get(kind="back_online")
        self.assertIn("من غير نت من", alert.message)
        self.assertIn("3 تسجيل", alert.message)


class RecordedAtTests(SimpleTestCase):

    def test_no_clock_and_nonsense_are_none(self):
        self.assertIsNone(views._recorded_at("0"))
        self.assertIsNone(views._recorded_at("abc"))
        future = timezone.now() + timedelta(hours=2)
        self.assertIsNone(views._recorded_at(int(future.timestamp())))

    def test_a_real_time(self):
        when = timezone.now() - timedelta(minutes=3)
        self.assertEqual(int(views._recorded_at(int(when.timestamp())).timestamp()),
                         int(when.timestamp()))


class OfflinePhotoTests(SimpleTestCase):
    """A motion photo the camera kept on its card, uploaded later."""

    def _upload(self, captured_at, *, after_hours):
        data = {"image": SimpleUploadedFile("m.jpg", b"JPEG", "image/jpeg"),
                "reason": "motion", "captured_at": str(captured_at)}
        request = APIRequestFactory().post("/api/robot/v1/snapshot/", data, format="multipart")
        with mock.patch.object(views, "_device_or_401", return_value=(mock.Mock(), None)), \
                mock.patch.object(security.RobotDeviceThrottle, "allow_request", return_value=True), \
                mock.patch.object(views.services, "is_after_hours", return_value=after_hours), \
                mock.patch.object(views.services, "raise_after_hours_alert") as alert, \
                mock.patch.object(views.RobotSnapshot, "objects") as snaps:
            snaps.create.return_value = mock.Mock(id=5, pk=5)
            views.snapshot_upload(request)
        return snaps, alert

    def test_motion_at_night_while_offline_still_alerts(self):
        taken = timezone.now() - timedelta(hours=3)
        snaps, alert = self._upload(int(taken.timestamp()), after_hours=True)
        self.assertEqual(snaps.create.call_args.kwargs["reason"], "after_hours")
        snaps.filter.return_value.update.assert_called_with(created_at=mock.ANY)
        self.assertIn("النت كان فاصل", alert.call_args.kwargs["message"])

    def test_daytime_motion_is_just_kept(self):
        taken = timezone.now() - timedelta(hours=3)
        snaps, alert = self._upload(int(taken.timestamp()), after_hours=False)
        self.assertEqual(snaps.create.call_args.kwargs["reason"], "motion")
        alert.assert_not_called()


class StrictTranscribeTests(SimpleTestCase):

    def test_failure_raises_but_silence_is_empty(self):
        import requests
        with mock.patch.object(audio, "_gemini_key", return_value="k"), \
                mock.patch.object(audio, "_stt_models", return_value=["m"]):
            with mock.patch("requests.post", side_effect=requests.ConnectionError("x")):
                with self.assertRaises(audio.SttUnavailable):
                    audio.transcribe(b"x", strict=True)
                self.assertEqual(audio.transcribe(b"x"), "")
            empty = mock.Mock(status_code=200, json=lambda: {"candidates": [{"content": {"parts": [{"text": ""}]}}]})
            with mock.patch("requests.post", return_value=empty):
                self.assertEqual(audio.transcribe(b"x", strict=True), "")


class GuardWindowTests(SimpleTestCase):
    """What the camera is told so it can tell night from day offline."""

    def test_the_default_night(self):
        g = services.guard_window(mock.Mock(guard_from=None, guard_to=None))
        self.assertEqual((g["from"], g["to"]), ("20:00", "08:00"))
        self.assertIsInstance(g["utc_offset"], int)

    def test_the_shops_own_hours(self):
        from datetime import time
        g = services.guard_window(mock.Mock(guard_from=time(22, 30), guard_to=time(7, 0)))
        self.assertEqual((g["from"], g["to"]), ("22:30", "07:00"))
