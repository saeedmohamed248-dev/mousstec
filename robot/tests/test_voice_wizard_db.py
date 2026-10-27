"""«اعملي فاتورة مبيعات» step by step (robot/wizard.py), end to end against the
ERP's own posting — and the staff called aloud when the robot hands over to
them. From the shop's log: the request went to the AI, which promised an
invoice the robot couldn't make."""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from rest_framework.test import APIRequestFactory

from inventory.models import Branch, Customer, Inventory, PurchaseInvoice, SaleInvoice, Treasury
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import (
    make_branch, make_customer, make_employee, make_inventory, make_product, make_treasury, make_vendor,
)
from robot import services, views, wizard
from robot.brain import pins
from robot.models import RobotAlert, RobotDevice, RobotStaffPin, RobotVoiceInteraction


class WizardTests(ERPTenantTestCase):

    def setUp(self):
        from hr.models import Employee

        cache.clear()
        self.addCleanup(cache.clear)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        till = Treasury.objects.filter(branch=self.branch).order_by("id").first() or make_treasury(self.branch)
        till.type, till.balance, till.is_active = "cash", Decimal("1000.00"), True
        till.save()
        self.till = till
        self.device = RobotDevice(name="موس", device_uid="esp-wizard", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(self.oil, self.branch, quantity=5)
        self.plug = make_product(name="بوجيه", part_number="12120037244", retail_price="100")
        make_inventory(self.plug, self.branch, quantity=10)
        self.ahmed = make_customer(name="أحمد محمود", phone="01012345678")
        user, _profile = make_employee(username="saeed", role="manager", branch=self.branch, first_name="سعيد")
        self.manager, _ = Employee.objects.get_or_create(user=user)
        RobotStaffPin.objects.create(employee=self.manager, pin_hash=pins.hash_pin("2468", iterations=1000))

    def say(self, text, employee="manager"):
        employee = self.manager if employee == "manager" else employee
        _intent, reply, payload = views._handle_voice(text, self.device, employee)
        return reply, payload

    def stock(self, product):
        return Inventory.objects.get(product=product, branch=self.branch).quantity

    def test_a_cash_sale_asked_step_by_step(self):
        steps = [("اعملي فاتورة مبيعات", "اسم العميل إيه؟"),
                 ("نقدي", "القطعة إيه؟"),
                 ("فلتر زيت", "فلتر زيت، الحتة بـ"),
                 ("أيوه", "فيه صنف تاني؟"),
                 ("3 بوجيه", "بوجيه، الحتة بـ"),
                 ("تمام", "فيه صنف تاني؟"),
                 ("لا خلاص", "كاش ولا آجل؟"),
                 ("كاش", "تمام: بيع فلتر زيت و3 بوجيه بإجمالي")]
        for said, expected in steps:
            reply, _payload = self.say(said)
            self.assertIn(expected, reply, said)
        self.assertFalse(SaleInvoice.objects.exists())                 # nothing before the branch
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        invoice = SaleInvoice.objects.get()
        self.assertEqual(invoice.total_amount, Decimal("750.00"))
        self.assertEqual((self.stock(self.oil), self.stock(self.plug)), (4, 7))

    def test_on_credit_to_a_registered_customer(self):
        for said in ("عايز أعمل فاتورة مبيعات", "أحمد محمود", "فلتر زيت", "أيوه", "لا"):
            self.say(said)
        reply, _ = self.say("آجل")
        self.assertIn("لـأحمد محمود", reply)
        self.say("هنا")
        invoice = SaleInvoice.objects.get()
        self.assertEqual(invoice.customer_id, self.ahmed.pk)

    def test_a_name_not_registered_goes_on_the_invoice_cash_only(self):
        for said in ("فاتورة مبيعات", "كريم حسن"):
            reply, _ = self.say(said)
        self.assertIn("أكمّلها نقدي باسمه؟", reply)
        for said in ("أيوه", "فلتر زيت", "أيوه", "لا"):
            self.say(said)
        reply, _ = self.say("آجل")                                      # never on «عميل نقدي»
        self.assertIn("مينفعش على «عميل نقدي»", reply)
        self.say("كاش")
        self.say("هنا")
        invoice = SaleInvoice.objects.get()
        self.assertTrue(invoice.customer.is_walk_in)
        self.assertEqual(invoice.walk_in_name, "كريم حسن")

    def test_several_parts_said_its_asked_which(self):
        make_product(name="فلتر زيت بوش", part_number="11427953999", retail_price="500")
        for said in ("فاتورة مبيعات", "نقدي"):
            self.say(said)
        reply, payload = self.say("فلتر")
        if payload["action"] == "wizard_choose":
            self.assertIn("تقصد أنهي؟", reply)
            reply, payload = self.say("الأولاني")
        self.assertEqual(payload["action"], "wizard_confirm", reply)

    def test_no_face_the_pin_once_then_remembered(self):
        reply, payload = self.say("اعملي فاتورة مبيعات", employee=None)
        self.assertEqual(payload["action"], "wizard_pin")
        reply, payload = self.say("واحد اتنين تلاتة اربعة", employee=None)   # wrong
        self.assertIn("مش صح", reply)
        reply, payload = self.say("اتنين اربعة ستة تمانية", employee=None)
        self.assertEqual(payload["action"], "wizard_customer", reply)
        self.say("الغي", employee=None)
        reply, payload = self.say("فاتورة مبيعات", employee=None)        # the same person, a moment later
        self.assertEqual(payload["action"], "wizard_customer")

    def test_three_wrong_pins_and_its_over(self):
        self.say("فاتورة مبيعات", employee=None)
        for _ in range(3):
            reply, payload = self.say("1111", employee=None)
        self.assertEqual(payload["action"], "wizard_pin_failed")
        self.assertFalse(wizard.active(self.device))

    def test_the_pin_said_is_never_kept(self):
        def voice(text):
            request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": text},
                                               HTTP_X_ROBOT_TOKEN=self.token)
            return views.voice(request).data
        voice("يا موس اعملي فاتورة مبيعات")
        body = voice("2468")                                           # no «يا موس»: its own question's answer
        self.assertTrue(body["addressed"])
        self.assertEqual(body["transcript"], "••••")
        self.assertFalse(RobotVoiceInteraction.objects.filter(transcript__contains="2468").exists())

    def test_a_pin_a_teacher_heard_is_billed_and_never_kept(self):
        import os

        from robot import teachers
        from robot.brain import wavs
        from robot.models import RobotHeard, RobotTeacherUsage
        from robot.tests.test_teachers_db import AZURE, azure_says
        import numpy as np

        views._handle_voice("فاتورة مبيعات", self.device, None)
        recording = wavs.ulaw_wav(np.full(32000, 800, dtype=np.int16))
        with mock.patch.dict(os.environ, AZURE), mock.patch("requests.post", return_value=azure_says("2468")):
            got = teachers.check(self.device, recording, "اتنين", keep=not wizard.wants_secret(self.device))
        self.assertEqual(got.text, "2468")                             # the teacher's hearing is used…
        self.assertFalse(RobotHeard.objects.exists())                  # …never kept, nor its recording
        self.assertEqual(RobotTeacherUsage.objects.get().kind, "secret")
        self.assertEqual(teachers.used_seconds("azure"), 2)            # its seconds still counted

    def test_a_role_without_the_right_is_told_so(self):
        from hr.models import Employee
        user, _ = make_employee(username="tech1", role="tech", branch=self.branch)
        tech, _ = Employee.objects.get_or_create(user=user)
        reply, payload = self.say("سجل مصروف", employee=tech)
        self.assertEqual(payload["action"], "denied")

    def test_an_expense_step_by_step(self):
        for said, expected in (("سجل مصروف", "المبلغ كام؟"), ("ميتين", "على إيه؟"),
                               ("بنزين", "تمام: مصروف 200 جنيه (بنزين)")):
            reply, _ = self.say(said)
            self.assertIn(expected, reply)
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(Treasury.objects.get(pk=self.till.pk).balance, Decimal("800.00"))

    def test_a_purchase_step_by_step(self):
        make_vendor(name="الأمل")
        for said, expected in (("اعمل فاتورة مشتريات", "من أنهي مورد؟"), ("الأمل", "القطعة إيه؟"),
                               ("2 فلتر زيت", "اشتريت الحتة بكام؟"), ("300", "أضيفها؟"), ("أيوه", "صنف تاني"),
                               ("لا", "كاش ولا آجل على المورد؟"), ("كاش", "تمام: شراء 2 فلتر زيت من الأمل")):
            reply, _ = self.say(said)
            self.assertIn(expected, reply, said)
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(PurchaseInvoice.objects.count(), 1)
        self.assertEqual(self.stock(self.oil), 7)

    def test_stopped_anywhere(self):
        for said in ("فاتورة مبيعات", "نقدي"):
            self.say(said)
        reply, payload = self.say("الغي")
        self.assertEqual(payload["action"], "wizard_cancelled")
        self.assertFalse(SaleInvoice.objects.exists())


class StaffCallTests(ERPTenantTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-staff", branch=branch)
        self.token = self.device.issue_token()
        self.device.save()

    def ask(self, text):
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": text},
                                           HTTP_X_ROBOT_TOKEN=self.token)
        return views.voice(request).data

    def test_handed_over_to_staff_they_are_called_aloud(self):
        handover = "أه طبعاً، لحظة واحدة هسأل لك حد من زمايلي الموظفين."
        with mock.patch.object(services, "ai_reply", return_value=handover):
            first = self.ask("يا موس انت بتعرف تركب الحاجات دي؟")
            again = self.ask("يا موس طب امتى؟")
        self.assertTrue(first["reply"].endswith(services.STAFF_CALL))
        self.assertTrue(first.get("staff_called"))
        self.assertFalse(again["reply"].endswith(services.STAFF_CALL))   # once a minute, not every time
        alert = RobotAlert.objects.get()
        self.assertIn("محتاج موظف", alert.message)
        self.assertIn("بتعرف تركب", alert.message)

    def test_words_that_dont_hand_over_call_nobody(self):
        self.assertFalse(services.hands_to_staff("أيوه، فلتر زيت موجود، عندي منه 3 حتت."))
        self.assertTrue(services.hands_to_staff("ملقتش سعرها، اسأل حد من الموظفين."))
        self.assertTrue(services.hands_to_staff("الفواتير بيعملها أحد الموظفين.. لحظة وهندهلك فوراً."))

    def test_a_conversation_the_robot_says_is_on_needs_no_name(self):
        # The robot's own clock: its answer came 40 s late, the server's window had shut meanwhile.
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": "عندك فلتر زيت"},
                                           HTTP_X_ROBOT_TOKEN=self.token)
        self.assertFalse(views.voice(request).data["addressed"])
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": "عندك فلتر زيت", "talking": "1"},
                                           HTTP_X_ROBOT_TOKEN=self.token)
        self.assertTrue(views.voice(request).data["addressed"])
