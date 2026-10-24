"""robot/brain/voices.py — many Arabic voices for `brain wake`, made once.

No Microsoft here: a stand-in edge_tts module, and the mp3 decoding
stood in for (soundfile and libsndfile are the shop PC's; when they're here
too, a real mp3 is read as well).
"""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import mock

import numpy as np

from robot.brain import voices, wavs


def _fake_edge(fail=()):
    mod = ModuleType("edge_tts")
    mod.asked = []

    async def list_voices():
        return [{"ShortName": "ar-EG-SalmaNeural", "Locale": "ar-EG"},
                {"ShortName": "ar-SA-HamedNeural", "Locale": "ar-SA"},
                {"ShortName": "en-US-AriaNeural", "Locale": "en-US"}]

    class Communicate:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz", **kw):
            self.text, self.voice = text, voice
            mod.asked.append((text, voice, rate, pitch))

        async def stream(self):
            if self.voice in fail:
                raise ConnectionError("dropped")
            yield {"type": "audio", "data": f"{self.voice}|{self.text}".encode()}

    mod.list_voices, mod.Communicate = list_voices, Communicate
    return mod


def _fake_decode(mp3: bytes):
    return np.full(8000 + len(mp3), 1000, np.int16)


class MakeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.lines = {"said": ["يا موس"], "talk": ["صباح الخير", "بكام"]}

    def _make(self, edge, **kw):
        with mock.patch.dict(sys.modules, {"edge_tts": edge, "soundfile": ModuleType("soundfile")}), \
                mock.patch.object(voices, "decode", _fake_decode):
            return voices.make(self.home, log=lambda *_: None, lines=self.lines, **kw)

    def test_every_arabic_voice_says_every_line_once(self):
        edge = _fake_edge()
        got = self._make(edge)
        self.assertEqual(sorted(v for v, _ in got["talk"]), ["ar-EG-SalmaNeural"] * 2 + ["ar-SA-HamedNeural"] * 2)
        self.assertEqual(len(got["said"]), 2)
        self.assertEqual(len(edge.asked), 6)                                   # not the English voice
        rates = {r for _, _, r, _ in edge.asked}
        self.assertTrue(rates <= set(voices.RATES))
        self.assertEqual(got["said"][0][1].dtype, np.int16)

    def test_made_once_then_from_disk(self):
        self._make(_fake_edge())
        again = _fake_edge()
        got = self._make(again)
        self.assertEqual(again.asked, [])                                      # nothing asked again
        self.assertEqual(len(got["talk"]), 4)

    def test_a_voice_that_fails_is_left_out_and_asked_again_next_time(self):
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):                # no waiting between tries
            got = self._make(_fake_edge(fail=("ar-SA-HamedNeural",)))
        self.assertEqual({v for v, _ in got["talk"]}, {"ar-EG-SalmaNeural"})
        later = _fake_edge()
        got = self._make(later)
        self.assertEqual({v for _, v, _, _ in later.asked}, {"ar-SA-HamedNeural"})
        self.assertEqual(len(got["talk"]), 4)

    def test_no_edge_here_learns_from_whats_on_disk(self):
        with mock.patch.dict(sys.modules, {"edge_tts": None}):
            got = voices.make(self.home, log=lambda *_: None, lines=self.lines)
        self.assertEqual(got, {"said": [], "talk": []})


class AsSaidTests(unittest.TestCase):

    def test_trimmed_and_padded_like_the_vad(self):
        x = np.concatenate([np.zeros(24000), np.full(12000, 8000.0), np.zeros(24000)])   # 24 kHz
        y = voices.as_said(x, 24000)
        self.assertEqual(y.dtype, np.int16)
        expected = int(voices.LEAD_IN * 16000) + 8000 + int(voices.TAIL * 16000)
        self.assertLess(abs(len(y) - expected), 400)

    @unittest.skipUnless(shutil.which("ffmpeg"), "no ffmpeg to make an mp3 with")
    def test_a_real_mp3(self):
        try:
            import soundfile  # noqa: F401
        except (ImportError, OSError):
            self.skipTest("soundfile isn't installed here")
        with TemporaryDirectory() as tmp:
            wav, mp3 = Path(tmp) / "a.wav", Path(tmp) / "a.mp3"
            t = np.arange(24000) / 24000
            wav.write_bytes(wavs.pcm_wav((8000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16), 24000))
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(wav), str(mp3)], check=True)
            y = voices.decode(mp3.read_bytes())
        self.assertGreater(len(y), 16000 * 0.9)
        self.assertGreater(int(np.abs(y).max()), 4000)


if __name__ == "__main__":
    unittest.main()
