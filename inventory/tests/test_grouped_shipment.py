"""
📦 Grouped shipment tests — shared costs spread across several purchase invoices.

Ten invoices from different vendors come in one container; shipping/customs
(landed) and travel (expense) are entered ONCE on the shipment. The system
splits each cost across the invoices by value, re-posts every invoice, and:

* each product's average cost = supplier price + its value-share of landed costs;
* travel never touches product cost;
* the treasury is debited exactly the cost total; the ledger still balances;
* re-saving with new amounts replaces the old shares (no double counting);
* an invoice with stock already sold can't join (nothing changes);
* deleting the shipment restores the plain supplier costs.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import (
    PurchaseInvoiceExtraCost, PurchaseShipment, Inventory, Product,
)
from inventory.services.shipment_service import allocate
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_vendor, make_treasury, make_product, make_inventory,
    make_purchase_invoice,
)
from .test_landed_cost import ledger_is_balanced

D = lambda v: Decimal(str(v))  # noqa: E731


class AllocateTests(ERPTenantTestCase):
    def test_allocate_sums_exactly_and_never_negative(self):
        shares = allocate(D('100.00'), [D('1'), D('1'), D('1')])
        self.assertEqual(sum(shares), D('100.00'))
        self.assertTrue(all(s >= 0 for s in shares))
        self.assertEqual(allocate(D('50'), [D('0'), D('0')]), [D('0.00'), D('0.00')])


@override_settings(ALLOWED_HOSTS=['*'])
class GroupedShipmentTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='50000.00')
        self.p1 = make_product(part_number='SH-1', name='موتور e90', average_cost='0', purchase_price='0')
        self.p2 = make_product(part_number='SH-2', name='جيربوكس', average_cost='0', purchase_price='0')
        make_inventory(self.p1, self.branch, quantity=0)
        make_inventory(self.p2, self.branch, quantity=0)
        # فاتورتين من موردين مختلفين: 30,000 و 10,000 (المجموع 40,000)
        self.inv1 = make_purchase_invoice(make_vendor(name='مورد 1'), self.branch,
                                          items=[(self.p1, 2, '15000.00')], status='draft')
        self.inv2 = make_purchase_invoice(make_vendor(name='مورد 2', phone='0111'), self.branch,
                                          items=[(self.p2, 1, '10000.00')], status='draft')
        for inv in (self.inv1, self.inv2):
            inv.status = 'posted'
            inv.save()
            inv.refresh_from_db()
        self.boss = User.objects.create_user('ship_boss', password='x', is_staff=True, is_superuser=True)

    def _save(self, **body):
        c = Client()
        c.force_login(self.boss)
        data = {'name': 'شحنة الإمارات', 'branch_id': self.branch.pk,
                'invoice_ids': [self.inv1.pk, self.inv2.pk],
                'costs': [{'kind': 'shipping', 'amount': '3000', 'treasury_id': self.treasury.pk},
                          {'kind': 'customs', 'amount': '1000'},
                          {'kind': 'travel', 'amount': '800', 'treasury_id': self.treasury.pk}]}
        data.update(body)
        return c.post('/system/purchases/shipments/save/', data=json.dumps(data),
                      content_type='application/json', HTTP_ACCEPT='application/json',
                      HTTP_HOST=self.host)

    def test_costs_spread_by_value_across_invoices(self):
        r = self._save()
        self.assertEqual(r.status_code, 200, r.content)
        # landed = 4000 على 40,000 = 10% → inv1 3000، inv2 1000
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('16500.00'))   # 15000 + 10%
        self.assertEqual(self.p2.average_cost, D('11000.00'))   # 10000 + 10%
        self.assertEqual(self.p1.purchase_price, D('15000.00'))  # سعر المورد الخام
        self.assertEqual(Inventory.objects.get(product=self.p1, branch=self.branch).quantity, 2)
        # الخزنة اتخصم منها الشحن + السفر بس (الجمارك آجل)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('50000.00') - D('3800.00'))
        shares = PurchaseInvoiceExtraCost.objects.filter(shipment__isnull=False)
        self.assertEqual(sum(s.amount for s in shares), D('4800.00'))
        self.assertTrue(ledger_is_balanced())

        # إعادة الحفظ بمبالغ جديدة بتستبدل الأنصبة القديمة (مفيش ازدواج)
        sh = PurchaseShipment.objects.get()
        r = self._save(id=sh.pk, costs=[{'kind': 'shipping', 'amount': '2000',
                                          'treasury_id': self.treasury.pk}])
        self.assertEqual(r.status_code, 200, r.content)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('15750.00'))   # 15000 + 5%
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('48000.00'))
        self.assertTrue(ledger_is_balanced())

        # حذف الشحنة بيرجّع التكلفة لسعر المورد
        c = Client()
        c.force_login(self.boss)
        c.post(f'/system/purchases/shipments/{sh.pk}/delete/', HTTP_HOST=self.host)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('15000.00'))
        self.assertFalse(PurchaseShipment.objects.exists())
        self.inv1.refresh_from_db()
        self.assertIsNone(self.inv1.shipment_id)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('50000.00'))

    def test_invoice_with_sold_stock_cannot_join(self):
        Inventory.objects.filter(product=self.p2, branch=self.branch).update(quantity=0)  # اتباع
        r = self._save()
        self.assertEqual(r.status_code, 409)
        self.assertFalse(PurchaseShipment.objects.exists())
        self.assertFalse(PurchaseInvoiceExtraCost.objects.exists())
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('15000.00'))

    def test_invoice_in_shipment_cannot_be_deleted_alone(self):
        self.assertEqual(self._save().status_code, 200)
        c = Client()
        c.force_login(self.boss)
        r = c.post(f'/system/purchases/{self.inv1.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('err=shipment', r['Location'])
        self.inv1.refresh_from_db()
        self.assertTrue(self.inv1.is_applied)

    def test_pick_invoices_from_purchase_list(self):
        c = Client()
        c.force_login(self.boss)
        page = c.get('/system/purchases/', HTTP_HOST=self.host)
        self.assertContains(page, f'class="sel-inv" value="{self.inv1.pk}"')
        self.assertContains(page, f'class="sel-inv" value="{self.inv2.pk}"')
        form = c.get(f'/system/purchases/shipments/new/?invoices={self.inv1.pk},{self.inv2.pk},x',
                     HTTP_HOST=self.host)
        self.assertEqual(form.status_code, 200)
        self.assertContains(form, f'const PRESELECT = [{self.inv1.pk}, {self.inv2.pk}];')
        # فاتورة دخلت شحنة مبقاش ليها checkbox في القائمة
        self.assertEqual(self._save().status_code, 200)
        page = c.get('/system/purchases/', HTTP_HOST=self.host)
        self.assertNotContains(page, f'class="sel-inv" value="{self.inv1.pk}"')

    def test_read_expenses_from_photo(self):
        from unittest import mock
        from django.core.files.uploadedfile import SimpleUploadedFile
        fake = {'items': [
            {'kind': 'shipping', 'label': 'شحن حاوية', 'amount': 30000, 'currency': 'AED'},
            {'kind': 'جمارك', 'label': 'تخليص', 'amount': '20,000', 'currency': 'AED'},
            {'kind': 'xyz', 'label': 'تذاكر طيران', 'amount': '٣٥٠٠', 'currency': ''},
            {'kind': 'food', 'label': 'غلط', 'amount': 'NaN'},
            {'kind': 'other', 'label': 'صفر', 'amount': 0},
        ]}
        c = Client()
        c.force_login(self.boss)
        with mock.patch('inventory.ai_services.scan_expenses_image_ai', return_value=fake):
            r = c.post('/system/purchases/shipments/expenses/extract/',
                       {'files': [SimpleUploadedFile('r.jpg', b'x', content_type='image/jpeg')]},
                       HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        items = r.json()['items']
        self.assertEqual([(i['kind'], i['behavior'], i['amount']) for i in items], [
            ('shipping', 'landed', 30000.0), ('customs', 'landed', 20000.0), ('travel', 'expense', 3500.0)])
        with mock.patch('inventory.ai_services.scan_expenses_image_ai', return_value={'items': []}):
            r = c.post('/system/purchases/shipments/expenses/extract/',
                       {'files': [SimpleUploadedFile('r.jpg', b'x', content_type='image/jpeg')]},
                       HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 422)
        r = c.post('/system/purchases/shipments/expenses/extract/',
                   {'files': [SimpleUploadedFile('r.pdf', b'x')]}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 400)

    def test_big_phone_photo_is_shrunk_before_the_ai(self):
        import base64
        import io
        from unittest import mock
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        buf = io.BytesIO()
        Image.new('RGB', (4000, 3000), (200, 30, 30)).save(buf, format='JPEG', quality=95)
        seen = {}

        def fake_ai(b64):
            seen['size'] = Image.open(io.BytesIO(base64.b64decode(b64))).size
            return {'items': [{'kind': 'shipping', 'label': 'شحن', 'amount': 100}]}
        c = Client()
        c.force_login(self.boss)
        with mock.patch('inventory.ai_services.scan_expenses_image_ai', side_effect=fake_ai):
            # اسم من غير امتداد (بعض الموبايلات) بس نوعه صورة → يتقبل
            r = c.post('/system/purchases/shipments/expenses/extract/',
                       {'files': [SimpleUploadedFile('photo', buf.getvalue(), content_type='image/jpeg')]},
                       HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertLessEqual(max(seen['size']), 1600)

    def test_pnl_counts_shipment_period_expenses(self):
        # السفر (800) «مصروف» على الشحنة لازم يظهر في الأرباح والخسائر — مرة واحدة
        self.assertEqual(self._save().status_code, 200)
        c = Client()
        c.force_login(self.boss)
        r = c.get('/system/reports/pnl/?period=all', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_exp'], D('800.00'))
        self.assertTrue(any('سفر' in (row['category__name'] or '') for row in r.context['exp_rows']))


class ReportPeriodTests(ERPTenantTestCase):
    def test_periods(self):
        from datetime import timedelta
        from django.test import RequestFactory
        from django.utils import timezone
        from inventory.views_lightning import _report_period
        rf = RequestFactory()
        today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)

        start, end, _l, key = _report_period(rf.get('/', {'period': 'week'}))
        self.assertEqual((today - start).days, 6)          # 7 أيام بالنهارده
        start, end, _l, key = _report_period(rf.get('/', {'period': 'last_month'}))
        self.assertEqual(key, 'last_month')
        self.assertEqual(end, today.replace(day=1))
        self.assertEqual(start.day, 1)
        self.assertLess(start, end)
        start, end, label, key = _report_period(
            rf.get('/', {'period': 'custom', 'from': '2026-09-30', 'to': '2026-09-01'}))
        self.assertEqual(key, 'custom')
        self.assertEqual(start.strftime('%Y-%m-%d'), '2026-09-01')     # بيقلبهم لو مقلوبين
        self.assertEqual(end.strftime('%Y-%m-%d'), '2026-10-01')       # «إلى» شاملة اليوم
        self.assertEqual(end - start, timedelta(days=30))
        # تاريخ غلط → يرجع للشهر الحالي بدل ما يقع
        _s, _e, _l, key = _report_period(rf.get('/', {'period': 'custom', 'from': 'xx'}))
        self.assertEqual(key, 'month')
