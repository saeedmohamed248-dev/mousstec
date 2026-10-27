"""robot/restock.py — «أبلغك أول ما ييجي؟»: the sale that waits for the part.

Out of stock with nothing to offer instead, the robot asks for a mobile
number; the number said next is kept (never said back). When the part is
back in the branch (seen on the heartbeat), staff get an alert, the robot
tells them, the morning briefing counts it, the customer is told at their
next visit, and the robot's page has a ready WhatsApp message. Offline, the
brain asks and keeps the number the same way and sends it when the net is
back. Runs on a tenant schema.
"""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from inventory.models import Branch, Inventory
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_customer, make_inventory, make_product
from robot import assistant, customers, memory, restock, services, views
from robot.brain import talk
from robot.brain.knowledge import Knowledge
from robot.brain.outbox import Outbox
from robot.models import RobotAlert, RobotCommand, RobotDevice, RobotRestockRequest

ASKED = "سير مكنة مش موجود هنا دلوقتي، وسعره 300 جنيه. " + talk.RESTOCK_ASK
SAVED = "تمام، سجّلت رقمك، وهبلّغك أول ما سير مكنة يوصل."


def _staff(role):
    return SimpleNamespace(name="كريم", user=SimpleNamespace(
        is_superuser=False, is_active=True, employee_profile=SimpleNamespace(role=role)))


@override_settings(ALLOWED_HOSTS=["*"])
class RestockTests(ERPTenantTestCase):

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-restock", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        memory.forget(self.device)
        self.addCleanup(memory.forget, self.device)
        self.belt = make_product(name="سير مكنة", part_number="11287628649", retail_price="300")
        make_inventory(self.belt, self.branch, quantity=0)

    def say(self, text):
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            return views._handle_voice(text, self.device, None)

    def beat(self):
        request = APIRequestFactory().post("/api/robot/v1/heartbeat/", {}, format="json",
                                           HTTP_X_ROBOT_TOKEN=self.token)
        return views.heartbeat(request)


class AskingTests(RestockTests):

    def test_asked_and_the_number_kept(self):
        self.assertEqual(self.say("عندك سير مكنة؟")[1], ASKED)
        _intent, reply, payload = self.say("010 1234 5678")
        self.assertEqual(reply, SAVED)
        self.assertNotIn("1234", reply)                      # never said out loud
        request = RobotRestockRequest.objects.get(pk=payload["request_id"])
        self.assertEqual((request.phone, request.status, request.product), ("01012345678", "waiting", self.belt))

    def test_yes_first_then_the_number_and_a_wrong_one(self):
        self.say("عندك سير مكنة؟")
        self.assertEqual(self.say("أيوه بلغني")[1], talk.RESTOCK_SAY_IT)
        self.assertEqual(self.say("010 123 456")[1], talk.RESTOCK_AGAIN)      # a mobile, one digit short
        self.assertEqual(self.say("صفر واحد صفر واحد اتنين تلاتة اربعة خمسة ستة سبعة تمانية")[1], SAVED)

    def test_a_part_number_after_the_question_is_a_part_question(self):
        oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(oil, self.branch, quantity=3)
        self.say("عندك سير مكنة؟")
        self.assertIn("فلتر زيت موجود", self.say("طب عندك 11427953125؟")[1])
        self.assertFalse(RobotRestockRequest.objects.exists())

    def test_said_twice_is_one_request(self):
        for _ in range(2):
            self.say("عندك سير مكنة؟")
            self.say("01012345678")
        self.assertEqual(RobotRestockRequest.objects.count(), 1)

    def test_the_customer_is_linked_but_never_the_walk_in(self):
        from inventory.models.customers import WALK_IN_PHONE
        regular = make_customer(name="أحمد", phone="01012345678")
        self.say("عندك سير مكنة؟")
        self.say("01012345678")
        self.assertEqual(RobotRestockRequest.objects.get().customer, regular)
        # «عميل نقدي» is everyone's: nothing waits on it, nothing is said to it.
        walk_in = make_customer(name="عميل نقدي", phone=WALK_IN_PHONE)
        self.assertEqual(talk.mobile_in(WALK_IN_PHONE), "")
        RobotRestockRequest.objects.create(branch=self.branch, product=self.belt, phone="01112345678",
                                           customer=walk_in, status="arrived")
        self.assertEqual(restock.waiting_for(walk_in), [])

    def test_not_asked_when_theres_something_else_to_offer(self):
        make_inventory(self.belt, make_branch(name="المعادي"), quantity=2)
        reply = self.say("عندك سير مكنة؟")[1]
        self.assertNotIn(talk.RESTOCK_ASK, reply)
        self.assertIsNone(memory.recall(self.device).get("restock_for"))

    def test_another_part_since_means_the_number_isnt_for_this_one(self):
        oil = make_product(name="فلتر زيت", retail_price="450")
        make_inventory(oil, self.branch, quantity=3)
        self.say("عندك سير مكنة؟")
        self.say("عندك فلتر زيت؟")
        self.say("01012345678")
        self.assertFalse(RobotRestockRequest.objects.exists())


class ArrivalTests(RestockTests):

    def _waiting(self, phone="01012345678", customer=None):
        return RobotRestockRequest.objects.create(device=self.device, branch=self.branch, product=self.belt,
                                                  phone=phone, customer=customer)

    def test_back_in_stock_tells_the_staff_once(self):
        self._waiting()
        self._waiting(phone="01112345678")
        self.beat()
        self.assertFalse(RobotAlert.objects.exists())              # still out
        Inventory.objects.filter(product=self.belt, branch=self.branch).update(quantity=4)   # no save() signal
        self.beat()
        self.assertEqual(RobotRestockRequest.objects.filter(status="arrived").count(), 2)
        alert = RobotAlert.objects.get()
        self.assertIn("سير مكنة: 2 عميل", alert.message)
        said = RobotCommand.objects.get(kind="say").payload["text"]
        self.assertEqual(said, "وصل سير مكنة، وفيه 2 عميل مستنيه. ابعتلهم من صفحة الروبوت.")
        self.assertNotIn("0101", said)
        self.beat()
        self.assertEqual(RobotAlert.objects.count(), 1)

    def test_the_briefing_and_the_customers_next_visit(self):
        regular = make_customer(name="أحمد", phone="01012345678")
        self._waiting(customer=regular)
        RobotRestockRequest.objects.update(status="arrived")
        self.assertIn("و1 عميل مستنيين أصناف وصلت", assistant.morning_briefing(_staff("manager"), self.device))
        greeting = customers.customer_greeting(regular, visit_count=3)["greeting"]
        self.assertIn("سير مكنة اللي سألت عليه وصل", greeting)

    def test_the_whatsapp_message_is_ready(self):
        request = self._waiting()
        link = restock.whatsapp_link(request)
        self.assertTrue(link.startswith("https://wa.me/201012345678?text="))
        from urllib.parse import unquote
        self.assertIn("سير مكنة اللي سألت عليه وصل، وسعره 300 جنيه", unquote(link))

    def test_the_staff_page_lists_sends_and_marks(self):
        request = self._waiting()
        request.status = "arrived"
        request.save()
        boss = User.objects.create_user("rs_boss", password="x", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(boss)
        host = self.domain.domain
        page = client.get(reverse("robot_ui:restock"), HTTP_HOST=host)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "https://wa.me/201012345678")
        self.assertContains(page, '<span class="block text-xs text-slate-400" dir="ltr">01012345678</span>', html=False)
        client.post(reverse("robot_ui:restock"), {"request_id": request.pk, "status": "notified"}, HTTP_HOST=host)
        request.refresh_from_db()
        self.assertEqual(request.status, "notified")
        self.assertIsNotNone(request.notified_at)


class OfflineTests(RestockTests):

    def test_the_brain_asks_the_same_and_sends_the_number_later(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        self.assertEqual(offline.answer("يا موس عندك سير مكنة؟", now=0.0).text, ASKED)
        reply = offline.answer("010 1234 5678", now=5.0)
        self.assertEqual(reply.text, SAVED)
        self.assertEqual(reply.event, {"kind": "restock",
                                       "payload": {"part_number": "11287628649", "phone": "01012345678"}})
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as tmp:
            box = Outbox(Path(tmp) / "outbox.jsonl")
            box.add(**reply.event)
            push = lambda events: services.apply_offline_events(self.device, events)   # noqa: E731
            self.assertEqual(box.flush(push), 1)
            self.assertEqual(box.pending(), [])
        self.assertEqual(RobotRestockRequest.objects.get().phone, "01012345678")

    def test_a_bad_event_doesnt_hold_back_the_rest_forever(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as tmp:
            box = Outbox(Path(tmp) / "outbox.jsonl")
            box.add("restock", {"part_number": "NOPE", "phone": "01012345678"})
            push = lambda events: services.apply_offline_events(self.device, events)   # noqa: E731
            for _ in range(5):
                box.flush(push, log=lambda *_: None)
            self.assertEqual(box.pending(), [])
