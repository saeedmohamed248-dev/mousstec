"""«🎓 تعلّم السمع» on «مخ موس»: a question it isn't sure it heard right goes
to the server with its recording (`check`), where a teacher hears it; what
the teachers taught (the catalog's `heard_fixes`) is written as was said
from then on — the net up or down."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

from robot.brain import config, loop, server, vad
from robot.tests.test_brain_offline import CATALOG, _clip

FOUND = "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه و50 قرش."
LESSON = {"heard": "فلترا الزي", "means": "فلتر زيت"}


class _Ears:
    """Vosk as the robot has it: the words, when each was said, how sure."""
    model_name, ready, error = "vosk", True, ""

    def __init__(self, text, confs=None):
        self.text = text
        self.words = [(w, i * 0.3, i * 0.3 + 0.3) for i, w in enumerate(text.split())]
        self.confs = confs or [1.0] * len(self.words)

    def timed(self, samples, prompt=""):
        return self.text, 0.3


class TeachTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.cfg.ears_use = "always"
        self.server = mock.Mock()
        self.server.catalog.return_value = dict(CATALOG, heard_fixes=[])
        self.server.voice.return_value = server.Answer(
            wav=None, reply=FOUND, transcript="", addressed=True, heard_by="azure", taught="عندك فلتر زيت")
        self.server.speak.return_value = _clip(5, 160)
        self.speaker = mock.Mock()
        self.lines = []

    def _brain(self, ears):
        brain = loop.Brain(self.cfg, ears=ears, server=self.server, mic=None,
                           speaker=self.speaker, log=self.lines.append)
        brain.refresh()
        for clip in brain.knowledge.clip_texts():
            brain.bank.put(clip, _clip(100, 160))
        brain.online = True
        brain._check = lambda *a: None          # the server's check after an answer here: not this test's
        return brain

    def _say(self, brain):
        brain.handle(vad.Utterance(samples=np.zeros(8000, np.int16), speech_ms=500,
                                   quietest=40, loudest=2500, cut=False))

    def test_unsure_words_go_to_a_teacher_with_the_recording(self):
        #                            يا   موس  عندك  فلترا الزي — the name's words don't count
        brain = self._brain(_Ears("يا موس عندك فلترا الزي", [0.2, 0.1, 0.95, 0.4, 0.45]))
        self._say(brain)
        kwargs = self.server.voice.call_args.kwargs
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["wav"].startswith(b"RIFF"))
        self.assertAlmostEqual(kwargs["confidence"], (0.95 + 0.4 + 0.45) / 3)
        self.assertIn("[TEACH] «يا موس عندك فلترا الزي» — azure heard «عندك فلتر زيت»", self.lines)
        self.assertFalse(any(line.startswith("[HERE]") for line in self.lines))

    def test_what_makes_no_sense_here_goes_to_a_teacher_too(self):
        brain = self._brain(_Ears("يا موس انظر الى كابوس"))        # sure of every word, and wrong
        self._say(brain)
        self.assertTrue(self.server.voice.call_args.kwargs["check"])

    def test_sure_and_understood_is_answered_here_as_before(self):
        brain = self._brain(_Ears("يا موس عندك فلتر زيت؟", [0.3, 0.2, 0.97, 0.95, 0.96]))
        self._say(brain)
        self.assertIn(f"[HERE] 🔊 {FOUND}", self.lines)
        self.assertFalse(self._checked())

    def _checked(self):
        call = self.server.voice.call_args
        return bool(call and call.kwargs.get("check"))

    def test_no_minutes_on_chatter_the_name_alone_or_when_off(self):
        for ears in (_Ears("عندك فلترا الزي", [0.4, 0.3, 0.3]), _Ears("يا موس", [0.1, 0.1])):
            with self.subTest(ears.text):
                self.server.voice.reset_mock()
                self._say(self._brain(ears))
                self.assertFalse(self._checked())
        self.cfg.teach = False
        self.server.voice.reset_mock()
        self._say(self._brain(_Ears("يا موس عندك فلترا الزي", [1, 1, 0.9, 0.3, 0.3])))
        self.assertFalse(self._checked())

    def test_what_the_teachers_taught_is_used_net_or_not(self):
        self.server.catalog.return_value = dict(CATALOG, heard_fixes=[LESSON])
        brain = self._brain(_Ears("يا موس عندك فلترا الزي"))
        self._say(brain)
        self.assertIn("[EARS] «يا موس عندك فلترا الزي» — learned: «فلترا الزي» = «فلتر زيت»", self.lines)
        self.assertIn(f"[HERE] 🔊 {FOUND}", self.lines)                # understood now: said here at once
        self.server.voice.reset_mock()
        brain.online = False                                               # the net down
        self._say(brain)
        self.server.voice.assert_not_called()
        self.assertIn(f"[OFFLINE] 🔊 {FOUND}", self.lines)
