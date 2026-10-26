"""«🎓 تعلّم السمع» end to end: «مخ موس» wasn't sure what it heard, sent the
recording with `check=1`; a teacher (Azure, then Google) heard it, its words
answered, and the pair taught the robot — within each teacher's minutes."""

import math
import os
import tempfile
from unittest import mock

import numpy as np
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, teachers, views
from robot.brain import wavs
from robot.models import RobotDevice, RobotHeard, RobotHeardFix

AZURE = {"ROBOT_AZURE_SPEECH_KEY": "az-key", "ROBOT_AZURE_SPEECH_REGION": "westeurope",
         "ROBOT_GOOGLE_STT_KEY": "", "ROBOT_AZURE_FREE_MINUTES": "300", "ROBOT_GOOGLE_FREE_MINUTES": "60"}
BOTH = dict(AZURE, ROBOT_GOOGLE_STT_KEY="g-key")
SECONDS = 2.5


def _recording(seconds=SECONDS):
    return wavs.ulaw_wav(np.full(int(16000 * seconds), 800, dtype=np.int16))


class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def azure_says(text, confidence=0.93):
    return _Resp({"RecognitionStatus": "Success", "DisplayText": text,
                  "NBest": [{"Display": text, "Confidence": confidence}]})


def google_says(text):
    return _Resp({"results": [{"alternatives": [{"transcript": text, "confidence": 0.9}]}],
                  "totalBilledTime": "3s"})


class TeacherTests(ERPTenantTestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        media = override_settings(MEDIA_ROOT=self.media.name)
        media.enable()
        self.addCleanup(media.disable)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-teach", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(self.oil, self.branch, quantity=3)

    def ask(self, said="يا موس عندك فلترا الزي", check="1", **extra):
        body = {"transcript": said, "check": check, "confidence": "0.55",
                "audio": SimpleUploadedFile("voice.wav", _recording(), content_type="audio/wav"), **extra}
        request = APIRequestFactory().post("/api/robot/v1/voice/", body, format="multipart",
                                           HTTP_X_ROBOT_TOKEN=self.token)
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            return views.voice(request).data

    def test_the_teacher_hears_it_and_the_answer_is_its(self):
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_says("يا موس، عندك فلتر زيت؟")) as post:
            data = self.ask()
        self.assertEqual((data["intent"], data["heard_by"], data["taught"]),
                         ("inventory_query", "azure", "عندك فلتر زيت"))
        self.assertIn("فلتر زيت موجود", data["reply"])
        call = post.call_args
        self.assertIn("westeurope.stt.speech.microsoft.com", call.args[0])
        self.assertEqual((call.kwargs["params"]["language"], call.kwargs["headers"]["Ocp-Apim-Subscription-Key"]),
                         ("ar-EG", "az-key"))
        self.assertEqual(call.kwargs["data"][20:22], b"\x01\x00")              # PCM, not the robot's µ-law
        heard = RobotHeard.objects.get()
        self.assertEqual((heard.local_text, heard.teacher, heard.teacher_text, heard.billed_seconds),
                         ("عندك فلترا الزي", "azure", "عندك فلتر زيت", math.ceil(SECONDS)))
        self.assertAlmostEqual(heard.local_confidence, 0.55)
        self.assertTrue(heard.audio.name.endswith(".wav"))                   # kept, to train on later

    def test_learned_when_seen_twice_and_sent_with_the_catalog(self):
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_says("يا موس عندك فلتر زيت")):
            self.ask()
            self.assertEqual(teachers.fixes(), [])                          # once is not a lesson
            self.ask()
        self.assertEqual(teachers.fixes(), [{"heard": "فلترا الزي", "means": "فلتر زيت"}])
        self.assertIn({"heard": "فلترا الزي", "means": "فلتر زيت"},
                      services.offline_catalog(self.branch)["heard_fixes"])
        RobotHeardFix.objects.update(is_active=False)                       # the owner turned it off
        self.assertEqual(teachers.fixes(), [])

    def test_never_a_lesson_in_money(self):
        teachers.learn("سجل مصروف مية جنيه", "سجل مصروف ميتين جنيه")
        teachers.learn("سجل مصروف مية جنيه", "سجل مصروف ميتين جنيه")
        self.assertFalse(RobotHeardFix.objects.exists())

    def test_azure_first_then_google_within_their_minutes(self):
        limits = dict(BOTH, ROBOT_AZURE_FREE_MINUTES=str(4 / 60))           # 4 s of Azure left
        with mock.patch.dict(os.environ, limits), \
                mock.patch("requests.post", side_effect=[azure_says("يا موس عندك فلتر زيت"),
                                                         google_says("يا موس عندك فلتر زيت")]) as post:
            self.assertEqual(self.ask()["heard_by"], "azure")                # 3 s billed: 1 s isn't enough
            self.assertEqual(self.ask()["heard_by"], "google")
        self.assertIn("speech.googleapis.com", post.call_args.args[0])
        config = post.call_args.kwargs["json"]["config"]
        self.assertEqual(config["languageCode"], "ar-EG")
        self.assertIn("فلتر زيت", config["speechContexts"][0]["phrases"])    # the shop's parts as hints
        self.assertEqual(teachers.used_seconds("google"), 3)

    def test_out_of_minutes_or_down_the_robots_own_words(self):
        with mock.patch.dict(os.environ, dict(AZURE, ROBOT_AZURE_FREE_MINUTES="0")), \
                mock.patch("requests.post") as post:
            data = self.ask("يا موس عندك فلتر زيت")
        post.assert_not_called()                                             # never past its budget
        self.assertNotIn("heard_by", data)
        self.assertIn("فلتر زيت موجود", data["reply"])
        with mock.patch.dict(os.environ, BOTH), \
                mock.patch("requests.post", side_effect=[RuntimeError("down"), google_says("يا موس عندك فلتر زيت")]):
            self.assertEqual(self.ask()["heard_by"], "google")              # Azure down: the next one

    def test_only_when_the_robot_asks(self):
        with mock.patch.dict(os.environ, AZURE), mock.patch("requests.post") as post:
            self.ask("عندك فلتر زيت")                                       # not said to the robot
            self.ask("يا موس عندك فلتر زيت", check="0")
        post.assert_not_called()
        self.assertFalse(RobotHeard.objects.exists())


@override_settings(ALLOWED_HOSTS=["*"])
class HearingPageTests(ERPTenantTestCase):

    def test_the_minutes_and_the_lessons_on_the_page(self):
        RobotHeardFix.objects.create(heard="فلترا الزي", means="فلتر زيت", seen=3)
        boss = User.objects.create_user("hearing_boss", password="x", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(boss)
        host, url = self.domain.domain, reverse("robot_ui:hearing")
        with mock.patch.dict(os.environ, AZURE):
            page = client.get(url, HTTP_HOST=host)
        self.assertContains(page, "فلترا الزي")
        self.assertContains(page, "اتستعمل 0 من 300 دقيقة الشهر ده")
        fix = RobotHeardFix.objects.get()
        client.post(url, {"fix_id": fix.pk}, HTTP_HOST=host)
        self.assertFalse(RobotHeardFix.objects.get().is_active)
        client.post(url, {"fix_id": fix.pk, "remove": "1"}, HTTP_HOST=host)
        self.assertFalse(RobotHeardFix.objects.exists())
