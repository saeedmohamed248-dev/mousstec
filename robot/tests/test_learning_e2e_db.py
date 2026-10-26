"""Offline learns from online, end to end: «مخ موس» — its own loop and its own
client (robot/brain/server.py) — talks to this server's own views, Vosk writes
«عندك سلطة الزيت» where «عندك فلتر زيت» was said, and Azure is the teacher.

1. Every question goes to the teacher ([robot] teach = yes): Azure hears it,
   and its words answer.
2. Twice the same: a lesson — the whole sentence, and the word — in the catalog.
3. The third time the robot answers at once from its own catalog: no teacher,
   no minutes.
4. The net down: the same answer, from the shop's computer alone.
"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, views
from robot.brain import config, loop, server, vad
from robot.models import RobotDevice, RobotHeard
from robot.tests.test_brain_offline import _clip
from robot.tests.test_brain_teach import _Ears
from robot.tests.test_teachers_db import AZURE, azure_says

import numpy as np

API = "http://shop.example/api/robot/v1"
ROUTES = {"/voice/": views.voice, "/sync/pull/": views.sync_pull, "/heartbeat/": views.heartbeat,
          "/speak/": views.speak}
FOUND = "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه."


class _Reply:
    """What `requests` would have handed the brain's client."""

    def __init__(self, resp):
        self.status_code, self.headers = resp.status_code, resp.headers
        self.data = getattr(resp, "data", None)
        self.content = b"" if self.data is not None else resp.content

    def json(self):
        if self.data is None:
            raise ValueError("not JSON")
        return self.data


class _Wire:
    """The brain's HTTP session, answered by this server's views (no network)."""

    def __init__(self):
        self.headers, self.factory = {}, APIRequestFactory()

    def post(self, url, timeout=None, data=None, files=None, json=None, headers=None):
        path = url.split("/api/robot/v1", 1)[1]
        extra = {"HTTP_" + k.upper().replace("-", "_"): v for k, v in {**self.headers, **(headers or {})}.items()}
        if json is not None:
            request = self.factory.post(path, json, format="json", **extra)
        else:
            body = dict(data or {})
            for key, (name, content, kind) in (files or {}).items():
                body[key] = SimpleUploadedFile(name, content, content_type=kind)
            request = self.factory.post(path, body, format="multipart", **extra)
        return _Reply(ROUTES[path](request))


@override_settings(GEMINI_API_KEY="")
class OfflineLearnsFromOnlineTests(ERPTenantTestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        media = override_settings(MEDIA_ROOT=self.tmp.name)
        media.enable()
        self.addCleanup(media.disable)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-learns", branch=self.branch)
        token = self.device.issue_token()
        self.device.save()
        oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(oil, self.branch, quantity=3)
        for target, name, value in ((views.audio_svc, "synthesize_wav", _clip(5, 160)),
                                    (services, "shelf_location", ""),
                                    (services, "ai_reply", "")):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, AZURE)
        env.start()
        self.addCleanup(env.stop)
        self.cfg = config.Config(api_base=API, token=token, home=Path(self.tmp.name) / "brain")
        self.cfg.ears_use, self.cfg.teach_all = "always", True          # brain.ini: teach = yes
        self.client = server.Server(API, token, session=_Wire())
        self.lines = []
        # Vosk, sure of every word, and wrong: «سلطة الزيت» for «فلتر زيت».
        self.brain = loop.Brain(self.cfg, ears=_Ears("يا موس عندك سلطة الزيت"), server=self.client,
                                mic=None, speaker=mock.Mock(), log=self.lines.append)
        self.brain._ask = lambda **kwargs: self.client.voice(**kwargs)   # not on a thread: the test's DB
        self.brain._check = lambda *args: None                            # the server's check after: likewise
        self.refresh()

    def refresh(self):
        self.assertTrue(self.brain.refresh())
        for clip in self.brain.knowledge.clip_texts():
            self.brain.bank.put(clip, _clip(100, 160))
        self.brain.online = True

    def say(self):
        self.brain.handle(vad.Utterance(samples=np.zeros(8000, np.int16), speech_ms=500,
                                        quietest=40, loudest=2500, cut=False))

    def test_the_teacher_teaches_and_the_robot_answers_alone_after(self):
        with mock.patch("requests.post", return_value=azure_says("يا موس، عندك فلتر زيت؟")) as azure:
            self.say()
            self.say()
            self.assertEqual(azure.call_count, 2)                          # 1. the teacher heard both
            self.assertEqual(self.lines.count("[TEACH] «يا موس عندك سلطة الزيت» — azure heard «عندك فلتر زيت»"), 2)
            self.assertEqual(self.lines.count(f"🔊 {FOUND}"), 2)            # and its words were answered
            self.assertEqual(RobotHeard.objects.filter(teacher="azure").count(), 2)

            self.refresh()                                                 # 2. the lesson, with the catalog
            self.assertEqual(self.brain.knowledge.heard_sentences, {"عندك سلطه الزيت": "عندك فلتر زيت"})
            self.assertIn({"heard": "سلطه الزيت", "means": "فلتر زيت"}, self.brain.knowledge.heard_fixes)

            self.lines.clear()
            self.say()                                                     # 3. at once, no teacher
            self.assertEqual(azure.call_count, 2)
            self.assertIn("[EARS] «يا موس عندك سلطة الزيت» — learned: «عندك فلتر زيت» (a teacher heard it so before)",
                          self.lines)
            self.assertIn(f"[HERE] 🔊 {FOUND}", self.lines)

            self.brain.online = False                                      # 4. the net down
            self.lines.clear()
            self.say()
            self.assertIn(f"[OFFLINE] 🔊 {FOUND}", self.lines)
            self.assertEqual(azure.call_count, 2)
        # What the robot said alone is what the server says to the right words.
        _intent, reply, _payload = views._handle_voice("عندك فلتر زيت", self.device, None)
        self.assertEqual(reply, FOUND)
