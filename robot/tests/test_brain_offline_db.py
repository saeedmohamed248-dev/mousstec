"""Offline, «مخ موس» says what the server would have said — word for word.

The server's catalog snapshot (/sync/pull/) is fed to the brain's offline
knowledge, and both are asked the same questions: the brain's sentence must
be the server's own. Also: the snapshot stays retail-only and now carries
what the offline answers need (car model, barcode, the robot's name).
"""

from decimal import Decimal
from unittest import mock

from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, views
from robot.brain.knowledge import Knowledge
from robot.models import RobotDevice


class OfflineMatchesServerTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-offline-parity", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125",
                                retail_price="450.50", b2b_wholesale_price=Decimal("321.00"))
        make_inventory(self.oil, self.branch, quantity=3)
        for model, price in (("E90", "1800"), ("F30", "2100")):
            pump = make_product(name="طرمبة مية", car_model=model, retail_price=price)
            make_inventory(pump, self.branch, quantity=1)
        shocks = make_product(name="مساعدين خلفي فيرنا", retail_price="950")
        make_inventory(shocks, self.branch, quantity=0)
        pads = make_product(name="تيل فرامل أمامي", retail_price="0")
        make_inventory(pads, self.branch, quantity=4)

    def _server_says(self, text):
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            _intent, reply, _payload = views._handle_voice(text, self.device, None)
        return reply

    def test_the_same_words_as_the_server(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        for question in ("عندك فلتر زيت؟", "بكام 11427953125 لو سمحت", "بكام ١١ ٤٢ ٧ ٩٥٣ ١٢٥",
                         "عندك طرمبه ميه E90؟", "طرمبة مية بكام", "فيه مساعدين خلفي فيرنا",
                         "تيل فرامل أمامي", "عندك فانوس عربية؟"):
            with self.subTest(question):
                said = offline.answer("يا موس " + question, now=0.0).text
                self.assertEqual(said, self._server_says(question))

    def test_the_snapshot_is_retail_only_and_has_what_offline_needs(self):
        request = APIRequestFactory().post("/api/robot/v1/sync/pull/", HTTP_X_ROBOT_TOKEN=self.token)
        data = views.sync_pull(request).data
        oil = next(p for p in data["parts"] if p["part_number"] == "11427953125")
        self.assertEqual(oil["retail_price"], 450.5)
        self.assertIn("car_model", oil)
        self.assertIn("barcode", oil)
        self.assertTrue(oil["active"])
        self.assertEqual(data["wake"]["name"], "موس")
        self.assertNotIn("321", str(data))                    # wholesale never leaves the server
