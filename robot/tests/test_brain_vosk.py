"""robot/brain/vosk_ears.py — light ears for an old CPU (no AVX2), with the
same face as Whisper's. No real model here: a stand-in vosk module."""

import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest import mock

import numpy as np
import requests

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

    def test_a_quiet_sentence_reaches_kaldi_louder(self):
        fake, got = _fake_vosk(), []
        with mock.patch.dict(sys.modules, {"vosk": fake}):
            ears = vosk_ears.VoskEars("vosk", home=self.home)
            ears.load(log=lambda *_: None)
            accept = fake.KaldiRecognizer.AcceptWaveform
            with mock.patch.object(fake.KaldiRecognizer, "AcceptWaveform",
                                   lambda rec, data: (got.append(np.frombuffer(data, "<i2")), accept(rec, data))[1]):
                ears.hear(np.array([250, -250] * 8000, dtype=np.int16))
        self.assertEqual(int(np.abs(got[0]).max()), 5000)            # ×20 at most: the room's hiss stays a hiss

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


def _model_zip(name="vosk-model-ar-mgb2-0.4"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/conf/mfcc.conf", "--sample-frequency=16000\n")
        zf.writestr(f"{name}/am/final.mdl", "x" * 5000)
    return buf.getvalue()


class _Reply:
    """What requests.get(stream=True) gives back: `body` from `start` on,
    breaking after `cut` bytes like the shop's line did."""

    def __init__(self, body, start=0, cut=None, ranged=True):
        self.body, self.start, self.cut = body, (start if ranged else 0), cut
        self.status_code = 206 if (start and ranged) else 200
        if start >= len(body) and ranged:
            self.status_code = 416
        sent = len(body) - self.start
        self.headers = {"Content-Length": str(sent)}
        if self.status_code == 206:
            self.headers["Content-Range"] = f"bytes {start}-{len(body) - 1}/{len(body)}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 416:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        rest = self.body[self.start:]
        for i in range(0, len(rest), 1000):
            if self.cut is not None and i >= self.cut:
                raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            yield rest[i:i + 1000]


class VoskDownloadTests(unittest.TestCase):
    """🐛 [FIX]: the model broke off at 10% on the shop's line and the robot
    was left with no ears. It goes on from where it broke now."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.zip = _model_zip()
        self.asked = []
        sleep = mock.patch("robot.brain.vosk_ears.time.sleep")
        self.slept = sleep.start()
        self.addCleanup(sleep.stop)

    def ears(self):
        ears = vosk_ears.VoskEars("vosk", home=self.home)
        ears.downloads = self.home / "Downloads"
        return ears

    def get(self, *replies):
        replies = list(replies)

        def get(url, stream, timeout, headers):
            self.asked.append(headers.get("Range", ""))
            start = int(headers["Range"][6:-1]) if headers else 0
            return replies.pop(0)(start)
        return mock.patch("requests.get", side_effect=get)

    def load(self, ears):
        lines = []
        with mock.patch.dict(sys.modules, {"vosk": _fake_vosk()}):
            ok = ears.load(log=lines.append)
        return ok, lines

    def test_a_broken_download_goes_on_from_where_it_broke(self):
        ears = self.ears()
        with self.get(lambda s: _Reply(self.zip, s, cut=3000), lambda s: _Reply(self.zip, s)):
            ok, lines = self.load(ears)
        self.assertTrue(ok, ears.error)
        self.assertEqual(self.asked, ["", "bytes=3000-"])            # the second ask: only the rest
        self.assertTrue((ears.folder / "conf" / "mfcc.conf").is_file())
        self.assertFalse(list(ears.folder.parent.glob("*.part")))
        self.assertTrue(any("going on from there (2/" in line for line in lines), lines)

    def test_what_was_left_from_last_time_is_kept(self):
        ears = self.ears()
        ears.folder.parent.mkdir(parents=True)
        (ears.folder.parent / "vosk-model-ar-mgb2-0.4.zip.part").write_bytes(self.zip[:4000])
        with self.get(lambda s: _Reply(self.zip, s)):
            ok, _ = self.load(ears)
        self.assertTrue(ok, ears.error)
        self.assertEqual(self.asked, ["bytes=4000-"])

    def test_a_server_that_ignores_the_range_starts_over(self):
        ears = self.ears()
        ears.folder.parent.mkdir(parents=True)
        (ears.folder.parent / "vosk-model-ar-mgb2-0.4.zip.part").write_bytes(b"junk" * 500)
        with self.get(lambda s: _Reply(self.zip, s, ranged=False)):
            ok, _ = self.load(ears)
        self.assertTrue(ok, ears.error)

    def test_it_gives_up_after_so_many_tries(self):
        ears = self.ears()
        replies = [lambda s: _Reply(self.zip, s, cut=0)] * vosk_ears.TRIES
        with self.get(*replies):
            ok, _ = self.load(ears)
        self.assertFalse(ok)
        self.assertIn("ChunkedEncodingError", ears.error)
        self.assertEqual(len(self.asked), vosk_ears.TRIES)

    def test_a_broken_zip_is_thrown_away_for_next_time(self):
        ears = self.ears()
        with self.get(lambda s: _Reply(b"not a zip" * 100, s)):
            ok, _ = self.load(ears)
        self.assertFalse(ok)
        self.assertIn("BadZipFile", ears.error)
        self.assertFalse(list(ears.folder.parent.glob("*.part")))

    def test_a_zip_from_the_browser_needs_no_download(self):
        ears = self.ears()
        ears.downloads.mkdir()
        (ears.downloads / "vosk-model-ar-mgb2-0.4.zip").write_bytes(self.zip)
        with mock.patch("requests.get", side_effect=AssertionError("no download")):
            ok, lines = self.load(ears)
        self.assertTrue(ok, ears.error)
        self.assertTrue((ears.folder / "conf").is_dir())
        self.assertTrue(any("unpacking" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
