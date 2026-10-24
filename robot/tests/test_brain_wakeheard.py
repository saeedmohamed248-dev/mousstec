"""robot/brain/wakeheard.py — how these ears hear «يا موس», learned on the
shop's computer. Vosk has no «موس» in its words: it wrote «يمس», «انظر إلى
كابوس», «حيث». Said a few times, what it heard is kept, and from then on
taken for the name at the start of a sentence — never in the middle, and
never a lone everyday word.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np

from robot.brain import __main__ as brain_main
from robot.brain import config, loop, server, vad, vosk_ears, wakeheard


class LearnTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_what_was_heard_for_the_name_is_kept(self):
        kept = wakeheard.save(self.home, ["يمس", "انظر إلى كابوس", "يمس", "حيث"], "موس")
        self.assertEqual(kept, ["يمس", "انظر الي كابوس", "حيث"])
        self.assertEqual(wakeheard.load(self.home), [["انظر", "الي", "كابوس"], ["يمس"], ["حيث"]])

    def test_what_isnt_the_name_is_left_out(self):
        for heard in ("", "يا موس", "ياموس", "في", "هات", "عندك فلتر زيت لو سمحت",   # nothing / name / chatter
                      "ايوه يا"):
            with self.subTest(heard):
                self.assertIsNone(wakeheard.keepable(heard, "موس"))

    def test_a_second_round_adds_to_the_first_and_forget_starts_over(self):
        wakeheard.save(self.home, ["يمس"], "موس")
        self.assertEqual(wakeheard.save(self.home, ["كابوس"], "موس"), ["يمس", "كابوس"])
        wakeheard.forget(self.home)
        self.assertEqual(wakeheard.load(self.home), [])

    def test_at_the_start_of_a_sentence_only(self):
        learned = [["انظر", "الي", "كابوس"], ["يمس"]]
        self.assertEqual(wakeheard.as_named("انظر إلى كابوس عندك فلترا الزيت", learned, "موس"),
                         (True, "يا موس عندك فلترا الزيت"))
        self.assertEqual(wakeheard.as_named("يمس", learned, "موس"), (True, "يا موس"))
        self.assertEqual(wakeheard.as_named("السلك ده يمس الموتور", learned, "موس"),
                         (False, "السلك ده يمس الموتور"))


class LoopTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.cfg.ears_use = "always"
        self.lines = []

    def test_the_brain_sends_the_name_as_it_was_meant(self):
        wakeheard.save(self.cfg.home, ["انظر إلى كابوس"], "موس")
        srv = mock.Mock()
        srv.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False)
        ears = SimpleNamespace(ready=True, model_name="vosk", error="",
                               timed=lambda samples, prompt="": ("انظر إلى كابوس عندك فلترا الزيت", 0.8))
        brain = loop.Brain(self.cfg, ears=ears, server=srv, mic=None, speaker=mock.Mock(),
                           log=self.lines.append)
        brain.handle(vad.Utterance(samples=np.zeros(16000, dtype=np.int16), speech_ms=1000,
                                   quietest=50, loudest=3000, cut=False))
        self.assertEqual(srv.voice.call_args.kwargs["transcript"], "يا موس عندك فلترا الزيت")
        self.assertIn("[EARS] «انظر إلى كابوس عندك فلترا الزيت» — «يا موس» as these ears hear it",
                      self.lines)


class WakeCommandTests(unittest.TestCase):

    def test_said_six_times_and_kept(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = config.Config(home=Path(tmp.name))
        said = iter(["يمس", "حيث", "يا موس", "يمس", "في", "انظر إلى كابوس"])
        ears = SimpleNamespace(ready=True, timed=lambda samples, prompt="": (next(said), 0.5))
        utt = vad.Utterance(samples=np.zeros(8000, dtype=np.int16), speech_ms=500,
                            quietest=50, loudest=3000, cut=False)

        class Mic:
            def __init__(self, device):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        out = []
        with mock.patch.object(brain_main, "_ears", return_value=ears), \
                mock.patch.object(brain_main, "_one_sentence", return_value=utt), \
                mock.patch.dict(sys.modules, {"robot.brain.sound": SimpleNamespace(Mic=Mic)}), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            brain_main.cmd_wake(cfg)
        self.assertEqual(wakeheard.load(cfg.home), [["انظر", "الي", "كابوس"], ["يمس"], ["حيث"]])
        self.assertTrue(any(line.startswith("[WAKE] kept: يمس · حيث · انظر الي كابوس") for line in out), out)


class TidyTests(unittest.TestCase):

    def test_kaldis_oddities_are_left_out(self):
        self.assertEqual(vosk_ears.tidy("إUٌٍأ"), "")
        self.assertEqual(vosk_ears.tidy("عندكً فلتر زيت e90"), "عندك فلتر زيت e90")


if __name__ == "__main__":
    unittest.main()
