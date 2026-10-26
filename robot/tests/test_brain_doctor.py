"""«🩺 فحص المخ» (python -m brain doctor): everything the robot needs, each
with what to do — what shops met before, found in a minute."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from robot.brain import VERSION, config, doctor, server
from robot.tests.test_brain_offline import CATALOG


class DoctorTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ini = config.HERE.parent / "brain.ini"
        self.cfg = config.Config(api_base="https://shop.example/api/robot/v1", token="t", home=self.home,
                                 source=self.ini, model="vosk")
        self.server = mock.Mock()
        self.server.heartbeat.return_value = 200
        self.server.status.return_value = {"brain": VERSION, "services": [
            {"service": "azure_stt", "label": "Azure — السمع", "ready": True, "ok": True, "minutes_left": 120},
            {"service": "gemini", "label": "Gemini", "ready": True, "ok": False, "code": "429",
             "remedy": "حصة Gemini المجانية خلصت: فعّل الفوترة."}]}
        self.lines = []

    def check(self):
        return doctor.check(self.cfg, self.server, log=self.lines.append)

    def said(self, piece):
        return any(piece in line for line in self.lines)

    def test_what_works_and_what_to_fix(self):
        (self.home / "catalog.json").write_text(json.dumps(dict(CATALOG, heard_sentences=[
            {"heard": "عندك سلطه الزيت", "means": "عندك فلتر زيت"}])), encoding="utf-8")
        self.assertEqual(self.check(), 1)                                  # Gemini's quota
        self.assertTrue(self.said(f"[DOCTOR] ✔ brain.ini: {self.ini}"))
        self.assertTrue(self.said("✔ the server answers, and knows this robot"))
        self.assertTrue(self.said("✔ server: Azure — السمع ✓ (120 min left)"))
        self.assertTrue(self.said("✘ server: Gemini ✗ 429"))
        self.assertTrue(self.said("→ حصة Gemini المجانية خلصت: فعّل الفوترة."))
        self.assertTrue(self.said("✔ offline: 6 parts known"))
        self.assertTrue(self.said("0 word(s), 1 whole sentence(s)"))
        self.assertTrue(self.said("offline voice: 0 of"))
        self.assertTrue(self.said("[DOCTOR] 1 to fix"))

    def test_no_brain_ini_or_no_key(self):
        self.cfg.source, self.cfg.token = None, ""
        self.assertEqual(self.check(), 2)
        self.assertTrue(self.said("✘ no brain.ini found"))
        self.assertTrue(self.said("✘ brain.ini has no token under [server]"))
        self.server.heartbeat.assert_not_called()

    def test_a_key_changed_on_the_dashboard(self):
        self.server.heartbeat.side_effect = server.Unauthorized("401")
        self.assertEqual(self.check(), 1)
        self.assertTrue(self.said("✘ the server refused this robot's key (401)"))
        self.assertTrue(self.said("download the brain again with its key"))

    def test_the_net_down_still_checks_the_rest(self):
        self.server.heartbeat.side_effect = server.ServerDown("timed out")
        self.cfg.model = "small"
        self.assertEqual(self.check(), 1)
        self.assertTrue(self.said("✘ no answer from the server (timed out)"))
        self.assertTrue(self.said("[ears] model = vosk in brain.ini"))     # Whisper on an old computer
        self.server.status.assert_not_called()

    def test_a_newer_brain_on_the_server(self):
        self.server.status.return_value = {"brain": "brain-99.0.0", "services": []}
        self.check()
        self.assertTrue(self.said(f"✘ this brain is {VERSION}, the server has brain-99.0.0"))

    def test_the_brain_unzipped_at_the_top_of_the_drive(self):
        # In the shop: C:\brain and C:\brain.ini (unzipped to C:\ instead of C:\mouss-brain).
        with mock.patch.object(doctor, "HERE", Path("/brain")):
            self.check()
        self.assertTrue(self.said("✘ the brain is at the top of the drive"))
        self.assertTrue(self.said("C:\\mouss-brain"))
