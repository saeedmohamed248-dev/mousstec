"""
🌙 مراجعة نوفمبر ٢٠٢٦ — المهام الليلية اللي كانت بتقع في صمت.

* المطابقة الليلية للخزائن كانت بتشتغل على schema «public» (مافيهاش خزائن) فتقع كل ليلة
  — دلوقتي بتلف على الشركات وبتنبّه صاحب المنصة بالفروق الجديدة بس.
* الـ DLQ: نسخة واحدة لكل مهمة فاشلة، وعدد المحاولات بيفضل محفوظ بين الإعادات،
  والإعادة بتبعت kwargs بأسماء المهمة الحقيقية.
* فحص تخزين التصاميم اليومي كان بيقع ImportError.
* صفحة «إضافة دفعة» لسوق المطابع في الأدمن كانت بتقع.
* تاريخ نهاية الضمان بتوقيت المحل مش UTC.
"""
import datetime as _dt
import importlib
import uuid
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.core.cache import cache
from django.test import SimpleTestCase
from django.utils import timezone

from inventory.models import SaleInvoiceItem, Treasury
from inventory.tasks import (RECON_LAST_KEY, dlq_task_kwargs, drain_dlq_and_retry,
                             process_financial_reconciliation)

from .base import ERPTenantTestCase
from .factories import (make_branch, make_customer, make_financial_transaction, make_product,
                        make_sale_invoice, make_treasury)


class NightlyReconciliationFanOutTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.till = make_treasury(self.branch, name='خزنة المطابقة', balance='0.00')
        make_financial_transaction(self.till, '500.00', 'in')
        cache.delete(RECON_LAST_KEY.format(self.tenant.schema_name))

    def _run_public(self):
        # زي Beat بالظبط: schema_name='public'
        return process_financial_reconciliation(schema_name='public')

    def test_public_run_reconciles_every_tenant_instead_of_crashing(self):
        with mock.patch('clients.services.ops_alerts.send_ops_alert') as alert:
            result = self._run_public()
        self.assertGreaterEqual(result['tenants'], 1)
        self.assertNotIn(self.tenant.schema_name, result['failed'])
        self.assertEqual(result['new_discrepancies'], 0)   # الرصيد = الحركات
        alert.assert_not_called()

    def test_new_discrepancy_alerts_the_owner_once(self):
        Treasury.objects.filter(pk=self.till.pk).update(balance=Decimal('700.00'))
        with mock.patch('clients.services.ops_alerts.send_ops_alert') as alert:
            first = self._run_public()
            second = self._run_public()          # نفس الفرق تاني ليلة ⇒ مفيش إيميل تاني
        self.assertGreaterEqual(first['new_discrepancies'], 1)
        self.assertEqual(second['new_discrepancies'], 0)
        self.assertEqual(alert.call_count, 1)
        body = alert.call_args[0][1]
        self.assertIn('خزنة المطابقة', body)
        self.assertIn('200.00', body)

        # الفرق اتغيّر ⇒ تنبيه جديد
        Treasury.objects.filter(pk=self.till.pk).update(balance=Decimal('650.00'))
        with mock.patch('clients.services.ops_alerts.send_ops_alert') as alert:
            third = self._run_public()
        self.assertGreaterEqual(third['new_discrepancies'], 1)
        alert.assert_called_once()

    def test_single_schema_run_still_works(self):
        Treasury.objects.filter(pk=self.till.pk).update(balance=Decimal('700.00'))
        state = process_financial_reconciliation(schema_name=self.tenant.schema_name)
        self.assertFalse(state['clean'])
        self.assertEqual(state['discrepancies'][0]['treasury_name'], 'خزنة المطابقة')

    def test_uses_shop_date_not_utc(self):
        # ١٢:٣٠ بالليل في القاهرة = ١٠:٣٠ مساءً UTC يوم امبارح
        cairo = timezone.make_aware(_dt.datetime(2026, 11, 13, 0, 30))
        with mock.patch('django.utils.timezone.now', return_value=cairo):
            state = process_financial_reconciliation(schema_name=self.tenant.schema_name)
        self.assertEqual(state['date'], '2026-11-13')


class WarrantyEndUsesShopDateTests(ERPTenantTestCase):
    def test_warranty_counts_from_cairo_day(self):
        branch = make_branch()
        product = make_product(warranty_months=1)
        invoice = make_sale_invoice(make_customer(), branch)
        cairo = timezone.make_aware(_dt.datetime(2026, 11, 13, 0, 30))   # UTC لسه ١٢ نوفمبر
        with mock.patch('django.utils.timezone.now', return_value=cairo):
            item = SaleInvoiceItem.objects.create(invoice=invoice, product=product, quantity=1,
                                                  unit_price=Decimal('100.00'))
        self.assertEqual(item.warranty_end_date, _dt.date(2026, 11, 13) + _dt.timedelta(days=30))


class DeadLetterQueueTests(SimpleTestCase):
    def setUp(self):
        from erp_core.orchestrator import DeadLetterQueue
        self.q = DeadLetterQueue(f'test_{uuid.uuid4().hex[:8]}')
        self.payload = {'schema': 'shop1', 'product_id': 7}

    def tearDown(self):
        client = cache.client.get_client()
        for key in client.scan_iter(f'{self.q._key}*'):
            client.delete(key)

    def test_repeated_failures_of_same_task_are_queued_once(self):
        for _ in range(3):   # المحاولة الأصلية + إعادتين من Celery
            self.q.push('b2b_marketplace_sync_task', self.payload, error='boom', schema='shop1')
        self.assertEqual(self.q.size(), 1)
        # بعد ما تخرج من الطابور، الفشل الجاي يدخل تاني
        self.assertIsNotNone(self.q.pop())
        self.q.push('b2b_marketplace_sync_task', self.payload, error='boom', schema='shop1')
        self.assertEqual(self.q.size(), 1)

    def test_retry_count_survives_redispatch(self):
        self.q.push('b2b_marketplace_sync_task', self.payload, error='boom')
        self.assertEqual(self.q.pop()['retry_count'], 0)
        self.q.mark_redispatched('b2b_marketplace_sync_task', self.payload, 1)
        self.q.push('b2b_marketplace_sync_task', self.payload, error='boom again')
        self.assertEqual(self.q.pop()['retry_count'], 1)

    def test_payload_is_mapped_to_real_task_kwargs(self):
        self.assertEqual(dlq_task_kwargs('b2b_marketplace_sync_task', {'schema': 's', 'product_id': 5}),
                         {'schema_name': 's', 'product_id': 5})
        self.assertEqual(
            dlq_task_kwargs('b2b_marketplace_remove_task', {'schema': 's', 'part_number': 'P1', 'condition': 'new'}),
            {'schema_name': 's', 'part_number': 'P1', 'condition': 'new'})
        self.assertEqual(dlq_task_kwargs('inventory_tasks_financial', {'schema': 's', 'date': None}),
                         {'schema_name': 's', 'date_str': None})
        self.assertIsNone(dlq_task_kwargs('inventory_tasks_ai_vision', {'schema': 's', 'invoice_id': 1}))
        self.assertEqual(dlq_task_kwargs('ai_bidding_award_agent', {'bid_id': 3}), {'bid_id': 3})

    def test_drain_redispatches_with_task_kwargs_and_gives_up_after_three(self):
        agent = 'b2b_marketplace_sync_task'
        with mock.patch('inventory.tasks.dlq', self.q), \
                mock.patch('celery.app.base.Celery.send_task') as send:
            for attempt in range(1, 4):
                self.q.push(agent, self.payload, error='boom')     # الإعادة اللي فاتت فشلت
                drain_dlq_and_retry()
                self.assertEqual(send.call_count, attempt)
                self.assertEqual(send.call_args.kwargs['kwargs'], {'schema_name': 'shop1', 'product_id': 7})
            self.q.push(agent, self.payload, error='still boom')   # فشلت للمرة الرابعة
            drain_dlq_and_retry()
        self.assertEqual(send.call_count, 3)        # مفيش إعادة رابعة
        self.assertEqual(self.q.size(), 0)


class SmallAdminAndCommandFixesTests(SimpleTestCase):
    def test_design_storage_audit_command_imports(self):
        importlib.import_module('clients.management.commands.audit_design_storage')

    def test_market_payment_admin_has_no_add_page(self):
        from print_market.admin import MarketPaymentAdmin
        from print_market.models import MarketPayment
        ma = MarketPaymentAdmin(MarketPayment, admin.site)
        self.assertFalse(ma.has_add_permission(mock.Mock()))
        self.assertEqual(ma.receipt_link(MarketPayment()), '—')
