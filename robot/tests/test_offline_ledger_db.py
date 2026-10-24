"""The counter page with the net down, end to end against the ERP's books.

A cash sale and an expense recorded on «مخ موس»'s counter page reach the
server through /sync/push/ and are written by the ERP's own pipeline: the
invoice posted at the retail price the customer paid, the cash in the
branch's treasury, the stock out; the expense paid from that treasury. Sent
twice, booked once. What the books can't take any more — the part was sold
online meanwhile, the role changed, the treasury can't cover it — is kept for
the manager with the reason, never forced and never lost. Runs on a tenant
schema.
"""

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import Branch, FinancialTransaction, Inventory, SaleInvoice, Treasury
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_employee, make_inventory, make_product, make_treasury
from robot import services
from robot.brain import pins
from robot.brain.counter import Counter
from robot.brain.knowledge import Knowledge
from robot.brain.ledger import Ledger
from robot.brain.outbox import Outbox
from robot.models import RobotAlert, RobotDevice, RobotEntry, RobotStaffPin


@override_settings(ALLOWED_HOSTS=["*"])
class OfflineLedgerTests(ERPTenantTestCase):

    def setUp(self):
        from hr.models import Employee

        connection.set_tenant(self.tenant)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        till = Treasury.objects.order_by("id").first() or make_treasury(self.branch)
        till.branch, till.type, till.is_active, till.balance = self.branch, "cash", True, Decimal("1000.00")
        till.save()
        Treasury.objects.exclude(pk=till.pk).filter(branch=self.branch, type="cash").update(is_active=False)
        self.till = till
        self.device = RobotDevice(name="موس", device_uid="esp-ledger", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450.50",
                                b2b_wholesale_price=Decimal("321.00"))
        make_inventory(self.oil, self.branch, quantity=3)
        self.belt = make_product(name="سير مكنة", part_number="11287628649", retail_price="300")
        make_inventory(self.belt, self.branch, quantity=1)
        user, _profile = make_employee(username="karim", role="cashier", branch=self.branch, first_name="كريم")
        self.cashier, _ = Employee.objects.get_or_create(user=user)
        user, _profile = make_employee(username="samy", role="stock", branch=self.branch, first_name="سامي")
        self.stock_man, _ = Employee.objects.get_or_create(user=user)

    def sale(self, uid="s1", employee=None, items=None, name=""):
        items = items if items is not None else [
            {"part_number": "11427953125", "name": "فلتر زيت", "qty": 2, "unit_price": "450.5"},
            {"part_number": "11287628649", "name": "سير مكنة", "qty": 1, "unit_price": "300"}]
        return {"client_uid": uid, "kind": "sale", "payload": {
            "employee_id": (employee or self.cashier).pk, "items": items, "customer_name": name,
            "receipt": "OFF-261024-101500-AB12", "at": 1792836900}}

    def push(self, *events):
        return services.apply_offline_events(self.device, list(events))

    def stock(self, product):
        return Inventory.objects.get(product=product, branch=self.branch).quantity

    def cash(self):
        self.till.refresh_from_db()
        return self.till.balance


class SaleTests(OfflineLedgerTests):

    def test_a_cash_sale_is_booked_once(self):
        result = self.push(self.sale())
        self.assertEqual((result["applied"], result["failed"]), (1, 0))
        entry = RobotEntry.objects.get(kind="sale")
        invoice = entry.sale_invoice
        self.assertEqual((entry.status, invoice.status), ("done", "posted"))
        self.assertEqual(invoice.total_amount, Decimal("1201.00"))
        self.assertEqual(invoice.paid_amount, invoice.total_amount)          # cash: nothing due
        self.assertTrue(invoice.customer.is_walk_in)
        self.assertEqual(self.cash(), Decimal("2201.00"))
        self.assertEqual((self.stock(self.oil), self.stock(self.belt)), (1, 0))
        self.assertIn("OFF-261024-101500-AB12", invoice.notes)
        prices = sorted(invoice.items.values_list("unit_price", flat=True))
        self.assertEqual(prices, [Decimal("300.00"), Decimal("450.50")])   # retail, never 321
        # Sent again (a push cut off before the answer): nothing more.
        again = self.push(self.sale())
        self.assertEqual(again["skipped_duplicates"], 1)
        self.assertEqual(SaleInvoice.objects.filter(invoice_type="sale").count(), 1)
        self.assertEqual(self.cash(), Decimal("2201.00"))

    def test_the_name_said_is_kept_on_the_walk_in_invoice(self):
        self.push(self.sale(name="أستاذ مجدي"))
        invoice = RobotEntry.objects.get().sale_invoice
        self.assertIn("أستاذ مجدي", invoice.notes)
        self.assertIn("OFF-261024-101500-AB12", invoice.notes)

    def test_sold_online_meanwhile_is_kept_for_review_not_forced(self):
        Inventory.objects.filter(product=self.belt, branch=self.branch).update(quantity=0)
        result = self.push(self.sale())
        self.assertEqual((result["applied"], result["failed"]), (1, 0))      # applied: never retried into a double
        entry = RobotEntry.objects.get()
        self.assertEqual(entry.status, "failed")
        self.assertIn("محتاجة مراجعة: المخزون مكفاش", entry.message)
        self.assertIsNone(entry.sale_invoice)
        self.assertEqual(entry.amount, Decimal("1201.00"))
        self.assertEqual(self.cash(), Decimal("1000.00"))
        self.assertEqual(self.stock(self.oil), 3)                            # nothing half-booked
        self.assertTrue(RobotAlert.objects.filter(message__contains="محتاجة مراجعة").exists())

    def test_a_role_that_cant_sell_is_kept_for_review(self):
        self.push(self.sale(employee=self.stock_man))
        entry = RobotEntry.objects.get()
        self.assertEqual(entry.status, "failed")
        self.assertIn("مش مسموحله", entry.message)
        self.assertEqual(self.cash(), Decimal("1000.00"))

    def test_a_price_changed_since_is_booked_as_paid_and_noted(self):
        self.oil.retail_price = Decimal("480")
        self.oil.save()
        self.push(self.sale())
        entry = RobotEntry.objects.get()
        self.assertEqual(entry.sale_invoice.total_amount, Decimal("1201.00"))   # what the customer paid
        self.assertIn("السعر اتغيّر", entry.message)

    def test_an_unknown_part_or_a_bad_line(self):
        self.push(self.sale(uid="a", items=[{"part_number": "NOPE", "qty": 1, "unit_price": "10"}]))
        self.push(self.sale(uid="b", items=[{"part_number": "11427953125", "qty": 0, "unit_price": "450.5"}]))
        # A malformed quantity or price is kept for the manager too, not a failed event.
        result = self.push(self.sale(uid="c", items=[{"part_number": "11427953125", "qty": "abc", "unit_price": "NaN"}]))
        self.assertEqual((result["applied"], result["failed"]), (1, 0))
        self.assertEqual(list(RobotEntry.objects.values_list("status", flat=True)), ["failed"] * 3)
        self.assertEqual(self.cash(), Decimal("1000.00"))


class ExpenseTests(OfflineLedgerTests):

    def expense(self, uid="e1", amount="200", description="بنزين", employee=None):
        return {"client_uid": uid, "kind": "expense", "payload": {
            "employee_id": (employee or self.cashier).pk, "amount": amount,
            "description": description, "at": 1792836900}}

    def test_paid_from_the_branch_cash_once(self):
        self.push(self.expense())
        entry = RobotEntry.objects.get(kind="expense")
        self.assertEqual(entry.status, "done")
        tx = entry.transaction
        self.assertEqual((tx.transaction_type, tx.amount, tx.treasury_id), ("out", Decimal("200.00"), self.till.pk))
        self.assertIn("بنزين", tx.description)
        self.assertEqual(self.cash(), Decimal("800.00"))
        self.push(self.expense())
        self.assertEqual(FinancialTransaction.objects.filter(transaction_type="out").count(), 1)

    def test_more_than_the_till_holds_is_kept_for_review(self):
        self.push(self.expense(amount="5000"))
        entry = RobotEntry.objects.get()
        self.assertEqual(entry.status, "failed")
        self.assertIn("رصيد خزنة", entry.message)
        self.assertEqual(self.cash(), Decimal("1000.00"))

    def test_a_sale_before_an_expense_pays_for_it(self):
        # The order they happened in: the day's cash covers the expense after it.
        result = self.push(self.sale(), self.expense(amount="1800"))
        self.assertEqual(result["applied"], 2)
        self.assertEqual(self.cash(), Decimal("401.00"))                     # 1000 + 1201 − 1800

    def test_a_role_that_cant_spend(self):
        self.push(self.expense(employee=self.stock_man))
        self.assertEqual(RobotEntry.objects.get().status, "failed")


class StaffTests(OfflineLedgerTests):

    def test_the_catalog_carries_who_may_do_what(self):
        RobotStaffPin.objects.create(employee=self.cashier, pin_hash=pins.hash_pin("1234"))
        RobotStaffPin.objects.create(employee=self.stock_man, pin_hash=pins.hash_pin("5678"))
        staff = {s["name"]: s for s in services.offline_catalog(self.branch)["staff"]}
        self.assertEqual(set(staff["كريم"]["can"]), {"sale", "expense"})
        self.assertEqual(staff["سامي"]["can"], ["stock_take"])
        self.assertTrue(pins.check_pin("1234", staff["كريم"]["pin"]))
        self.assertNotIn("1234", str(staff))                                 # the hash only
        self.cashier.user.is_active = False
        self.cashier.user.save()
        self.assertNotIn("كريم", {s["name"] for s in services.offline_catalog(self.branch)["staff"]})

    def test_the_hash_is_djangos_own_format(self):
        self.assertTrue(check_password("4321", pins.hash_pin("4321")))
        self.assertFalse(pins.valid("12"))
        self.assertFalse(pins.valid("12345678"))
        self.assertFalse(pins.valid("12a4"))

    def test_a_manager_sets_a_pin_on_the_robots_page(self):
        boss = User.objects.create_user("pin_boss", password="x", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(boss)
        host = self.domain.domain
        page = client.get(reverse("robot_ui:staff_pins"), HTTP_HOST=host)
        self.assertContains(page, "كريم")
        client.post(reverse("robot_ui:staff_pins"), {"employee_id": self.cashier.pk, "pin": "2468"}, HTTP_HOST=host)
        saved = RobotStaffPin.objects.get(employee=self.cashier)
        self.assertTrue(pins.check_pin("2468", saved.pin_hash))
        self.assertEqual(saved.set_by, boss)
        bad = client.post(reverse("robot_ui:staff_pins"), {"employee_id": self.cashier.pk, "pin": "12"}, HTTP_HOST=host)
        self.assertContains(bad, "الرقم السري لازم يكون")
        client.post(reverse("robot_ui:staff_pins"), {"employee_id": self.cashier.pk, "remove": "1"}, HTTP_HOST=host)
        self.assertFalse(RobotStaffPin.objects.exists())


class CounterToBooksTests(OfflineLedgerTests):
    """The brain's counter page, its outbox and the server's books, together."""

    def test_sold_on_the_counter_with_the_net_down_then_booked(self):
        RobotStaffPin.objects.create(employee=self.cashier, pin_hash=pins.hash_pin("1234"))
        with TemporaryDirectory() as tmp:
            knowledge = Knowledge(services.offline_catalog(self.branch))
            box = Outbox(Path(tmp) / "outbox.jsonl")
            ledger = Ledger(Path(tmp) / "sold.json", box)
            counter = Counter(knowledge, box, ledger, log=lambda *_: None)
            token, error = counter.login("10.0.0.5", self.cashier.pk, "1234")
            self.assertEqual(error, "")
            sess = counter.session(token)
            self.assertEqual(counter.add(sess, "11287628649", 2), "المتاح من سير مكنة 1 بس.")
            self.assertEqual(counter.add(sess, "11287628649", 1), "")
            self.assertEqual(counter.add(sess, "11427953125", 2), "")
            receipt, error = counter.confirm(sess, "أستاذ مجدي")
            self.assertEqual((error, receipt["total"]), ("", Decimal("1201.0")))
            belt = counter.part("11287628649")
            self.assertEqual(counter.left(belt), 0)                          # out: the next sale can't take it
            # The net is back: the brain sends, then fetches the stock again.
            taken = box.flush(lambda events: services.apply_offline_events(self.device, events),
                              on_sent=ledger.sent)
            self.assertEqual(taken, 1)
            self.assertEqual(counter.left(belt), 0)                          # sent, not in a catalog yet
            knowledge.update(services.offline_catalog(self.branch))
            ledger.catalog_fetched()
            self.assertEqual(counter.left(counter.part("11287628649")), 0)  # the server's own 0 now
            self.assertEqual(counter.left(counter.part("11427953125")), 1)
        self.assertEqual(self.cash(), Decimal("2201.00"))
        self.assertIn("أستاذ مجدي", RobotEntry.objects.get().sale_invoice.notes)
