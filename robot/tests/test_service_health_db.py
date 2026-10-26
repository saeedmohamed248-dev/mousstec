"""«🩺 صحة الخدمات» on the dashboard and for «مخ موس» (/status/), a brain older
than the server's flagged on its card, and a whole sentence the robot learned
turned off on «🎓 تعلّم السمع»."""

import os
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import health, teachers, views
from robot.brain import VERSION
from robot.models import RobotDevice, RobotHeard

AZURE = {"ROBOT_AZURE_SPEECH_KEY": "az-key", "ROBOT_AZURE_SPEECH_REGION": "westeurope", "ROBOT_GOOGLE_STT_KEY": ""}


@override_settings(ALLOWED_HOSTS=["*"])
class ServiceHealthTests(ERPTenantTestCase):

    def setUp(self):
        health._written.clear()
        for service in health.SERVICES:
            cache.delete(health._key(service))
        self.addCleanup(health._written.clear)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-health", branch=self.branch,
                                  firmware_version="brain-0.0.1")
        self.token = self.device.issue_token()
        self.device.save()
        boss = User.objects.create_user("health_boss", password="x", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(boss)
        self.host = self.domain.domain

    def test_the_dashboard_says_what_fails_and_what_to_do(self):
        health.note("gemini", False, code=429, detail="gemini-3.6-flash 429")
        health.note("azure_stt", True)
        with override_settings(GEMINI_API_KEY="k"), mock.patch.dict(os.environ, AZURE):
            page = self.client.get(reverse("robot_ui:dashboard"), HTTP_HOST=self.host)
        self.assertContains(page, "صحة الخدمات")
        self.assertContains(page, "فعّل الفوترة (Billing)")
        self.assertContains(page, "فاضل 300 دقيقة الشهر ده")                 # Azure's free minutes, none used
        self.assertContains(page, f"فيه مخ أحدث ({VERSION})")                  # this robot runs brain-0.0.1

    def test_the_card_in_english(self):
        health.note("gemini", False, code=429)
        self.client.cookies["django_language"] = "en"
        with override_settings(GEMINI_API_KEY="k"):
            page = self.client.get(reverse("robot_ui:dashboard"), HTTP_HOST=self.host)
        self.assertContains(page, "Service health")
        self.assertContains(page, "turn on billing for the key in Google AI Studio")
        self.assertContains(page, f"A newer brain is out ({VERSION})")

    def test_a_brain_up_to_date_is_not_flagged(self):
        RobotDevice.objects.filter(pk=self.device.pk).update(firmware_version=VERSION)
        page = self.client.get(reverse("robot_ui:dashboard"), HTTP_HOST=self.host)
        self.assertNotContains(page, "فيه مخ أحدث")

    def test_the_robot_asks_when_it_starts(self):
        health.note("azure_tts", False, code=401)
        with mock.patch.dict(os.environ, AZURE):
            request = APIRequestFactory().post("/api/robot/v1/status/", HTTP_X_ROBOT_TOKEN=self.token)
            data = views.service_status(request).data
        self.assertEqual(data["brain"], VERSION)
        tts = next(s for s in data["services"] if s["service"] == "azure_tts")
        self.assertEqual((tts["ok"], tts["code"]), (False, "401"))
        self.assertIn("ROBOT_AZURE_SPEECH_KEY", tts["remedy"])
        refused = APIRequestFactory().post("/api/robot/v1/status/", HTTP_X_ROBOT_TOKEN="wrong")
        self.assertEqual(views.service_status(refused).status_code, 401)

    def test_a_whole_sentence_turned_off_on_the_hearing_page(self):
        for _ in range(2):
            RobotHeard.objects.create(device=self.device, local_text="عندك سلطة الزيت", teacher="azure",
                                      teacher_text="عندك فلتر زيت", seconds=2)
        self.assertEqual([s["heard"] for s in teachers.sentences()], ["عندك سلطه الزيت"])
        url = reverse("robot_ui:hearing")
        page = self.client.get(url, HTTP_HOST=self.host)
        self.assertContains(page, "جمل اتعلّمها كاملة")
        self.assertContains(page, "«عندك سلطه الزيت»")
        self.client.post(url, {"sentence": "عندك سلطه الزيت", "means": "عندك فلتر زيت"}, HTTP_HOST=self.host)
        self.assertEqual(teachers.sentences(), [])
