"""
🛡️ مسح فرع ليه فواتير/خزائن/مخزون لازم يترفض (PROTECT) — قبل كده كان CASCADE
وبيمسح فلوس الفرع كلها. ومسارات الـ API الوهمية اتشالت.
"""
from django.db.models import ProtectedError
from django.urls import NoReverseMatch, reverse

from inventory.models import Branch, SaleInvoice, Treasury
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_sale_invoice, make_treasury,
)


class BranchProtectTests(ERPTenantTestCase):
    def test_branch_with_sale_invoice_cannot_be_deleted(self):
        branch = make_branch('فرع فيه فواتير')
        treasury = make_treasury(branch)
        inv = make_sale_invoice(make_customer(), branch, treasury=treasury)
        with self.assertRaises(ProtectedError):
            branch.delete()
        self.assertTrue(SaleInvoice.objects.filter(pk=inv.pk).exists())
        self.assertTrue(Treasury.objects.filter(pk=treasury.pk).exists())

    def test_branch_with_treasury_only_cannot_be_deleted(self):
        branch = make_branch('فرع فيه خزنة')
        make_treasury(branch)
        with self.assertRaises(ProtectedError):
            branch.delete()

    def test_branch_with_stock_cannot_be_deleted(self):
        branch = make_branch('فرع فيه مخزون')
        make_inventory(make_product(), branch, quantity=3)
        with self.assertRaises(ProtectedError):
            branch.delete()

    def test_empty_branch_can_still_be_deleted(self):
        branch = make_branch('فرع فاضي')
        branch.delete()
        self.assertFalse(Branch.objects.filter(pk=branch.pk).exists())

    def test_fake_success_endpoints_are_gone(self):
        for name in ('graphql_endpoint', 'v1_legacy_system_sync'):
            with self.assertRaises(NoReverseMatch):
                reverse(name)
