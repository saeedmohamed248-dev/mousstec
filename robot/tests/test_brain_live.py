"""«إرسال الصوت أول بأول» on «مخ موس» (robot/brain/live.py): the sentence's
sound goes up while it's said, its answer comes back on the same socket —
and whenever the socket can't take a sentence (none, dropped, a line too
slow), /voice/ with the whole recording, as before."""

import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np

from robot.brain import config, doctor, live, loop, server, vad, wavs
from robot.tests.test_brain import _frames, _speech
from robot.tests.test_brain_offline import CATALOG, _clip
from robot.tests.test_brain_teach import _Ears

API = "https://shop.example/api/robot/v1"
ANSWER = {"reply": "أيوه، فلتر زيت موجود.", "addressed": True, "transcript": "يا موس عندك فلتر زيت",
          "timing": "hear=5;think=9"}
GAPPED, STEADY = SimpleNamespace(gapped=True, said_ms=1000), SimpleNamespace(gapped=False, said_ms=1000)   # the sentence so far (vad.Segmenter)


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("waited too long")
        time.sleep(0.01)


class _Socket:
    """The server's end, as websocket-client hands it to the brain: what went
    up is kept, an `end` is answered (`answer`; False: never)."""

    def __init__(self, answer=None, voice=b"RIFF-the-reply", stale=False, close_on_end=False):
        self.sent, self.closed = [], False
        self.answer = ANSWER if answer is None else answer
        self.voice, self.stale, self.close_on_end = voice, stale, close_on_end
        self.hold = None                         # an Event: sound waits for it (a slow line)
        self._down = queue.Queue()
        self._down.put(json.dumps({"type": "ready"}))

    def send(self, payload):
        if self.closed:
            raise ConnectionError("closed")
        message = json.loads(payload)
        self.sent.append(("text", message))
        if message["type"] != "end":
            return
        if self.close_on_end:
            self.close()
            return
        if self.answer is False:
            return
        if self.stale:                           # an older sentence's answer, late
            self._down.put(json.dumps({"type": "reply", "id": message["id"] - 1, "reply": "قديم", "audio": True}))
            self._down.put(b"OLD")
        self._down.put(json.dumps({**self.answer, "type": "reply", "id": message["id"], "audio": bool(self.voice)},
                                  ensure_ascii=False))
        if self.voice:
            self._down.put(self.voice)

    def send_binary(self, payload):
        if self.hold is not None:
            self.hold.wait(5)
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(("sound", payload))

    def recv(self):
        item = self._down.get()
        if item is None:
            raise ConnectionError("closed")
        return item

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True
        self._down.put(None)

    def texts(self, kind=None):
        return [m for k, m in self.sent if k == "text" and (kind is None or m["type"] == kind)]

    def sound(self):
        return b"".join(p for k, p in self.sent if k == "sound")


def _gained(frames):
    gain = wavs.StreamGain()
    return wavs.ulaw_encode(np.concatenate([gain(f) for f in frames]))


class _LiveFixture:

    def setUp(self):
        self.lines, self.sockets = [], []
        self.rng = np.random.default_rng(11)

    def _live(self, *sockets):
        self.sockets = list(sockets)
        made = iter(self.sockets)

        def connect(url, token, timeout):
            self.assertEqual((url, token), ("wss://shop.example/ws/robot/voice/", "tok"))
            return next(made)
        voice = live.LiveVoice(API, "tok", log=self.lines.append, connect=connect).start()
        self.addCleanup(voice.stop)
        _wait(lambda: voice.ready)
        return voice


class LiveVoiceTests(_LiveFixture, unittest.TestCase):

    def test_where_the_socket_is(self):
        self.assertEqual(live.socket_url("https://a.mousstec.com/api/robot/v1"), "wss://a.mousstec.com/ws/robot/voice/")
        self.assertEqual(live.socket_url("http://192.168.1.5:8000/api/robot/v1/"), "ws://192.168.1.5:8000/ws/robot/voice/")

    def test_the_sentence_goes_up_as_its_said_and_its_answer_comes_back(self):
        sock = _Socket()
        voice = self._live(sock)
        frames = _speech(640, self.rng)
        self.assertTrue(voice.begin(teach=False))
        for f in frames:
            voice.feed(f)
        answer = voice.end(np.concatenate(frames), audio=True, transcript="يا موس عندك فلتر زيت", check=True,
                           confidence=0.5, caps="media", said_ago_ms=900)
        self.assertEqual((answer.reply, answer.wav, answer.timing), (ANSWER["reply"], sock.voice, "hear=5;think=9"))
        self.assertTrue(answer.live and answer.addressed)
        start, end = sock.texts("start")[0], sock.texts("end")[0]
        self.assertEqual(start, {"type": "start", "id": 1, "teach": 0})
        self.assertEqual(sock.sound(), _gained(frames))               # µ-law, brought up as Vosk hears it
        self.assertEqual(end, {"type": "end", "id": 1, "audio": 1, "keep": len(frames) * vad.FRAME,
                               "ptt": "0", "reply_audio": "ulaw", "caps": "media", "said_ago_ms": "900",
                               "check": "1", "confidence": "0.500", "transcript": "يا موس عندك فلتر زيت"})
        self.assertIn("[LIVE] connected: the sound goes up while it's said — the answers come sooner", self.lines)

    def test_its_last_frames_go_up_with_the_end(self):
        sock = _Socket()
        voice = self._live(sock)
        frames = _speech(640, self.rng)
        voice.begin()
        for f in frames[:-3]:
            voice.feed(f)
        voice.end(np.concatenate(frames), audio=True, transcript="x")
        self.assertEqual(sock.sound(), _gained(frames))

    def test_words_only_no_sound_with_them(self):
        sock = _Socket()
        voice = self._live(sock)
        voice.begin()
        voice.feed(_speech(320, self.rng)[0])
        voice.end(np.zeros(512, np.int16), audio=False, transcript="يا موس عندك فلتر زيت", known="trusted")
        end = sock.texts("end")[0]
        self.assertEqual((end["audio"], end["keep"], end["known"]), (0, 0, "trusted"))

    def test_its_name_heard_azure_starts_once(self):
        sock = _Socket()
        voice = self._live(sock)
        voice.begin(teach=False)
        voice.teach()
        voice.teach()
        voice.end(np.zeros(512, np.int16), audio=False, transcript="x")
        self.assertEqual(sock.texts("teach"), [{"type": "teach", "id": 1}])

    def test_dropped_it_is_forgotten_there_too(self):
        sock = _Socket()
        voice = self._live(sock)
        voice.begin()
        voice.feed(_speech(320, self.rng)[0])
        voice.drop()
        _wait(lambda: sock.texts("drop"))
        self.assertEqual(sock.texts("drop"), [{"type": "drop", "id": 1}])
        with self.assertRaises(live.LiveDown):
            voice.end(np.zeros(512, np.int16), audio=True)

    def test_no_socket_no_sentence_on_it_and_said_once(self):
        tries = []

        def connect(url, token, timeout):
            tries.append(1)
            raise ConnectionError("the net is down")
        with mock.patch.object(live, "RETRY_S", (0.01,)):
            voice = live.LiveVoice(API, "tok", log=self.lines.append, connect=connect).start()
            self.addCleanup(voice.stop)
            _wait(lambda: len(tries) >= 3)
        self.assertFalse(voice.begin())
        self.assertEqual(self.lines, ["[LIVE] no live socket (ConnectionError) — the whole recording after "
                                      "each sentence, as before"])

    def test_a_server_not_updated_yet_is_tried_again_rarely(self):
        refused = Exception("Handshake status 404 Not Found")
        refused.status_code = 404
        tries = []

        def connect(url, token, timeout):
            tries.append(1)
            raise refused
        voice = live.LiveVoice(API, "tok", log=self.lines.append, connect=connect).start()
        self.addCleanup(voice.stop)
        _wait(lambda: self.lines)
        time.sleep(0.2)
        self.assertEqual(len(tries), 1)                                # the next try: in 15 minutes
        self.assertIn("HTTP 404: not updated yet, or the token", self.lines[0])

    def test_the_socket_dropped_mid_sentence_nothing_was_answered(self):
        first, second = _Socket(), _Socket()
        with mock.patch.object(live, "RETRY_S", (0.01,)):
            voice = self._live(first, second)
            voice.begin()
            voice.feed(_speech(320, self.rng)[0])
            first.close()                                              # the server restarted
            _wait(lambda: voice.ready and voice._gen == 2)                 # back, on a new socket
        with self.assertRaises(live.LiveDown):
            voice.end(np.zeros(512, np.int16), audio=True, transcript="x")
        self.assertEqual(second.texts("end"), [])                      # half a sentence never answered there

    def test_a_line_slower_than_the_speaking(self):
        sock = _Socket()
        sock.hold = threading.Event()
        voice = self._live(sock)
        with mock.patch.object(live, "BEHIND", 2 * live.PIECE):
            voice.begin()
            for f in _speech(3200, self.rng):
                voice.feed(f)
            self.assertTrue(voice.lagging)
            with self.assertRaises(live.LiveDown):
                voice.end(np.zeros(512, np.int16), audio=True, transcript="x")
        sock.hold.set()
        _wait(lambda: sock.texts("drop"))                              # and forgotten there

    def test_no_answer_in_time_is_the_server_down(self):
        voice = self._live(_Socket(answer=False))
        voice.begin()
        with self.assertRaises(server.ServerDown):
            voice.end(np.zeros(512, np.int16), audio=False, transcript="x", timeout=0.3)

    def test_closed_after_it_was_asked_is_the_server_down(self):
        voice = self._live(_Socket(close_on_end=True))
        voice.begin()
        with self.assertRaises(server.ServerDown):
            voice.end(np.zeros(512, np.int16), audio=False, transcript="x", timeout=3)

    def test_an_older_answer_and_its_voice_are_passed_over(self):
        sock = _Socket(stale=True)
        voice = self._live(sock)
        voice.begin()
        answer = voice.end(np.zeros(512, np.int16), audio=False, transcript="x")
        self.assertEqual((answer.reply, answer.wav), (ANSWER["reply"], sock.voice))

    def test_websocket_client_is_asked_with_the_token_in_a_header(self):
        made = {}

        def create_connection(url, **kwargs):
            made.update(kwargs, url=url)
            return "a socket"
        with mock.patch.dict(sys.modules, {"websocket": SimpleNamespace(create_connection=create_connection)}):
            self.assertEqual(live._connect("wss://a/ws/robot/voice/", "tok", 5), "a socket")
        self.assertEqual(made["url"], "wss://a/ws/robot/voice/")
        self.assertEqual(made["header"], ["X-Robot-Token: tok"])         # never in the URL: logs keep URLs
        self.assertTrue(made["sslopt"]["ca_certs"].endswith(".pem"))      # what requests trusts


class _BrainFixture:

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base=API, token="tok", home=Path(self.tmp.name))
        self.cfg.ears_use, self.cfg.teach_all, self.cfg.counter_port = "always", True, 0
        self.server = mock.Mock()
        self.server.catalog.return_value = dict(CATALOG, heard_fixes=[])
        self.server.speak.return_value = _clip(5, 160)
        self.server.voice.return_value = server.Answer(wav=b"HTTP-WAV", reply="من الـ HTTP", transcript="",
                                                       addressed=True)
        self.speaker = mock.Mock()
        self.lines = []
        self.rng = np.random.default_rng(12)

    def _brain(self, sock, ears=None):
        voice = live.LiveVoice(API, "tok", log=self.lines.append, connect=lambda *a: sock).start()
        self.addCleanup(voice.stop)
        _wait(lambda: voice.ready)
        brain = loop.Brain(self.cfg, ears=ears or _Ears("يا موس عندك فلتر زيت"), server=self.server, mic=None,
                           speaker=self.speaker, log=self.lines.append, live=voice)
        brain.refresh()
        for clip in brain.knowledge.clip_texts():
            brain.bank.put(clip, _clip(100, 160))
        brain.online = True
        brain._check = lambda *a: None
        return brain

    def _say(self, brain, frames):
        """What run() does: the sound up as it comes, then the sentence."""
        brain._live_begin()
        if brain._live_on:
            for f in frames:
                brain.live.feed(f)
        try:
            brain.handle(vad.Utterance(samples=np.concatenate(frames), speech_ms=len(frames) * vad.FRAME_MS,
                                       quietest=40, loudest=2500, cut=False))
        finally:
            brain._live_drop()


class BrainLiveTests(_BrainFixture, unittest.TestCase):
    """The robot's loop with the socket: what goes up, when, and the answer."""

    def test_the_teachers_question_goes_over_the_socket(self):
        sock = _Socket()
        brain = self._brain(sock)
        self._say(brain, _speech(960, self.rng))
        self.server.voice.assert_not_called()                          # no second trip with the recording
        self.speaker.play_wav.assert_called_once_with(sock.voice)
        end = sock.texts("end")[0]
        self.assertEqual((end["audio"], end["check"], end["transcript"]), (1, "1", "يا موس عندك فلتر زيت"))
        spoke = next(line for line in self.lines if line.startswith("[VOICE] spoke"))
        self.assertIn("(live; up 0.0 s, answer 0.0 s, voice 0 KB in 0.0 s; hear=5;think=9)", spoke)   # where the time went

    def test_the_socket_gone_the_whole_recording_goes_over_http(self):
        sock = _Socket()
        brain = self._brain(sock)
        frames = _speech(960, self.rng)
        brain._live_begin()
        for f in frames:
            brain.live.feed(f)
        sock.close()
        _wait(lambda: not brain.live.ready)
        brain.handle(vad.Utterance(samples=np.concatenate(frames), speech_ms=960, quietest=40, loudest=2500,
                                   cut=False))
        self.assertIsNotNone(self.server.voice.call_args.kwargs["wav"])  # /voice/, with the recording
        self.speaker.play_wav.assert_called_once_with(b"HTTP-WAV")
        self.assertIn("[LIVE] the socket dropped: the whole recording, as before", self.lines)

    def test_answered_here_the_sound_up_is_forgotten(self):
        sock = _Socket()
        brain = self._brain(sock)
        self.cfg.teach = False                                         # no teacher: the catalog answers here
        brain._live_begin()
        self.assertFalse(brain._live_on)                               # and no sound goes up at all
        self.cfg.teach, self.cfg.teach_all = True, False               # only what it isn't sure of
        self._say(brain, _speech(960, self.rng))                       # sure, and understood: here
        _wait(lambda: sock.texts("drop"))
        self.assertEqual([m["type"] for m in sock.texts()], ["start", "drop"])
        self.assertEqual(sock.texts("start")[0]["teach"], 0)

    def test_azure_starts_on_a_conversation_once_it_sounds_like_words(self):
        sock = _Socket()
        brain = self._brain(sock)
        brain._live_begin()                                            # «يا موس …»: the name not heard yet
        brain._live_teach(None, GAPPED)
        brain._live_drop()
        brain._talking_until = time.monotonic() + 10                   # a conversation's on: no name needed
        brain._live_begin()
        heard = SimpleNamespace(partial="عندك كابوس")                   # words it can't answer by itself
        brain._live_teach(heard, STEADY)                               # no gap yet: a fan, so far
        brain._live_teach(heard, GAPPED)                               # the gaps words have
        brain._live_teach(SimpleNamespace(partial="عندك كابوس احمر"), GAPPED)
        brain._live_drop()
        _wait(lambda: len(sock.texts("drop")) == 2)
        self.assertEqual([m["teach"] for m in sock.texts("start")], [0, 0])   # never at its first frame
        self.assertEqual(sock.texts("teach"), [{"type": "teach", "id": 2}])
        brain.online = False                                           # the net down: nothing goes up
        brain._live_begin()
        self.assertFalse(brain._live_on)

    def test_the_server_listening_azure_starts_at_its_first_gap(self):
        sock = _Socket()
        ears = _Ears("x")
        ears.ready = False                                             # no ears here: the server listens
        brain = self._brain(sock, ears)
        brain._live_begin()
        brain._live_teach(None, STEADY)
        self.assertFalse(brain.live.teaching)                          # a radio's steady sound: no minutes
        brain._live_teach(None, GAPPED)
        _wait(lambda: sock.texts("teach"))

    def test_its_name_while_its_said_starts_the_teacher(self):
        sock = _Socket()
        brain = self._brain(sock)
        brain._live_begin()
        brain._live_teach(SimpleNamespace(partial="عندك فلتر"), GAPPED)          # chatter so far
        brain._live_teach(SimpleNamespace(partial="يا موس عندك"), GAPPED)        # said to it
        brain._live_teach(SimpleNamespace(partial="يا موس عندك فلتر"), GAPPED)
        _wait(lambda: sock.texts("teach"))
        brain._live_drop()
        _wait(lambda: sock.texts("drop"))
        self.assertEqual(len(sock.texts("teach")), 1)

    def test_a_fans_steady_sound_never_reaches_azure(self):
        sock = _Socket()
        ears = _Ears("x")
        ears.ready = False                                             # the server hears everything here
        rng = self.rng
        # Never a quiet frame for longer than a sentence may last (12 s): cut there, and never sent.
        fan = [(rng.standard_normal(vad.FRAME) * 2500).astype(np.int16) for _ in range(400)]
        frames = _frames(40, 1500, rng) + fan

        class Mic:
            name = "test mic"

            def frames(self):
                yield from frames
                raise KeyboardInterrupt

            def drain(self):
                pass
        brain = self._brain(sock, ears)
        brain.mic = Mic()
        brain.server.heartbeat.return_value = 200
        brain.server.commands.return_value = []
        with self.assertRaises(KeyboardInterrupt):
            brain.run()
        _wait(lambda: sock.texts("drop"))
        self.assertEqual(sock.texts("teach"), [])                      # sent up, and forgotten: no minutes
        self.assertTrue(any("never went quiet" in line for line in self.lines), self.lines)

    def test_the_loop_sends_the_sound_while_its_said(self):
        sock = _Socket()
        frames = _frames(40, 1500, self.rng) + _speech(1200, self.rng) + _frames(40, 1500, self.rng)

        class Mic:
            name = "test mic"

            def frames(self):
                yield from frames
                raise KeyboardInterrupt

            def drain(self):
                pass
        brain = self._brain(sock)
        brain.mic = Mic()
        brain.server.heartbeat.return_value = 200
        brain.server.commands.return_value = []
        with self.assertRaises(KeyboardInterrupt):
            brain.run()
        self.server.voice.assert_not_called()
        self.assertGreaterEqual(len(sock.sound()), 1200 // vad.FRAME_MS * vad.FRAME)   # the sentence, as said
        self.assertLessEqual(sock.texts("end")[0]["keep"], len(sock.sound()))   # the pause after it: not kept
        self.speaker.play_wav.assert_called_once_with(sock.voice)


class DoctorLiveTests(unittest.TestCase):

    def test_the_doctor_says_whether_the_sound_goes_up_as_its_said(self):
        lines, cfg = [], config.Config(api_base=API, token="tok")
        doctor._live(cfg, lambda line: lines.append("good " + line), lambda line: lines.append("tip " + line),
                     connect=lambda *a: _Socket())
        self.assertEqual(lines, ["good live sound: the sound goes up while it's said — answers come sooner"])
        refused = Exception("Handshake status 404")
        refused.status_code = 404

        def old_server(*a):
            raise refused
        lines.clear()
        doctor._live(cfg, lines.append, lines.append, connect=old_server)
        self.assertIn("HTTP 404", lines[0])
        cfg.live = False
        lines.clear()
        doctor._live(cfg, lines.append, lines.append)
        self.assertIn("[server] live = no", lines[0])


class RoundTwentyThreeBrainTests(_BrainFixture, unittest.TestCase):
    """What the shop's log showed (brain-0.9.6)."""

    def test_a_cough_never_reaches_azure(self):
        sock = _Socket()
        ears = _Ears("x")
        ears.ready = False                                             # the server hears everything here
        brain = self._brain(sock, ears)
        brain._live_begin()
        brain._live_teach(None, SimpleNamespace(gapped=True, said_ms=250))   # a cough: gaps at once, short
        self.assertFalse(brain.live.teaching)
        brain._live_teach(None, GAPPED)
        self.assertTrue(brain.live.teaching)

    def test_a_sentence_it_knows_spends_no_minutes(self):
        sock = _Socket()
        brain = self._brain(sock)
        brain.knowledge.trusted_words = frozenset({"عندك", "فلتر", "زيت"})
        brain.knowledge.heard_sentences = {"عندك سلطه الزيت": "عندك فلتر زيت"}
        brain._live_begin()
        for partial in ("يا موس", "يا موس عندك فلتر", "يا موس عندك سلطة"):      # all could be answered here
            brain._live_teach(SimpleNamespace(partial=partial), GAPPED)
        self.assertFalse(brain.live.teaching)
        brain._live_teach(SimpleNamespace(partial="يا موس عندك كابوس"), GAPPED)   # a word it doesn't know
        self.assertTrue(brain.live.teaching)

    def test_the_hold_on_is_cut_short_when_the_answer_comes(self):
        sock = _Socket()
        brain = self._brain(sock)
        brain.bank.put(loop.HOLD_ON, _clip(100, 1600))
        stopped = threading.Event()
        self.speaker.play.side_effect = lambda *a: stopped.wait(5)    # «ثانية واحدة، بشوفلك», still talking
        self.speaker.stop.side_effect = stopped.set
        slow = threading.Event()
        brain._voice = lambda **kw: (slow.wait(0.4), server.Answer(wav=None, reply="x", transcript="",
                                                                   addressed=True))[1]
        with mock.patch.object(loop, "HOLD_ON_S", 0.1):
            started = time.monotonic()
            brain._ask(transcript="x")
        self.assertTrue(stopped.is_set())                              # cut, not said to its end
        self.assertLess(time.monotonic() - started, 2)

    def test_the_sockets_answer_never_waits_on_an_http_request(self):
        sock = _Socket()
        brain = self._brain(sock)
        brain._net.acquire()                                           # the server checking an answer given here
        self.addCleanup(brain._net.release)
        frames = _speech(960, self.rng)
        brain._live_begin()
        for f in frames:
            brain.live.feed(f)
        brain._said_samples = np.concatenate(frames)
        answer = brain._voice(transcript="يا موس عندك فلتر زيت", wav=b"RIFF")
        self.assertTrue(answer.live)

    def test_it_tells_the_server_a_conversation_is_on(self):
        sock = _Socket()
        brain = self._brain(sock, _Ears("عندك كابوس"))                 # no name: a follow-up
        brain._talking_until = time.monotonic() + 30                   # it spoke a moment ago, here
        self._say(brain, _speech(960, self.rng))
        self.assertEqual(sock.texts("end")[0]["talking"], "1")

    def test_its_name_said_last_is_its_name(self):
        from robot.brain import wakeheard
        self.assertEqual(wakeheard.as_named("الموظفين يمس", [["يمس"]], "موس"), (True, "يا موس الموظفين"))
        self.assertEqual(wakeheard.as_named("يمس عندك", [["يمس"]], "موس"), (True, "يا موس عندك"))
        self.assertEqual(wakeheard.as_named("يمس", [["يمس"]], "موس"), (True, "يا موس"))
        self.assertEqual(wakeheard.as_named("عندك فلتر", [["يمس"]], "موس"), (False, "عندك فلتر"))


class LiveSocketRoundTwentyThreeTests(_LiveFixture, unittest.TestCase):

    def test_a_server_restarting_is_tried_again_soon(self):
        tries = []

        def connect(url, token, timeout):
            tries.append(1)
            if len(tries) == 1:
                down = Exception("Handshake status 502 Bad Gateway")
                down.status_code = 502
                raise down
            return _Socket()
        with mock.patch.object(live, "RETRY_S", (0.05,)):
            voice = live.LiveVoice(API, "tok", log=self.lines.append, connect=connect).start()
            self.addCleanup(voice.stop)
            _wait(lambda: voice.ready, timeout=3)                      # not 15 minutes later
        self.assertEqual(len(tries), 2)

    def test_closed_before_it_was_asked_goes_over_http_at_once(self):
        sock = _Socket()
        voice = self._live(sock)
        voice.begin()
        sock.closed = True                                             # its `end` can't go out…
        voice._replies.put(live._CLOSED)                               # …and the reader saw it close first
        started = time.monotonic()
        with self.assertRaises(live.LiveDown):
            voice.end(np.zeros(512, np.int16), audio=False, transcript="x")
        self.assertLess(time.monotonic() - started, 3)                 # not its whole 15 s

    def test_why_the_socket_dropped_is_said(self):
        sock = _Socket()
        with mock.patch.object(live, "RETRY_S", (5,)):
            self._live(sock)
            sock.close()
            _wait(lambda: any("dropped" in line for line in self.lines))
        self.assertIn("[LIVE] the live socket dropped (ConnectionError)", " ".join(self.lines))
