"""«تسريع الرد»: what the robot's ears didn't write (inside a conversation) is heard AND understood
on the server in one Gemini call — not heard first, then understood. And the shop's noise the server
hears as English, with no name and no part number, isn't answered."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.utils import timezone

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import audio, teachers, understand, views
from robot.models import RobotDevice


class ServerHearsOnceTests(ERPTenantTestCase):

    def setUp(self):
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-hears-once", branch=branch)
        self.device.issue_token()
        self.device.listening_until = timezone.now() + timedelta(seconds=20)     # in a conversation
        self.device.save()
        self.wav = audio.make_wav(b"\x00\x00" * 16000, 16000)

    def turn(self, both):
        timing = {"t0": 0}
        with mock.patch.object(teachers, "configured", return_value=["gemini", "azure"]), \
                mock.patch.object(understand, "hear_and_mean", return_value=both) as once, \
                mock.patch.object(teachers, "hear") as hear, \
                mock.patch.object(understand, "meaning") as meaning:
            body, _after = views._voice_turn(SimpleNamespace(data={"talking": "1"}), self.device, self.wav, timing)
        return body, once, hear, meaning

    def test_heard_and_understood_in_one_call(self):
        meant = understand.Meaning(intent="part", command="عندك حساس كامه B48؟", ms=1700)
        body, once, hear, meaning = self.turn(("عندك حساس كامه B48", meant))
        once.assert_called_once()
        hear.assert_not_called()                                         # no second hearing
        meaning.assert_not_called()                                      # nor a second understanding
        self.assertNotEqual(body.get("reason"), "chatter")

    def test_english_noise_isnt_answered(self):
        body, *_ = self.turn(("All day after midnight.", None))
        self.assertEqual((body["intent"], body.get("reason")), ("ignored", "chatter"))


class AVendorThatsACustomerTests(ERPTenantTestCase):
    """«تاجر اسمه أحمد سعيد عليه فلوس»: understood as a vendor, he's a customer — found there."""

    def test_not_a_vendor_then_a_customer(self):
        from inventory.models import Customer
        from robot import assistant

        Customer.objects.create(name="أحمد سعيد الكنج", phone="01033334444", balance=1500)
        reply, payload = assistant._balance_answer("vendor_balance", "علينا كام للمورد أحمد سعيد الكنج")
        self.assertTrue(payload["found"])
        self.assertIn("أحمد سعيد الكنج عليه", reply)
