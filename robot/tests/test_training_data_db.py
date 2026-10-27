"""«📦 بيانات تدريب موديل المحل»: the recordings with the words a teacher heard
in them, downloaded as one zip (Hugging Face «audiofolder»: audio/*.wav and
metadata.csv) — what a model of the shop's own is trained on. Managers only."""

import csv
import io
import tempfile
import zipfile

import numpy as np
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import audio, teachers
from robot.brain import wavs
from robot.models import RobotDevice, RobotHeard


@override_settings(ALLOWED_HOSTS=["*"])
class TrainingDataTests(ERPTenantTestCase):

    def setUp(self):
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        settings = override_settings(MEDIA_ROOT=media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice.objects.create(name="موس", device_uid="esp-training", branch=branch)
        self.boss = User.objects.create_user("training_boss", password="x", is_staff=True, is_superuser=True)
        self.host = self.domain.domain

    def heard(self, local, said, teacher="azure", with_audio=True):
        row = RobotHeard(device=self.device, local_text=local, teacher=teacher, teacher_text=said,
                         teacher_confidence=0.91, seconds=2.0)
        if with_audio:
            row.audio.save("clip.wav", ContentFile(wavs.ulaw_wav(np.full(32000, 900, dtype=np.int16))), save=False)
        row.save()
        return row

    def download(self, client):
        response = client.get(reverse("robot_ui:hearing_dataset"), HTTP_HOST=self.host)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("attachment;", response["Content-Disposition"])
        return zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))

    def test_the_recordings_with_the_right_words(self):
        first = self.heard("عندك سلطه الزيت", "عندك فلتر زيت")
        second = self.heard("عندك فلتر زيت", "عندك فلتر زيت", teacher="gemini")
        self.heard("اي", "You know you.")                                   # an English guess: not a lesson
        self.heard("عندك فلتر", "عندك فلتر هوا", with_audio=False)          # its recording let go
        client = Client()
        client.force_login(self.boss)
        page = client.get(reverse("robot_ui:hearing"), HTTP_HOST=self.host)
        self.assertContains(page, "بيانات تدريب موديل المحل")
        self.assertContains(page, "نزّل بيانات التدريب")
        zf = self.download(client)
        self.assertEqual(sorted(zf.namelist()), sorted(["README.md", f"audio/{first.pk}.wav",
                                                        f"audio/{second.pk}.wav", "metadata.csv"]))
        rows = list(csv.DictReader(io.StringIO(zf.read("metadata.csv").decode("utf-8"))))
        self.assertEqual([(r["file_name"], r["transcription"], r["robot_heard"], r["teacher"]) for r in rows],
                         [(f"audio/{first.pk}.wav", "عندك فلتر زيت", "عندك سلطه الزيت", "azure"),
                          (f"audio/{second.pk}.wav", "عندك فلتر زيت", "عندك فلتر زيت", "gemini")])
        tag, rate, samples = audio.wav_parts(zf.read(f"audio/{first.pk}.wav"))
        self.assertEqual((tag, rate, len(samples)), (1, 16000, 64000))      # 16-bit PCM, as training takes
        self.assertEqual(teachers.training_summary()["count"], 2)          # what the zip holds: no English guess

    def test_managers_only(self):
        self.heard("عندك فلتر زيت", "عندك فلتر زيت")
        nobody = User.objects.create_user("training_nobody", password="x")
        client = Client()
        client.force_login(nobody)
        response = client.get(reverse("robot_ui:hearing_dataset"), HTTP_HOST=self.host)
        self.assertNotEqual(response.status_code, 200)

    def test_under_daphne_it_streams_and_keeps_its_name(self):
        """Production serves through daphne (ASGI): the zip goes out piece by piece — Django 4.2
        would collect a sync generator whole — and an Arabic shop name doesn't cost the file its name."""
        from unittest import mock

        from asgiref.sync import async_to_sync
        from django.test import AsyncClient

        from robot import services

        from django_tenants.utils import get_tenant_domain_model

        self.heard("عندك سلطة الزيت", "عندك فلتر زيت")
        # The async client always says «testserver»: that's this shop's address for the test.
        get_tenant_domain_model().objects.get_or_create(domain="testserver", tenant=self.tenant,
                                                        defaults={"is_primary": False})
        client = AsyncClient()
        client.force_login(self.boss)

        async def fetch():
            response = await client.get(reverse("robot_ui:hearing_dataset"))
            return response, b"".join([piece async for piece in response.streaming_content])

        with mock.patch.object(services, "shop_name", return_value="فيكس إت"):
            response, body = async_to_sync(fetch)()
        self.assertTrue(response.is_async)
        self.assertIn("filename*=utf-8''mouss-hearing-", response["Content-Disposition"])
        self.assertIn("README.md", zipfile.ZipFile(io.BytesIO(body)).namelist())
