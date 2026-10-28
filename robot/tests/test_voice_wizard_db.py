"""«اعملي فاتورة مبيعات» step by step (robot/wizard.py), end to end against the
ERP's own posting — and the staff called aloud when the robot hands over to
them. From the shop's log: the request went to the AI, which promised an
invoice the robot couldn't make."""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from rest_framework.test import APIRequestFactory

from inventory.models import Branch, Inventory, PurchaseInvoice, SaleInvoice, Treasury
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import (
    make_branch, make_customer, make_employee, make_inventory, make_product, make_treasury, make_vendor,
)
from robot import services, views, wizard
from robot.brain import pins
from robot.models import RobotAlert, RobotDevice, RobotStaffPin, RobotVoiceInteraction


class _WizardFixture(ERPTenantTestCase):

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


class WizardTests(_WizardFixture):

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
        self.assertEqual((wizard._pin_digits("صفر واحد تسعة تسعة"), wizard._pin_digits("رقم 9988")),
                         ("0199", "9988"))
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

    def test_words_that_arent_a_pin_dont_count_against_it(self):
        # From the shop's log: «طرق مبيع», «خطورة يبيع قطع الغيار», «أنا عايز عمل فطوري» — three
        # wrong PINs, the invoice off — then «The رقم 9988.», the real one, went to the AI.
        self.say("عايز أعمل فاتورة مبيعات", employee=None)
        for said in ("طرق مبيع", "خطورة يبيع قطع الغيار"):
            reply, payload = self.say(said, employee=None)
            self.assertEqual(payload["action"], "wizard_pin", said)
            self.assertIn("رقم رقم", reply)
        reply, payload = self.say("The رقم 2468.", employee=None)
        self.assertEqual(payload["action"], "wizard_customer", reply)

    def test_only_talk_and_no_pin_lets_it_go(self):
        self.say("فاتورة مبيعات", employee=None)
        for said in ("عندك فلتر زيت", "طب ماشي", "شكرا"):
            reply, payload = self.say(said, employee=None)
        self.assertEqual(payload["action"], "wizard_cancelled")
        self.assertFalse(wizard.active(self.device))

    def test_asked_for_again_it_begins_again_its_wrong_pins_kept(self):
        self.say("فاتورة مبيعات", employee=None)
        self.say("1111", employee=None)
        self.say("1111", employee=None)
        reply, payload = self.say("فاتورة مبيعات", employee=None)       # begun again…
        self.assertEqual(payload["action"], "wizard_pin")
        reply, payload = self.say("1111", employee=None)                # …never three more tries
        self.assertEqual(payload["action"], "wizard_pin_failed")

    def test_too_many_wrong_pins_lock_the_robot_a_while(self):
        for _ in range(2):
            self.say("فاتورة مبيعات", employee=None)
            for _ in range(3):
                self.say("1111", employee=None)
        reply, payload = self.say("فاتورة مبيعات", employee=None)
        self.assertEqual(payload["action"], "wizard_pin_locked")
        reply, payload = self.say("فاتورة مبيعات")                     # the face the camera knows: fine
        self.assertEqual(payload["action"], "wizard_customer")

    def test_an_invoice_without_which_one_is_asked(self):
        for said in ("يا موس أنا مدير وعايز عمل فاتورة", "عايز أعمل فاتورة"):
            reply, payload = self.say(said)
            self.assertEqual(payload["action"], "wizard_kind", said)
            self.assertIn("مبيعات ولا مشتريات؟", reply)
        reply, payload = self.say("مبيعات")
        self.assertEqual(payload["action"], "wizard_customer", reply)
        wizard._end(self.device)
        self.say("اعملي فاتورة")
        reply, payload = self.say("مشتريات")
        self.assertEqual(payload["action"], "wizard_vendor", reply)
        wizard._end(self.device)
        self.say("اعملي فاتورة")
        self.say("مش عارف")
        reply, payload = self.say("ولا حاجة")
        self.assertEqual(payload["action"], "wizard_cancelled")

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

    def test_a_pin_said_just_after_the_pin_step_let_go_is_never_kept_either(self):
        self.say("فاتورة مبيعات", employee=None)
        for _ in range(3):
            self.say("1111", employee=None)
        self.assertTrue(wizard.wants_secret(self.device))           # the real one may come now
        cache.delete(wizard._hush_key(self.device))
        self.assertFalse(wizard.wants_secret(self.device))

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

    def test_an_apology_is_answered_without_the_ai(self):
        # From the shop's log: «يا موس معلش» waited 7.6 s on the AI for «ولا يهمك».
        from robot.brain import talk
        with mock.patch.object(services, "ai_reply", return_value="لا") as ai:
            body = self.ask("يا موس معلش")
        self.assertEqual(body["reply"], talk.NEVER_MIND)
        ai.assert_not_called()

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


class StockByVoiceTests(_WizardFixture):
    """«عاوز الروبوت يقدر يضيف منتجات في المخزون جديدة أو موجودة يزوّد عدد» — through the ERP's
    own stock setting (inventory_service.set_branch_stock): the movement, the average cost, the
    ledger. And from the shop's log: «الغي الفاتوره… حط لي منتج في المخزون» at the vendor's step."""

    def _journal(self):
        from inventory.models import JournalEntry
        return JournalEntry.objects.filter(reference__startswith="RobotVoiceStock")

    def test_more_pieces_of_a_part_it_has(self):
        from inventory.models import InventoryMovement
        reply, payload = self.say("ضيف 3 فلتر زيت للمخزون")
        self.assertEqual(payload["action"], "wizard_confirm", reply)
        self.assertIn("عندك منه 5 في الفرع ده. أزود 3 حتت تبقى 8؟", reply)
        for said, expected in (("أيوه", "فيه صنف تاني؟"), ("بوجيه 2", "عندك منه 10"), ("تمام", "صنف تاني"),
                               ("لا", "تمام: إضافة للمخزون: 3 فلتر زيت و2 بوجيه")):
            reply, _ = self.say(said)
            self.assertIn(expected, reply, said)
        self.assertEqual(self.stock(self.oil), 5)                       # nothing before the branch
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual((self.stock(self.oil), self.stock(self.plug)), (8, 12))
        moved = InventoryMovement.objects.get(product=self.oil, reference_type="RobotVoiceStock")
        self.assertEqual((moved.reason, moved.quantity_change, moved.quantity_before), ("adjustment", 3, 5))
        self.assertEqual(SaleInvoice.objects.count() + PurchaseInvoice.objects.count(), 0)   # no money moved
        self.assertEqual(Treasury.objects.get(pk=self.till.pk).balance, Decimal("1000.00"))

    def test_a_new_part_asked_for_step_by_step(self):
        from inventory.models import Product
        steps = [("حط لي منتج في المخزون", "القطعة إيه؟ والعدد كام؟"),
                 ("كويل اشعال", "مش لاقي «كويل اشعال» في الأصناف. أضيفه صنف جديد؟"),
                 ("أيوه", "رقم القطعة إيه؟"),
                 ("12138657273", "سعر البيع كام؟"),
                 ("ستمية", "اشتريت الحتة بكام؟"),
                 ("400", "هتضيف كام حتة؟"),
                 ("4", "صنف جديد: كويل اشعال، رقمه 12138657273، سعر البيع"),
                 ("أيوه", "فيه صنف تاني؟"),
                 ("لا", "إضافة للمخزون: 4 كويل اشعال (صنف جديد)")]
        for said, expected in steps:
            reply, _ = self.say(said)
            self.assertIn(expected, reply, said)
        self.assertFalse(Product.objects.filter(part_number="12138657273").exists())
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        coil = Product.objects.get(part_number="12138657273")
        self.assertEqual((coil.name, coil.retail_price, coil.average_cost), ("كويل اشعال", Decimal("600.00"),
                                                                            Decimal("400.00")))
        self.assertEqual(self.stock(coil), 4)
        self.assertEqual(self._journal().get().journal_type, "opening")   # its first pieces: opening stock

    def test_a_new_parts_number_already_there_is_that_part(self):
        self.say("ضيف منتج جديد")
        self.say("كويل اشعال 2")
        self.say("أيوه")
        reply, payload = self.say("11427953125")                         # فلتر زيت's own number
        self.assertIn("فلتر زيت: عندك منه 5 في الفرع ده. أزود حتتين", reply.replace("2 حتت", "حتتين"))

    def test_no_number_known_its_made_one(self):
        from inventory.models import Product
        for said in ("صنف جديد", "كويل اشعال", "أيوه", "مفيش", "500", "مش عارف", "حتتين", "أيوه", "لا", "هنا"):
            reply, payload = self.say(said)
        self.assertEqual(payload["action"], "entry_done", reply)
        coil = Product.objects.get(name="كويل اشعال")
        self.assertTrue(coil.part_number.startswith("ROBOT-"))
        self.assertEqual(self.stock(coil), 2)

    def test_only_who_may_add_stock(self):
        from hr.models import Employee
        user, _ = make_employee(username="cashier1", role="cashier", branch=self.branch)
        cashier, _ = Employee.objects.get_or_create(user=user)
        reply, payload = self.say("ضيف 3 فلتر زيت للمخزون", employee=cashier)
        self.assertEqual(payload["action"], "denied")
        self.assertIn("إدخال بضاعة", reply)

    # ---- from the shop's log
    def test_cancelled_mid_purchase_for_the_stock_it_begins_that(self):
        make_vendor(name="الأمل")
        self.say("اعمل فاتورة مشتريات")
        reply, payload = self.say("لا اعملها لي، حطها لي في المخزون، الغي الفاتوره واعمل لي ااا حط لي منتج في المخزون.")
        self.assertEqual(payload["action"], "wizard_item", reply)
        self.assertIn("إضافة للمخزون", reply)

    def test_thanks_or_nothing_wanted_ends_it(self):
        for said in ("شكرا", "أنا بقولك شكرا. خلاص. مش عايز حاجة.", "الغي الفاتورة دي"):
            self.say("اعمل فاتورة مشتريات")
            reply, payload = self.say(said)
            self.assertEqual(payload["action"], "wizard_cancelled", said)

    def test_a_sentence_isnt_read_back_as_a_name_and_three_misses_let_go(self):
        self.say("اعمل فاتورة مشتريات")
        reply, _ = self.say("لا أنا عايزك تكلف لي حاجة تانية خالص")
        self.assertNotIn("تكلف", reply)
        self.assertIn("قول اسم المورد بس", reply)
        self.say("مورد مش موجود")
        reply, payload = self.say("ولا ده")
        self.assertEqual(payload["action"], "wizard_cancelled", reply)

    def test_no_thanks_to_another_part_is_still_no(self):
        for said in ("فاتورة مبيعات", "نقدي", "فلتر زيت", "أيوه"):
            self.say(said)
        reply, payload = self.say("لا شكرا")
        self.assertEqual(payload["action"], "wizard_payment", reply)


class PhotoIntakeLedgerTests(_WizardFixture):
    """«إدخال البضاعة بالصورة» (services.intake_part, /intake/): the quantity was written straight
    in — no ledger entry, no average cost, an unexplained «manual edit» movement."""

    def test_more_pieces_of_a_part_go_through_the_books(self):
        from inventory.models import InventoryMovement, JournalEntry
        self.oil.average_cost = Decimal("300.00")
        self.oil.save(update_fields=["average_cost"])
        InventoryMovement.objects.create(product=self.oil, branch=self.branch, reason="adjustment",
                                         quantity_change=5, quantity_before=0, quantity_after=5)
        result = services.intake_part(device=self.device, branch=self.branch, part_number="11427953125",
                                      quantity=2, employee=self.manager)
        self.assertEqual((result["created_new_product"], result["on_hand"]), (False, 7))
        self.assertEqual(self.stock(self.oil), 7)
        moved = InventoryMovement.objects.get(product=self.oil, reference_type="RobotIntake")
        self.assertEqual((moved.quantity_change, moved.quantity_before, moved.quantity_after), (2, 5, 7))
        entry = JournalEntry.objects.get(reference__startswith="RobotIntake")
        self.assertEqual((entry.journal_type, entry.total_debit), ("adjustment", Decimal("600.00")))

    def test_a_new_part_is_made_with_its_pieces(self):
        from inventory.models import InventoryMovement, Product
        result = services.intake_part(device=self.device, branch=self.branch, name="كويل اشعال",
                                      part_number="12138657273", retail_price="600", quantity=3,
                                      employee=self.manager)
        coil = Product.objects.get(part_number="12138657273")
        self.assertTrue(result["created_new_product"])
        self.assertEqual((self.stock(coil), result["on_hand"]), (3, 3))
        self.assertTrue(InventoryMovement.objects.filter(product=coil, reference_type="RobotIntake",
                                                         quantity_change=3).exists())


class WizardChatTests(_WizardFixture):
    """From the shop's log (brain-0.9.9): an invoice left at «اسم العميل إيه؟» took «إزيك»،
    «بقول لك إزيك»، «شكرا يا أبو» for customers' names — «مش لاقي عميل متسجل اسمه إزيك…»."""

    def test_small_talk_is_answered_never_taken_for_a_name(self):
        self.say("فاتورة مبيعات")
        for said in ("إزيك", "بقول لك إزيك؟"):
            reply, payload = self.say(said)
            self.assertNotIn("مش لاقي", reply, said)
            self.assertEqual(reply, "الحمد لله، تمام. اسم العميل إيه؟ ولو نقدي قول «نقدي».", said)
        reply, payload = self.say("معلش")
        self.assertTrue(reply.startswith("ولا يهمك."))
        reply, payload = self.say("شكرا يا أبو")
        self.assertEqual(payload["action"], "wizard_cancelled", reply)

    def test_its_yes_and_no_are_still_its_own(self):
        self.say("فاتورة مبيعات")
        self.say("كريم حسن")
        reply, payload = self.say("تمام")                               # «أكمّلها نقدي باسمه؟» — yes
        self.assertEqual(payload["action"], "wizard_item", reply)
        wizard._end(self.device)
        self.say("فاتورة مبيعات")
        self.say("كريم حسن")
        reply, payload = self.say("لا")                                 # — no: the name again
        self.assertEqual(payload["action"], "wizard_customer", reply)

    def test_names_never_found_let_it_go(self):
        self.say("فاتورة مبيعات")
        for said in ("حد مش موجود", "حد تاني خالص", "واحد غريب"):
            reply, payload = self.say(said)
        self.assertEqual(payload["action"], "wizard_cancelled", reply)

    def test_begun_on_the_phone_its_answered_on_the_phone_only(self):
        views._handle_voice("فاتورة مبيعات", self.device, self.manager, via="phone")
        self.assertTrue(wizard.active(self.device, "phone"))
        self.assertFalse(wizard.active(self.device, "robot"))           # a customer needs «يا موس»
        self.assertIsNone(wizard.answer("إزيك", self.device, None))     # the shop's robot: not its answer
        _intent, reply, payload = views._handle_voice("نقدي", self.device, self.manager, via="phone")
        self.assertEqual(payload["action"], "wizard_item", reply)


class StockFromTheLogTests(_WizardFixture):
    """From the shop's log (brain-0.9.9): «طقم فانوس F45» was taken for «طقم جوان قاعدة مبرد F48
    F45», then «لأ، عايزك تكتب منتج جديد اسمه طقم فانوس» was asked «تزود كام حتة؟» again and again;
    «مبيعات ولا مشتريات؟» — «لا، بس عايزك تضيف لي منتج في المخزون» was asked the same again."""

    def setUp(self):
        super().setUp()
        self.gasket = make_product(name="طقم جوان قاعدة مبرد - F48 F45 - ELRING", part_number="11537643755",
                                   retail_price="900")
        make_inventory(self.gasket, self.branch, quantity=2)

    def test_a_part_that_isnt_what_was_said_is_asked_about(self):
        with mock.patch.object(services, "find_product", return_value=self.gasket):
            reply, payload = self.say("ضيف طقم فانوس اف فورتي فايف للمخزون")
        self.assertEqual(payload["action"], "wizard_which", reply)
        self.assertIn("أقرب حاجة عندي «طقم جوان قاعدة مبرد", reply)
        self.assertIn("ولا أضيف «طقم فانوس F45» صنف جديد؟", reply)
        reply, payload = self.say("جديد")
        self.assertEqual(payload["action"], "wizard_new_number", reply)
        self.assertIn("صنف جديد اسمه طقم فانوس F45", reply)

    def test_its_the_one_after_all(self):
        with mock.patch.object(services, "find_product", return_value=self.gasket):
            self.say("ضيف طقم فانوس اف فورتي فايف للمخزون")
        reply, payload = self.say("أيوه")
        self.assertIn("طقم جوان قاعدة مبرد", reply)
        self.assertIn("تزود كام حتة؟", reply)

    def test_a_new_part_named_anywhere(self):
        with mock.patch.object(services, "find_product", return_value=self.gasket):
            self.say("ضيف طقم فانوس اف فورتي فايف للمخزون")
        reply, payload = self.say("لأ، أنا عايز طقم فانوس. عايزك تكتب منتج جديد اسمه طقم فانوس.")
        self.assertEqual(reply, "تمام، صنف جديد اسمه طقم فانوس. رقم القطعة إيه؟ ولو مش عارفه قول «مفيش».")

    def test_the_count_asked_for_another_part_or_let_go(self):
        self.say("ضيف فلتر زيت للمخزون")                               # no count: asked
        reply, payload = self.say("لا أنا عايز بوجيه")
        self.assertIn("بوجيه", reply)
        wizard._end(self.device)
        self.say("ضيف فلتر زيت للمخزون")
        for said in ("مش عارف", "استنى بقى", "هو المنتج اسمه ايه"):
            reply, payload = self.say(said)
        self.assertNotEqual(payload["action"], "wizard_qty", reply)

    def test_which_one_answered_with_the_stock(self):
        self.say("عايز أعمل فاتورة")
        reply, payload = self.say("لا، بس عايزك تضيف لي منتج في المخزون")
        self.assertIn("إضافة للمخزون", reply)
        self.assertEqual(payload["action"], "wizard_item", reply)


class NewPartFromTheLogTests(_WizardFixture):
    """From the shop's log (brain-0.9.10): «تعرف تضيفلي في مخزون منتج جديد» looked up «تعرف تضيفلي
    مخزون» as the part; «كيف صمت» to «أضيفه صنف جديد؟» became its name, then «شكرا ليك obama» did."""

    def setUp(self):
        super().setUp()
        for name, found in (("find_product", None), ("match_part_in_sentence", (None, []))):
            patcher = mock.patch.object(services, name, return_value=found)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_new_part_with_no_name_said_is_asked_its_name(self):
        reply, payload = self.say("تعرف تضيفلي في مخزون منتج جديد")
        self.assertEqual(reply, "تمام، صنف جديد. اسمه إيه؟")
        reply, payload = self.say("طقم فانوس اف فورتي فايف")
        self.assertEqual(payload["action"], "wizard_new_number", reply)   # no «أضيفه صنف جديد؟» again
        self.assertIn("صنف جديد اسمه طقم فانوس F45", reply)

    def test_thanks_with_an_english_guess_ends_it(self):
        reply, payload = self.say("ضيف طقم فانوس للمخزون")
        self.assertEqual(payload["action"], "wizard_new", reply)
        reply, payload = self.say("شكرا ليك obama")
        self.assertEqual(payload["action"], "wizard_cancelled", reply)
        self.assertIsNone(wizard._get(self.device))

    def test_names_said_again_and_again_are_let_go(self):
        self.say("ضيف طقم فانوس للمخزون")
        for said in ("كيف صمت", "حاجه غريبه"):
            reply, payload = self.say(said)
            self.assertEqual(payload["action"], "wizard_new", reply)
        reply, payload = self.say("كلام تاني خالص")
        self.assertEqual(payload["action"], "wizard_cancelled", reply)
