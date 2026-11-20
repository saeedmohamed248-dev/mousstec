"""🔢 ترقيم الفواتير المتسلسل (DocumentSequence).

رقم الفاتورة كان id من sequence بتاع Postgres: أي حفظ يقع في النص بياكل رقم، وبعد ما Postgres
يقع ويقوم الأرقام بتنط ٣٢ — فجوات مش مفهومة قدام المراجع. دلوقتي الرقم بيتاخد من عدّاد جوه نفس
الـ transaction، وصفحة «تسلسل أرقام الفواتير» بتقول سبب أي رقم ناقص.
"""
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import DocumentSequence, SaleInvoice
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_inventory, make_product


@override_settings(ALLOWED_HOSTS=['*'])
class InvoiceNumberingTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.branch = make_branch()
        self.customer = make_customer('عميل الترقيم')

    def new(self, **kw):
        return SaleInvoice.objects.create(invoice_type='sale', customer=self.customer, branch=self.branch, **kw)

    def test_numbers_follow_each_other_and_a_failed_save_gives_its_number_back(self):
        a = self.new()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.new()
                raise RuntimeError('الحفظ وقع في النص')
        b = self.new()
        self.assertEqual(b.pk, a.pk + 1)            # الرقم اللي اتاخد في الحفظ الواقع رجع
        seq = DocumentSequence.objects.get(name='sale_invoice')
        self.assertEqual(seq.last_value, b.pk)

    def test_number_ignores_postgres_sequence_jump(self):
        a = self.new()
        with connection.cursor() as cur:          # زي ما يحصل بعد crash recovery
            cur.execute("SELECT setval(pg_get_serial_sequence('inventory_saleinvoice', 'id'), %s)", [a.pk + 32])
        b = self.new()
        self.assertEqual(b.pk, a.pk + 1)

    def test_rows_inserted_without_the_counter_never_collide(self):
        a = self.new()
        # حد دخّل صف برقم صريح (استيراد/fixtures) من غير العدّاد
        manual = SaleInvoice(pk=a.pk + 5, invoice_type='sale', customer=self.customer, branch=self.branch)
        super(SaleInvoice, manual).save(force_insert=True)
        b = self.new()
        self.assertEqual(b.pk, a.pk + 6)
        # وبالعكس: insert بالـ sequence بتاع Postgres مايلبسش في رقم مستخدم
        c = SaleInvoice(invoice_type='sale', customer=self.customer, branch=self.branch)
        super(SaleInvoice, c).save(force_insert=True)
        self.assertGreater(c.pk, b.pk)

    def test_sequence_page_explains_every_missing_number(self):
        boss = User.objects.create_user('seq_boss', password='x', is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(boss)
        # فجوة «قديمة» قبل العدّاد: صف بـ id بعيد والعدّاد لسه ماتعملش
        DocumentSequence.objects.all().delete()
        legacy = SaleInvoice(pk=10, invoice_type='sale', customer=self.customer, branch=self.branch)
        super(SaleInvoice, legacy).save(force_insert=True)
        first = self.new()
        self.assertEqual(first.pk, 11)
        self.assertEqual(DocumentSequence.objects.get(name='sale_invoice').start_value, 10)
        doomed = self.new()
        third = self.new()
        doomed_no = doomed.pk
        doomed.delete()                                 # مسح ⇒ الرقم مابيرجعش وبيتسجّل
        fourth = self.new()
        self.assertEqual(fourth.pk, third.pk + 1)

        r = c.get(reverse('inventory:invoice_sequence_report'), HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['last_number'], fourth.pk)
        by_kind = {row['kind']: row for row in r.context['rows']}
        self.assertEqual(by_kind['deleted']['number'], doomed_no)
        self.assertEqual((by_kind['legacy']['low'], by_kind['legacy']['number'], by_kind['legacy']['count']),
                         (1, 9, 9))                     # ١ → ٩ في سطر واحد
        self.assertNotIn('unknown', by_kind)
        self.assertEqual(r.context['total_missing'], 10)
        self.assertContains(r, 'اتمسحت')

    def test_cashier_cannot_open_the_sequence_page(self):
        from .factories import make_employee
        user, _p = make_employee(username='seq_cashier', role='cashier', branch=self.branch)
        c = Client()
        c.force_login(user)
        r = c.get(reverse('inventory:invoice_sequence_report'), HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 403)

    def test_wix_order_without_matching_items_does_not_burn_a_number(self):
        from clients.services import wix_sync
        part = make_product(part_number='SEQ-1', name='صنف', retail_price='100.00')
        make_inventory(part, self.branch, quantity=5)
        before = self.new()
        conn = SimpleNamespace(client_id=self.tenant.pk, last_order_sync_at=None, default_branch_id=self.branch.pk,
                               orders_imported=0, save=lambda **kw: None, mark_error=lambda e: None)
        order = {'id': 'W-NOPE', 'number': 77, 'buyerInfo': {'phone': '01055556666'},
                 'lineItems': [{'sku': 'NOT-IN-STOCK', 'quantity': 1, 'price': {'amount': '10'}}]}
        fake = mock.Mock()
        fake.search_orders.return_value = SimpleNamespace(ok=True, data={'orders': [order]}, error='')
        with mock.patch.object(wix_sync, '_client', return_value=fake):
            summary = wix_sync.pull_orders(conn)
        connection.set_tenant(self.tenant)
        self.assertEqual((summary['imported'], summary['skipped']), (0, 1), summary)
        self.assertFalse(SaleInvoice.objects.filter(wix_order_id='W-NOPE').exists())
        self.assertEqual(self.new().pk, before.pk + 1)  # مفيش رقم ضاع
