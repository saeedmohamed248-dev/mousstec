"""Selling by voice («بعت 2 فلتر زيت كاش»), end to end against the ERP's own
posting: the branch question first, then stock out of that branch, the cash
into its treasury — or, on credit, onto a customer registered by name, never
«عميل نقدي». Retail price only. Runs on a tenant schema.
"""

from decimal import Decimal

from inventory.models import Branch, Customer, Inventory, SaleInvoice, Treasury
from inventory.models.customers import WALK_IN_PHONE
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_customer, make_inventory, make_product, make_treasury
from robot import entries, views
from robot.models import RobotDevice, RobotEntry


class VoiceSaleTests(ERPTenantTestCase):

    def setUp(self):
        # The plan allows two branches and two treasuries: reuse, then set up.
        existing = list(Branch.objects.order_by("id")[:2])
        while len(existing) < 2:
            existing.append(make_branch(name=f"فرع {len(existing)}"))
        self.maadi, self.nasr = existing
        for branch, name in ((self.maadi, "المعادي"), (self.nasr, "مدينة نصر")):
            branch.name = name
            branch.save(update_fields=["name"])
        tills = list(Treasury.objects.order_by("id")[:2])
        while len(tills) < 2:
            tills.append(make_treasury(self.maadi, name=f"خزنة {len(tills)}"))
        for till, branch, name in ((tills[0], self.maadi, "كاش المعادي"),
                                   (tills[1], self.nasr, "كاش مدينة نصر")):
            till.branch, till.name, till.type = branch, name, "cash"
            till.balance, till.is_active = Decimal("1000.00"), True
            till.save()
        self.maadi_cash, self.nasr_cash = tills
        self.device = RobotDevice(name="موس", device_uid="esp-sale", branch=self.nasr)
        self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(self.oil, self.maadi, quantity=5)
        make_inventory(self.oil, self.nasr, quantity=1)
        self.ahmed = make_customer(name="أحمد محمود", phone="01012345678")
        self.allowed = True
        self._can = views.permissions.employee_can
        views.permissions.employee_can = lambda employee, action: self.allowed
        self.addCleanup(setattr, views.permissions, "employee_can", self._can)

    def say(self, text):
        return views._handle_voice(text, self.device, None)

    def stock(self, branch):
        return Inventory.objects.get(product=self.oil, branch=branch).quantity

    def till(self, till):
        return Treasury.objects.get(pk=till.pk).balance


class CashTests(VoiceSaleTests):

    def test_a_cash_sale_goes_to_the_branch_that_was_named(self):
        _, reply, payload = self.say("بعت 2 فلتر زيت كاش")
        self.assertEqual(payload["action"], "entry_ask_branch")
        self.assertEqual(reply.split(". ")[0], "تمام: بيع 2 فلتر زيت بإجمالي 900 جنيه كاش")
        self.assertFalse(SaleInvoice.objects.exists())                 # nothing before the branch
        _, reply, payload = self.say("المعادي")
        self.assertEqual(payload["action"], "entry_done", reply)
        invoice = SaleInvoice.objects.get()
        self.assertEqual((invoice.branch, invoice.status, invoice.total_amount, invoice.paid_amount),
                         (self.maadi, "posted", Decimal("900.00"), Decimal("900.00")))
        self.assertTrue(invoice.customer.is_walk_in)
        self.assertEqual((self.stock(self.maadi), self.stock(self.nasr)), (3, 1))
        self.assertEqual((self.till(self.maadi_cash), self.till(self.nasr_cash)),
                         (Decimal("1900.00"), Decimal("1000.00")))
        entry = RobotEntry.objects.get()
        self.assertEqual((entry.kind, entry.status, entry.sale_invoice), ("sale", "done", invoice))

    def test_said_another_way(self):
        for said, qty in (("بيع فلتر زيت", 1), ("سجل بيع اتنين فلتر زيت", 2), ("بعت حتتين فلتر زيت كاش", 2),
                          ("بعنا 3 حتت فلتر زيت", 3)):
            with self.subTest(said):
                sale = entries.parse_sale(said)
                self.assertEqual((sale["quantity"], sale["query"], sale["credit"]), (qty, "فلتر زيت", False))

    def test_count_and_price_said_in_words(self):
        _, reply, _ = self.say("بعت اتنين فلتر زيت بأربعمية وخمسين كاش")
        self.assertEqual(reply.split(". ")[0], "تمام: بيع 2 فلتر زيت بإجمالي 900 جنيه كاش")
        _, reply, payload = self.say("المعادي")
        self.assertEqual(payload["action"], "entry_done", reply)
        self.assertEqual(SaleInvoice.objects.get().total_amount, Decimal("900.00"))
        self.assertEqual((self.stock(self.maadi), self.till(self.maadi_cash)), (3, Decimal("1900.00")))
        # A price said in words is heard too: never sold at retail when 300 was said.
        _, reply, payload = self.say("بعت فلتر زيت بتلتمية كاش")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("بسعر البيع بس", reply)

    def test_a_water_pump_is_not_a_hundred(self):
        pump = make_product(name="طرمبة مية", part_number="16117190946", retail_price="300")
        make_inventory(pump, self.maadi, quantity=2)
        _, reply, _ = self.say("بعت طرمبة مية كاش")
        self.assertEqual(reply.split(". ")[0], "تمام: بيع 1 طرمبة مية بإجمالي 300 جنيه كاش")
        self.say("المعادي")
        self.assertEqual(Inventory.objects.get(product=pump, branch=self.maadi).quantity, 1)
        self.assertEqual(self.till(self.maadi_cash), Decimal("1300.00"))

    def test_not_enough_in_the_named_branch_writes_nothing(self):
        self.say("بعت 2 فلتر زيت كاش")
        _, reply, payload = self.say("هنا")                             # مدينة نصر has one
        self.assertEqual(payload["action"], "entry_failed")
        self.assertEqual(reply, "المتاح في فرع مدينة نصر 1 بس من فلتر زيت، فمسجلتش البيع.")
        self.assertFalse(SaleInvoice.objects.exists())
        self.assertEqual((self.stock(self.nasr), self.till(self.nasr_cash)), (1, Decimal("1000.00")))
        self.assertEqual(RobotEntry.objects.get().status, "failed")

    def test_retail_price_only(self):
        _, reply, payload = self.say("بعت فلتر زيت بسعر 300 كاش")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("بسعر البيع بس", reply)
        self.assertEqual(self.say("بعت فلتر زيت بسعر 450 كاش")[2]["action"], "entry_ask_branch")
        self.oil.retail_price = 0
        self.oil.save()
        self.assertIn("مش متسجل", self.say("بعت فلتر زيت كاش")[1])

    def test_only_who_may_sell(self):
        self.allowed = False
        _, reply, payload = self.say("بعت 2 فلتر زيت كاش")
        self.assertEqual(payload["action"], "denied")
        self.assertFalse(RobotEntry.objects.exists())


class CreditTests(VoiceSaleTests):

    def test_on_credit_to_a_customer_by_name(self):
        _, reply, _ = self.say("بعت لاحمد محمود 2 فلتر زيت آجل")       # «احمد» for «أحمد»
        self.assertEqual(reply.split(". ")[0], "تمام: بيع 2 فلتر زيت لـأحمد محمود بإجمالي 900 جنيه آجل")
        _, reply, payload = self.say("المعادي")
        self.assertEqual(payload["action"], "entry_done", reply)
        invoice = SaleInvoice.objects.get()
        self.assertEqual((invoice.customer, invoice.paid_amount, invoice.total_amount),
                         (self.ahmed, Decimal("0.00"), Decimal("900.00")))
        self.assertEqual(self.till(self.maadi_cash), Decimal("1000.00"))  # no cash came in
        self.assertEqual(self.stock(self.maadi), 3)
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("900.00"))

    def test_the_name_after_the_part_too(self):
        sale = entries.parse_sale("بعت 2 فلتر زيت لأحمد محمود على الحساب")
        self.assertEqual((sale["quantity"], sale["query"], sale["customer"], sale["credit"]),
                         (2, "فلتر زيت", self.ahmed, True))

    def test_never_on_the_walk_in_customer(self):
        make_customer(name="عميل نقدي", phone=WALK_IN_PHONE)
        for said in ("بعت 2 فلتر زيت آجل", "بعت 2 فلتر زيت لعميل نقدي آجل"):
            with self.subTest(said):
                _, reply, payload = self.say(said)
                self.assertEqual(payload["action"], "entry_incomplete")
                self.assertIn("الآجل ممنوع على عميل نقدي", reply)
        self.assertFalse(RobotEntry.objects.exists())
        # Even a request written down some other way can't land on it.
        with self.assertRaises(entries.EntryError):
            entries.record_sale(branch=self.maadi, product_id=self.oil.pk, quantity=1,
                                unit_price=Decimal("450"), customer_id=None, credit=True)
        self.assertFalse(SaleInvoice.objects.exists())

    def test_two_customers_with_the_name_are_asked_apart(self):
        make_customer(name="أحمد محمود", phone="01112345678")
        _, reply, payload = self.say("بعت لأحمد محمود فلتر زيت آجل")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("فيه أكتر من عميل اسمه", reply)

    def test_a_part_starting_with_lam_is_still_a_part(self):
        lamp = make_product(name="لمبة فرامل", part_number="63217160798", retail_price="120")
        make_inventory(lamp, self.maadi, quantity=4)
        sale = entries.parse_sale("بعت 2 لمبة فرامل كاش")
        self.assertEqual((sale["quantity"], sale["query"], sale["customer"]), (2, "لمبة فرامل", None))


class SeveralPartsTests(VoiceSaleTests):
    """«بعت 2 فلتر زيت وطرمبة مية و3 بوجيه كاش»: one invoice, each part at retail."""

    def setUp(self):
        super().setUp()
        self.pump = make_product(name="طرمبة مية", part_number="11517586925", retail_price="300")
        self.plug = make_product(name="بوجيه", part_number="12120037244", retail_price="100")
        make_inventory(self.pump, self.maadi, quantity=2)
        make_inventory(self.plug, self.maadi, quantity=10)

    def held(self, product):
        return Inventory.objects.get(product=product, branch=self.maadi).quantity

    def test_several_parts_on_one_invoice(self):
        _, reply, payload = self.say("بعت 2 فلتر زيت وطرمبة مية و3 بوجيه كاش")
        self.assertEqual(payload["action"], "entry_ask_branch")
        self.assertEqual(reply.split(". ")[0],
                         "تمام: بيع 2 فلتر زيت وطرمبة مية و3 بوجيه بإجمالي 1500 جنيه كاش")
        self.assertFalse(SaleInvoice.objects.exists())                 # nothing before the branch
        _, reply, payload = self.say("المعادي")
        self.assertEqual(payload["action"], "entry_done", reply)
        invoice = SaleInvoice.objects.get()
        self.assertEqual((invoice.branch, invoice.status, invoice.total_amount, invoice.paid_amount),
                         (self.maadi, "posted", Decimal("1500.00"), Decimal("1500.00")))
        self.assertEqual(sorted((i.product.name, i.quantity, i.unit_price) for i in invoice.items.all()),
                         [("بوجيه", 3, Decimal("100.00")), ("طرمبة مية", 1, Decimal("300.00")),
                          ("فلتر زيت", 2, Decimal("450.00"))])
        self.assertEqual((self.held(self.oil), self.held(self.pump), self.held(self.plug)), (3, 1, 7))
        self.assertEqual(self.till(self.maadi_cash), Decimal("2500.00"))
        self.assertEqual(RobotEntry.objects.get().sale_invoice, invoice)

    def test_said_in_words(self):
        _, reply, _ = self.say("بعت اتنين فلتر زيت وتلاتة بوجيه بإجمالي ألف ومية كاش")
        self.assertIn("إجمالي الفاتورة دي 1200 جنيه", reply)          # 900 + 300, not 1100
        _, reply, _ = self.say("بعت اتنين فلتر زيت وتلاتة بوجيه بإجمالي ألف وميتين كاش")
        self.assertEqual(reply.split(". ")[0], "تمام: بيع 2 فلتر زيت و3 بوجيه بإجمالي 1200 جنيه كاش")

    def test_one_part_short_sells_nothing(self):
        self.say("بعت 2 فلتر زيت و3 طرمبة مية كاش")
        _, reply, payload = self.say("المعادي")
        self.assertEqual(payload["action"], "entry_failed")
        self.assertEqual(reply, "المتاح في فرع المعادي 2 بس من طرمبة مية، فمسجلتش البيع.")
        self.assertFalse(SaleInvoice.objects.exists())
        self.assertEqual((self.held(self.oil), self.held(self.pump)), (5, 2))
        self.assertEqual(self.till(self.maadi_cash), Decimal("1000.00"))

    def test_a_part_not_found_is_asked_not_left_out(self):
        _, reply, payload = self.say("بعت فلتر زيت وكاوتش كاش")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertEqual(reply, "مش لاقي «كاوتش» في الأصناف — قول رقم القطعة.")
        self.assertFalse(RobotEntry.objects.exists())

    def test_on_credit_to_a_customer_by_name(self):
        self.say("بعت لأحمد محمود فلتر زيت وطرمبة مية آجل")
        _, reply, payload = self.say("المعادي")
        self.assertEqual(payload["action"], "entry_done", reply)
        invoice = SaleInvoice.objects.get()
        self.assertEqual((invoice.customer, invoice.total_amount, invoice.paid_amount),
                         (self.ahmed, Decimal("750.00"), Decimal("0.00")))
        self.assertEqual(Customer.objects.get(pk=self.ahmed.pk).balance, Decimal("750.00"))
        self.assertEqual(self.till(self.maadi_cash), Decimal("1000.00"))

    def test_never_on_credit_to_the_walk_in_customer(self):
        _, reply, payload = self.say("بعت فلتر زيت وطرمبة مية آجل")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("الآجل ممنوع على عميل نقدي", reply)

    def test_a_price_said_is_the_retail_total(self):
        _, reply, payload = self.say("بعت فلتر زيت وطرمبة مية بإجمالي 700 كاش")
        self.assertEqual(payload["action"], "entry_incomplete")
        self.assertIn("إجمالي الفاتورة دي 750 جنيه", reply)
        self.assertEqual(self.say("بعت فلتر زيت وطرمبة مية بإجمالي 750 كاش")[2]["action"],
                         "entry_ask_branch")

    def test_the_same_part_twice_is_one_line(self):
        _, reply, _ = self.say("بعت فلتر زيت وفلتر زيت كاش")
        self.assertEqual(reply.split(". ")[0], "تمام: بيع 2 فلتر زيت بإجمالي 900 جنيه كاش")

    def test_one_name_with_wa_in_it_is_one_part(self):
        hose = make_product(name="خرطوم وصلة", part_number="11537545260", retail_price="80")
        make_inventory(hose, self.maadi, quantity=3)
        pump = make_product(name="طرمبة بنزين وكالة", part_number="16117373497", retail_price="900")
        make_inventory(pump, self.maadi, quantity=1)
        for said, summary in (("بعت خرطوم وصلة كاش", "تمام: بيع 1 خرطوم وصلة بإجمالي 80 جنيه كاش"),
                              ("بعت طرمبة بنزين وكالة كاش",
                               "تمام: بيع 1 طرمبة بنزين وكالة بإجمالي 900 جنيه كاش")):
            with self.subTest(said):
                self.assertEqual(self.say(said)[1].split(". ")[0], summary)

    def test_one_part_on_two_lines_counts_both(self):
        # Two lines of the same part can't each take the last pieces.
        with self.assertRaises(entries.EntryError):
            entries.record_sale(branch=self.maadi, customer_id=None, credit=False,
                                lines=[(self.pump.pk, 2, Decimal("300")), (self.pump.pk, 1, Decimal("300"))])
        self.assertFalse(SaleInvoice.objects.exists())
        self.assertEqual(self.held(self.pump), 2)
