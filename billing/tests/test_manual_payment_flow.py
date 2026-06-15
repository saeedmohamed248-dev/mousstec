"""
Regression tests for the unified Vodafone Cash / InstaPay manual payment flow.

Covers:
  * ManualPaymentReceipt purchase_type='design' → confirm() activates
    DesignPurchase (status='paid').
  * ManualPaymentReceipt purchase_type='tenant_topup' → confirm() activates
    TenantDesignTopUp (status='paid', paid_at set).
  * design_store_buy view with payment_method=vodafone_cash creates a
    ManualPaymentReceipt and routes the user to the unified upload page
    (NOT the legacy single-screen flow).
  * design_store_buy view with payment_method=instapay creates a
    ManualPaymentReceipt with payment_method='instapay'.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.test import TestCase, RequestFactory

from clients.views.design_store_views import design_store_buy

from clients.models import (
    Client as TenantClient,
    DesignPackage,
    DesignPurchase,
    ManualPaymentReceipt,
    MarketplaceCustomer,
    TenantDesignTopUp,
)


def _customer():
    c = MarketplaceCustomer.objects.create(
        customer_type='individual',
        full_name='علي محمد',
        phone=f'+2010{uuid.uuid4().int % 100000000:08d}',
        sector='printing',
        is_verified=True,
    )
    # Set a session_token so _marketplace_auth can resolve it from the
    # `mp_session` cookie in view-level tests.
    c.session_token = uuid.uuid4()
    c.save(update_fields=['session_token'])
    return c


def _design_package():
    return DesignPackage.objects.create(
        slug=f'tst-pkg-{uuid.uuid4().hex[:6]}',
        target_audience='customer',
        name_ar='باقة اختبار',
        designs_count=10,
        price_egp=Decimal('250.00'),
        is_active=True,
    )


class ManualPaymentReceiptConfirmTests(TestCase):
    """Receipt.confirm() must activate the right underlying purchase."""

    def test_design_receipt_confirm_marks_purchase_paid(self):
        customer = _customer()
        pkg = _design_package()
        purchase = DesignPurchase.objects.create(
            customer=customer, package=pkg,
            designs_total=10, price_paid=pkg.price_egp,
            payment_method='vodafone_cash', status='pending',
        )
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='design', purchase_id=purchase.pk,
            amount=pkg.price_egp, payment_method='vodafone_cash',
            customer=customer, sender_phone='01000000000',
            txn_reference='VF-TEST-9999',
        )

        receipt.confirm(notes='تم التحقق')

        receipt.refresh_from_db()
        purchase.refresh_from_db()
        self.assertEqual(receipt.status, 'confirmed')
        self.assertEqual(purchase.status, 'paid')
        self.assertEqual(purchase.payment_reference, 'VF-TEST-9999')
        self.assertEqual(purchase.sender_phone, '01000000000')
        self.assertIsNotNone(purchase.paid_at)

    def test_design_receipt_confirm_is_idempotent(self):
        customer = _customer()
        pkg = _design_package()
        purchase = DesignPurchase.objects.create(
            customer=customer, package=pkg,
            designs_total=10, price_paid=pkg.price_egp,
            payment_method='instapay', status='pending',
        )
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='design', purchase_id=purchase.pk,
            amount=pkg.price_egp, payment_method='instapay',
            customer=customer, sender_phone='01000000000',
            txn_reference='IP-1', status='confirmed',
        )
        # Already confirmed — confirm() should be a no-op (not re-activate).
        receipt.confirm()
        purchase.refresh_from_db()
        self.assertEqual(purchase.status, 'pending')  # untouched

    def test_tenant_topup_receipt_confirm_marks_paid(self):
        # bulk_create bypasses django-tenants' post_save signal that would
        # otherwise spin up a real Postgres schema for the test tenant.
        schema = f'tst_{uuid.uuid4().hex[:8]}'
        TenantClient.objects.bulk_create([TenantClient(
            schema_name=schema,
            name='ورشة الاختبار',
            owner_name='عبدالله',
            phone='01000000000',
            industry='printing',
            business_type='print_shop',
        )])
        tenant = TenantClient.objects.get(schema_name=schema)

        topup = TenantDesignTopUp.objects.create(
            tenant=tenant,
            designs_total=20, designs_used=0,
            price_paid=Decimal('500.00'),
            payment_method='vodafone_cash',
            status='pending',
        )
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='tenant_topup', purchase_id=topup.pk,
            amount=topup.price_paid, payment_method='vodafone_cash',
            tenant=tenant, sender_phone='01099999999',
            txn_reference='VF-TOPUP-77',
        )

        receipt.confirm(notes='ok')

        topup.refresh_from_db()
        self.assertEqual(topup.status, 'paid')
        self.assertEqual(topup.payment_reference, 'VF-TOPUP-77')
        self.assertIsNotNone(topup.paid_at)


class DesignStoreBuyRoutingTests(TestCase):
    """design_store_buy must create a ManualPaymentReceipt for manual methods."""

    def setUp(self):
        self.rf = RequestFactory()
        self.customer = _customer()

    def _call_buy(self, pkg, method):
        req = self.rf.post(
            f'/marketplace/design-store/buy/{pkg.slug}/',
            data={'payment_method': method},
        )
        req.COOKIES['mp_session'] = str(self.customer.session_token)
        return design_store_buy(req, package_slug=pkg.slug)

    def test_vodafone_cash_creates_receipt_and_redirects_to_unified_upload(self):
        pkg = _design_package()
        resp = self._call_buy(pkg, 'vodafone_cash')

        self.assertEqual(resp.status_code, 200, resp.content[:300])
        import json as _json
        data = _json.loads(resp.content)
        self.assertIn('/payment/manual/upload/', data['redirect'])
        purchase = DesignPurchase.objects.get(pk=data['purchase_id'])
        receipt = ManualPaymentReceipt.objects.get(
            purchase_type='design', purchase_id=purchase.pk,
        )
        self.assertEqual(receipt.payment_method, 'vodafone_cash')
        self.assertEqual(receipt.status, 'pending')

    def test_instapay_creates_receipt_with_correct_method(self):
        pkg = _design_package()
        resp = self._call_buy(pkg, 'instapay')

        self.assertEqual(resp.status_code, 200, resp.content[:300])
        import json as _json
        data = _json.loads(resp.content)
        purchase = DesignPurchase.objects.get(pk=data['purchase_id'])
        receipt = ManualPaymentReceipt.objects.get(
            purchase_type='design', purchase_id=purchase.pk,
        )
        self.assertEqual(receipt.payment_method, 'instapay')


class ManualPaymentUploadPageTests(TestCase):
    """The unified upload page must render Vodafone + InstaPay handles."""

    def test_upload_page_renders_both_payment_handles(self):
        customer = _customer()
        pkg = _design_package()
        purchase = DesignPurchase.objects.create(
            customer=customer, package=pkg,
            designs_total=10, price_paid=pkg.price_egp,
            payment_method='vodafone_cash', status='pending',
        )
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='design', purchase_id=purchase.pk,
            amount=pkg.price_egp, payment_method='vodafone_cash',
            customer=customer, sender_phone='', txn_reference='',
        )

        from clients.views.manual_payment_views import manual_payment_upload
        rf = RequestFactory()
        req = rf.get(f'/payment/manual/upload/{receipt.receipt_code}/')
        resp = manual_payment_upload(req, receipt_code=receipt.receipt_code)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('01094850763', body)
        self.assertIn('@instapay', body)
