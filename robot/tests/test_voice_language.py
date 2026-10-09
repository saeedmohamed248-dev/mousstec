"""The robot answers in the language it was spoken to: Egyptian Arabic or English.

Replies are written in Egyptian Arabic; an English speaker gets them
translated, and the TTS voice follows the text. No network and no database:
Gemini and the device are stubbed.
"""

import sys
import unittest
from unittest import mock

from django.test import SimpleTestCase

from robot import audio, language, security, services, views


class DetectTests(unittest.TestCase):

    def test_egyptian_arabic(self):
        self.assertEqual(language.detect("عندك فلتر زيت BMW؟"), "ar")

    def test_english(self):
        self.assertEqual(language.detect("do you have an oil filter for the X5"), "en")

    def test_english_with_an_arabic_part_name_is_still_english(self):
        self.assertEqual(language.detect("do you have the فلتر"), "en")

    def test_part_numbers_and_fault_codes_dont_count(self):
        self.assertEqual(language.detect("كود P0301"), "ar")
        self.assertEqual(language.detect("عندك 11517586925"), "ar")

    def test_nothing_to_go_on_is_arabic(self):
        self.assertEqual(language.detect(""), "ar")
        self.assertEqual(language.detect("11517586925"), "ar")


class ToEnglishTests(unittest.TestCase):

    def test_arabic_goes_through_gemini(self):
        with mock.patch.object(audio, "gemini_generate", return_value="It's in stock for 450 pounds.") as gen:
            self.assertEqual(language.to_english("متوفر بسعر 450 جنيه."), "It's in stock for 450 pounds.")
        self.assertIn("متوفر بسعر 450 جنيه.", gen.call_args.args[0][1]["text"])

    def test_english_already_needs_no_call(self):
        with mock.patch.object(audio, "gemini_generate") as gen:
            self.assertEqual(language.to_english("Yes, we have it."), "Yes, we have it.")
        gen.assert_not_called()

    def test_without_translation_the_arabic_is_spoken(self):
        with mock.patch.object(audio, "gemini_generate", return_value=""):
            self.assertEqual(language.to_english("مش متوفر."), "مش متوفر.")

    def test_the_translation_is_scrubbed_for_prices(self):
        with mock.patch.object(audio, "gemini_generate", return_value="The wholesale price is 300."):
            self.assertEqual(language.to_english("سعر"), "[price detail withheld]")

    def test_an_arabic_speaker_is_answered_in_arabic(self):
        with mock.patch.object(audio, "gemini_generate") as gen:
            self.assertEqual(language.reply_in("ar", "أيوه، متوفر."), "أيوه، متوفر.")
        gen.assert_not_called()


class VoiceAnswersInTheSpeakersLanguageTests(SimpleTestCase):

    def _ask(self, transcript, *, gate=None, handled=("inventory_query", "أيوه، متوفر.", {})):
        from rest_framework.test import APIRequestFactory
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": transcript},
                                           format="multipart")
        gate = gate or (True, transcript, False)
        with mock.patch.object(views, "_device_or_401", return_value=(mock.Mock(), None)), \
                mock.patch.object(security.RobotDeviceThrottle, "allow_request", return_value=True), \
                mock.patch.object(views.entries, "is_answer", return_value=False), \
                mock.patch.object(views.wakename, "gate", return_value=gate), \
                mock.patch.object(views.wakename, "keep_listening"), \
                mock.patch.object(views, "_voice_employee", return_value=None), \
                mock.patch.object(views, "_handle_voice", return_value=handled) as handle, \
                mock.patch.object(views, "RobotVoiceInteraction"), \
                mock.patch.object(audio, "gemini_generate", return_value="Yes, it's in stock.") as gen:
            data = views.voice(request).data
        return data, handle, gen

    def test_english_question_english_answer(self):
        data, handle, gen = self._ask("hey mouss do you have an oil filter")
        self.assertEqual((data["lang"], data["reply"]), ("en", "Yes, it's in stock."))
        self.assertEqual(handle.call_args.kwargs["lang"], "en")
        gen.assert_called_once()

    def test_arabic_question_arabic_answer(self):
        data, _, gen = self._ask("يا موس عندك فلتر زيت")
        self.assertEqual((data["lang"], data["reply"]), ("ar", "أيوه، متوفر."))
        gen.assert_not_called()

    def test_just_the_name_in_english(self):
        data, _, _ = self._ask("hey mouss", gate=(True, "", True))
        self.assertEqual(data["reply"], "Yes? How can I help?")

    def test_just_the_name_in_arabic(self):
        data, _, _ = self._ask("يا موس", gate=(True, "", True))
        self.assertEqual(data["reply"], "أيوه، تحت أمرك.")


class GeneralAnswerTests(unittest.TestCase):
    """The free-form answer (no rule matched)."""

    def test_asked_for_in_english_when_spoken_to_in_english(self):
        llm = mock.Mock(return_value="Sure, we open at 10.")
        with mock.patch("inventory.ai_services.call_llm_layer", llm):
            self.assertEqual(services.ai_reply("when do you open", "en"), "Sure, we open at 10.")
        self.assertIn("English", llm.call_args.args[0][0]["content"])

    def test_gemini_answers_when_the_llm_gateway_cant(self):
        with mock.patch("inventory.ai_services.call_llm_layer", return_value=None), \
                mock.patch.object(audio, "gemini_generate", return_value="بنفتح الساعة 10.") as gen:
            self.assertEqual(services.ai_reply("بتفتحوا امتى"), "بنفتح الساعة 10.")
        self.assertEqual(gen.call_args.kwargs["what"], "reply")


class TtsVoiceFollowsTheTextTests(unittest.TestCase):

    def _lang_for(self, text):
        fake = mock.Mock()
        with mock.patch.dict(sys.modules, {"gtts": fake}):
            audio._gtts_synthesize(text, "ar")
        return fake.gTTS.call_args.kwargs["lang"]

    def test_english_reply_english_voice(self):
        self.assertEqual(self._lang_for("Yes, the فلتر is on shelf A3."), "en")

    def test_arabic_reply_arabic_voice(self):
        self.assertEqual(self._lang_for("أيوه، الفلتر BMW على الرف A3."), "ar")
