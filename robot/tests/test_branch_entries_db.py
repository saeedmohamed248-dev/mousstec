"""The branch question, end to end against the real ERP pipeline.

A spoken expense / purchase goes through the robot's voice router, waits for
a branch, and is then written with the ERP's own posting (treasury balance,
stock in, vendor balance). Runs on a tenant schema like the inventory suite.
"""

from decimal import Decimal

from inventory.models import (
    Branch, FinancialTransaction, Inventory, PurchaseInvoice, Treasury, Vendor,
)
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import (
    make_branch, make_expense_category, make_product, make_treasury, make_vendor,
)
from robot import entries, views
from robot.models import RobotDevice, RobotEntry


class _Manager:
    """Stands in for the face-recognized employee: allowed to do anything."""
    name = "المدير"


class BranchQuestionEndToEndTests(ERPTenantTestCase):

    def setUp(self):
        # The tenant's plan allows two branches, and a new tenant may already
        # have its default one: rename it instead of adding a third.
        existing = list(Branch.objects.order_by("id")[:2])
        while len(existing) < 2:
            existing.append(make_branch(name=f"فرع {len(existing)}"))
        self.maadi, self.nasr = existing
        for branch, name in ((self.maadi, "المعادي"), (self.nasr, "مدينة نصر")):
            branch.name = name
            branch.save(update_fields=["name"])
        # Same for treasuries (two per plan): reuse, then set them up.
        tills = list(Treasury.objects.order_by("id")[:2])
        while len(tills) < 2:
            tills.append(make_treasury(self.maadi, name=f"خزنة {len(tills)}"))
        for till, branch, name in ((tills[0], self.maadi, "كاش المعادي"),
                                   (tills[1], self.nasr, "كاش مدينة نصر")):
            till.branch, till.name, till.type = branch, name, "cash"
            till.balance, till.is_active = Decimal("1000.00"), True
            till.save()
        self.maadi_cash, self.nasr_cash = tills
        make_expense_category("بنزين")
        self.device = RobotDevice(name="موس", device_uid="esp-test", branch=self.nasr)
        self.device.issue_token()
        self.device.save()
        self.product = make_product(name="فلتر زيت", part_number="11427953125")
        self.vendor = make_vendor(name="الأمل")
        self._can = views.permissions.employee_can
        views.permissions.employee_can = lambda employee, action: True

    def tearDown(self):
        views.permissions.employee_can = self._can

    def _say(self, text):
        return views._handle_voice(text, self.device, None)

    def test_expense_goes_to_the_branch_that_was_named(self):
        _, reply, payload = self._say("سجل مصروف 200 جنيه بنزين")
        self.assertEqual(payload["action"], "entry_ask_branch")
        self.assertIn("أنهي فرع", reply)
        self.assertFalse(FinancialTransaction.objects.exists())   # nothing yet

        _, reply, payload = self._say("المعادي")
        self.assertEqual(payload["action"], "entry_done")
        tx = FinancialTransaction.objects.get()
        self.assertEqual((tx.treasury, tx.transaction_type, tx.amount),
                         (self.maadi_cash, "out", Decimal("200.00")))
        self.assertEqual(Treasury.objects.get(pk=self.maadi_cash.pk).balance, Decimal("800.00"))
        self.assertEqual(Treasury.objects.get(pk=self.nasr_cash.pk).balance, Decimal("1000.00"))
        entry = RobotEntry.objects.get()
        self.assertEqual((entry.status, entry.branch, entry.transaction),
                         ("done", self.maadi, tx))

    def test_here_means_the_robots_own_branch(self):
        self._say("مصروف 50 جنيه شاي")
        self._say("هنا")
        self.assertEqual(Treasury.objects.get(pk=self.nasr_cash.pk).balance, Decimal("950.00"))

    def test_cancel_records_nothing(self):
        self._say("سجل مصروف 200 جنيه بنزين")
        self._say("الغي")
        self.assertFalse(FinancialTransaction.objects.exists())
        self.assertEqual(RobotEntry.objects.get().status, "cancelled")

    def test_an_expense_larger_than_the_treasury_is_refused(self):
        self._say("سجل مصروف 5000 جنيه بنزين")
        _, reply, payload = self._say("المعادي")
        self.assertEqual(payload["action"], "entry_failed")
        self.assertIn("مش مكفي", reply)
        self.assertFalse(FinancialTransaction.objects.exists())
        self.assertEqual(RobotEntry.objects.get().status, "failed")

    def test_cash_purchase_is_posted_on_the_named_branch(self):
        _, reply, payload = self._say("اشترينا 3 فلتر زيت من الأمل بسعر 150 كاش")
        self.assertEqual(payload["action"], "entry_ask_branch")
        _, reply, payload = self._say("المعادي")
        self.assertEqual(payload["action"], "entry_done", reply)

        invoice = PurchaseInvoice.objects.get()
        self.assertEqual((invoice.branch, invoice.status, invoice.total_amount),
                         (self.maadi, "posted", Decimal("450.00")))
        self.assertEqual(Inventory.objects.get(product=self.product, branch=self.maadi).quantity, 3)
        self.assertFalse(Inventory.objects.filter(product=self.product, branch=self.nasr,
                                                  quantity__gt=0).exists())
        self.assertEqual(Treasury.objects.get(pk=self.maadi_cash.pk).balance, Decimal("550.00"))

    def test_credit_purchase_goes_on_the_vendor_account(self):
        self._say("اشترينا 2 فلتر زيت من الأمل بسعر 100")
        self._say("مدينة نصر")
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, Decimal("200.00"))
        self.assertEqual(Treasury.objects.get(pk=self.nasr_cash.pk).balance, Decimal("1000.00"))

    def test_branch_without_cash_treasury_refuses_cash_spending(self):
        self.nasr_cash.is_active = False
        self.nasr_cash.save()
        self._say("سجل مصروف 20 جنيه بنزين")
        _, reply, _ = self._say("هنا")
        self.assertIn("مالوش خزنة كاش", reply)
        self.assertIsNone(entries.cash_treasury(self.nasr))


class SaleBranchEndToEndTests(BranchQuestionEndToEndTests):
    """`/sale/` asks for the branch, then sells from and into that branch."""

    # Only the sale tests here; the voice ones already ran in the parent class.
    test_expense_goes_to_the_branch_that_was_named = None
    test_here_means_the_robots_own_branch = None
    test_cancel_records_nothing = None
    test_an_expense_larger_than_the_treasury_is_refused = None
    test_cash_purchase_is_posted_on_the_named_branch = None
    test_credit_purchase_goes_on_the_vendor_account = None
    test_branch_without_cash_treasury_refuses_cash_spending = None

    def _sell(self, **body):
        from unittest import mock
        from rest_framework.test import APIRequestFactory
        request = APIRequestFactory().post(
            "/api/robot/v1/sale/",
            {"part_number": self.product.part_number, "payment": "cash", **body},
            format="json")
        from django.contrib.auth.models import User
        from hr.models import Employee
        user, _ = User.objects.get_or_create(username="kareem", defaults={"first_name": "كريم"})
        employee, _ = Employee.objects.get_or_create(user=user)
        with mock.patch.object(views, "_device_or_401", return_value=(self.device, None)), \
                mock.patch.object(views, "_require_permission", return_value=(employee, None)):
            return views.sale(request)

    def test_without_a_branch_nothing_is_sold(self):
        from inventory.models import SaleInvoice
        response = self._sell()
        self.assertEqual(response.status_code, 428)
        self.assertEqual({b["name"] for b in response.data["branches"]},
                         {"المعادي", "مدينة نصر"})
        self.assertFalse(SaleInvoice.objects.exists())

    def test_sale_on_the_named_branch(self):
        from inventory.tests.factories import make_inventory
        make_inventory(self.product, self.maadi, quantity=5)
        response = self._sell(branch_id=self.maadi.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["branch"], "المعادي")
        self.assertEqual(Inventory.objects.get(product=self.product, branch=self.maadi).quantity, 4)
        self.assertEqual(Treasury.objects.get(pk=self.maadi_cash.pk).balance, Decimal("1100.00"))
        entry = RobotEntry.objects.get()
        self.assertEqual((entry.kind, entry.branch_id), ("sale", self.maadi.pk))

    def test_cash_sale_refused_without_cash_treasury(self):
        from inventory.models import SaleInvoice
        from inventory.tests.factories import make_inventory
        make_inventory(self.product, self.nasr, quantity=5)
        self.nasr_cash.is_active = False
        self.nasr_cash.save()
        response = self._sell(branch_id=self.nasr.pk)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(SaleInvoice.objects.exists())
