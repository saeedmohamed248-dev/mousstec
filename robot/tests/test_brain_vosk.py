"""robot/brain/vosk_ears.py — light ears for an old CPU (no AVX2), with the
same face as Whisper's. No real model here: a stand-in vosk module."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import mock

import numpy as np

from robot.brain import __main__ as brain_main
from robot.brain import config, vosk_ears


def _fake_vosk(said="يا موس عندك فلتر زيت"):
    mod = ModuleType("vosk")
    mod.heard = []

    class Model:
        def __init__(self, path):
            mod.path = path

    class KaldiRecognizer:
        def __init__(self, model, rate):
            self.rate = rate

        def AcceptWaveform(self, data):
            mod.heard.append((self.rate, len(data)))
            return True

        def FinalResult(self):
            return json.dumps({"text": said if len(mod.heard) > 1 else ""}, ensure_ascii=False)

    mod.Model, mod.KaldiRecognizer, mod.SetLogLevel = Model, KaldiRecognizer, lambda level: None
    return mod


class VoskEarsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "models" / "vosk-model-ar-mgb2-0.4" / "conf").mkdir(parents=True)

    def test_it_hears_like_whisper_does(self):
        fake = _fake_vosk()
        with mock.patch.dict(sys.modules, {"vosk": fake}):
            ears = vosk_ears.VoskEars("vosk", home=self.home)
            self.assertTrue(ears.load(log=lambda *_: None))
            text, took = ears.timed(np.zeros(16000 * 2, dtype=np.int16), "hint")
        self.assertTrue(ears.ready)
        self.assertEqual(text, "يا موس عندك فلتر زيت")
        self.assertTrue(fake.path.endswith("vosk-model-ar-mgb2-0.4"))
        self.assertEqual(fake.heard[-1], (16000, 16000 * 2 * 2))      # 16 kHz, 16-bit, as recorded

    def test_not_installed_says_so(self):
        with mock.patch.dict(sys.modules, {"vosk": None}):
            ears = vosk_ears.VoskEars("vosk", home=self.home)
            self.assertFalse(ears.load(log=lambda *_: None))
        self.assertIn("vosk isn't installed", ears.error)
        self.assertEqual(ears.hear(np.zeros(10, dtype=np.int16)), "")

    def test_brain_ini_picks_it(self):
        cfg = config.Config(model="vosk", home=self.home)
        with mock.patch.dict(sys.modules, {"vosk": _fake_vosk()}):
            ears = brain_main._ears(cfg)
        self.assertIsInstance(ears, vosk_ears.VoskEars)
        self.assertTrue(ears.ready)
        self.assertFalse(vosk_ears.is_vosk("small"))


if __name__ == "__main__":
    unittest.main()
