"""Money by voice besides invoices and expenses (robot/money.py) — a collection from a customer,
a payment to a vendor, an employee's advance — the staff's questions about the books
(robot/assistant.py), and a PIN asked for anything only staff may ask, with no camera
(views._answer_voice). All against the ERP's own services."""

from decimal import Decimal

from inventory.models import Customer, FinancialTransaction, Treasury, Vendor
from inventory.tests.factories import make_customer, make_employee, make_vendor
from robot import assistant, money, views, wizard
from robot.models import RobotEntry
from robot.tests.test_voice_wizard_db import _WizardFixture


class _MoneyFixture(_WizardFixture):

    def setUp(self):
        super().setUp()
        self.vendor = make_vendor(name="الأمل")

    def till_balance(self):
        return Treasury.objects.get(pk=self.till.pk).balance

    def sell_on_credit(self):
        """«أحمد محمود» owes 450 (a filter, on credit), by the wizard itself."""
        for said in ("فاتورة مبيعات", "أحمد محمود", "فلتر زيت", "أيوه", "لا", "آجل", "هنا"):
            reply, payload = self.say(said)
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("450.00"))

    def buy_on_credit(self):
        """The shop owes «الأمل» 450 (3 filters at 150, on the vendor)."""
        reply, payload = self.say("اشترينا 3 فلتر زيت من الأمل بسعر 150")
        self.assertEqual(payload["action"], "entry_ask_branch", reply)
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, Decimal("450.00"))


class ParseTests(_MoneyFixture):

    def test_what_is_and_is_not_a_money_request(self):
        cases = {
            "حصّل 500 جنيه من أحمد محمود": ("collection", Decimal("500"), "احمد محمود"),
            "سجل تحصيل خمسمية جنيه من العميل أحمد محمود": ("collection", Decimal("500"), "احمد محمود"),
            "استلمت 200 من أحمد محمود": ("collection", Decimal("200"), "احمد محمود"),
            "العميل أحمد محمود دفع 300 جنيه": ("collection", Decimal("300"), "احمد محمود"),
            "ادفع 1000 جنيه للمورد الأمل": ("vendor_pay", Decimal("1000"), "الامل"),
            "سدد 250 لمورد الأمل": ("vendor_pay", Decimal("250"), "الامل"),
            "دفعنا للمورد الأمل 400 جنيه": ("vendor_pay", Decimal("400"), "الامل"),
            "سجل سلفة 500 جنيه للموظف محمد علي": ("advance", Decimal("500"), "محمد علي"),
            "سلفة لمحمد علي ألف جنيه": ("advance", Decimal("1000"), "لمحمد علي"),
        }
        for said, (kind, amount, name) in cases.items():
            with self.subTest(said):
                got = money.parse(said)
                self.assertIsNotNone(got)
                self.assertEqual((got["kind"], got["amount"], got["name"]), (kind, amount, name))
        for said in ("دفعنا 200 جنيه كهربا", "سجل مصروف 200 بنزين", "استلمت الطلب من أحمد", "عندك فلتر زيت؟",
                     "صاحبي دفع 500 في الطرمبة", "بعت 2 فلتر زيت كاش", "اعملي فاتورة مبيعات"):
            with self.subTest(said):
                self.assertIsNone(money.parse(said))

    def test_an_expense_is_still_an_expense(self):
        reply, payload = self.say("دفعنا 200 جنيه كهربا")
        self.assertEqual((payload["action"], payload.get("kind")), ("entry_ask_branch", "expense"), reply)


class CollectionTests(_MoneyFixture):

    def test_a_collection_pays_the_customers_invoice_into_the_branch_till(self):
        self.sell_on_credit()
        before = self.till_balance()
        reply, payload = self.say("حصّل 200 جنيه من أحمد محمود")
        self.assertEqual((payload["action"], payload["kind"]), ("entry_ask_branch", "collection"), reply)
        self.assertIn("تحصيل 200 جنيه من أحمد محمود (عليه 450 جنيه)", reply)
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("450.00"))   # nothing yet
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("250.00"))
        self.assertEqual(self.till_balance(), before + Decimal("200.00"))
        paid = FinancialTransaction.objects.filter(customer=self.ahmed, transaction_type="in").latest("id")
        self.assertEqual(paid.amount, Decimal("200.00"))
        self.assertIsNotNone(paid.sale_invoice_id)                     # on their open invoice, as the screen does
        self.assertEqual(RobotEntry.objects.filter(kind="collection", status="done").count(), 1)

    def test_never_more_than_they_owe_never_a_stranger_never_walk_in(self):
        self.sell_on_credit()
        reply, payload = self.say("حصّل 5000 جنيه من أحمد محمود")       # a zero too many, heard
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("عليه 450 جنيه بس", reply)
        reply, payload = self.say("حصّل 100 جنيه من كريم حسن")
        self.assertIn("مش لاقي العميل", reply)
        reply, payload = self.say("حصّل 100 جنيه من عميل نقدي")
        self.assertEqual(payload["action"], "entry_incomplete", reply)
        make_customer(name="منى سعيد", phone="01055555555")
        reply, payload = self.say("حصّل 100 جنيه من منى سعيد")
        self.assertIn("مش عليه حاجة", reply)
        self.assertFalse(RobotEntry.objects.filter(kind="collection").exists())
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("450.00"))

    def test_cancelled_at_the_branch_question_nothing_moves(self):
        self.sell_on_credit()
        before = self.till_balance()
        self.say("حصّل 200 جنيه من أحمد محمود")
        reply, payload = self.say("الغي")
        self.assertEqual(payload["action"], "entry_cancelled", reply)
        self.assertEqual((self.till_balance(), Customer.objects.get(pk=self.ahmed.pk).balance),
                         (before, Decimal("450.00")))

    def test_only_who_may(self):
        from hr.models import Employee
        user, _ = make_employee(username="tech1", role="tech", branch=self.branch)
        tech, _ = Employee.objects.get_or_create(user=user)
        self.sell_on_credit()
        reply, payload = self.say("حصّل 200 جنيه من أحمد محمود", employee=tech)
        self.assertEqual(payload["action"], "denied")
        self.assertIn("التحصيل من عميل", reply)


class VendorPaymentTests(_MoneyFixture):

    def test_a_payment_leaves_the_till_and_the_vendors_account(self):
        self.buy_on_credit()
        before = self.till_balance()
        reply, payload = self.say("ادفع 200 جنيه للمورد الأمل")
        self.assertEqual((payload["action"], payload["kind"]), ("entry_ask_branch", "vendor_pay"), reply)
        self.assertIn("دفعة 200 جنيه للمورد الأمل (علينا له 450 جنيه)", reply)
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, Decimal("250.00"))
        self.assertEqual(self.till_balance(), before - Decimal("200.00"))

    def test_never_more_than_is_owed_nor_than_the_till_has(self):
        reply, payload = self.say("ادفع 200 جنيه للمورد الأمل")           # nothing owed yet
        self.assertIn("مفيش حاجة علينا للمورد الأمل", reply)
        self.buy_on_credit()
        reply, payload = self.say("ادفع 2000 جنيه للمورد الأمل")
        self.assertIn("450 جنيه بس", reply)
        Treasury.objects.filter(pk=self.till.pk).update(balance=Decimal("50.00"))
        self.say("ادفع 200 جنيه للمورد الأمل")
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_failed", reply)
        self.assertIn("فمسجلتش حاجة", reply)
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, Decimal("450.00"))
        self.assertEqual(self.till_balance(), Decimal("50.00"))


class AdvanceTests(_MoneyFixture):

    def setUp(self):
        from hr.models import Employee
        super().setUp()
        user, _ = make_employee(username="mohamed", role="tech", branch=self.branch,
                                first_name="محمد", last_name="علي")
        self.mohamed, _ = Employee.objects.get_or_create(user=user)
        Employee.objects.filter(pk=self.mohamed.pk).update(base_salary=Decimal("6000.00"))

    def test_an_hr_manager_asks_approved_and_paid_from_the_till(self):
        from hr.models import Advance, Employee
        Employee.objects.filter(pk=self.manager.pk).update(is_hr_manager=True)
        self.manager.refresh_from_db()
        before = self.till_balance()
        reply, payload = self.say("سجل سلفة 500 جنيه للموظف محمد علي")
        self.assertEqual((payload["action"], payload["kind"]), ("entry_ask_branch", "advance"), reply)
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        advance = Advance.objects.get(employee=self.mohamed)
        self.assertEqual(advance.status, "approved")
        self.assertIsNotNone(advance.disbursed_at)
        self.assertEqual(self.till_balance(), before - Decimal("500.00"))

    def test_anyone_else_it_is_a_request_and_no_money_moves(self):
        from hr.models import Advance, Employee
        user, _ = make_employee(username="acc1", role="accountant", branch=self.branch)
        accountant, _ = Employee.objects.get_or_create(user=user)
        before = self.till_balance()
        self.say("سجل سلفة 500 جنيه للموظف محمد علي", employee=accountant)
        reply, payload = self.say("هنا", employee=accountant)
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertIn("مستني موافقة", reply)
        self.assertEqual(Advance.objects.get(employee=self.mohamed).status, "pending")
        self.assertEqual(self.till_balance(), before)

    def test_hr_policy_is_the_erps_own(self):
        from hr.models import Advance, Employee
        Employee.objects.filter(pk=self.manager.pk).update(is_hr_manager=True)
        self.manager.refresh_from_db()
        before = self.till_balance()
        self.say("سجل سلفة 50000 جنيه للموظف محمد علي")                   # far over the policy's share of a salary
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_failed", reply)
        self.assertFalse(Advance.objects.exists())
        self.assertEqual(self.till_balance(), before)

    def test_the_till_short_nothing_is_left_half_done(self):
        from hr.models import Advance, Employee
        Employee.objects.filter(pk=self.manager.pk).update(is_hr_manager=True)
        self.manager.refresh_from_db()
        Treasury.objects.filter(pk=self.till.pk).update(balance=Decimal("100.00"))
        self.say("سجل سلفة 500 جنيه للموظف محمد علي")
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_failed", reply)
        self.assertFalse(Advance.objects.exists())                       # not approved-and-unpaid
        self.assertEqual(self.till_balance(), Decimal("100.00"))

    def test_a_first_name_two_have_is_asked_whole(self):
        make_employee(username="mohamed2", role="tech", branch=self.branch, first_name="محمد", last_name="حسن")
        from hr.models import Employee
        from django.contrib.auth import get_user_model
        Employee.objects.get_or_create(user=get_user_model().objects.get(username="mohamed2"))
        reply, payload = self.say("سجل سلفة 500 جنيه للموظف محمد")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("أكتر من واحد", reply)


class StaffQuestionTests(_MoneyFixture):

    def test_the_safe(self):
        reply, payload = self.say("رصيد الخزنة كام؟")
        self.assertEqual(payload["action"], "treasury", reply)
        self.assertIn("1000 جنيه", reply)
        for said in ("الخزنة فيها كام", "في الخزنة كام", "كام في الخزنه"):
            self.assertEqual(assistant.staff_question(said), "treasury", said)

    def test_who_owes_what(self):
        self.sell_on_credit()
        self.buy_on_credit()
        for said in ("أحمد محمود عليه كام؟", "حساب العميل أحمد محمود", "كام على العميل أحمد محمود"):
            reply, payload = self.say(said)
            self.assertEqual(payload["action"], "customer_balance", said)
            self.assertIn("أحمد محمود عليه 450 جنيه", reply)
        for said in ("حساب المورد الأمل", "علينا كام للمورد الأمل", "المورد الأمل ليه كام"):
            reply, payload = self.say(said)
            self.assertEqual(payload["action"], "vendor_balance", said)
            self.assertIn("علينا للمورد الأمل 450 جنيه", reply)

    def test_a_part_is_not_a_customer(self):
        self.assertIsNone(assistant.staff_question("الفلتر عليه كام"))
        self.assertIsNone(assistant.staff_question("مصاريف 200 بنزين النهارده"))
        self.assertIsNone(assistant.staff_question("عندك فلتر زيت"))

    def test_what_was_spent_and_sold(self):
        self.say("سجل مصروف 200 جنيه بنزين")
        self.say("هنا")
        reply, payload = self.say("صرفنا كام النهارده؟")
        self.assertEqual(payload["action"], "spent_today", reply)
        self.assertIn("200 جنيه", reply)
        self.sell_on_credit()
        reply, payload = self.say("مبيعات الشهر ده كام؟")
        self.assertEqual(payload["action"], "sales_month", reply)
        self.assertIn("1 فاتورة بـ 450 جنيه", reply)
        reply, payload = self.say("مبيعات امبارح")
        self.assertEqual(payload["action"], "sales_yesterday", reply)
        self.assertIn("مفيش مبيعات امبارح", reply)

    def test_who_is_in(self):
        from django.utils import timezone
        from hr.models import AttendanceRecord
        reply, payload = self.say("مين حاضر النهارده؟")
        self.assertEqual((payload["action"], payload["count"]), ("present", 0), reply)
        AttendanceRecord.objects.create(employee=self.manager, date=timezone.localdate(),
                                        clock_in=timezone.now(), status="present")
        reply, payload = self.say("مين حاضر النهارده؟")
        self.assertEqual(payload["count"], 1, reply)
        self.assertIn("سعيد", reply)
        reply, payload = self.say("مين غايب النهارده؟")
        self.assertEqual(payload["action"], "absent", reply)
        self.assertNotIn("سعيد", reply)


class PinForAnythingTests(_MoneyFixture):
    """No camera: whoever asks for what only staff may is asked their PIN — and it's done."""

    def test_an_expense_said_at_once_asks_the_pin_then_goes_on(self):
        reply, payload = self.say("سجل مصروف 200 جنيه بنزين", employee=None)
        self.assertEqual(payload["action"], "wizard_pin", reply)
        self.assertTrue(wizard.wants_secret(self.device))                # what's said next is never kept
        reply, payload = self.say("اتنين اربعة ستة تمانية", employee=None)
        self.assertEqual((payload["action"], payload.get("kind")), ("entry_ask_branch", "expense"), reply)
        self.assertFalse(wizard.wants_secret(self.device))
        reply, payload = self.say("هنا", employee=None)
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(self.till_balance(), Decimal("800.00"))

    def test_a_staff_question_and_then_no_pin_again_for_a_while(self):
        reply, payload = self.say("رصيد الخزنة كام؟", employee=None)
        self.assertEqual(payload["action"], "wizard_pin", reply)
        reply, payload = self.say("2468", employee=None)
        self.assertEqual(payload["action"], "treasury", reply)
        reply, payload = self.say("صرفنا كام النهارده؟", employee=None)   # the same person, a moment later
        self.assertEqual(payload["action"], "spent_today", reply)

    def test_a_wrong_pin_does_nothing_and_three_end_it(self):
        self.say("سجل مصروف 200 جنيه بنزين", employee=None)
        for _ in range(2):
            reply, payload = self.say("1111", employee=None)
            self.assertEqual(payload["action"], "wizard_pin", reply)
        reply, payload = self.say("1111", employee=None)
        self.assertEqual(payload["action"], "wizard_pin_failed", reply)
        self.assertFalse(RobotEntry.objects.exists())
        self.assertEqual(self.till_balance(), Decimal("1000.00"))

    def test_the_pins_owner_without_the_role_is_refused(self):
        from hr.models import Employee
        from robot.brain import pins
        from robot.models import RobotStaffPin
        user, _ = make_employee(username="tech9", role="tech", branch=self.branch)
        tech, _ = Employee.objects.get_or_create(user=user)
        RobotStaffPin.objects.create(employee=tech, pin_hash=pins.hash_pin("1357", iterations=1000))
        self.say("رصيد الخزنة كام؟", employee=None)
        reply, payload = self.say("1357", employee=None)
        self.assertEqual(payload["action"], "denied", reply)
        self.assertIn("رصيد الخزنة", reply)
        reply, payload = self.say("رصيد الخزنة كام؟", employee=None)      # known now: refused, not asked again
        self.assertEqual(payload["action"], "denied", reply)

    def test_a_customers_question_never_asks_a_pin(self):
        reply, payload = self.say("عندك فلتر زيت؟", employee=None)
        self.assertNotEqual(payload.get("action"), "wizard_pin", reply)
        self.assertIn("فلتر زيت", reply)

    def test_the_whole_turn_through_the_view_keeps_no_pin(self):
        from rest_framework.test import APIRequestFactory
        from robot.models import RobotVoiceInteraction

        def post(text):
            request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": text}, format="json",
                                               HTTP_X_ROBOT_TOKEN=self.token)
            return views.voice(request).data

        with self.settings(GEMINI_API_KEY=""):
            body = post("يا موس رصيد الخزنة كام")
            self.assertEqual(body.get("action"), "wizard_pin", body)
            body = post("اتنين اربعة ستة تمانية")
            self.assertEqual(body.get("action"), "treasury", body)
        self.assertFalse(RobotVoiceInteraction.objects.filter(transcript__contains="تمانية").exists())


class ADayInTheShopTests(_MoneyFixture):
    """One day, said to the robot through /voice/ itself, no camera and no AI: a customer's
    question, then the staff's whole day — and the books after it."""

    def post(self, text):
        from rest_framework.test import APIRequestFactory
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": text}, format="json",
                                           HTTP_X_ROBOT_TOKEN=self.token)
        return views.voice(request).data

    def test_the_day(self):
        from inventory.models import SaleInvoice
        with self.settings(GEMINI_API_KEY="", OPENAI_API_KEY="", TOGETHER_API_KEY=""):
            day = [
                # a customer
                ("يا موس السلام عليكم", None, "وعليكم السلام"),
                ("عندك فلتر زيت؟", None, "فلتر زيت"),
                ("شكرا", None, ""),
                # the cashier: a credit sale, by PIN
                ("يا موس اعملي فاتورة مبيعات", "wizard_pin", "الرقم السري"),
                ("اتنين اربعة ستة تمانية", "wizard_customer", "اسم العميل"),
                ("أحمد محمود", "wizard_item", "القطعة إيه"),
                ("فلتر زيت", "wizard_confirm", "450"),
                ("أيوه", "wizard_more", "صنف تاني"),
                ("لا", "wizard_payment", "كاش ولا آجل"),
                ("آجل", "entry_ask_branch", "أحمد محمود"),
                ("هنا", "entry_done", "سجلت"),
                # an expense at once, a collection, the books — the PIN is remembered
                ("يا موس سجل مصروف 100 جنيه شاي", "entry_ask_branch", "مصروف 100 جنيه"),
                ("هنا", "entry_done", "سجلت"),
                ("يا موس حصل 150 جنيه من أحمد محمود", "entry_ask_branch", "تحصيل 150 جنيه"),
                ("هنا", "entry_done", "سجلت"),
                ("يا موس أحمد محمود عليه كام", "customer_balance", "عليه 300 جنيه"),
                ("يا موس رصيد الخزنة كام", "treasury", "1050 جنيه"),
                ("يا موس صرفنا كام النهارده", "spent_today", "100 جنيه"),
                ("يا موس بعنا كام النهارده", "sales_today", "1 فاتورة بـ 450 جنيه"),
            ]
            for said, action, words in day:
                body = self.post(said)
                self.assertTrue(body.get("addressed"), (said, body))
                if action is not None:
                    self.assertEqual(body.get("action"), action, (said, body.get("reply")))
                self.assertIn(words, body.get("reply", ""), said)
        invoice = SaleInvoice.objects.get()
        self.assertEqual((invoice.customer_id, invoice.total_amount), (self.ahmed.pk, Decimal("450.00")))
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("300.00"))
        self.assertEqual(self.till_balance(), Decimal("1050.00"))          # 1000 − 100 + 150
        self.assertEqual(self.stock(self.oil), 4)


class PinLetGoTests(_MoneyFixture):

    def test_something_else_said_at_the_pin_is_answered_not_held(self):
        reply, payload = self.say("رصيد الخزنة كام؟", employee=None)
        self.assertEqual(payload["action"], "wizard_pin", reply)
        reply, payload = self.say("عندك فلتر زيت؟", employee=None)        # the next customer
        self.assertIn("فلتر زيت", reply)
        self.assertNotEqual(payload.get("action"), "wizard_pin")
        self.assertFalse(wizard.wants_secret(self.device))


class QuoteTests(_MoneyFixture):

    def test_a_quote_is_a_draft_nothing_leaves_the_shelf_or_the_till(self):
        from inventory.models import SaleInvoice
        before = self.till_balance()
        for said, action in (("اعملي عرض سعر", "wizard_customer"), ("أحمد محمود", "wizard_item"),
                             ("2 فلتر زيت", "wizard_confirm"), ("أيوه", "wizard_more")):
            reply, payload = self.say(said)
            self.assertEqual(payload["action"], action, reply)
        reply, payload = self.say("لا")                                   # no «كاش ولا آجل؟»: no money in a quote
        self.assertEqual((payload["action"], payload["kind"]), ("entry_ask_branch", "quote"), reply)
        self.assertIn("عرض سعر 2 فلتر زيت لـأحمد محمود بإجمالي 900 جنيه", reply)
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        invoice = SaleInvoice.objects.get()
        self.assertIn(f"عرض سعر رقم {invoice.pk}", reply)
        self.assertEqual((invoice.status, invoice.total_amount, invoice.customer_id),
                         ("quotation", Decimal("900.00"), self.ahmed.pk))
        self.assertEqual((self.stock(self.oil), self.till_balance()), (5, before))
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("0.00"))


class ReturnTests(_MoneyFixture):

    def sell_cash(self, said="بعت 2 فلتر زيت كاش"):
        from inventory.models import SaleInvoice
        self.say(said)
        reply, payload = self.say("هنا")
        self.assertEqual(payload["action"], "entry_done", reply)
        return SaleInvoice.objects.filter(is_return=False).latest("id")

    def test_a_piece_back_the_cash_out_the_stock_in(self):
        from inventory.models import SaleInvoice
        invoice = self.sell_cash()
        self.assertEqual((self.stock(self.oil), self.till_balance()), (3, Decimal("1900.00")))
        reply, payload = self.say(f"رجّع 1 فلتر زيت من فاتورة رقم {invoice.pk}")
        self.assertEqual((payload["action"], payload["kind"]), ("entry_confirm", "return"), reply)
        self.assertIn(f"مرتجع فاتورة {invoice.pk}", reply)
        self.assertEqual(self.stock(self.oil), 3)                         # nothing before «أيوه»
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertIn("رد للعميل 450 جنيه كاش", reply)
        self.assertEqual((self.stock(self.oil), self.till_balance()), (4, Decimal("1450.00")))
        ret = SaleInvoice.objects.get(is_return=True)
        self.assertEqual((ret.original_invoice_id, ret.status), (invoice.pk, "posted"))
        # the rest of it, then nothing's left
        self.say(f"رجّع فاتورة رقم {invoice.pk}")
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual((self.stock(self.oil), self.till_balance()), (5, Decimal("1000.00")))
        reply, payload = self.say(f"رجّع فاتورة رقم {invoice.pk}")
        self.assertIn("اترجعت كلها", reply)

    def test_on_credit_nothing_leaves_the_till_the_account_goes_down(self):
        self.sell_on_credit()
        from inventory.models import SaleInvoice
        invoice = SaleInvoice.objects.get()
        before = self.till_balance()
        self.say(f"رجع فاتورة رقم {invoice.pk}")
        reply, payload = self.say("ايوه")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(self.till_balance(), before)
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("0.00"))
        self.assertEqual(self.stock(self.oil), 5)

    def test_what_cannot_be_returned(self):
        invoice = self.sell_cash()
        for said, words in ((f"رجّع 5 فلتر زيت من فاتورة رقم {invoice.pk}", "2 بس"),
                            (f"رجّع بوجيه من فاتورة رقم {invoice.pk}", "مش لاقي «بوجيه»"),
                            ("رجّع فاتورة رقم 999999", "مش لاقي فاتورة"),
                            ("اعمل مرتجع", "قول رقم الفاتورة")):
            reply, payload = self.say(said)
            self.assertEqual(payload["action"], "entry_incomplete", said)
            self.assertIn(words, reply, said)
        self.assertIsNone(money.parse("رجع الصوت زي ما كان"))
        self.assertIsNone(money.parse("ارجع بكره"))

    def test_the_till_short_or_a_no_nothing_is_returned(self):
        invoice = self.sell_cash()
        self.say(f"رجّع فاتورة رقم {invoice.pk}")
        reply, payload = self.say("لا")
        self.assertEqual(payload["action"], "entry_cancelled", reply)
        Treasury.objects.filter(pk=self.till.pk).update(balance=Decimal("10.00"))
        self.say(f"رجّع فاتورة رقم {invoice.pk}")
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_failed", reply)
        from inventory.models import SaleInvoice
        self.assertFalse(SaleInvoice.objects.filter(is_return=True).exists())
        self.assertEqual((self.stock(self.oil), self.till_balance()), (3, Decimal("10.00")))


class TransferTests(_MoneyFixture):

    def setUp(self):
        from inventory.tests.factories import make_treasury
        super().setUp()
        Treasury.objects.filter(pk=self.till.pk).update(name="خزنة الكاش")
        self.bank = make_treasury(self.branch, name="حساب البنك", balance="0.00", type="bank")

    def test_between_the_branchs_treasuries(self):
        reply, payload = self.say("حوّل 300 جنيه من خزنة الكاش لخزنة البنك")
        self.assertEqual((payload["action"], payload["kind"]), ("entry_confirm", "transfer"), reply)
        self.assertIn("تحويل 300 جنيه من خزنة الكاش إلى حساب البنك", reply)
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual((self.till_balance(), Treasury.objects.get(pk=self.bank.pk).balance),
                         (Decimal("700.00"), Decimal("300.00")))
        # a transfer isn't an expense
        reply, payload = self.say("صرفنا كام النهارده؟")
        self.assertIn("مفيش مصاريف", reply)

    def test_never_more_than_is_there_and_both_named(self):
        reply, payload = self.say("حول 5000 جنيه من خزنة الكاش للبنك")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("1000 جنيه بس", reply)
        reply, payload = self.say("حول 100 جنيه من خزنة الكاش لخزنة المريخ")
        self.assertIn("مش عارف أنهي خزنة", reply)
        reply, payload = self.say("حول 100 جنيه")
        self.assertIn("قولها كاملة", reply)
        self.assertEqual(self.till_balance(), Decimal("1000.00"))


class PayrollTests(_MoneyFixture):

    def setUp(self):
        from hr.models import Employee
        super().setUp()
        Employee.objects.filter(pk=self.manager.pk).update(base_salary=Decimal("300.00"))
        from django.utils import timezone
        self.month = timezone.localdate().month

    def test_calculated_then_paid_on_a_yes(self):
        from hr.models import PayrollRun
        reply, payload = self.say("احسب مرتبات الشهر ده")
        self.assertEqual(payload["action"], "payroll_calculated", reply)
        self.assertIn("مفيش فلوس خرجت", reply)
        run = PayrollRun.objects.get()
        self.assertEqual((run.status, run.period_month), ("calculated", self.month))
        self.assertEqual(self.till_balance(), Decimal("1000.00"))
        reply, payload = self.say(f"اصرف مرتبات شهر {self.month}")
        self.assertEqual((payload["action"], payload["kind"]), ("entry_confirm", "payroll"), reply)
        self.assertEqual(PayrollRun.objects.get().status, "calculated")   # nothing before «أيوه»
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_done", reply)
        run.refresh_from_db()
        self.assertEqual(run.status, "paid")
        self.assertEqual(self.till_balance(), Decimal("1000.00") - run.total_net)
        reply, payload = self.say(f"اصرف مرتبات شهر {self.month}")
        self.assertIn("اتصرفت قبل كده", reply)

    def test_not_calculated_yet_or_not_a_manager(self):
        from hr.models import Employee, PayrollRun
        reply, payload = self.say("اصرف المرتبات")
        self.assertIn("لسه متحسبتش", reply)
        user, _ = make_employee(username="acc7", role="accountant", branch=self.branch)
        accountant, _ = Employee.objects.get_or_create(user=user)
        reply, payload = self.say("احسب مرتبات الشهر ده", employee=accountant)
        self.assertEqual(payload["action"], "payroll_calculated", reply)
        reply, payload = self.say("اصرف المرتبات", employee=accountant)   # keeps them; doesn't pay them
        self.assertEqual(payload["action"], "denied", reply)
        self.assertEqual(PayrollRun.objects.get().status, "calculated")

    def test_no_treasury_covers_them_none_is_paid(self):
        from hr.models import PayrollRun
        self.say("احسب مرتبات الشهر ده")
        Treasury.objects.all().update(balance=Decimal("1.00"))
        self.say("اصرف المرتبات")
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_failed", reply)
        self.assertEqual(PayrollRun.objects.get().status, "calculated")   # not left approved-and-unpaid
        self.assertFalse(FinancialTransaction.objects.filter(description__startswith="راتب").exists())


class PurchaseReturnTests(_MoneyFixture):

    def test_back_to_the_vendor_stock_out_and_off_their_account(self):
        from inventory.models import PurchaseInvoice
        self.buy_on_credit()                                              # 3 filters at 150: 450 owed
        invoice = PurchaseInvoice.objects.get()
        self.assertEqual(self.stock(self.oil), 8)
        reply, payload = self.say(f"رجّع 1 فلتر زيت من فاتورة مشتريات رقم {invoice.pk}")
        self.assertEqual((payload["action"], payload["kind"]), ("entry_confirm", "pur_return"), reply)
        self.assertIn(f"مرتجع مشتريات للمورد الأمل من فاتورة {invoice.pk}: 1 فلتر زيت", reply)
        self.assertEqual(self.stock(self.oil), 8)                          # nothing before «أيوه»
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertIn("150 جنيه اتخصمت من حساب المورد", reply)
        self.assertEqual((self.stock(self.oil), Vendor.objects.get(pk=self.vendor.pk).balance),
                         (7, Decimal("300.00")))
        self.assertEqual(self.till_balance(), Decimal("1000.00"))          # on account: no cash moves
        # the rest of it, then nothing's left
        self.say(f"رجع فاتورة مشتريات رقم {invoice.pk}")
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual((self.stock(self.oil), Vendor.objects.get(pk=self.vendor.pk).balance),
                         (5, Decimal("0.00")))
        reply, payload = self.say(f"رجع فاتورة مشتريات رقم {invoice.pk}")
        self.assertIn("اترجعت كلها", reply)

    def test_a_sale_return_is_not_a_purchase_return(self):
        self.assertEqual(money.parse("رجع فاتورة رقم 5")["kind"], "return")
        self.assertEqual(money.parse("رجع فاتورة مشتريات رقم 5")["kind"], "pur_return")
        self.assertEqual(money.parse("رجع 2 فلتر زيت للمورد من فاتورة رقم 5")["kind"], "pur_return")
        self.assertEqual(money.parse("رجع فاتورة مبيعات رقم 5")["invoice"], 5)

    def test_more_than_the_shelf_has_is_the_erps_refusal(self):
        from inventory.models import Inventory, PurchaseInvoice
        self.buy_on_credit()
        invoice = PurchaseInvoice.objects.get()
        Inventory.objects.filter(product=self.oil, branch=self.branch).update(quantity=1)   # sold meanwhile
        self.say(f"رجع فاتورة مشتريات رقم {invoice.pk}")
        reply, payload = self.say("أيوه")
        self.assertEqual(payload["action"], "entry_failed", reply)
        self.assertEqual((self.stock(self.oil), Vendor.objects.get(pk=self.vendor.pk).balance),
                         (1, Decimal("450.00")))


class AttendanceVoiceTests(_MoneyFixture):

    def test_in_then_out_ones_own_by_pin(self):
        from hr.models import AttendanceRecord, HRSettings
        settings = HRSettings.get_settings()
        settings.require_face_verification = settings.require_location = False
        settings.save()
        reply, payload = self.say("سجل حضوري", employee=None)
        self.assertEqual(payload["action"], "wizard_pin", reply)           # who are you?
        reply, payload = self.say("2468", employee=None)
        self.assertEqual(payload["action"], "clock_in", reply)
        self.assertIn("يا سعيد، سجلت حضورك الساعة", reply)
        record = AttendanceRecord.objects.get(employee=self.manager)
        self.assertIsNotNone(record.clock_in)
        reply, payload = self.say("سجل حضوري")
        self.assertEqual(payload["action"], "attendance_refused", reply)   # once a day: HR's rule, said
        reply, payload = self.say("سجل انصرافي")
        self.assertEqual(payload["action"], "clock_out", reply)
        record.refresh_from_db()
        self.assertIsNotNone(record.clock_out)

    def test_hr_wants_the_face_a_pin_is_not_one(self):
        from hr.models import AttendanceRecord, HRSettings
        settings = HRSettings.get_settings()
        settings.require_face_verification, settings.require_location = True, False
        settings.save()
        self.say("سجل حضوري", employee=None)
        reply, payload = self.say("2468", employee=None)
        self.assertEqual(payload["action"], "attendance_refused", reply)
        self.assertFalse(AttendanceRecord.objects.filter(clock_in__isnull=False).exists())

    def test_what_customers_say_is_not_a_clock(self):
        for said in ("ماشي", "أنا ماشي", "أنا جيت", "الحضور بكام", "عندك فلتر زيت"):
            self.assertIsNone(assistant.attendance_command(said), said)
        self.assertEqual(assistant.attendance_command("سجل انصرافي"), "out")
        self.assertEqual(assistant.attendance_command("يا ريت سجل لي حضوري"), "in")


class DaySummaryTests(_MoneyFixture):

    def test_the_day_in_one_sentence_and_what_needs_them_once(self):
        self.sell_on_credit()
        self.say("حصّل 150 جنيه من أحمد محمود")
        self.say("هنا")
        self.say("سجل مصروف 100 جنيه شاي")
        self.say("هنا")
        from inventory.models import Inventory
        Inventory.objects.filter(product=self.plug, branch=self.branch).update(quantity=0)   # running low
        reply, payload = self.say("قفّل اليوم")
        self.assertEqual(payload["action"], "day_summary", reply)
        for words in ("اتباع 1 فاتورة بـ 450 جنيه", "المصاريف 100 جنيه", "اتحصل من العملاء 150 جنيه",
                      "الخزنة فيها دلوقتي 1050 جنيه"):
            self.assertIn(words, reply)
        self.assertTrue(payload.get("heads_up"), reply)
        self.assertIn("على فكرة، فيه", reply)
        self.assertIn("صنف قرّب يخلص", reply)
        reply, payload = self.say("ملخص اليوم")                            # told today: not again
        self.assertEqual(payload["action"], "day_summary")
        self.assertNotIn("على فكرة", reply)

    def test_customers_owing_for_over_a_month(self):
        from datetime import timedelta
        from django.utils import timezone
        from inventory.models import SaleInvoice
        self.sell_on_credit()
        SaleInvoice.objects.update(date_created=timezone.now() - timedelta(days=45))
        self.assertEqual(assistant._overdue_customers(self.branch), 1)
        reply, payload = self.say("بعنا كام النهارده؟")
        self.assertIn("فيه 1 عميل عليهم فلوس بقالها أكتر من شهر", reply)


class FinetuneExportTests(_MoneyFixture):

    def test_only_what_was_understood_for_sure_as_chat_examples(self):
        import json
        from robot import understand
        from robot.models import RobotUnderstanding
        common = dict(device=self.device, source="gemini", meant="عندك فلتر زيت", needs_context=False)
        RobotUnderstanding.objects.create(heard="عندك فلترا الزي", teacher_text="عندك فلتر زيت", intent="part",
                                          command="عندك فلتر زيت؟", confidence=0.95, **common)
        RobotUnderstanding.objects.create(heard="هم هم", intent="unclear", command="", confidence=0.9, **common)
        RobotUnderstanding.objects.create(heard="يمكن فلتر", intent="part", command="عندك فلتر؟", confidence=0.4,
                                          **common)
        (row,) = list(understand.finetune_rows())
        roles = [m["role"] for m in row["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant"])
        self.assertEqual(json.loads(row["messages"][1]["content"])["hearings"], ["عندك فلترا الزي", "عندك فلتر زيت"])
        self.assertEqual(json.loads(row["messages"][2]["content"])["command"], "عندك فلتر زيت؟")
