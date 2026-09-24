"""The robot answers only when it's called by its name.

People talk near the robot all day; answering every sentence would be noise
(and would log their conversations). Speech must name the robot ("يا موس …"),
unless it's a follow-up within the conversation window, the push-to-talk
button is held, or the robot is running a flow it started (enrollment, count).
"""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from robot import views, wakename


def _device(**kw):
    d = mock.Mock(wake_name="موس", wake_aliases=[], wake_required=True,
                  listening_until=None)
    for k, v in kw.items():
        setattr(d, k, v)
    return d


class NameMatchingTests(SimpleTestCase):

    def test_called_at_the_start(self):
        self.assertEqual(wakename.strip_name(_device(), "يا موس، عندك طرمبة مية؟"),
                         (True, "عندك طرمبة مية"))

    def test_called_at_the_end(self):
        addressed, rest = wakename.strip_name(_device(), "عندك فلتر زيت يا موس")
        self.assertTrue(addressed)
        self.assertEqual(rest, "عندك فلتر زيت")

    def test_original_spelling_is_kept_for_part_lookup(self):
        _, rest = wakename.strip_name(_device(), "موس اجرد الكنترول والطرمبة")
        self.assertIn("الطرمبة", rest)          # not normalized to "الطرمبه"

    def test_stt_spelling_variants(self):
        for heard in ("ماوس عندك بوجيهات", "Mouss do you have plugs", "ياموس عندك بوجيهات",
                      "يا مُوس عندك بوجيهات"):
            self.assertTrue(wakename.strip_name(_device(), heard)[0], heard)

    def test_custom_name_and_aliases(self):
        d = _device(wake_name="ريكس", wake_aliases=["ركس"])
        self.assertTrue(wakename.strip_name(d, "ركس فين الفلاتر")[0])
        self.assertFalse(wakename.strip_name(d, "موس فين الفلاتر")[0])

    def test_name_inside_another_word_does_not_count(self):
        self.assertFalse(wakename.strip_name(_device(), "هات الموسيقى")[0])

    def test_conversation_between_people_is_not_addressed(self):
        self.assertFalse(wakename.strip_name(_device(), "هات المفتاح ١٠ يا محمد")[0])


class GateTests(SimpleTestCase):

    def test_chatter_is_ignored(self):
        self.assertEqual(wakename.gate(_device(), "الشاي برد"), (False, "", False))

    def test_bare_name_asks_what_they_want(self):
        self.assertEqual(wakename.gate(_device(), "يا موس"), (True, "", True))

    def test_follow_up_within_the_window_needs_no_name(self):
        d = _device(listening_until=timezone.now() + timedelta(seconds=10))
        self.assertEqual(wakename.gate(d, "وبكام؟"), (True, "وبكام؟", False))

    def test_after_the_window_the_name_is_needed_again(self):
        d = _device(listening_until=timezone.now() - timedelta(seconds=1))
        self.assertFalse(wakename.gate(d, "وبكام؟")[0])

    def test_push_to_talk_and_ongoing_flows_skip_the_name(self):
        self.assertTrue(wakename.gate(_device(), "كنترول ٣", push_to_talk=True)[0])
        self.assertTrue(wakename.gate(_device(), "مش موجود", ongoing_flow=True)[0])

    def test_name_can_be_made_optional(self):
        self.assertEqual(wakename.gate(_device(wake_required=False), "عندك فلتر؟"),
                         (True, "عندك فلتر؟", False))


class VoiceEndpointTests(SimpleTestCase):

    def _post(self, transcript, device=None):
        device = device or _device()
        request = APIRequestFactory().post("/api/robot/v1/voice/",
                                           {"transcript": transcript}, format="json")
        with mock.patch.object(views, "_device_or_401", return_value=(device, None)), \
                mock.patch.object(views.enrollment, "active_session", return_value=None), \
                mock.patch.object(views.services, "get_open_stock_take", return_value=None), \
                mock.patch.object(views, "_voice_employee", return_value=None), \
                mock.patch.object(views, "_handle_voice",
                                  return_value=("inventory_query", "متوفر", {})) as handle, \
                mock.patch.object(views, "RobotVoiceInteraction") as log:
            response = views.voice(request)
        return response, handle, log, device

    def test_unaddressed_speech_gets_no_answer_and_no_log(self):
        response, handle, log, _ = self._post("هو الزبون مشي؟")
        self.assertFalse(response.data["addressed"])
        self.assertEqual(response.data["reply"], "")
        handle.assert_not_called()
        log.objects.create.assert_not_called()

    def test_addressed_speech_is_handled_without_the_name(self):
        response, handle, _, device = self._post("يا موس عندك طرمبة مية؟")
        self.assertEqual(response.data["reply"], "متوفر")
        self.assertEqual(handle.call_args.args[0], "عندك طرمبة مية")
        device.save.assert_called_with(update_fields=["listening_until"])

    def test_just_the_name_opens_the_conversation(self):
        response, handle, _, _ = self._post("يا موس")
        self.assertEqual(response.data["intent"], "wake")
        self.assertTrue(response.data["reply"])
        handle.assert_not_called()
