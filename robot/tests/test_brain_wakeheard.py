"""robot/brain/wakeheard.py — how these ears hear «يا موس», learned on the
shop's computer. Vosk has no «موس» in its words: it wrote «يمس», «انظر إلى
كابوس», «حيث». Said a few times, what it heard is kept, and from then on
taken for the name at the start of a sentence — never in the middle, and
never a lone everyday word.
"""

import itertools
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

    def test_someone_elses_name_is_never_the_robots(self):
        # 🐛 In the shop «يا موس» came out as «احمد», «مصطفي», «يحمد» among the rest:
        # kept, the robot would answer «احمد، هات الفلتر» said to a colleague.
        for heard in ("احمد", "مصطفى", "يحمد", "ياحمد", "يا محمد", "مصطفي يمس"):
            with self.subTest(heard):
                self.assertIsNone(wakeheard.keepable(heard, "موس"))
        self.assertEqual(wakeheard.keepable("يموت", "موس"), "يموت")      # not a name: kept
        # Kept before this rule: left out when loaded, and from the file at the next save.
        wakeheard._path(self.home).write_text("يمس\nيمس يمس\nيموت\nاحمد\nمصطفي\nيحمد\n", encoding="utf-8")
        self.assertEqual(wakeheard.load(self.home), [["يمس", "يمس"], ["يمس"], ["يموت"]])
        self.assertEqual(wakeheard.as_named("احمد هات الفلتر", wakeheard.load(self.home), "موس"),
                         (False, "احمد هات الفلتر"))
        self.assertEqual(wakeheard.save(self.home, [], "موس"), ["يمس يمس", "يمس", "يموت"])

    def test_what_the_ears_wrote_for_the_others_called_is_left_out(self):
        kept = wakeheard.save(self.home, ["يمس", "حيث", "انظر إلى كابوس"], "موس",
                              others=["حيث", "يا كريم", ""])
        self.assertEqual(kept, ["يمس", "انظر الي كابوس"])

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

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = config.Config(home=Path(tmp.name))
        self.out = []

    def _run(self, report, words=None, commands=False, record=True):
        # Step 1's ten «يا موس», then how the ears wrote the others called by name in step 2.
        words = itertools.chain(words or ["يمس", "حيث", "يا موس", "يمس", "في", "انظر إلى كابوس"] + ["يمس"] * 4,
                                itertools.repeat("يا كريم"))
        ears = SimpleNamespace(ready=True, timed=lambda samples, prompt="": (next(words), 0.5))
        utt = vad.Utterance(samples=np.full(8000, 7, dtype=np.int16), speech_ms=500,
                            quietest=50, loudest=3000, cut=False)
        learned = {}

        class Mic:
            def __init__(self, device):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def drain(self):
                pass

        def learn(own, voices, log, cache):
            learned.update(said=len(own["said"]), names=len(own["names"]), near=len(own["near"]),
                           talk=len(own["talk"]), room=len(own["room"]), voices=len(voices.get("said", [])))
            saver = SimpleNamespace(save=lambda path: Path(path).write_bytes(b"model"))
            return saver, (saver if commands else None), report

        with mock.patch.object(brain_main, "_ears", return_value=ears), \
                mock.patch.object(brain_main, "_one_sentence", return_value=utt), \
                mock.patch.object(brain_main, "_record", side_effect=lambda mic, s: np.zeros(int(s * 16000), np.int16)), \
                mock.patch("robot.brain.kws.learn", learn), \
                mock.patch("robot.brain.voices.make", return_value={"said": [("v1", np.zeros(800, np.int16))]}), \
                mock.patch.dict(sys.modules, {"robot.brain.sound": SimpleNamespace(Mic=Mic)}), \
                mock.patch("builtins.print", lambda *a, **k: self.out.append(" ".join(map(str, a)))):
            brain_main.cmd_wake(self.cfg, record=record)
        return learned

    def test_five_steps_kept_and_learned(self):
        learned = self._run({"held_out": 3, "found": 3, "others": 20, "false": 0, "threshold": 0.6,
                             "usable": True})
        self.assertEqual(learned, {"said": 10, "names": 6, "near": len(brain_main.NEAR_WORDS),
                                   "talk": 20 * 16000, "room": 10 * 16000, "voices": 1})
        home = Path(self.cfg.home)
        self.assertEqual(len(list((home / "wake").glob("said-*.wav"))), 10)
        self.assertTrue((home / "wake_model.npz").exists())
        self.assertEqual(wakeheard.load(home), [["انظر", "الي", "كابوس"], ["يمس"], ["حيث"]])
        self.assertIn("[WAKE] test: found «يا موس» 3 of 3 times it hadn't heard (the shop's own), "
                      "woke wrongly 0 of 20 times (bar 0.6)", self.out)

    def test_a_spelling_heard_for_a_colleague_too_is_not_kept(self):
        self._run({"held_out": 3, "found": 3, "others": 20, "false": 0, "threshold": 0.6, "usable": True},
                  words=["يمس", "حيث", "انظر إلى كابوس"] + ["يمس"] * 7 + ["حيث"])
        self.assertEqual(wakeheard.load(Path(self.cfg.home)), [["انظر", "الي", "كابوس"], ["يمس"]])

    def test_a_second_run_learns_from_both(self):
        report = {"held_out": 3, "found": 1, "others": 20, "false": 2, "threshold": 0.8, "usable": False}
        self._run(report)
        learned = self._run(report)
        self.assertEqual(learned["said"], 20)                                 # what was there, and the new
        self.assertFalse((Path(self.cfg.home) / "wake_model.npz").exists())   # not good enough: not kept
        self.assertTrue(any("not good enough yet" in line for line in self.out))

    def test_the_commands_kept_only_when_good_enough(self):
        good = {"held_out": 3, "found": 3, "others": 20, "false": 0, "threshold": 0.6, "usable": True,
                "commands_held_out": 10, "commands_right": 8, "commands_false": 0}
        self._run(good, commands=True)
        self.assertTrue((Path(self.cfg.home) / "wake_commands.npz").exists())
        self._run(dict(good, commands_right=3), commands=True)
        self.assertFalse((Path(self.cfg.home) / "wake_commands.npz").exists())
        self.assertTrue(any("commands not good enough yet" in line for line in self.out))

    def test_wake_voices_needs_no_mic(self):
        self._run({"held_out": 3, "found": 3, "others": 20, "false": 0, "threshold": 0.6, "usable": True})
        learned = self._run({"held_out": 8, "found": 8, "others": 40, "false": 0, "threshold": 0.6,
                             "usable": True}, words=[], record=False)
        self.assertEqual(learned["said"], 10)                                 # what was recorded before

    def test_forget_starts_over(self):
        self._run({"held_out": 3, "found": 3, "others": 20, "false": 0, "threshold": 0.6, "usable": True})
        with mock.patch("builtins.print"):
            brain_main.cmd_wake(self.cfg, forget=True)
        home = Path(self.cfg.home)
        self.assertFalse(list((home / "wake").glob("*.wav")) or (home / "wake_model.npz").exists())
        self.assertEqual(wakeheard.load(home), [])


class TidyTests(unittest.TestCase):

    def test_when_each_word_was_said(self):
        result = {"text": "يمس عندك", "result": [{"word": "يمس", "start": 0.3, "end": 0.7, "conf": 0.4},
                                                  {"word": "عندك", "start": 0.8, "end": 1.1}, {"bad": 1}]}
        self.assertEqual(vosk_ears._words(result), [("يمس", 0.3, 0.7), ("عندك", 0.8, 1.1)])
        self.assertEqual(vosk_ears._words({}), [])

    def test_kaldis_oddities_are_left_out(self):
        self.assertEqual(vosk_ears.tidy("إUٌٍأ"), "")
        self.assertEqual(vosk_ears.tidy("عندكً فلتر زيت e90"), "عندك فلتر زيت e90")


if __name__ == "__main__":
    unittest.main()
