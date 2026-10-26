"""Every branch, on the books: the catalog carries the other branches' stock
(and the parts only they have), offline answers say the same as the server,
and the counter page's sale or expense on another branch is booked there —
its invoice, its stock, its cash."""

from decimal import Decimal

from inventory.models import Inventory, Treasury
from inventory.tests.factories import make_branch, make_inventory, make_product, make_treasury
from robot import services, views
from robot.brain.knowledge import Knowledge
from robot.models import RobotEntry
from robot.tests.test_offline_ledger_db import OfflineLedgerTests


class AllBranchesTests(OfflineLedgerTests):

    def setUp(self):
        super().setUp()
        self.maadi = make_branch(name="فرع المعادي")
        self.maadi_till = make_treasury(self.maadi)
        self.maadi_till.type, self.maadi_till.is_active, self.maadi_till.balance = "cash", True, Decimal("500.00")
        self.maadi_till.save()
        make_inventory(self.oil, self.maadi, quantity=5)
        self.pump = make_product(name="طرمبة مية", part_number="11517586925", retail_price="1800", car_model="")
        make_inventory(self.pump, self.maadi, quantity=2)               # only المعادي has it

    def test_the_catalog_has_every_branch(self):
        catalog = services.offline_catalog(self.branch)
        self.assertEqual(catalog["branch_id"], self.branch.pk)
        self.assertIn({"id": self.maadi.pk, "name": "فرع المعادي"}, catalog["branches"])
        parts = {p["part_number"]: p for p in catalog["parts"]}
        self.assertEqual(parts["11427953125"]["elsewhere"], [{"id": self.maadi.pk, "name": "فرع المعادي", "stock": 5}])
        self.assertEqual(parts["11517586925"]["stock"], 0)               # not here, but known
        self.assertNotIn("321", str(catalog["parts"]))                   # still retail only

    def test_offline_says_what_the_server_says(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        said = offline.answer("يا موس عندك طرمبة مية؟", now=0.0).text
        intent, reply, _payload = views._handle_voice("عندك طرمبة مية؟", self.device, None)
        self.assertEqual(said, reply)
        self.assertIn("بس موجود في فرع المعادي.", reply)                 # not «فرع فرع المعادي»

    def test_a_counter_sale_on_another_branch_is_booked_there(self):
        event = self.sale(items=[{"part_number": "11427953125", "name": "فلتر زيت", "qty": 4, "unit_price": "450.5"}])
        event["payload"]["branch_id"] = self.maadi.pk
        result = self.push(event)
        self.assertEqual((result["applied"], result["failed"]), (1, 0))
        entry = RobotEntry.objects.get(kind="sale")
        self.assertEqual((entry.status, entry.branch, entry.sale_invoice.branch), ("done", self.maadi, self.maadi))
        self.assertEqual(Inventory.objects.get(product=self.oil, branch=self.maadi).quantity, 1)
        self.assertEqual(self.stock(self.oil), 3)                        # this branch's untouched
        self.assertEqual(self.cash(), Decimal("1000.00"))

    def test_a_counter_expense_on_another_branch_is_paid_from_its_cash(self):
        self.push({"client_uid": "e1", "kind": "expense", "payload": {
            "employee_id": self.cashier.pk, "amount": "200.00", "description": "بنزين",
            "branch_id": self.maadi.pk, "at": 1792836900}})
        entry = RobotEntry.objects.get(kind="expense")
        self.assertEqual((entry.status, entry.branch), ("done", self.maadi))
        self.maadi_till.refresh_from_db()
        self.assertEqual(self.maadi_till.balance, Decimal("300.00"))
        self.assertEqual(self.cash(), Decimal("1000.00"))

    def test_a_branch_gone_since_is_kept_for_review(self):
        event = self.sale()
        event["payload"]["branch_id"] = 999999
        self.push(event)
        entry = RobotEntry.objects.get(kind="sale")
        self.assertEqual(entry.status, "failed")
        self.assertIn("الفرع اللي اتسجّلت عليه مبقاش موجود", entry.message)
        self.assertEqual(Treasury.objects.get(pk=self.till.pk).balance, Decimal("1000.00"))
