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

    def test_the_big_vosk_has_teachers_too(self):
        # 🐛 [FIX]: «model = vosk-big» never sent a question to a teacher.
        ears = _Ears("يا موس عندك فلترا الزي", [0.2, 0.1, 0.95, 0.4, 0.45])
        ears.model_name = "vosk-big"
        self._say(self._brain(ears))
        self.assertTrue(self.server.voice.call_args.kwargs["check"])

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

    def test_the_question_after_its_name_goes_to_a_teacher_too(self):
        # 🐛 In the shop: «يا موس» ← «أيوه، تحت أمرك» ← «عندك» (Vosk lost «فلتر زيت»):
        # the question came without the name, and the teacher never heard it.
        ears = _Ears("يا موس", [0.3, 0.3])
        brain = self._brain(ears)
        self._say(brain)
        self.assertIn("[HERE] 🔊 أيوه، تحت أمرك.", self.lines)
        ears.text, ears.words, ears.confs = "عندك", [("عندك", 0.0, 0.3)], [0.9]
        self._say(brain)
        self.assertTrue(self._checked())                     # what makes no sense here: to the teacher
        # Long after the conversation, the same is someone else's chatter: no minutes on it.
        self.server.voice.reset_mock()
        brain._talking_until = 0.0
        self._say(brain)
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

    def test_a_sentence_a_teacher_heard_so_twice_is_answered_at_once(self):
        # Azure heard «عندك فلتر زيت» each time Vosk wrote «عندك سلطة الزيت»: no teacher again,
        # and the answer comes from this computer at once — every question taught or not.
        self.cfg.teach_all = True
        self.server.catalog.return_value = dict(CATALOG, heard_sentences=[
            {"heard": "عندك سلطه الزيت", "means": "عندك فلتر زيت"}])
        brain = self._brain(_Ears("يا موس عندك سلطة الزيت", [0.2, 0.1, 0.5, 0.3, 0.4]))
        self._say(brain)
        self.assertIn("[EARS] «يا موس عندك سلطة الزيت» — learned: «عندك فلتر زيت» (a teacher heard it so before)",
                      self.lines)
        self.assertFalse(self._checked())
        self.assertIn(f"[HERE] 🔊 {FOUND}", self.lines)
        brain.online = False                                               # the net down: the same
        self._say(brain)
        self.assertIn(f"[OFFLINE] 🔊 {FOUND}", self.lines)

    def test_a_sentence_the_ears_got_right_needs_no_teacher(self):
        self.cfg.teach_all = True
        self.server.catalog.return_value = dict(CATALOG, heard_sentences=[
            {"heard": "عندك فلتر زيت", "means": "عندك فلتر زيت؟"}])
        brain = self._brain(_Ears("يا موس عندك فلتر زيت", [0.2, 0.1, 0.6, 0.6, 0.6]))
        self._say(brain)
        self.assertFalse(self._checked())
        self.assertIn(f"[HERE] 🔊 {FOUND}", self.lines)
        self.assertFalse(any("learned" in line for line in self.lines))  # nothing to say: it heard right

    def test_any_other_sentence_still_goes_to_the_teacher(self):
        self.cfg.teach_all = True
        self.server.catalog.return_value = dict(CATALOG, heard_sentences=[
            {"heard": "عندك فلتر زيت", "means": "عندك فلتر زيت"}])
        self._say(self._brain(_Ears("يا موس عندك فلتر زيت بوش")))
        self.assertTrue(self._checked())

    def test_a_learned_sentence_is_heard_by_a_teacher_again_now_and_then(self):
        # Answered at once every time; every RECHECK_EVERY-th time the server's check behind the
        # answer is a teacher's, on the recording — a wrong lesson gets heard again, and unlearned.
        self.cfg.teach_all = True
        self.server.catalog.return_value = dict(CATALOG, heard_sentences=[
            {"heard": "عندك سلطه الزيت", "means": "عندك فلتر زيت"}])
        brain = self._brain(_Ears("يا موس عندك سلطة الزيت"))
        checks = []
        brain._check = lambda *args: checks.append(args)
        for _ in range(loop.RECHECK_EVERY):
            self._say(brain)
        self.assertEqual(self.lines.count(f"[HERE] 🔊 {FOUND}"), loop.RECHECK_EVERY)   # never held for it
        self.assertFalse(self._checked())
        heard_again = [c for c in checks if c[3] is not None]
        self.assertEqual(len(heard_again), 1)
        text, _said, _here, wav, heard, known = heard_again[0]
        self.assertEqual(known, "learned")
        self.assertEqual((text, heard), ("يا موس عندك فلتر زيت", "يا موس عندك سلطة الزيت"))
        self.assertTrue(wav.startswith(b"RIFF"))
        self.cfg.teach = False                                             # teach = no: never
        checks.clear()
        for _ in range(loop.RECHECK_EVERY):
            self._say(brain)
        self.assertFalse([c for c in checks if c[3] is not None])

    def test_the_server_check_with_a_recording_is_a_teachers(self):
        brain = self._brain(_Ears("يا موس عندك فلتر زيت"))
        brain._check = loop.Brain._check.__get__(brain)
        brain._check("يا موس عندك فلتر زيت", 0.0, FOUND, b"RIFF....", "يا موس عندك سلطة الزيت")
        kwargs = self.server.voice.call_args.kwargs
        self.assertEqual((kwargs["check"], kwargs["heard"], kwargs["wav"], kwargs["reply_audio"]),
                         (True, "يا موس عندك سلطة الزيت", b"RIFF....", ""))

    def test_offline_unsure_of_nonsense_asks_again(self):
        # The net down, Vosk unsure, and nothing here makes sense of it: «مسمعتكش كويس», not «ملقتش».
        from robot.brain import talk

        brain = self._brain(_Ears("يا موس عندك كابوس", [0.2, 0.1, 0.9, 0.3]))
        brain.online = False
        self._say(brain)
        self.assertIn(f"[OFFLINE] 🔊 {talk.HEARD_BADLY}", self.lines)
        self.lines.clear()
        brain = self._brain(_Ears("يا موس عندك كابوس", [0.9, 0.9, 0.95, 0.95]))   # sure of it: as before
        brain.online = False
        self._say(brain)
        self.assertFalse(any(talk.HEARD_BADLY in line for line in self.lines))

    def test_a_sentence_of_words_these_ears_write_right_is_answered_at_once(self):
        # The teachers heard «عندك», «فلتر», «زيت» as Vosk wrote them, time and again: no teacher.
        self.cfg.teach_all = True
        self.server.catalog.return_value = dict(CATALOG, heard_trusted=["عندك", "فلتر", "زيت"])
        brain = self._brain(_Ears("يا موس عندك فلتر زيت", [0.3, 0.2, 0.6, 0.5, 0.6]))
        checks = []
        brain._check = lambda *args: checks.append(args)
        self._say(brain)
        self.assertFalse(self._checked())
        self.assertIn(f"[HERE] 🔊 {FOUND}", self.lines)
        self.assertEqual(checks[0][5], "trusted")                          # said so to the server's check
        for _ in range(loop.RECHECK_EVERY - 1):
            self._say(brain)
        self.assertEqual(len([c for c in checks if c[3] is not None]), 1)  # heard by a teacher again, once
        # A word it hasn't earned: the teacher hears it.
        self.server.voice.reset_mock()
        self._say(self._brain(_Ears("يا موس عندك فلتر زيت بوش")))
        self.assertTrue(self._checked())

