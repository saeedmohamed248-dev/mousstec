"""📅 المخزون: ترتيب الرصّة بتاريخ دخول البضاعة (الأحدث/الأقدم) + البحث عن اللي دخل في مدة معيّنة.

الصنف مالوش تاريخ إنشاء — «تاريخ الدخول» = آخر حركة زوّدت رصيده (توريد/تحويل وارد/رصيد بداية).
"""
import datetime

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from inventory.models import InventoryMovement
from .base import ERPTenantTestCase
from .factories import make_branch, make_inventory, make_product


@override_settings(ALLOWED_HOSTS=['*'])
class ProductEntryDateTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.boss, _ = User.objects.get_or_create(username='ed_boss', defaults=dict(is_staff=True, is_superuser=True))
        self.c = Client()
        self.c.force_login(self.boss)
        now = timezone.now()
        self.old = self._product('ED-OLD', 'أ قديم', now - datetime.timedelta(days=60))
        self.mid = self._product('ED-MID', 'ب وسط', now - datetime.timedelta(days=10))
        self.new = self._product('ED-NEW', 'ج جديد', now - datetime.timedelta(days=1))
        self.none = make_product(part_number='ED-NONE', name='د من غير حركة')
        make_inventory(self.none, self.branch, quantity=0)
        # بيع (خروج) أحدث من كل حاجة ⇒ مايتحسبش «دخول»
        InventoryMovement.objects.create(product=self.old, branch=self.branch, reason='sale', quantity_change=-1,
                                         quantity_before=3, quantity_after=2)

    def _product(self, sku, name, when):
        p = make_product(part_number=sku, name=name)
        make_inventory(p, self.branch, quantity=3)
        m = InventoryMovement.objects.create(product=p, branch=self.branch, reason='purchase', quantity_change=3,
                                             quantity_before=0, quantity_after=3)
        InventoryMovement.objects.filter(pk=m.pk).update(created_at=when)
        return p

    def _names(self, **params):
        r = self.c.get('/system/products/', params, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        return [row['product'].part_number for row in r.context['rows']
                if row['product'].part_number.startswith('ED-')], r

    def test_sort_newest_and_oldest_by_entry_date(self):
        names, r = self._names(sort='newest')
        self.assertEqual(names, ['ED-NEW', 'ED-MID', 'ED-OLD', 'ED-NONE'])   # اللي مالوش حركة في الآخر
        self.assertContains(r, '📅')
        names, _ = self._names(sort='oldest')
        self.assertEqual(names, ['ED-OLD', 'ED-MID', 'ED-NEW', 'ED-NONE'])
        names, _ = self._names()                                             # الافتراضي بالاسم زي ما هو
        self.assertEqual(names, ['ED-OLD', 'ED-MID', 'ED-NEW', 'ED-NONE'])

    def test_filter_by_entry_date_range_and_keeps_filters_across_pages_and_export(self):
        today = timezone.localdate()
        frm = (today - datetime.timedelta(days=15)).isoformat()
        names, r = self._names(**{'from': frm, 'to': today.isoformat(), 'sort': 'oldest'})
        self.assertEqual(names, ['ED-MID', 'ED-NEW'])
        self.assertContains(r, f'value="{frm}"')
        self.assertContains(r, f"p.set('from', \"{frm}\")")                  # التصدير بنفس المدة
        self.assertContains(r, f'name="from" value="{frm}"', count=3)       # الفلتر + الإجراءات الجماعية
        # تاريخ غلط بيتجاهل من غير ما الصفحة تقع
        names, _ = self._names(**{'from': 'xx'})
        self.assertEqual(len(names), 4)
        # التصدير «كل النتائج» بيطلع نفس اللي في المدة بس
        r = self.c.get('/system/products/export/', {'fmt': 'xlsx', 'all': '1', 'cols': 'sku',
                                                     'from': frm}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        import io
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(r.content)).active
        skus = {c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith('ED-')}
        self.assertEqual(skus, {'ED-MID', 'ED-NEW'})
