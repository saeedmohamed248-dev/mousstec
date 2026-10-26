"""«🩺 صحة الخدمات»: each online service the robot leans on notes how its last
call went (robot/health.py) — Azure's hearing and voice, Google's hearing,
Gemini — and the owner reads why one isn't answering and what to do; «مخ
موس» says it in its log when it starts (robot/brain/loop.check_services)."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from robot import audio, health, teachers
from robot.brain import VERSION, config, loop

AZURE = {"ROBOT_AZURE_SPEECH_KEY": "az-key", "ROBOT_AZURE_SPEECH_REGION": "westeurope", "ROBOT_GOOGLE_STT_KEY": ""}


class _HealthTest(SimpleTestCase):

    def setUp(self):
        health._written.clear()
        for service in health.SERVICES:
            cache.delete(health._key(service))
        self.addCleanup(health._written.clear)


class NoteTests(_HealthTest):

    def test_a_failure_says_why_and_what_to_do(self):
        health.note("gemini", False, code=429, detail="gemini-3.6-flash 429")
        with override_settings(GEMINI_API_KEY="k"):
            gemini = next(s for s in health.report() if s["service"] == "gemini")
        self.assertEqual((gemini["ok"], gemini["code"], gemini["ready"]), (False, "429", True))
        self.assertIn("الفوترة", gemini["remedy"])

    def test_not_set_up_says_so(self):
        with mock.patch.dict(os.environ, {"ROBOT_GOOGLE_STT_KEY": ""}):
            google = next(s for s in health.report() if s["service"] == "google_stt")
        self.assertFalse(google["ready"])
        self.assertIsNone(google["ok"])
        self.assertIn("المفتاح ناقص", google["remedy"])

    def test_the_same_news_is_written_once_a_minute(self):
        with mock.patch.object(cache, "set", wraps=cache.set) as written:
            for _ in range(5):
                health.note("azure_tts", True)
            health.note("azure_tts", False, code=401)                  # news: written at once
        self.assertEqual(written.call_count, 2)
        self.assertEqual(health.last("azure_tts")["code"], "401")

    def test_codes_from_what_failed(self):
        import requests

        refused = requests.HTTPError("401 Client Error")
        refused.response = mock.Mock(status_code=401)
        for exc, code in ((refused, "401"), (requests.ReadTimeout("read timed out"), "timeout"),
                          (requests.ConnectionError("refused"), "network"), (RuntimeError("HTTP 429"), "429"),
                          (RuntimeError("down"), None)):
            with self.subTest(exc):
                self.assertEqual(health.code_of(exc), code)

    def test_every_answer_has_a_remedy(self):
        for service in health.SERVICES:
            for code in ("401", "403", "404", "429", "500", "timeout", "network", "418"):
                with self.subTest(service=service, code=code):
                    self.assertTrue(health.remedy(service, code))


class NotedWhereCalledTests(_HealthTest):

    def _ok(self, text="تمام"):
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        return ok

    def test_gemini_all_models_failing_notes_the_quota_first(self):
        audio._forget_model_health()
        self.addCleanup(audio._forget_model_health)
        replies = [mock.Mock(status_code=404, text="gone"), mock.Mock(status_code=429, text="quota"),
                   mock.Mock(status_code=503, text="busy")]
        with mock.patch.object(audio, "_gemini_key", return_value="k"), \
                mock.patch.object(audio, "_stt_models", return_value=["a", "b", "c"]), \
                mock.patch("requests.post", side_effect=replies):
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "")
        self.assertEqual((health.last("gemini")["ok"], health.last("gemini")["code"]), (False, "429"))
        with mock.patch.object(audio, "_gemini_key", return_value="k"), \
                mock.patch.object(audio, "_stt_models", return_value=["a"]), \
                mock.patch("requests.post", return_value=self._ok()):
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "تمام")
        self.assertTrue(health.last("gemini")["ok"])

    def test_azure_voice_refused(self):
        audio._azure_tts_rest["until"] = 0.0
        self.addCleanup(audio._azure_tts_rest.update, until=0.0)
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=mock.Mock(status_code=401, content=b"", text="denied")):
            self.assertIsNone(audio._azure_pcm("أيوه، تحت أمرك.", 16000))
        self.assertEqual(health.last("azure_tts")["code"], "401")
        self.assertIn("ROBOT_AZURE_SPEECH_KEY", health.remedy("azure_tts", "401"))


class BrainSaysTests(SimpleTestCase):
    """«مخ موس» at start: the server's services in one line, what to do, a newer brain."""

    def _brain(self, status):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = mock.Mock()
        server.status.return_value = status
        lines = []
        brain = loop.Brain(config.Config(api_base="x", token="y", home=Path(tmp.name)), ears=mock.Mock(ready=False),
                           server=server, mic=None, speaker=mock.Mock(), log=lines.append)
        return brain, server, lines

    def test_the_services_and_what_to_do(self):
        newer = "brain-99.0.0"
        brain, server, lines = self._brain({"brain": newer, "services": [
            {"service": "azure_stt", "label": "Azure — السمع", "ready": True, "ok": True, "minutes_left": 243.5},
            {"service": "google_stt", "label": "Google — السمع", "ready": False, "ok": None},
            {"service": "gemini", "label": "Gemini", "ready": True, "ok": False, "code": "429",
             "remedy": "حصة Gemini المجانية خلصت: فعّل الفوترة."}]})
        server.heartbeat.return_value = 200
        brain.beat()                                                       # offline → online: it asks
        self.assertIn("[SERVER] Azure — السمع ✓ (243.5 min left) · Google — السمع — · Gemini ✗ 429", lines)
        self.assertIn("[SERVER] Gemini: حصة Gemini المجانية خلصت: فعّل الفوترة.", lines)
        self.assertTrue(any(line.startswith(f"[UPDATE] a newer brain is on the server ({newer}; this one is {VERSION})")
                            for line in lines))
        brain.beat()                                                       # still online: not again
        server.status.assert_called_once()

    def test_a_server_not_updated_says_nothing(self):
        for status in ({}, None, {"brain": VERSION, "services": []}):
            with self.subTest(status):
                brain, server, lines = self._brain(status)
                server.heartbeat.return_value = 200
                brain.beat()
                self.assertEqual([line for line in lines if not line.startswith("[HB]")], [])


class TeacherNotesTests(_HealthTest):
    """The teachers' calls are noted too (robot/teachers.check, robot/teachers.hear)."""

    def test_azure_hearing_refused_then_fine(self):
        from robot.tests.test_teachers_db import _recording, azure_says

        device = mock.Mock(wake_name="موس", branch=None)
        refused = mock.Mock(status_code=401)
        refused.raise_for_status.side_effect = RuntimeError("HTTP 401")
        with mock.patch.dict(os.environ, AZURE), mock.patch.object(teachers, "minutes_left", return_value=100), \
                mock.patch("requests.post", return_value=refused):
            self.assertEqual(teachers.hear(device, _recording()), "")
        self.assertEqual(health.last("azure_stt")["code"], "401")
        health._written.clear()
        with mock.patch.dict(os.environ, AZURE), mock.patch.object(teachers, "minutes_left", return_value=100), \
                mock.patch("requests.post", return_value=azure_says("يا موس عندك فلتر زيت")), \
                mock.patch("robot.models.RobotHeard.objects.create"):
            self.assertEqual(teachers.hear(device, _recording()), "يا موس عندك فلتر زيت")
        self.assertTrue(health.last("azure_stt")["ok"])
