"""
💸 تحصيل فاتورة اشتراك «مستحقة» (فودافون كاش / إنستاباي / تحصيل يدوي من لوحة المنصة).

اللي اتلقى في المراجعة:
* فترة الاشتراك كانت بتتحسب وقت طلب الدفع وبتتطبّق زي ما هي وقت التأكيد —
  الشركة لو طلبت التجديد مرتين (أو الأدمن أصدر فاتورتين) الاتنين بيتحسبوا على
  نفس الفترة: بتدفع مرتين وتاخد شهر واحد. ولو التأكيد اتأخر بعد ما الاشتراك
  خلص، الأيام اللي فاتت بتضيع من الفترة المدفوعة. (الدفع بالكارت كان بيحسبها
  وقت الدفع من الأول — settle_gateway_invoice.)
* تأكيد الإيصال كان بيبلع أي خطأ في التفعيل: الأدمن يشوف «✅ اتفعّل» والإيصال
  «مؤكد» والاشتراك ماتمدّش — ومفيش طريقة يعيد المحاولة (المؤكد مابيتأكدش تاني).
* ترقية باقة التشخيص: الباقة بتتخزّن في ملاحظات الإيصال، وملاحظة العميل وقت رفع
  الإيصال كانت بتمسحها — التأكيد يفشل في صمت والعميل دفع ومفيش ترقية.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from clients.models import (
    Client as TenantClient, CustomerDiagnosticsSubscription, ManualPaymentReceipt, MarketplaceCustomer,
    Plan, PlatformInvoice, TenantSubscription,
)
from clients.services.platform_billing import issue_subscription_invoice


def _tenant(end_date):
    schema = f'tst_{uuid.uuid4().hex[:8]}'
    # bulk_create: من غير ما django-tenants يعمل سكيمة حقيقية
    TenantClient.objects.bulk_create([TenantClient(
        schema_name=schema, name='مركز الاختبار', owner_name='صاحب', phone='01000000000',
        industry='automotive', subscription_end_date=end_date)])
    return TenantClient.objects.get(schema_name=schema)


def _plan():
    plan, _ = Plan.objects.get_or_create(
        slug='silver-settle', defaults={'name': 'Silver', 'industry': 'automotive',
                                        'monthly_price': Decimal('550.00'), 'is_active': True})
    return plan


def _manual_receipt(tenant, plan, ref):
    inv = issue_subscription_invoice(tenant, plan, 1, paid=False)
    receipt = ManualPaymentReceipt.objects.create(
        purchase_type='subscription', purchase_id=inv.pk, amount=inv.total,
        payment_method='vodafone_cash', tenant=tenant, sender_phone='01000000001',
        txn_reference=ref)
    return inv, receipt


class SubscriptionSettlementTests(TestCase):

    def setUp(self):
        # حفظ شركة مالهاش سكيمة (bulk_create) بيخلّي django-tenants يعملها كاملة
        # (دقيقة لكل اختبار) — والاختبارات دي على جداول المنصة بس.
        patcher = mock.patch.object(TenantClient, 'auto_create_schema', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_pending_renewals_extend_back_to_back(self):
        today = timezone.localdate()
        tenant = _tenant(today + timedelta(days=5))
        plan = _plan()
        first_inv, first = _manual_receipt(tenant, plan, 'VF-A1')
        second_inv, second = _manual_receipt(tenant, plan, 'VF-A2')
        # الاتنين اتعملوا والاشتراك لسه منتهي بعد ٥ أيام ⇒ نفس الفترة وقت الطلب
        self.assertEqual(first_inv.period_start, second_inv.period_start)

        first.confirm()
        second.confirm()

        first_inv.refresh_from_db()
        second_inv.refresh_from_db()
        tenant.refresh_from_db()
        self.assertEqual((first_inv.status, second_inv.status), ('paid', 'paid'))
        self.assertEqual(second_inv.period_start, first_inv.period_end)
        self.assertEqual(tenant.subscription_end_date, today + timedelta(days=5 + 30 + 30))

    def test_late_confirmation_starts_from_the_confirmation_day(self):
        today = timezone.localdate()
        tenant = _tenant(today - timedelta(days=20))      # الاشتراك خلص من ٢٠ يوم
        plan = _plan()
        inv, receipt = _manual_receipt(tenant, plan, 'VF-B1')
        # الطلب اتعمل من ١٠ أيام والأدمن أكّد النهارده
        PlatformInvoice.objects.filter(pk=inv.pk).update(
            period_start=today - timedelta(days=10), period_end=today + timedelta(days=20))
        receipt.confirm()
        inv.refresh_from_db()
        tenant.refresh_from_db()
        self.assertEqual((inv.period_start, inv.period_end), (today, today + timedelta(days=30)))
        self.assertEqual(tenant.subscription_end_date, today + timedelta(days=30))
        self.assertEqual(TenantSubscription.objects.get(tenant=tenant).current_period_end,
                         today + timedelta(days=30))

    def test_failed_activation_keeps_the_receipt_pending(self):
        today = timezone.localdate()
        tenant = _tenant(today)
        inv, receipt = _manual_receipt(tenant, _plan(), 'VF-C1')
        with mock.patch.object(PlatformInvoice, 'mark_paid', side_effect=RuntimeError('boom')):
            with self.assertRaises(Exception):
                receipt.confirm()
        receipt.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual((receipt.status, inv.status), ('pending', 'issued'))
        receipt.confirm()     # إعادة المحاولة بعد ما المشكلة اتحلّت
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')

    def test_reused_transfer_reference_is_refused_not_silently_dropped(self):
        today = timezone.localdate()
        tenant = _tenant(today)
        plan = _plan()
        _inv1, first = _manual_receipt(tenant, plan, 'VF-SAME')
        _inv2, second = _manual_receipt(tenant, plan, 'VF-SAME')
        first.confirm()
        with self.assertRaises(ValidationError):
            second.confirm()
        second.refresh_from_db()
        self.assertEqual(second.status, 'pending')

    def test_diagnostics_tier_survives_the_customers_upload_note(self):
        from django.contrib.auth.models import AnonymousUser
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import RequestFactory
        from clients.views.manual_payment_views import manual_payment_upload
        customer = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='سائق', is_verified=True,
            phone=f'+2010{uuid.uuid4().int % 100000000:08d}', sector='automotive')
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='diagnostics', purchase_id=sub.pk, amount=Decimal('199.00'),
            payment_method='vodafone_cash', customer=customer, notes='pro',
            sender_phone='', txn_reference='')
        png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
               b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01'
               b'\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
        request = RequestFactory().post('/x/', {
            'txn_reference': 'VF-DIAG-1', 'sender_phone': '01011112222',
            'notes': 'حوّلت من رقم أخويا',
            'receipt_image': SimpleUploadedFile('r.png', png, content_type='image/png')})
        request.user = AnonymousUser()
        r = manual_payment_upload(request, receipt.receipt_code)
        self.assertEqual(r.status_code, 200, r.content)
        receipt.refresh_from_db()
        self.assertIn('حوّلت من رقم أخويا', receipt.notes)
        receipt.confirm()
        sub.refresh_from_db()
        self.assertEqual(sub.tier, 'pro')
        self.assertEqual(ManualPaymentReceipt.objects.get(pk=receipt.pk).status, 'confirmed')

    def test_diagnostics_tier_falls_back_to_the_amount_paid(self):
        receipt = ManualPaymentReceipt(purchase_type='diagnostics', purchase_id=1, amount=Decimal('399.00'),
                                       notes='أي كلام من العميل')
        self.assertEqual(receipt.diagnostics_tier(), 'empire')
        receipt.amount = Decimal('123.00')
        with self.assertRaises(ValidationError):
            receipt.diagnostics_tier()
