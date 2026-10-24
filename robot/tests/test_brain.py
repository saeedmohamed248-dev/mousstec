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

    def test_ulaw_up_is_the_servers_codec_and_half_the_bytes(self):
        samples = np.array([0, 1, -1, 100, -100, 5000, -5000, 32767, -32768, 12345], dtype=np.int16)
        self.assertEqual(wavs.ulaw_encode(samples), server_audio.ulaw_encode(samples.astype("<i2").tobytes()))
        wav = wavs.ulaw_wav(samples)
        tag, rate, body = server_audio.wav_parts(wav)
        self.assertEqual((tag, rate, len(body)), (7, 16000, len(samples)))
        pcm = server_audio.as_pcm_wav(wav)                       # what speech-to-text gets
        self.assertEqual(server_audio.wav_parts(pcm)[0], 1)
        long = np.zeros(16000, dtype=np.int16)
        self.assertLess(len(wavs.ulaw_wav(long)), len(wavs.pcm_wav(long)) * 0.51)

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

    def test_a_quiet_mic_is_brought_up_for_the_ears(self):
        # 🐛 [FIX]: the shop's mic peaked at a few hundred, and neither Vosk nor Whisper heard a word.
        t = np.arange(16000) / 16000
        quiet = (1500 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
        up = wavs.louder(quiet)
        self.assertAlmostEqual(int(np.abs(up).max()), wavs.LOUD_PEAK, delta=200)
        self.assertEqual(wavs.gain_for(quiet), wavs.LOUD_PEAK / np.percentile(np.abs(quiet), 99.9))
        self.assertEqual(up.dtype, np.int16)

    def test_loud_enough_silence_and_a_click_are_left_alone(self):
        rng = np.random.default_rng(3)
        loud = (rng.standard_normal(16000) * 8000).clip(-32768, 32767).astype(np.int16)
        self.assertIs(wavs.louder(loud), loud)                       # already loud: as it is
        silence = np.zeros(8000, dtype=np.int16)
        self.assertEqual(wavs.louder(silence).tolist(), silence.tolist())
        hiss = (rng.standard_normal(16000) * 5).astype(np.int16)    # the room, nobody talking
        self.assertLessEqual(wavs.gain_for(hiss), wavs.MOST_GAIN)
        clicked = (rng.standard_normal(16000) * 300).astype(np.int16)
        clicked[100] = 30000                                         # one click doesn't set the gain
        self.assertGreater(wavs.gain_for(clicked), 10)
        self.assertEqual(int(wavs.louder(clicked).max()), 32767)     # clipped, never wrapped round


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

    def test_a_dll_that_wont_load_leaves_it_to_the_server(self):
        # Windows without the C++ runtime: ctranslate2.dll raises FileNotFoundError on import.
        class Broken:
            def __getattr__(self, name):
                raise FileNotFoundError("Could not find module 'ctranslate2.dll' (or one of its dependencies)")
        e = ears.Ears("small")
        with mock.patch.dict("sys.modules", {"faster_whisper": Broken()}), \
                mock.patch.object(ears.os, "name", "nt"):
            self.assertFalse(e.load())
        self.assertFalse(e.ready)
        self.assertIn("ctranslate2.dll", e.error)
        self.assertIn(ears.VC_REDIST, e.error)

    def test_turned_off_never_loads_it(self):
        for off in ("off", "Server", " none "):
            e = ears.Ears(off)
            with mock.patch.dict("sys.modules", {"faster_whisper": None}):
                self.assertFalse(e.load())
            self.assertIn("model = off", e.error)

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

    def test_once_at_temperature_zero_and_no_longer_than_a_question(self):
        seen = []

        def transcribe(audio, language=None, beam_size=1, temperature=None, condition_on_previous_text=True,
                       without_timestamps=False, vad_filter=False, hotwords=None, max_new_tokens=None):
            seen.append({"temperature": temperature, "max_new_tokens": max_new_tokens})
            return iter([]), None
        e = ears.Ears("small")
        e._model = SimpleNamespace(transcribe=transcribe)
        e._capped = True
        e.hear(np.zeros(1600, dtype=np.int16))
        self.assertEqual(seen, [{"temperature": 0.0, "max_new_tokens": ears.MAX_TOKENS}])

    def test_whisper_gets_a_quiet_sentence_louder(self):
        heard = []
        e = ears.Ears("small")
        e._model = SimpleNamespace(transcribe=lambda audio, **kw: (heard.append(audio), (iter([]), None))[1])
        e.hear(np.full(1600, 1500, dtype=np.int16) * np.array([1, -1] * 800, dtype=np.int16))
        self.assertAlmostEqual(float(np.abs(heard[0]).max()), wavs.LOUD_PEAK / 32768, places=2)

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
        self.assertEqual((sent["transcript"], sent["reply_audio"], sent["ptt"]), ("يا موس", "ulaw", "0"))
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
                           "[ears]\nmodel = medium\nuse = Offline\n[robot]\nname = زيكو\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"MOUSS_TOKEN": "t2"}, clear=False):
                cfg = config.load(ini)
        self.assertEqual((cfg.api_base, cfg.token, cfg.model, cfg.name),
                         ("https://a/api/robot/v1", "t2", "medium", "زيكو"))
        self.assertEqual(cfg.missing(), [])
        self.assertEqual(cfg.ears_use, "offline")

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

    def release(self, why):
        self.ready, self.error = False, why


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
        self.server.voice.assert_called_once_with(transcript="يا موس عندك فلتر زيت؟", caps="",
                                                  said_ago_ms=mock.ANY)
        self.speaker.play_wav.assert_called_once_with(b"WAV")
        self.assertTrue(any("heard in 0.4 s" in line for line in self.lines))
        self.assertTrue(any(line.startswith("[VOICE] spoke") for line in self.lines))

    def test_it_says_how_long_ago_they_started_talking(self):
        # 🐛 [FIX]: Whisper took 16 s here, and «عندك فلتر زيت؟» after «أيوه، تحت أمرك»
        # reached the server after its 20 s window: it's checked at when it was said now.
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه", transcript="", addressed=True)
        clock = [100.0, 116.0]                     # heard 16 s after it ended, here
        with mock.patch.object(loop.time, "monotonic", lambda: clock.pop(0) if len(clock) > 1 else clock[0]):
            self._brain().handle(self._utt())
        said_ago = self.server.voice.call_args.kwargs["said_ago_ms"]
        # 16 s hearing + the 600 ms pause that ended it + 1 s of speech
        self.assertEqual(said_ago, 16000 + self.cfg.end_silence_ms + 1000)

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

    def test_speech_to_text_down_is_told_apart_from_noise(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False,
                                                       reason="stt_down", detail="every Gemini model failed: m 429")
        self._brain(_FakeEars(ready=False)).handle(self._utt())
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False,
                                                       reason="no_transcript")
        self._brain(_FakeEars(ready=False)).handle(self._utt())
        self.assertIn("[VOICE] the server can't turn speech into text right now: every Gemini model failed: "
                      "m 429 — check GEMINI_API_KEY and its quota on the server", self.lines)
        self.assertIn("[VOICE] the server heard nothing it could use (1000 ms, level 50–3000: "
                      "a voice is usually 1000+)", self.lines)

    def test_what_the_server_heard_is_in_the_log(self):
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه، موجود.", addressed=True,
                                                       transcript="يا موس عندك فلتر زيت؟")
        self._brain(_FakeEars(ready=False)).handle(self._utt())
        self.assertIn("[VOICE] heard «يا موس عندك فلتر زيت؟»", self.lines)

    def test_what_was_heard_is_in_the_log_when_it_wasnt_for_the_robot(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="يا موز عندك فلتر؟",
                                                       addressed=False)
        self._brain(_FakeEars(ready=False)).handle(self._utt())
        self.assertIn("[VOICE] not addressed to me (heard «يا موز عندك فلتر؟»): start with «يا موس»", self.lines)

    def test_no_whisper_the_server_listens(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False,
                                                       reason="no_transcript")
        self._brain(_FakeEars(ready=False)).handle(self._utt())
        sent = self.server.voice.call_args.kwargs["wav"]
        self.assertEqual(wavs.parse(sent)[:2], (wavs.ULAW, 16000))      # half the upload

    def test_a_slow_computer_lets_the_server_listen_while_its_up(self):
        ears_ = _FakeEars()
        ears_.warmup = 7.5                                      # a 2-core Celeron, no AVX
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False)
        brain = self._brain(ears_)
        brain.handle(self._utt())
        self.assertIn("wav", self.server.voice.call_args.kwargs)
        self.assertEqual(ears_.prompts, [])                      # Whisper here wasn't asked
        brain.online = False                                     # the heartbeat says it's down
        with mock.patch.object(brain, "answer_offline") as offline:
            brain.handle(self._utt())
        offline.assert_called_once_with("يا موس عندك فلتر زيت؟", mock.ANY)
        self.assertEqual(len(ears_.prompts), 1)

    def test_the_server_gone_mid_sentence_whisper_here_takes_it(self):
        ears_ = _FakeEars()
        ears_.warmup = 7.5
        self.server.voice.side_effect = server.ServerDown("no route")
        brain = self._brain(ears_)
        with mock.patch.object(brain, "answer_offline") as offline:
            brain.handle(self._utt())
        offline.assert_called_once_with("يا موس عندك فلتر زيت؟", mock.ANY)
        self.assertFalse(brain.online)

    def test_brain_ini_decides_who_listens(self):
        for use, warmup, server_first in (("auto", 0.8, False), ("auto", 7.5, True),
                                          ("always", 7.5, False), ("offline", 0.8, True)):
            with self.subTest(use=use, warmup=warmup):
                self.cfg.ears_use = use
                ears_ = _FakeEars()
                ears_.warmup = warmup
                self.assertEqual(self._brain(ears_).server_hears_first, server_first)

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
        self.cfg.counter_port = 0                # no counter page in this test
        brain = loop.Brain(self.cfg, ears=_FakeEars(), server=self.server, mic=Mic(),
                           speaker=self.speaker, log=self.lines.append)
        with self.assertRaises(KeyboardInterrupt):
            brain.run()
        self.speaker.play_wav.assert_called_once_with(b"WAV")
        self.assertEqual(Mic.drained, 2)
        self.assertTrue(any("whisper hiccup" in line for line in self.lines))
        self.assertTrue(brain._stop.is_set())

    def _run_on(self, frames, ears_):
        class Mic:
            name = "test mic"

            def frames(self):
                yield from frames
                raise KeyboardInterrupt

            def drain(self):
                pass

        self.server.heartbeat.return_value = 200
        self.server.catalog.return_value = {}
        self.server.speak.return_value = None
        self.cfg.counter_port = 0
        self.cfg.ears_use = "always"
        brain = loop.Brain(self.cfg, ears=ears_, server=self.server, mic=Mic(),
                           speaker=self.speaker, log=self.lines.append)
        with self.assertRaises(KeyboardInterrupt):
            brain.run()
        return brain

    def test_vosk_hears_it_while_it_is_said(self):
        # 🐛 [FIX]: heard after they stopped, it took as long again (2–3 s on the Celeron).
        rng = np.random.default_rng(5)
        cough = _speech(96, rng)                                    # too short for a sentence
        frames = (_frames(40, 1500, rng) + cough + _frames(40, 1500, rng)
                  + _speech(1200, rng) + _frames(40, 1500, rng))
        streams = []

        class Stream:
            def __init__(self):
                self.frames, self.dropped, self.error = [], False, ""
                streams.append(self)

            def feed(self, frame):
                self.frames.append(frame)

            def finish(self):
                return "يا موس عندك فلتر زيت؟", 0.05

            def drop(self):
                self.dropped = True

        ears_ = _FakeEars(text="the slow way")
        ears_.stream = Stream
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه", transcript="", addressed=True)
        self._run_on(frames, ears_)
        self.assertEqual(ears_.prompts, [])                          # never heard a second time
        self.assertEqual(self.server.voice.call_args.kwargs["transcript"], "يا موس عندك فلتر زيت؟")
        self.assertEqual([s.dropped for s in streams], [True, False])   # the cough, then the sentence
        self.assertGreaterEqual(len(streams[1].frames) * vad.FRAME_MS, 1200)
        self.assertTrue(any("heard in 0.1 s" in line for line in self.lines), self.lines)

    def test_its_own_voice_mid_sentence_doesnt_stop_the_next_one_streaming(self):
        rng = np.random.default_rng(7)
        speech, streams = _speech(1200, rng), []
        brain = None

        class Stream:
            error = ""

            def __init__(self):
                self.dropped = False
                streams.append(self)
                if len(streams) == 1:
                    brain._heard_itself.set()            # the day spoke while they talked

            def feed(self, frame):
                pass

            def finish(self):
                return "يا موس عندك فلتر زيت؟", 0.05

            def drop(self):
                self.dropped = True

        ears_ = _FakeEars()
        ears_.stream = Stream
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه", transcript="", addressed=True)
        frames = _frames(40, 1500, rng) + speech + _frames(40, 1500, rng) + speech + _frames(40, 1500, rng)
        real_brain = loop.Brain

        def keep(*a, **kw):
            nonlocal brain
            brain = real_brain(*a, **kw)
            return brain
        with mock.patch.object(loop, "Brain", keep):
            self._run_on(frames, ears_)
        # Dropped there; what was left of it, and the next sentence, both heard as said.
        self.assertEqual([s.dropped for s in streams], [True, False, False])
        self.assertEqual(ears_.prompts, [])

    def test_a_broken_stream_hears_it_the_slow_way(self):
        rng = np.random.default_rng(6)
        frames = _frames(40, 1500, rng) + _speech(1200, rng) + _frames(40, 1500, rng)

        class Stream:
            error = ""

            def feed(self, frame):
                pass

            def finish(self):
                self.error = "MemoryError: no RAM"
                return "", 0.0

        ears_ = _FakeEars()
        ears_.stream = Stream
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه", transcript="", addressed=True)
        self._run_on(frames, ears_)
        self.assertEqual(len(ears_.prompts), 1)                      # heard after all
        self.assertIn("[EARS] MemoryError: no RAM: heard again, the slow way", self.lines)

    def test_far_too_slow_whisper_is_let_go_so_offline_is_said_at_once(self):
        class Mic:
            name = "test mic"

            def frames(self):
                raise KeyboardInterrupt
                yield

        self.server.heartbeat.return_value = 200
        self.server.catalog.return_value = {}
        self.server.speak.return_value = None
        self.cfg.counter_port = 0
        ears_ = _FakeEars()
        ears_.warmup = 48.0                                      # the shop's Celeron
        brain = loop.Brain(self.cfg, ears=ears_, server=self.server, mic=Mic(),
                           speaker=self.speaker, log=self.lines.append)
        with self.assertRaises(KeyboardInterrupt):
            brain.run()
        self.assertFalse(ears_.ready)
        self.assertTrue(any("too slow on this computer (48 s a sentence)" in line for line in self.lines))
        self.server.voice.side_effect = server.ServerDown("502")
        with mock.patch.object(brain, "say_offline") as offline:
            brain.handle(self._utt())
        offline.assert_called_once()
        self.assertEqual(ears_.prompts, [])                      # no 48 s wait

    def test_use_always_keeps_a_slow_whisper_no_google(self):
        class Mic:
            name = "test mic"

            def frames(self):
                raise KeyboardInterrupt
                yield

        self.server.heartbeat.return_value = 200
        self.server.catalog.return_value = {}
        self.server.speak.return_value = None
        self.cfg.counter_port = 0
        self.cfg.ears_use = "always"
        ears_ = _FakeEars()
        ears_.warmup = 33.0
        brain = loop.Brain(self.cfg, ears=ears_, server=self.server, mic=Mic(),
                           speaker=self.speaker, log=self.lines.append)
        with self.assertRaises(KeyboardInterrupt):
            brain.run()
        self.assertTrue(ears_.ready)
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False)
        brain.handle(self._utt())
        self.assertEqual(len(ears_.prompts), 1)                     # heard here
        self.assertEqual(self.server.voice.call_args.kwargs.get("transcript"), "يا موس عندك فلتر زيت؟")

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
