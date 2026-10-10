"""
📦 Products page — bulk delete/archive/restore + Excel/PDF export with chosen columns.

* A product with stock is never deleted (skipped).
* A product with history (invoice lines / movements) is archived, not deleted —
  deleting it would cascade away its purchase/sale lines.
* A product with no history at all is deleted for real.
* Archived products can be restored, and come back by themselves when bought again.
* Export honours the selected rows and the chosen columns.
"""
import io
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Product, PurchaseInvoiceItem
from .base import ERPTenantTestCase
from .factories import make_branch, make_inventory, make_product, make_purchase_invoice, make_vendor


@override_settings(ALLOWED_HOSTS=['*'])
class ProductBulkTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.with_stock = make_product(part_number='PB-1', name='صنف عليه رصيد', retail_price='100', purchase_price='60')
        make_inventory(self.with_stock, self.branch, quantity=5)
        self.with_history = make_product(part_number='PB-2', name='صنف ليه تاريخ', retail_price='50', purchase_price='30')
        make_inventory(self.with_history, self.branch, quantity=0)
        inv = make_purchase_invoice(make_vendor(), self.branch, items=[(self.with_history, 1, '30')], status='draft')
        self.assertTrue(PurchaseInvoiceItem.objects.filter(invoice=inv).exists())
        self.junk = make_product(part_number='PB-3', name='صنف غلط', retail_price='0', purchase_price='0')
        make_inventory(self.junk, self.branch, quantity=0)
        self.boss = User.objects.create_user('pb_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)

    def test_bulk_delete_is_safe_and_restorable(self):
        ids = f'{self.with_stock.pk},{self.with_history.pk},{self.junk.pk}'
        r = self.c.post('/system/products/bulk-action/', {'action': 'delete', 'ids': ids}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.assertIn('deleted=1', r['Location'])
        self.assertIn('archived=1', r['Location'])
        self.assertIn('skipped=1', r['Location'])
        self.assertTrue(Product.objects.get(pk=self.with_stock.pk).is_active)          # عليه رصيد → اتساب
        self.assertFalse(Product.objects.get(pk=self.with_history.pk).is_active)       # تاريخ → أرشفة
        self.assertTrue(PurchaseInvoiceItem.objects.filter(product=self.with_history).exists())
        self.assertFalse(Product.objects.filter(pk=self.junk.pk).exists())             # غلط → حذف نهائي

        # بيظهر في فلتر المؤرشفة ويترجع
        page = self.c.get('/system/products/?stock=archived', HTTP_HOST=self.host)
        self.assertContains(page, 'PB-2')
        self.assertNotContains(page, 'PB-1')
        self.c.post('/system/products/bulk-action/',
                    {'action': 'restore', 'ids': str(self.with_history.pk), 'stock': 'archived'},
                    HTTP_HOST=self.host)
        self.assertTrue(Product.objects.get(pk=self.with_history.pk).is_active)

    def test_archived_product_comes_back_when_bought_again(self):
        Product.objects.filter(pk=self.with_history.pk).update(is_active=False)
        inv = make_purchase_invoice(make_vendor(name='مورد 2', phone='0122'), self.branch,
                                    items=[(self.with_history, 2, '30')], status='draft')
        inv.status = 'posted'
        inv.save()
        self.assertTrue(Product.objects.get(pk=self.with_history.pk).is_active)

    def test_export_selected_rows_and_columns(self):
        import openpyxl
        r = self.c.get('/system/products/export/', {
            'fmt': 'xlsx', 'cols': 'sku,name,stock,purchase_price,wholesale_price',
            'ids': f'{self.with_stock.pk},{self.junk.pk}'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
        values = [[c for c in row] for row in ws.iter_rows(values_only=True)]
        flat = [str(v) for row in values for v in row if v is not None]
        self.assertIn('PB-1', flat)
        self.assertIn('PB-3', flat)
        self.assertNotIn('PB-2', flat)                       # مش محدد
        self.assertTrue(any('سعر الجملة' in v for v in flat))
        self.assertFalse(any('سعر البيع' in v for v in flat))  # عمود مش مختار
        r = self.c.get('/system/products/export/', {'fmt': 'pdf', 'all': '1'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)

    def test_cashier_cannot_bulk_delete(self):
        from .factories import make_employee
        cashier, _p = make_employee(username='pb_cash', role='cashier', branch=self.branch)
        c = Client()
        c.force_login(cashier)
        c.post('/system/products/bulk-action/', {'action': 'delete', 'ids': str(self.junk.pk)},
               HTTP_HOST=self.host)
        self.assertTrue(Product.objects.filter(pk=self.junk.pk).exists())



@override_settings(ALLOWED_HOSTS=['*'])
class ProductKpiTests(ERPTenantTestCase):
    """أرقام الكروت فوق: التكلفة بمتوسط التكلفة، إجمالي الجملة، والنافد في الفرع بس."""

    def test_kpi_numbers_for_a_branch(self):
        from .factories import make_employee
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.with_stock = make_product(part_number='PB-1', name='صنف عليه رصيد', retail_price='100', purchase_price='60')
        make_inventory(self.with_stock, self.branch, quantity=5)
        for sku in ('PB-2', 'PB-3'):
            make_inventory(make_product(part_number=sku, name=sku, retail_price='50', purchase_price='30'),
                           self.branch, quantity=0)
        # صنف بمتوسط تكلفة 120 (فيه شحن) وآخر شراء 100، سعره 90 (أقل من التكلفة)، من غير جملة
        p = make_product(part_number='PB-K', name='صنف KPI', retail_price='90', purchase_price='100',
                         average_cost='120', b2b_wholesale_price='0')
        make_inventory(p, self.branch, quantity=2)
        # صنف في فرع تاني بس — مايتعدّش «نافد» في الفرع ده
        other = make_branch(name='فرع تاني')
        q = make_product(part_number='PB-O', name='صنف فرع تاني', retail_price='10', purchase_price='5')
        make_inventory(q, other, quantity=0)
        Product.objects.filter(pk=self.with_stock.pk).update(b2b_wholesale_price=Decimal('80'))

        mgr, _p = make_employee(username='pb_mgr', role='manager', branch=self.branch)
        c = Client()
        c.force_login(mgr)
        r = c.get('/system/products/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        sm = r.context['summary']
        # PB-1: 5 × متوسط 60 = 300 ، PB-K: 2 × 120 (مش آخر شراء 100) = 240
        self.assertEqual(sm['capital'], Decimal('540'))
        self.assertEqual(sm['retail'], Decimal('680'))          # 5×100 + 2×90
        self.assertEqual(sm['wholesale'], Decimal('400'))       # 5×80 + 2×0
        self.assertEqual(sm['below_cost'], 1)                   # PB-K
        self.assertEqual(sm['no_wholesale'], 1)                 # PB-K (PB-1 عنده جملة)
        # نافد في الفرع ده = PB-2 و PB-3 بس (مش PB-O بتاع الفرع التاني)
        self.assertEqual(sm['out_count'], 2)
