"""«مخ موس» answers stock and prices from this computer at once, and the
server checks it after (loop.Brain._answer_here): 1.2–3 s saved on the
shop's line. The server's word wins: another price, or sold out a moment
ago, and the robot corrects itself.
"""

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

from robot.brain import config, loop, server, vad
from robot.tests.test_brain_offline import CATALOG, _clip

FOUND = "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه و50 قرش."


class _Ears:
    model_name, ready, error = "vosk", True, ""

    def __init__(self, text):
        self.text = text

    def timed(self, samples, prompt=""):
        return self.text, 0.3


class AnswerHereTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.cfg.ears_use = "always"
        self.server = mock.Mock()
        self.server.catalog.return_value = CATALOG
        self.server.voice.return_value = server.Answer(wav=None, reply=FOUND, transcript="", addressed=True)
        self.server.speak.return_value = _clip(5, 160)
        self.speaker = mock.Mock()
        self.lines = []
        self.checked = threading.Event()

    def _brain(self, text="يا موس عندك فلتر زيت؟", voiced=True):
        brain = loop.Brain(self.cfg, ears=_Ears(text), server=self.server, mic=None,
                           speaker=self.speaker, log=self.lines.append)
        brain.refresh()
        if voiced:
            for clip in brain.knowledge.clip_texts():
                brain.bank.put(clip, _clip(100, 160))
        brain.online = True
        check = brain._check

        def checked(*a):
            try:
                check(*a)
            finally:
                self.checked.set()
        brain._check = checked
        return brain

    def _say(self, brain):
        brain.handle(vad.Utterance(samples=np.zeros(8000, np.int16), speech_ms=500,
                                   quietest=40, loudest=2500, cut=False))
        self.checked.wait(5)

    def test_said_at_once_and_the_server_asked_after(self):
        brain = self._brain()
        self._say(brain)
        self.assertIn(f"[HERE] 🔊 {FOUND}", self.lines)
        self.speaker.play.assert_called_once()                     # the voice bank's, not the server's
        kwargs = self.server.voice.call_args.kwargs
        self.assertEqual((kwargs["transcript"], kwargs["reply_audio"]), ("يا موس عندك فلتر زيت؟", ""))
        self.server.speak.assert_not_called()                     # same answer: nothing more said

    def test_the_server_says_otherwise_and_it_corrects_itself(self):
        self.server.voice.return_value = server.Answer(
            wav=None, reply="فلتر زيت مش موجود هنا دلوقتي، وسعره 450 جنيه و50 قرش.", transcript="", addressed=True)
        brain = self._brain()
        self._say(brain)
        self.assertTrue(any("the server says otherwise" in line for line in self.lines), self.lines)
        self.assertTrue(self.server.speak.call_args.args[0].startswith(loop.SORRY))
        self.assertEqual(self.speaker.play.call_count, 2)                      # the answer, then the correction

    def test_a_count_still_above_none_isnt_corrected(self):
        self.server.voice.return_value = server.Answer(
            wav=None, reply="أيوه، فلتر زيت موجود، عندي منه 2 حتت، بسعر 450 جنيه و50 قرش.", transcript="",
            addressed=True)
        brain = self._brain()
        self._say(brain)
        self.server.speak.assert_not_called()

    def test_what_isnt_stock_and_price_is_the_servers(self):
        for said in ("يا موس بعت 2 فلتر زيت كاش", "يا موس شغل القرآن", "يا موس عندك جنط سبور؟"):
            with self.subTest(said):
                self.server.voice.reset_mock()
                brain = self._brain(said)
                self._say(brain)
                self.assertNotEqual(self.server.voice.call_args.kwargs.get("reply_audio"), "")
                self.assertFalse(any(line.startswith("[HERE]") for line in self.lines))

    def test_after_an_answer_that_asked_the_next_is_the_servers(self):
        brain = self._brain("يا موس عندك طرمبة مية؟")            # two pumps: «تقصد أنهي؟»
        self.server.voice.return_value = server.Answer(
            wav=None, reply="عندي أكتر من نوع: طرمبة مية (E90)، طرمبة مية (F30). تقصد أنهي؟",
            transcript="", addressed=True)
        self._say(brain)
        self.server.voice.reset_mock()
        brain.ears.text = "يا موس عندك فلتر زيت؟"
        self._say(brain)
        self.assertNotEqual(self.server.voice.call_args.kwargs.get("reply_audio"), "")

    def test_no_voice_for_it_yet_or_switched_off_is_the_servers(self):
        brain = self._brain(voiced=False)
        self._say(brain)
        self.assertNotEqual(self.server.voice.call_args.kwargs.get("reply_audio"), "")
        self.cfg.answer_here = False
        self.server.voice.reset_mock()
        self._say(self._brain())
        self.assertNotEqual(self.server.voice.call_args.kwargs.get("reply_audio"), "")

    def test_the_catalog_every_minute_said_only_when_it_changes(self):
        brain = self._brain()
        brain.refresh()
        self.assertEqual(sum(line.startswith("[EARS] catalog:") for line in self.lines), 1)
        self.assertEqual(loop.FRESH_CATALOG_S, 60)

    def test_brain_ini_switches_it_off(self):
        ini = Path(self.tmp.name) / "brain.ini"
        ini.write_text("[robot]\nanswer_here = no\n", encoding="utf-8")
        self.assertFalse(config.load(ini).answer_here)
        ini.write_text("[robot]\nname = موس\n", encoding="utf-8")
        self.assertTrue(config.load(ini).answer_here)


if __name__ == "__main__":
    unittest.main()
