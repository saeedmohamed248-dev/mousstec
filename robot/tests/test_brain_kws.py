"""robot/brain/kws.py — «يا موس» heard by a model made for it.

The fixed half (openWakeWord's melspectrogram + speech embedding, Apache 2.0)
runs in plain numpy on the shop's PC — it must give what onnxruntime gives
(tests/data/wake_reference.npz was made with it). The learned half is
checked on made-up sounds: it learns the one it's taught, at the start of a
sentence only, and the brain puts the name in place of what Vosk wrote.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np

from robot.brain import config, kws, loop, server, vad

REFERENCE = Path(__file__).resolve().parent / "data" / "wake_reference.npz"
RATE = 16000


class FeaturesTests(unittest.TestCase):

    def test_the_same_numbers_as_onnxruntime(self):
        ref = np.load(REFERENCE)
        f = kws.Features()
        mel = f.melspec(ref["samples"])
        np.testing.assert_allclose(mel, ref["mel"], atol=1e-4)
        np.testing.assert_allclose(f.embed(mel), ref["emb"], rtol=1e-4, atol=1e-3)

    def test_one_look_is_sixteen_fingerprints(self):
        f = kws.Features()
        prints = f.fingerprints(np.zeros(kws.CLIP, np.int16))
        self.assertEqual(prints.shape, (kws.SPAN, 96))
        self.assertEqual(kws.windows(prints).shape, (1, kws.SPAN * 96))


def _tone(f0, f1, seconds, rng, level=6000):
    t = np.arange(int(seconds * RATE)) / RATE
    sweep = np.sin(2 * np.pi * (f0 + (f1 - f0) * t / (2 * seconds)) * t)
    return (level * sweep * np.hanning(len(t)) + rng.standard_normal(len(t)) * 80).astype(np.int16)


def _utterance(core, rng):
    """As the VAD cuts it: 300 ms of room before, 150 ms after."""
    room = (rng.standard_normal(int(0.45 * RATE)) * 60).astype(np.int16)
    return np.concatenate([room[:4800], core, room[4800:]])


class LearnTests(unittest.TestCase):
    """Made-up «words»: the name is a rising sweep then a hiss; the others
    are falling sweeps and tones; «اسكت» a short hiss–tone pair. Two
    «voices» each (a pitch apart), one of every four held back for the test."""

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(3)

        def name(pitch=1.0):
            return np.concatenate([_tone(300 * pitch, 900 * pitch, 0.35, rng),
                                   (rng.standard_normal(int(0.2 * RATE)) * 2500).astype(np.int16)])

        def other(i, pitch=1.0):
            return _tone((1200 - 100 * (i % 5)) * pitch, (250 + 40 * (i % 7)) * pitch, 0.5, rng)

        def stop(pitch=1.0):
            return np.concatenate([(rng.standard_normal(int(0.15 * RATE)) * 3000).astype(np.int16),
                                   _tone(2000 * pitch, 2000 * pitch, 0.3, rng)])

        own = {"said": [_utterance(name(), rng) for _ in range(6)],
               "names": [_utterance(other(i), rng) for i in range(3)],
               "near": [_utterance(_tone(500 + 80 * i, 500 + 80 * i, 0.5, rng), rng) for i in range(3)],
               "talk": np.concatenate([_utterance(other(i + 3), rng) for i in range(12)]),
               "room": (rng.standard_normal(4 * RATE) * 60).astype(np.int16)}
        pitches = {f"v{i}": 0.85 + 0.05 * i for i in range(8)}
        voices = {"said": [(v, _utterance(name(f), rng)) for v, f in pitches.items()],
                  "talk": [(v, _utterance(other(i, f), rng)) for v, f in pitches.items() for i in range(3)],
                  "stop": [(v, _utterance(stop(f), rng)) for v, f in pitches.items()]}
        with mock.patch.object(kws, "AUGMENT", 3):
            cls.detector, cls.commands, cls.report = kws.learn(own, voices, log=lambda *_: None)
        cls.rng, cls.name, cls.other, cls.stop = rng, staticmethod(name), staticmethod(other), staticmethod(stop)

    def test_it_learns_the_name_and_not_the_rest(self):
        self.assertTrue({"held_out", "found", "others", "false", "threshold", "usable", "voices",
                         "commands_right"} <= set(self.report))
        self.assertEqual(self.report["voices"], 8)
        self.assertTrue(0.5 <= self.detector.threshold <= kws.HIGHEST_BAR)
        fresh = [_utterance(np.concatenate([self.name(), _tone(700, 400, 0.4, self.rng)]), self.rng)
                 for _ in range(3)]                                    # «يا موس» + more words
        self.assertGreaterEqual(sum(self.detector.heard(x)[0] for x in fresh), 2)
        self.assertEqual(sum(self.detector.heard(_utterance(self.other(i), self.rng))[0] for i in range(4)), 0)

    def test_the_command_after_the_name(self):
        said = _utterance(np.concatenate([self.name(), self.stop()]), self.rng)
        ok, _p, end = self.detector.heard(said)
        self.assertEqual(self.commands.heard(said, end)[0], "stop")
        self.assertIsNone(self.commands.heard(_utterance(self.other(2), self.rng))[0])

    def test_only_at_the_start_of_a_sentence(self):
        late = _utterance(np.concatenate([_tone(700, 400, 2.5, self.rng), self.name()]), self.rng)
        self.assertFalse(self.detector.heard(late)[0])

    def test_where_the_name_ended(self):
        ok, prob, end = self.detector.heard(_utterance(self.name(), self.rng))
        self.assertTrue(0.3 <= end <= 1.2, end)

    def test_kept_and_read_back(self):
        with TemporaryDirectory() as tmp:
            for model, cls in ((self.detector, kws.Detector), (self.commands, kws.Commands)):
                path = Path(tmp) / "model.npz"
                model.save(path)
                again = cls.load(path)
                self.assertEqual(again.threshold, np.float32(model.threshold))
        x = _utterance(self.name(), self.rng)
        with TemporaryDirectory() as tmp:
            self.detector.save(Path(tmp) / "m.npz")
            again = kws.Detector.load(Path(tmp) / "m.npz")
        self.assertAlmostEqual(again.score(x)[0], self.detector.score(x)[0], places=5)
        self.assertIsNone(kws.Detector.load(Path("/nonexistent/wake_model.npz")))

    def test_the_voices_fingerprints_are_kept_for_next_time(self):
        with TemporaryDirectory() as tmp:
            cache = kws.PrintCache(Path(tmp) / "prints.npz")
            clip = _utterance(self.name(), self.rng)
            first = cache.prints(self.detector.features, clip)
            cache.save()
            again = kws.PrintCache(Path(tmp) / "prints.npz")
            with mock.patch.object(kws.Features, "fingerprints", side_effect=AssertionError("computed again")):
                np.testing.assert_allclose(again.prints(self.detector.features, clip), first)


class BrainTests(unittest.TestCase):
    """The brain puts «يا موس» where the words had something else."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.cfg.ears_use = "always"
        self.lines = []
        self.server = mock.Mock()
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False)

    def _say(self, text, woke, words=(), command=None):
        ears = SimpleNamespace(ready=True, model_name="vosk", error="", words=list(words),
                               timed=lambda samples, prompt="": (text, 0.5))
        brain = loop.Brain(self.cfg, ears=ears, server=self.server, mic=None, speaker=mock.Mock(),
                           log=self.lines.append)
        brain.detector = SimpleNamespace(heard=lambda samples: woke)
        if command is not None:
            brain.commands = SimpleNamespace(heard=lambda samples, after=0.0: command)
        brain.handle(vad.Utterance(samples=np.zeros(RATE, np.int16), speech_ms=1000,
                                   quietest=50, loudest=3000, cut=False))
        return self.server.voice.call_args.kwargs.get("transcript") if self.server.voice.called else None

    def test_the_words_vosk_wrote_for_the_name_are_replaced(self):
        words = [("انظر", 0.3, 0.5), ("إلى", 0.5, 0.6), ("كابوس", 0.6, 1.0), ("عندك", 1.1, 1.4),
                 ("فلترا", 1.4, 1.8), ("الزيت", 1.8, 2.2)]
        sent = self._say("انظر إلى كابوس عندك فلترا الزيت", (True, 0.93, 1.05), words)
        self.assertEqual(sent, "يا موس عندك فلترا الزيت")
        self.assertIn("[WAKE] «يا موس» heard (0.93) — the words had «انظر إلى كابوس عندك فلترا الزيت»",
                      self.lines)

    def test_the_question_right_after_the_name_is_kept(self):
        # The model's «ended» can be a little late: «فلتر» started before it, ended after.
        words = [("يمس", 0.3, 0.8), ("فلتر", 0.9, 1.3), ("زيت", 1.3, 1.6)]
        self.assertEqual(self._say("يمس فلتر زيت", (True, 0.9, 1.0), words), "يا موس فلتر زيت")
        many = [("ا", 0.1, 0.2), ("ب", 0.2, 0.3), ("ج", 0.3, 0.4), ("د", 0.4, 0.5), ("زيت", 1.3, 1.6)]
        self.assertEqual(self._say("ا ب ج د زيت", (True, 0.9, 1.0), many), "يا موس د زيت")   # three at most

    def test_the_name_alone_when_vosk_wrote_nothing(self):
        self.assertEqual(self._say("", (True, 0.9, 0.9)), "يا موس")

    def test_someone_else_called_by_name_is_left_alone(self):
        self.assertEqual(self._say("يا محمد شوف الزبون", (True, 0.8, 0.9)), "يا محمد شوف الزبون")
        self.assertTrue(any("someone else called" in line for line in self.lines))

    def test_the_names_other_spellings_go_out_as_the_name(self):
        # A server not updated yet doesn't know «يمس» or «موسى»: it gets «يا موس».
        self.assertEqual(self._say("موسى زيك فيه", None), "يا موس زيك فيه")
        self.assertEqual(self._say("يا موس عندك فلتر؟", None), "يا موس عندك فلتر؟")   # as it was said

    def test_nothing_changes_when_it_heard_nothing_or_the_words_have_it(self):
        self.assertEqual(self._say("عندك فلتر زيت", (False, 0.2, 0.0)), "عندك فلتر زيت")
        self.assertEqual(self._say("يا موس عندك فلتر", (True, 0.99, 0.9)), "يا موس عندك فلتر")
        self.assertEqual(self._say("عندك فلتر زيت", None), "عندك فلتر زيت")      # no model yet


    def test_a_command_vosk_wrote_wrong_goes_out_as_the_server_reads_it(self):
        self.assertEqual(self._say("يا موس وتي الصوت", (True, 0.9, 0.9), command=("quieter", 0.93)),
                         "يا موس وطي الصوت")
        self.assertIn("[WAKE] «وطي الصوت» by its sound (0.93) — the words had «وتي الصوت»", self.lines)

    def test_a_question_is_never_turned_into_a_command(self):
        heard = ("stop", 0.95)
        self.assertEqual(self._say("يا موس عندك فلتر زيت لو سمحت", None, command=heard),
                         "يا موس عندك فلتر زيت لو سمحت")                     # a sentence, not a command
        self.assertEqual(self._say("يا موس وقف الصوت", None, command=("quieter", 0.9)),
                         "يا موس وقف الصوت")                                    # the words had it
        self.assertEqual(self._say("عندك فلتر", (False, 0.1, 0.0), command=heard), "عندك فلتر")      # not to the robot
        self.assertEqual(self._say("يا موس صباح الخير", None, command=heard), "يا موس صباح الخير")   # no piece of one
        self.assertEqual(self._say("يا موس شغل العربية", None, command=("quran", 0.97)),
                         "يا موس شغل العربية")                                 # the car, not the Quran
        self.assertEqual(self._say("يا موس", None, command=("louder", 0.9)), "يا موس علي الصوت")   # words lost: by sound


if __name__ == "__main__":
    unittest.main()
