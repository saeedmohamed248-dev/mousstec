"""Speech-to-text for the robot's mic, and what /voice/ says when it hears nothing.

STT used the google-generativeai SDK, which isn't installed in the image, so
every clip came back empty and the robot never answered anyone. It now calls
the Gemini REST API like the rest of the ERP, and falls through to the next
model when one is retired.
"""

import base64
import unittest
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from robot import audio, security, views


class _Resp:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text or str(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def _ok(text):
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


class GeminiTranscribeTests(unittest.TestCase):

    def setUp(self):
        for target, value in (("_gemini_key", "k"),
                              ("_stt_models", ["retired-model", "current-model"])):
            patcher = mock.patch.object(audio, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_wav_goes_inline_and_the_text_comes_back(self):
        with mock.patch("requests.post", return_value=_ok(" يا موس عندك فلتر زيت؟ ")) as post:
            self.assertEqual(audio.transcribe(b"RIFFdata"), "يا موس عندك فلتر زيت؟")
        self.assertIn("/models/retired-model:generateContent", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["params"], {"key": "k"})
        inline = post.call_args.kwargs["json"]["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(inline["mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(inline["data"]), b"RIFFdata")

    def test_a_retired_model_falls_through_to_the_next(self):
        replies = [_Resp(404, text="model not found"), _ok("أهلا")]
        with mock.patch("requests.post", side_effect=replies) as post:
            self.assertEqual(audio.transcribe(b"x"), "أهلا")
        self.assertEqual(post.call_count, 2)
        self.assertIn("/models/current-model:", post.call_args.args[0])

    def test_thinking_parts_are_left_out(self):
        reply = _Resp(200, {"candidates": [{"content": {"parts": [
            {"text": "the user greets", "thought": True}, {"text": "مرحبا"}]}}]})
        with mock.patch("requests.post", return_value=reply):
            self.assertEqual(audio.transcribe(b"x"), "مرحبا")

    def test_no_network_is_empty_and_stops_there(self):
        import requests
        with mock.patch("requests.post", side_effect=requests.ConnectionError("down")) as post:
            self.assertEqual(audio.transcribe(b"x"), "")
        self.assertEqual(post.call_count, 1)

    def test_every_model_failing_is_empty(self):
        with mock.patch("requests.post", return_value=_Resp(500, text="oops")):
            self.assertEqual(audio.transcribe(b"x"), "")

    def test_no_key_means_no_call(self):
        with mock.patch.object(audio, "_gemini_key", return_value=""), \
                mock.patch("requests.post") as post:
            self.assertEqual(audio.transcribe(b"x"), "")
        post.assert_not_called()


class SttModelListTests(unittest.TestCase):

    def test_the_configured_model_comes_first_without_repeats(self):
        with mock.patch.dict("os.environ", {"ROBOT_STT_MODEL": "gemini-flash-latest"}):
            models = audio._stt_models()
        self.assertEqual(models[0], "gemini-flash-latest")
        self.assertEqual(len(models), len(set(models)))
        self.assertNotIn("gemini-2.0-flash", models)          # retired


class VoiceHeardNothingTests(SimpleTestCase):
    """/voice/ with a clip nobody could transcribe."""

    def _post(self, **fields):
        from rest_framework.test import APIRequestFactory
        data = {"audio": SimpleUploadedFile("voice.wav", b"RIFF....WAVE", "audio/wav"), **fields}
        request = APIRequestFactory().post("/api/robot/v1/voice/", data, format="multipart")
        with mock.patch.object(views, "_device_or_401", return_value=(object(), None)), \
                mock.patch.object(views.audio_svc, "transcribe", return_value=""), \
                mock.patch.object(security.RobotDeviceThrottle, "allow_request", return_value=True):
            return views.voice(request)

    def test_says_why_and_stays_quiet(self):
        data = self._post().data
        self.assertEqual((data["reason"], data["addressed"], data["reply"]),
                         ("no_transcript", False, ""))

    def test_with_the_button_held_it_asks_again(self):
        data = self._post(ptt="1").data
        self.assertEqual((data["reason"], data["addressed"]), ("no_transcript", True))
        self.assertIn("قول تاني", data["reply"])


class WakeNameHintTests(unittest.TestCase):

    def test_the_devices_name_is_in_the_prompt(self):
        with mock.patch.object(audio, "_gemini_key", return_value="k"), \
                mock.patch.object(audio, "_stt_models", return_value=["m"]), \
                mock.patch("requests.post", return_value=_ok("يا زيكو")) as post:
            audio.transcribe(b"x", name="زيكو")
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("«زيكو»", prompt)
        self.assertNotIn("{name}", prompt)
