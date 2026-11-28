"""«🗣️ صوت Gemini» (ROBOT_TTS_PROVIDER=gemini): the robot speaks in a Gemini voice the owner picked
by ear — more natural Egyptian than Azure's Shakir — with Azure standing in when it can't. No network."""

import base64
import os
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from robot import audio


def _said(sound: bytes, mime="audio/L16;codec=pcm;rate=24000", status=200):
    resp = mock.Mock(status_code=status, text="")
    resp.json.return_value = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(sound).decode()}}]}}]}
    return resp


@override_settings(GEMINI_API_KEY="gm-key")
class GeminiVoiceTests(SimpleTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        audio._gemini_tts_rest["until"] = 0.0
        for patcher in (mock.patch.object(audio, "TTS_PROVIDER", "gemini"),
                        mock.patch.dict(os.environ, {"ROBOT_TTS_GEMINI_VOICE": "Orus"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_said_in_the_chosen_voice_in_egyptian_at_the_robots_rate(self):
        pcm24 = b"\x10\x00" * 24000                                    # 1 s at 24 kHz
        with mock.patch("requests.post", return_value=_said(pcm24)) as post:
            providers = []
            wav = audio.synthesize_wav("أهلاً بحضرتك.", rate=16000, providers=providers)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"],
                         "Orus")
        said = body["contents"][0]["parts"][0]["text"]
        self.assertTrue(said.startswith(audio.GEMINI_TTS_STYLE) and said.endswith("أهلاً بحضرتك."))
        self.assertIn(audio.GEMINI_TTS_MODEL, post.call_args.args[0])
        self.assertEqual(providers, ["gemini"])
        self.assertEqual(len(audio.wav_parts(wav)[2]), 16000 * 2)      # 1 s at 16 kHz
        with mock.patch("requests.post") as again:
            audio.synthesize_wav("أهلاً بحضرتك.", rate=16000)
        again.assert_not_called()                                       # said before: kept

    def test_a_whole_wav_answer_too(self):
        wav24 = audio.make_wav(b"\x01\x00" * 2400, 24000)
        with mock.patch("requests.post", return_value=_said(wav24, mime="audio/wav")):
            pcm = audio._gemini_pcm("تمام.", 16000)
        self.assertEqual(len(pcm), 1600 * 2)

    def test_azure_stands_in_and_a_refused_key_rests(self):
        with mock.patch("requests.post", return_value=_said(b"", status=429)) as post, \
                mock.patch.object(audio, "_azure_pcm", return_value=b"\x00\x00" * 100) as azure:
            self.assertEqual(audio._voiced_pcm("تمام.", "ar", 16000), (b"\x00\x00" * 100, "azure"))
            audio._voiced_pcm("ماشي.", "ar", 16000)
        self.assertEqual(post.call_count, 1)                           # not asked again while it rests
        self.assertEqual(azure.call_count, 2)

    def test_only_when_chosen(self):
        with mock.patch.object(audio, "TTS_PROVIDER", "auto"), mock.patch("requests.post") as post:
            self.assertIsNone(audio._gemini_pcm("تمام.", 16000))
        post.assert_not_called()
