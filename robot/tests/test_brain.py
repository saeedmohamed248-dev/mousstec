"""«مخ موس» (robot/brain/): the robot on a real computer — hears on its own,
asks the ERP with words, says the answer.

No mic, speaker or Whisper here: the audio math, the VAD, the hint, the
filter for Whisper's inventions, the API client and the loop are checked
with stand-ins. The WAV/µ-law side is checked against the server's own
robot/audio.py — both ends must agree on every sample.
"""

import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np

from robot import audio as server_audio
from robot.brain import config, ears, loop, server, vad, wavs


class WavTests(unittest.TestCase):

    def test_ulaw_decodes_like_the_server(self):
        every = bytes(range(256))
        expected = np.frombuffer(server_audio.ulaw_decode(every), dtype="<i2")
        self.assertTrue(np.array_equal(wavs.ulaw_decode(every), expected))

    def test_the_servers_wavs_are_read_back(self):
        pcm = np.array([0, 1000, -1000, 32767, -32768], dtype="<i2").tobytes()
        tag, rate, samples = wavs.parse(server_audio.make_wav(pcm))
        self.assertEqual((tag, rate, samples.tolist()), (wavs.PCM, 16000, [0, 1000, -1000, 32767, -32768]))
        tag, rate, samples = wavs.parse(server_audio.make_wav(server_audio.ulaw_encode(pcm),
                                                              codec=server_audio.ULAW))
        self.assertEqual(tag, wavs.ULAW)
        self.assertEqual(samples[1], wavs.ulaw_decode(server_audio.ulaw_encode(pcm))[1])

    def test_own_wav_is_what_the_server_reads(self):
        samples = np.arange(-50, 50, dtype=np.int16)
        tag, rate, body = server_audio.wav_parts(wavs.pcm_wav(samples))
        self.assertEqual((tag, rate), (1, 16000))
        self.assertEqual(np.frombuffer(body, dtype="<i2").tolist(), samples.tolist())

    def test_a_list_chunk_and_an_unknown_data_size(self):
        pcm = np.array([5, 6, 7], dtype="<i2").tobytes()
        fmt = struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        data = (b"RIFF\0\0\0\0WAVE" + b"LIST" + struct.pack("<I", 4) + b"INFO"
                + b"fmt " + fmt + b"data" + struct.pack("<I", 0xFFFFFFFF) + pcm)
        self.assertEqual(wavs.parse(data)[2].tolist(), [5, 6, 7])

    def test_not_a_wav(self):
        self.assertIsNone(wavs.parse(b"ID3 mp3 bytes"))

    def test_48k_mic_to_16k_keeps_the_tone(self):
        t = np.arange(48000) / 48000
        x = (10000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        y = wavs.resample(x, 48000, 16000)
        self.assertEqual(len(y), 16000)
        peak_hz = np.argmax(np.abs(np.fft.rfft(y))) * 16000 / len(y)
        self.assertAlmostEqual(peak_hz, 440, delta=2)

    def test_44k1_to_16k_length(self):
        self.assertEqual(len(wavs.resample(np.zeros(44100), 44100, 16000)), 16000)


def _frames(level, ms, rng):
    """Frames of noise at about `level` RMS."""
    n = max(1, ms // vad.FRAME_MS)
    return [(rng.standard_normal(vad.FRAME) * level).astype(np.int16) for _ in range(n)]


def _speech(ms, rng, loud=2500, gap_every=5):
    """Syllables with short gaps — the way words come out."""
    frames = []
    for i in range(max(1, ms // vad.FRAME_MS)):
        level = 60 if i % gap_every == gap_every - 1 else loud
        frames.append((rng.standard_normal(vad.FRAME) * level).astype(np.int16))
    return frames


class SegmenterTests(unittest.TestCase):

    def setUp(self):
        self.rng = np.random.default_rng(7)

    def _run(self, seg, frames):
        return [u for u in (seg.feed(f) for f in frames) if u is not None]

    def test_one_sentence_between_quiet(self):
        seg = vad.Segmenter()
        frames = _frames(40, 2000, self.rng) + _speech(1600, self.rng) + _frames(40, 1500, self.rng)
        [utt] = self._run(seg, frames)
        self.assertAlmostEqual(utt.speech_ms, 1600, delta=100)
        self.assertFalse(utt.cut)
        self.assertFalse(utt.never_quiet)
        # the lead-in (300 ms) and 150 ms of the pause are kept, not the whole pause
        self.assertAlmostEqual(len(utt.samples) / 16000 * 1000, 300 + 1600 + 150, delta=100)

    def test_a_loud_shop_sets_the_bar(self):
        # Louder than the first guess: the room's level is learned while
        # recording too, so the shop's hum stops "speaking" after a few
        # seconds — and what it caught meanwhile is marked as noise.
        seg = vad.Segmenter()
        utts = self._run(seg, _frames(400, 15000, self.rng))
        self.assertLessEqual(len(utts), 1)
        self.assertTrue(all(u.never_quiet for u in utts))
        self.assertGreater(seg.start_level(), 1000)
        self.assertEqual(self._run(seg, _frames(400, 3000, self.rng)), [])
        [utt] = self._run(seg, _speech(1500, self.rng, loud=6000) + _frames(400, 1500, self.rng))
        self.assertFalse(utt.never_quiet)               # someone talking over the hum

    def test_the_bar_comes_back_down(self):
        seg = vad.Segmenter()
        self._run(seg, _frames(400, 15000, self.rng))
        self._run(seg, _frames(40, 2000, self.rng))     # the compressor stopped
        self.assertEqual(seg.start_level(), 150)

    def test_a_click_isnt_a_sentence(self):
        seg = vad.Segmenter()
        frames = _frames(40, 1000, self.rng) + _speech(100, self.rng) + _frames(40, 1500, self.rng)
        self.assertEqual(self._run(seg, frames), [])

    def test_endless_noise_is_cut_and_called_noise(self):
        seg = vad.Segmenter(max_ms=4000)
        frames = _frames(30, 1000, self.rng) + _frames(3000, 9000, self.rng)
        utts = self._run(seg, frames)
        self.assertTrue(utts)
        self.assertTrue(utts[0].cut)
        self.assertTrue(utts[0].never_quiet)

    def test_reset_drops_what_was_in_progress(self):
        seg = vad.Segmenter()
        self._run(seg, _frames(40, 1000, self.rng) + _speech(800, self.rng))
        seg.reset()
        self.assertEqual(self._run(seg, _frames(40, 1500, self.rng)), [])


class HintAndFilterTests(unittest.TestCase):

    CATALOG = {"parts": [
        {"name": "فلتر هواء لانسر 2015 أصلي ياباني", "stock": 0},
        {"name": "تيل فرامل أمامي كورولا", "stock": 9},
        {"name": "تيل فرامل أمامي كورولا", "stock": 3},
        {"name": "12345", "stock": 50},
        {"name": "Oil filter Corolla", "stock": 4},
    ]}

    def test_name_first_stock_first_no_repeats(self):
        text = ears.hints(self.CATALOG, "زيكو")
        self.assertTrue(text.startswith("يا زيكو"))
        self.assertLess(text.index("تيل فرامل"), text.index("فلتر هواء"))
        self.assertEqual(text.count("تيل فرامل أمامي كورولا"), 1)
        self.assertNotIn("12345", text)
        self.assertIn("فلتر هواء لانسر 2015", text)          # the first four words
        self.assertNotIn("أصلي ياباني", text)

    def test_it_fits_what_whisper_reads(self):
        many = {"parts": [{"name": f"قطعة رقم {i} للعربية", "stock": 1} for i in range(500)]}
        self.assertLessEqual(len(ears.hints(many)), ears.PROMPT_CHARS)

    def test_whispers_inventions_are_dropped(self):
        for invented in ("ترجمة نانسي قنقر", "  شكراً للمشاهدة.  ", "...", "؟", "Thanks for watching!"):
            self.assertEqual(ears.clean(invented), "", invented)

    def test_real_questions_stay(self):
        self.assertEqual(ears.clean(" يا موس  عندك فلتر زيت؟ "), "يا موس عندك فلتر زيت؟")
        self.assertEqual(ears.clean("thanks, how much is the oil filter for the corolla?"),
                         "thanks, how much is the oil filter for the corolla?")

    def test_no_whisper_installed(self):
        e = ears.Ears("small")
        with mock.patch.dict("sys.modules", {"faster_whisper": None}):
            self.assertFalse(e.load())
        self.assertFalse(e.ready)
        self.assertIn("faster-whisper", e.error)
        self.assertEqual(e.hear(np.zeros(100, dtype=np.int16)), "")

    def test_the_hint_is_given_once(self):
        seen = []
        e = ears.Ears("small")
        e._model = SimpleNamespace(transcribe=lambda audio, **kw: (seen.append(kw), (iter([]), None))[1])
        e._hotwords = True
        e.hear(np.zeros(1600, dtype=np.int16), "يا موس")
        e._hotwords = False
        e.hear(np.zeros(1600, dtype=np.int16), "يا موس")
        self.assertEqual(seen[0].get("hotwords"), "يا موس")
        self.assertNotIn("initial_prompt", seen[0])
        self.assertEqual(seen[1].get("initial_prompt"), "يا موس")
        self.assertNotIn("hotwords", seen[1])

    def test_low_confidence_segments_are_skipped(self):
        e = ears.Ears("small")
        seg = lambda text, nsp, lp: SimpleNamespace(text=text, no_speech_prob=nsp, avg_logprob=lp)
        e._model = SimpleNamespace(transcribe=lambda audio, **kw: (
            iter([seg(" يا موس", 0.1, -0.3), seg(" بلا بلا", 0.9, -1.5)]), None))
        self.assertEqual(e.hear(np.zeros(1600, dtype=np.int16), "hint"), "يا موس")


class _Resp:
    def __init__(self, status=200, content=b"", headers=None, data=None):
        self.status_code, self.content = status, content
        self.headers = headers or {"Content-Type": "application/json"}
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class ServerTests(unittest.TestCase):

    def _server(self, *responses):
        session = mock.Mock()
        session.headers = {}
        session.post.side_effect = list(responses)
        return server.Server("https://shop.example/api/robot/v1/", "tok", session=session), session

    def test_words_go_up_the_voice_comes_back(self):
        wav = wavs.pcm_wav(np.zeros(10, dtype=np.int16))
        srv, session = self._server(_Resp(200, wav, {
            "Content-Type": "audio/wav", "X-Robot-Addressed": "1",
            "X-Robot-Transcript": "%D9%8A%D8%A7%20%D9%85%D9%88%D8%B3",
            "X-Robot-Reply": "%D8%A3%D9%8A%D9%88%D9%87", "X-Robot-Timing": "hear=0;think=9;voice=40"}))
        answer = srv.voice(transcript="يا موس")
        self.assertEqual((answer.wav, answer.transcript, answer.reply, answer.addressed),
                         (wav, "يا موس", "أيوه", True))
        self.assertEqual(answer.timing, "hear=0;think=9;voice=40")
        url = session.post.call_args.args[0]
        sent = session.post.call_args.kwargs["data"]
        self.assertEqual(url, "https://shop.example/api/robot/v1/voice/")
        self.assertEqual((sent["transcript"], sent["reply_audio"], sent["ptt"]), ("يا موس", "pcm", "0"))
        self.assertIsNone(session.post.call_args.kwargs["files"])
        self.assertEqual(session.headers["X-Robot-Token"], "tok")

    def test_not_for_the_robot(self):
        srv, _ = self._server(_Resp(data={"intent": "ignored", "reply": "", "addressed": False}))
        answer = srv.voice(transcript="هات الشاي يا محمد")
        self.assertFalse(answer.addressed)
        self.assertIsNone(answer.wav)

    def test_without_whisper_the_recording_goes_up(self):
        srv, session = self._server(_Resp(data={"reason": "no_transcript", "addressed": False}))
        answer = srv.voice(wav=b"RIFF")
        self.assertEqual(answer.reason, "no_transcript")
        self.assertIn("audio", session.post.call_args.kwargs["files"])
        self.assertNotIn("transcript", session.post.call_args.kwargs["data"])

    def test_no_net_and_wrong_token(self):
        import requests
        srv, _ = self._server(requests.ConnectionError("down"), _Resp(401), _Resp(502))
        with self.assertRaises(server.ServerDown):
            srv.voice(transcript="x")
        with self.assertRaises(server.Unauthorized):
            srv.heartbeat("brain")
        with self.assertRaises(server.ServerDown):
            srv.catalog()

    def test_speak_asks_for_a_wav(self):
        srv, session = self._server(_Resp(200, b"RIFF", {"Content-Type": "audio/wav"}), _Resp(204))
        self.assertEqual(srv.speak("أهلاً"), b"RIFF")
        self.assertEqual(session.post.call_args.kwargs["data"]["format"], "wav")
        self.assertIsNone(srv.speak("أهلاً"))


class ConfigTests(unittest.TestCase):

    def test_ini_then_environment(self):
        with TemporaryDirectory() as tmp:
            ini = Path(tmp) / "brain.ini"
            ini.write_text("[server]\napi_base = https://a/api/robot/v1  ; the shop\ntoken = t1\n"
                           "[ears]\nmodel = medium\n[robot]\nname = زيكو\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"MOUSS_TOKEN": "t2"}, clear=False):
                cfg = config.load(ini)
        self.assertEqual((cfg.api_base, cfg.token, cfg.model, cfg.name),
                         ("https://a/api/robot/v1", "t2", "medium", "زيكو"))
        self.assertEqual(cfg.missing(), [])

    def test_missing_settings_are_named(self):
        with TemporaryDirectory() as tmp:
            cfg = config.load(Path(tmp) / "none.ini")
        self.assertEqual(cfg.missing(), ["api_base", "token"])


class _FakeEars:
    model_name = "small"

    def __init__(self, text="يا موس عندك فلتر زيت؟", ready=True):
        self.text, self.ready, self.error = text, ready, ""
        self.prompts = []

    def timed(self, samples, prompt=""):
        self.prompts.append(prompt)
        return self.text, 0.4


class BrainTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.server = mock.Mock()
        self.speaker = mock.Mock()
        self.lines = []

    def _brain(self, ears_=None):
        return loop.Brain(self.cfg, ears=ears_ or _FakeEars(), server=self.server, mic=None,
                          speaker=self.speaker, log=self.lines.append)

    def _utt(self):
        return vad.Utterance(samples=np.zeros(16000, dtype=np.int16), speech_ms=1000,
                             quietest=50, loudest=3000, cut=False)

    def test_heard_here_answered_there_spoken(self):
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه، عندي", transcript="",
                                                       addressed=True, timing="hear=0;think=8")
        self._brain().handle(self._utt())
        self.server.voice.assert_called_once_with(transcript="يا موس عندك فلتر زيت؟")
        self.speaker.play_wav.assert_called_once_with(b"WAV")
        self.assertTrue(any("heard in 0.4 s" in line for line in self.lines))
        self.assertTrue(any(line.startswith("[VOICE] spoke") for line in self.lines))

    def test_words_only_fetches_the_voice(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="أيوه", transcript="", addressed=True)
        self.server.speak.return_value = b"WAV2"
        self._brain().handle(self._utt())
        self.server.speak.assert_called_once_with("أيوه")
        self.speaker.play_wav.assert_called_once_with(b"WAV2")

    def test_noise_isnt_sent_to_the_ears(self):
        ears_ = _FakeEars()
        noise = vad.Utterance(samples=np.zeros(16000, dtype=np.int16), speech_ms=12000,
                              quietest=2800, loudest=3100, cut=True)
        self._brain(ears_).handle(noise)
        self.assertEqual(ears_.prompts, [])
        self.server.voice.assert_not_called()

    def test_nothing_understood_sends_nothing(self):
        self._brain(_FakeEars(text="")).handle(self._utt())
        self.server.voice.assert_not_called()
        self.speaker.play_wav.assert_not_called()

    def test_not_for_the_robot_stays_quiet(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False)
        self._brain().handle(self._utt())
        self.speaker.play_wav.assert_not_called()

    def test_no_whisper_the_server_listens(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False,
                                                       reason="no_transcript")
        self._brain(_FakeEars(ready=False)).handle(self._utt())
        sent = self.server.voice.call_args.kwargs["wav"]
        self.assertEqual(wavs.parse(sent)[1], 16000)

    def test_offline_says_so_in_its_own_voice_not_every_time(self):
        voice = Path(self.tmp.name) / "voice"
        voice.mkdir()
        (voice / "offline.wav").write_bytes(b"OFFLINE")
        self.server.voice.side_effect = server.ServerDown("no route")
        brain = self._brain()
        brain.handle(self._utt())
        brain.handle(self._utt())
        self.speaker.play_wav.assert_called_once_with(b"OFFLINE")
        self.assertFalse(brain.online)

    def test_the_catalog_becomes_the_hint(self):
        self.server.catalog.return_value = {"parts": [{"name": "مساعدين خلفي فيرنا", "stock": 2}],
                                            "part_count": 1}
        ears_ = _FakeEars()
        brain = self._brain(ears_)
        brain.refresh()
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False)
        brain.handle(self._utt())
        self.assertIn("مساعدين خلفي فيرنا", ears_.prompts[-1])

    def test_offline_phrase_is_fetched_once(self):
        self.server.speak.return_value = b"RIFF-offline"
        brain = self._brain()
        brain.keep_offline_phrase()
        brain.keep_offline_phrase()
        self.server.speak.assert_called_once()
        self.assertEqual((Path(self.tmp.name) / "voice" / "offline.wav").read_bytes(), b"RIFF-offline")

    def test_heartbeat_says_only_changes(self):
        self.server.heartbeat.side_effect = [200, 200, server.ServerDown("x")]
        brain = self._brain()
        for _ in range(3):
            brain.beat()
        self.assertEqual([line for line in self.lines if line.startswith("[HB]")],
                         ["[HB] server reachable (online)", "[HB] server unreachable (offline)"])

    def test_the_loop_listens_answers_and_forgets_its_own_voice(self):
        rng = np.random.default_rng(3)
        # two sentences: the first one's answer fails, the robot goes on to the second
        frames = (_frames(40, 1500, rng) + _speech(1200, rng) + _frames(40, 1500, rng)
                  + _speech(1200, rng) + _frames(40, 1500, rng))

        class Mic:
            name = "test mic"
            drained = 0

            def frames(self):
                yield from frames
                raise KeyboardInterrupt          # the owner pressed Ctrl+C

            def drain(self):
                Mic.drained += 1

        self.server.voice.side_effect = [RuntimeError("whisper hiccup"),
                                         server.Answer(wav=b"WAV", reply="أيوه", transcript="",
                                                       addressed=True)]
        self.server.heartbeat.return_value = 200
        self.server.catalog.return_value = {}
        self.server.speak.return_value = None
        brain = loop.Brain(self.cfg, ears=_FakeEars(), server=self.server, mic=Mic(),
                           speaker=self.speaker, log=self.lines.append)
        with self.assertRaises(KeyboardInterrupt):
            brain.run()
        self.speaker.play_wav.assert_called_once_with(b"WAV")
        self.assertEqual(Mic.drained, 2)
        self.assertTrue(any("whisper hiccup" in line for line in self.lines))
        self.assertTrue(brain._stop.is_set())

    def test_a_surprise_doesnt_stop_the_heartbeat(self):
        brain = self._brain()
        self.server.heartbeat.side_effect = [ValueError("bad json"), 200]
        waits = []

        def wait(seconds):
            waits.append(seconds)
            return len(waits) >= 2 and brain._stop.set()

        with mock.patch.object(brain._stop, "wait", side_effect=wait):
            brain._background()
        self.assertEqual(self.server.heartbeat.call_count, 2)
        self.assertTrue(any("ValueError" in line for line in self.lines))
