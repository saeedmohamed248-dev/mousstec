"""The fast voice: half-size audio both ways and the answer in one request.

On the shop's slow 4G the robot answered seconds late: the question went up
as 16-bit PCM, the words came back, a second request fetched their voice,
and the whole clip downloaded before the first word. Now µ-law goes both
ways, /voice/ returns the spoken reply itself, repeated replies come from a
cache, and Gemini doesn't "think" before writing down what was said.
No network and no database.
"""

import base64
import struct
from unittest import mock
from urllib.parse import unquote

from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from robot import audio, views


def _ref_encode(pcm: int) -> int:
    """The firmware's muEncode, line for line."""
    sign = (pcm >> 8) & 0x80
    v = -pcm if sign else pcm
    v = min(v, 32635) + 0x84
    exp, mask = 7, 0x4000
    while not (v & mask) and exp > 0:
        exp -= 1
        mask >>= 1
    mant = (v >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | mant) & 0xFF


def _pcm(*samples) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


class UlawTests(SimpleTestCase):

    def test_encoding_matches_the_firmware_for_every_sample(self):
        every = list(range(-32768, 32768))
        self.assertEqual(audio.ulaw_encode(_pcm(*every)), bytes(_ref_encode(s) for s in every))

    def test_decoding_inverts_encoding_closely(self):
        pcm = _pcm(0, 100, -100, 1000, -1000, 12000, -12000, 32767, -32768)
        back = struct.unpack("<9h", audio.ulaw_decode(audio.ulaw_encode(pcm)))
        for want, got in zip(struct.unpack("<9h", pcm), back):
            self.assertLessEqual(abs(want - got), max(8, abs(want) // 16), (want, got))

    def test_a_ulaw_wav_becomes_pcm_for_speech_to_text(self):
        pcm = _pcm(*range(-4000, 4000, 40))
        wav = audio.make_wav(audio.ulaw_encode(pcm), 16000, audio.ULAW)
        self.assertEqual(len(wav), 44 + len(pcm) // 2)       # half the bytes
        tag, rate, samples = audio.wav_parts(audio.as_pcm_wav(wav))
        self.assertEqual((tag, rate, len(samples)), (1, 16000, len(pcm)))

    def test_pcm_and_junk_pass_through(self):
        pcm_wav = audio.make_wav(_pcm(1, 2, 3))
        self.assertEqual(audio.as_pcm_wav(pcm_wav), pcm_wav)
        self.assertEqual(audio.as_pcm_wav(b"RIFFdata"), b"RIFFdata")

    def test_ffmpegs_unknown_data_size_reads_to_the_end(self):
        wav = bytearray(audio.make_wav(_pcm(5, 6, 7, 8)))
        wav[40:44] = b"\xff\xff\xff\xff"                     # what ffmpeg writes to a pipe
        self.assertEqual(audio.wav_parts(bytes(wav))[2], _pcm(5, 6, 7, 8))

    def test_transcribe_sends_gemini_pcm(self):
        wav = audio.make_wav(audio.ulaw_encode(_pcm(*range(0, 3000, 30))), 16000, audio.ULAW)
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"candidates": [{"content": {"parts": [{"text": "يا موس"}]}}]}
        with mock.patch.object(audio, "_gemini_key", return_value="k"), \
                mock.patch.object(audio, "_stt_models", return_value=["m"]), \
                mock.patch("requests.post", return_value=ok) as post:
            self.assertEqual(audio.transcribe(wav), "يا موس")
        sent = base64.b64decode(post.call_args.kwargs["json"]["contents"][0]["parts"][1]
                                ["inline_data"]["data"])
        self.assertEqual(audio.wav_parts(sent)[0], 1)


class NoThinkingTests(SimpleTestCase):

    def _ok(self):
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"candidates": [{"content": {"parts": [{"text": "تمام"}]}}]}
        return ok

    def setUp(self):
        audio._THINKING_REQUIRED.clear()
        audio._forget_model_health()
        self.addCleanup(audio._forget_model_health)
        for target, value in (("_gemini_key", "k"), ("_stt_models", ["m"])):
            patcher = mock.patch.object(audio, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_thinking_is_switched_off(self):
        with mock.patch("requests.post", return_value=self._ok()) as post:
            audio.gemini_generate([{"text": "x"}])
        config = post.call_args.kwargs["json"]["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingBudget": 0})

    def test_a_model_that_must_think_is_asked_again_and_remembered(self):
        refused = mock.Mock(status_code=400, text="Budget 0 is invalid. This model only works in thinking mode.")
        with mock.patch("requests.post", side_effect=[refused, self._ok(), self._ok()]) as post:
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "تمام")
            self.assertNotIn("thinkingConfig", post.call_args.kwargs["json"]["generationConfig"])
            audio.gemini_generate([{"text": "x"}])
        self.assertEqual(post.call_count, 3)                  # no second refusal
        self.assertNotIn("thinkingConfig", post.call_args.kwargs["json"]["generationConfig"])


class ModelHealthTests(SimpleTestCase):
    """The shop's logs: gemini-3.6-flash 429/503 and gemini-flash-latest 503 or
    a 15 s hang on every sentence before the model that worked was reached."""

    def _ok(self, text="يا موس"):
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        return ok

    def setUp(self):
        audio._forget_model_health()
        self.addCleanup(audio._forget_model_health)
        for target, value in (("_gemini_key", "k"), ("_stt_models", ["busy", "slow", "good"])):
            patcher = mock.patch.object(audio, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _models_called(self, post):
        return [c.args[0].split("/models/")[1].split(":")[0] for c in post.call_args_list]

    def test_the_model_that_answered_goes_first_next_time(self):
        import requests
        replies = [mock.Mock(status_code=503, text="overloaded"),
                   requests.ReadTimeout("hung"), self._ok(), self._ok()]
        with mock.patch("requests.post", side_effect=replies) as post:
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "يا موس")
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "يا موس")
        self.assertEqual(self._models_called(post), ["busy", "slow", "good", "good"])

    def test_a_model_that_hangs_no_longer_ends_the_try(self):
        import requests
        with mock.patch("requests.post", side_effect=[requests.ReadTimeout("hung"), self._ok(),
                                                       ]) as post:
            audio._stt_models.return_value = ["slow", "good"]
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "يا موس")
        self.assertEqual(self._models_called(post), ["slow", "good"])

    def test_no_internet_still_stops_at_once(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.ConnectionError("down")) as post:
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "")
        self.assertEqual(post.call_count, 1)

    def test_a_quota_429_rests_as_long_as_google_says(self):
        import time
        quota = mock.Mock(status_code=429, text='{"error": {"details": [{"retryDelay": "40s"}]}}')
        with mock.patch("requests.post", side_effect=[quota, self._ok()]):
            audio._stt_models.return_value = ["busy", "good"]
            audio.gemini_generate([{"text": "x"}])
        self.assertAlmostEqual(audio._model_rest["busy"] - time.monotonic(), 40, delta=2)

    def test_resting_models_are_still_the_last_resort(self):
        audio._stt_models.return_value = ["busy", "good"]
        audio._rest("busy", 429)
        with mock.patch("requests.post", side_effect=[mock.Mock(status_code=503, text="x"),
                                                       self._ok()]) as post:
            self.assertEqual(audio.gemini_generate([{"text": "x"}]), "يا موس")
        self.assertEqual(self._models_called(post), ["good", "busy"])


class SynthesizeUlawTests(SimpleTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        loud = audio.make_wav(_pcm(*([1000, -1000] * 400)))
        for target, value in (("shutil.which", "/usr/bin/ffmpeg"),
                              ("subprocess.run", mock.Mock(stdout=loud))):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ulaw_is_half_the_size_and_evened_out(self):
        with mock.patch.object(audio, "_synthesize_with", return_value=(b"mp3", "edge")):
            wav = audio.synthesize_wav("أيوه، تحت أمرك.", codec=audio.ULAW)
        tag, rate, samples = audio.wav_parts(wav)
        self.assertEqual((tag, rate, len(samples)), (7, 16000, 800))
        peak = max(abs(s) for s in struct.unpack("<800h", audio.ulaw_decode(samples)))
        self.assertGreater(peak, 3500)                       # quiet speech boosted (×4 max)

    def test_a_repeated_reply_comes_from_the_cache(self):
        with mock.patch.object(audio, "_synthesize_with", return_value=(b"mp3", "edge")) as tts:
            first = audio.synthesize_wav("أيوه، تحت أمرك.", codec=audio.ULAW)
            again = audio.synthesize_wav("أيوه، تحت أمرك.", codec=audio.ULAW)
        self.assertEqual(first, again)
        tts.assert_called_once()

    def test_a_long_reply_is_voiced_in_pieces_at_once_and_each_kept(self):
        said = []

        def voice(text, language="ar"):
            said.append(text)
            return b"mp3", "edge"
        reply = ("عندي كذا نوع: طرمبة مية E90 أصلي ألماني، طرمبة مية F30 تايواني، "
                 "طرمبة مية MINI R56 أصلي، طرمبة مية X5 E70 تقليد كويس. تقصد أنهي؟")
        with mock.patch.object(audio, "_synthesize_with", side_effect=voice):
            used = []
            wav = audio.synthesize_wav(reply, codec=audio.ULAW, providers=used)
            self.assertEqual(used, ["edge"])
            pieces = audio._pieces(reply)
            self.assertGreaterEqual(len(pieces), 3)
            self.assertEqual(sorted(said), sorted(pieces))       # every word voiced, once
            self.assertEqual(len(audio.wav_parts(wav)[2]), 800 * len(pieces))   # in order, end to end
            said.clear()
            used = []
            audio.synthesize_wav("تقصد أنهي؟", providers=used)  # a piece said before
        self.assertEqual((said, used), ([], ["cache"]))

    def test_pieces_split_where_a_voice_pauses(self):
        self.assertEqual(audio._pieces("أيوه، عندي. بكام؟ 450 جنيه."), ["أيوه، عندي.", "بكام؟", "450 جنيه."])
        long = "، ".join(f"صنف رقم {n} بسعر {n}00 جنيه" for n in range(1, 9)) + "."
        self.assertTrue(all(len(piece) <= 60 for piece in audio._pieces(long)))
        self.assertEqual(" ".join(audio._pieces(long)), long)

    def test_the_gtts_stand_in_is_not_kept(self):
        with mock.patch.object(audio, "_synthesize_with", return_value=(b"mp3", "gtts")) as tts:
            audio.synthesize_wav("أهلاً", codec=audio.ULAW)
            audio.synthesize_wav("أهلاً", codec=audio.ULAW)
        self.assertEqual(tts.call_count, 2)


def _device():
    return mock.Mock(wake_name="موس", wake_aliases=[], wake_required=True, listening_until=None)


class VoiceAnswersWithAudioTests(SimpleTestCase):

    def _post(self, data, wav=b"RIFF....WAVE"):
        request = APIRequestFactory().post("/api/robot/v1/voice/", data, format="multipart")
        with mock.patch.object(views, "_device_or_401", return_value=(_device(), None)), \
                mock.patch.object(views.entries, "pending_for", return_value=None), \
                mock.patch.object(views.enrollment, "active_session", return_value=None), \
                mock.patch.object(views, "_voice_employee", return_value=None), \
                mock.patch.object(views, "_handle_voice",
                                  return_value=("inventory_query", "متوفر بسعر 450 جنيه.", {})), \
                mock.patch.object(views, "RobotVoiceInteraction"), \
                mock.patch.object(views.audio_svc, "synthesize_wav", return_value=wav) as tts:
            return views.voice(request), tts

    def test_the_reply_comes_back_spoken_in_the_same_request(self):
        response, tts = self._post({"transcript": "يا موس عندك فلتر زيت؟", "reply_audio": "ulaw"})
        self.assertEqual(response["Content-Type"], "audio/wav")
        self.assertEqual(response.content, b"RIFF....WAVE")
        self.assertEqual(tts.call_args.kwargs["codec"], "ulaw")
        self.assertEqual(unquote(response["X-Robot-Reply"]), "متوفر بسعر 450 جنيه.")
        self.assertEqual(unquote(response["X-Robot-Transcript"]), "يا موس عندك فلتر زيت؟")
        self.assertEqual(response["X-Robot-Addressed"], "1")
        marks = [part.split("=")[0] for part in response["X-Robot-Timing"].split(";")]
        self.assertEqual(marks, ["hear", "think", "voice"])
        self.assertEqual(tts.call_args.kwargs["providers"], [])   # the mock said nothing: no «tts=»
        response["X-Robot-Reply"].encode("latin-1")             # a header must be ASCII-safe

    def test_old_firmware_still_gets_json(self):
        response, tts = self._post({"transcript": "يا موس عندك فلتر زيت؟"})
        self.assertEqual(response.data["reply"], "متوفر بسعر 450 جنيه.")
        tts.assert_not_called()

    def test_no_voice_right_now_falls_back_to_json(self):
        response, _ = self._post({"transcript": "يا موس عندك فلتر زيت؟", "reply_audio": "ulaw"},
                                 wav=None)
        self.assertEqual(response.data["reply"], "متوفر بسعر 450 جنيه.")

    def test_chatter_stays_silent_json(self):
        response, tts = self._post({"transcript": "هو الزبون مشي؟", "reply_audio": "ulaw"})
        self.assertFalse(response.data["addressed"])
        tts.assert_not_called()


class FastVoiceOfferTests(SimpleTestCase):

    def test_the_heartbeat_offers_it(self):
        request = APIRequestFactory().post("/api/robot/v1/heartbeat/", {}, format="json")
        with mock.patch.object(views, "_device_or_401", return_value=(mock.Mock(), None)):
            data = views.heartbeat(request).data
        self.assertEqual(data["voice"], {"ulaw": True})

    def test_speak_honours_the_codec(self):
        request = APIRequestFactory().post("/api/robot/v1/speak/",
                                           {"text": "أهلاً", "format": "wav", "codec": "ulaw"},
                                           format="json")
        with mock.patch.object(views, "_device_or_401", return_value=(mock.Mock(), None)), \
                mock.patch.object(views.audio_svc, "synthesize_wav", return_value=b"RIFF") as tts:
            views.speak(request)
        self.assertEqual(tts.call_args.kwargs["codec"], "ulaw")
