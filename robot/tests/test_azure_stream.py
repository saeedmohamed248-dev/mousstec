"""teachers.AzureStream: Azure hearing a sentence while it's said — what goes
up (a WAV header, then the sound as it comes, 30 s at most), and what it
costs whatever comes of it (robot/tests/test_live_voice_db.py: the socket)."""

import os
import threading
import time
import unittest
from unittest import mock

from robot import audio as audio_svc
from robot import teachers
from robot.tests.test_teachers_db import AZURE, _Resp, azure_says


class _Post:
    """requests.post, reading the chunks as they come."""

    def __init__(self, answer=None, hold=None):
        self.answer, self.hold = answer or azure_says("عندك فلتر زيت"), hold
        self.body, self.kwargs = None, None

    def __call__(self, url, **kwargs):
        self.kwargs = kwargs
        self.body = b"".join(kwargs["data"])
        if self.hold is not None:
            self.hold.wait(5)
        return self.answer


@mock.patch.dict(os.environ, AZURE)
class AzureStreamTests(unittest.TestCase):

    def test_the_header_then_the_sound_as_it_comes(self):
        post = _Post()
        stream = teachers.AzureStream(post=post)
        for _ in range(5):
            stream.feed(b"\x01\x00" * 16000)          # 1 s at a time
        heard = stream.result(5)
        self.assertEqual((heard.text, heard.billed_seconds), ("عندك فلتر زيت", 5))
        self.assertEqual(post.body[:44], teachers._stream_header())
        tag, rate, _ = audio_svc.wav_parts(post.body)
        self.assertEqual((tag, rate), (1, 16000))         # 16-bit PCM, 16 kHz: what Azure is told
        self.assertEqual(len(post.body) - 44, 5 * 32000)
        self.assertEqual(post.kwargs["headers"]["Content-Type"], "audio/wav; codecs=audio/pcm; samplerate=16000")
        self.assertEqual(post.kwargs["headers"]["Expect"], "100-continue")    # what Azure asks of chunks
        self.assertTrue(stream.taken)

    def test_no_more_than_thirty_seconds_go_up(self):
        post = _Post()
        stream = teachers.AzureStream(post=post)
        for _ in range(40):
            stream.feed(b"\x00\x00" * 16000)
        stream.result(5)
        self.assertEqual(len(post.body) - 44, teachers.MOST_SECONDS * 32000)
        self.assertEqual(stream.billed(), teachers.MOST_SECONDS)

    def test_let_go_its_trip_is_cut_and_its_seconds_count(self):
        post = _Post()
        stream = teachers.AzureStream(post=post)
        stream.feed(b"\x00\x00" * 24000)                  # 1.5 s
        stream.cancel()
        self.assertTrue(stream.wait(5))
        self.assertIsInstance(stream.error, teachers._Dropped)
        self.assertEqual(stream.billed(), 2)

    def test_refused_it_costs_nothing(self):
        stream = teachers.AzureStream(post=_Post(_Resp({}, status=400)))
        stream.feed(b"\x00\x00" * 32000)
        with self.assertRaises(RuntimeError):
            stream.result(5)
        self.assertEqual(stream.billed(), 0)

    def test_too_slow_it_is_let_go(self):
        hold = threading.Event()
        stream = teachers.AzureStream(post=_Post(hold=hold))
        stream.feed(b"\x00\x00" * 16000)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            stream.result(0.3)
        self.assertLess(time.monotonic() - started, 2)
        hold.set()

    def test_gone_quiet_it_gives_up(self):
        with mock.patch.object(teachers, "STREAM_IDLE_S", 0.2):
            stream = teachers.AzureStream(post=_Post())
            stream.feed(b"\x00\x00" * 16000)
            self.assertTrue(stream.wait(5))               # nothing more came, and no end either
        self.assertIsInstance(stream.error, teachers._Dropped)
