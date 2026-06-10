"""
Phase 2 — KYC / Trust Score tests.

Verifies:
1. Trust score calculation matches the documented 5-tier ladder.
2. Masking helpers redact name / phone / email correctly.
3. Reveal policy: contact details are masked for strangers, revealed
   for the seller themselves, and revealed for buyers who have funded
   an escrow on this listing.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.test import TestCase

from clients.models import (
    MarketplaceCustomer,
    UserVerification,
    PartCarMake,
    PartListing,
    PartOrder,
)
from clients.services.trust import (
    mask_name, mask_phone, mask_email,
    can_reveal_contact, contact_view,
    get_trust_badge,
)


def _fresh_phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


def _make_customer(email='', is_verified=False) -> MarketplaceCustomer:
    return MarketplaceCustomer.objects.create(
        customer_type='individual',
        full_name='Saied Mohamed',
        phone=_fresh_phone(),
        email=email,
        sector='automotive',
        is_verified=is_verified,
    )


# ---------------------------------------------------------------------------
# 1. Trust score ladder
# ---------------------------------------------------------------------------
class TrustScoreTests(TestCase):
    def test_new_customer_starts_at_zero(self):
        c = _make_customer()
        v = UserVerification.objects.create(customer=c)
        self.assertEqual(v.trust_score, 0)
        self.assertEqual(v.trust_tier, 'new')

    def test_phone_otp_pushes_to_basic_20(self):
        c = _make_customer(is_verified=True)
        v = UserVerification.objects.create(customer=c)
        self.assertEqual(v.trust_score, 20)
        self.assertEqual(v.trust_tier, 'basic')

    def test_email_present_pushes_to_email_40(self):
        c = _make_customer(email='saied@example.com', is_verified=True)
        v = UserVerification.objects.create(customer=c)
        self.assertEqual(v.trust_score, 40)
        self.assertEqual(v.trust_tier, 'email')

    def test_id_approved_pushes_to_id_70(self):
        c = _make_customer(email='x@y.com', is_verified=True)
        v = UserVerification.objects.create(customer=c, id_status='approved')
        self.assertEqual(v.trust_score, 70)
        self.assertEqual(v.trust_tier, 'id')

    def test_business_license_pushes_to_business_100(self):
        c = _make_customer(email='x@y.com', is_verified=True)
        v = UserVerification.objects.create(
            customer=c, id_status='approved',
            workshop_license_status='approved',
        )
        self.assertEqual(v.trust_score, 100)
        self.assertEqual(v.trust_tier, 'business')


# ---------------------------------------------------------------------------
# 2. Masking primitives
# ---------------------------------------------------------------------------
class MaskingTests(TestCase):
    def test_mask_name_handles_multiword(self):
        self.assertEqual(mask_name('Saied Mohamed Ali'), 'S**** M****** A**')

    def test_mask_name_handles_empty(self):
        self.assertEqual(mask_name(''), '—')
        self.assertEqual(mask_name(None), '—')

    def test_mask_phone_keeps_head_and_tail(self):
        self.assertEqual(mask_phone('+201234567890'), '+201*****7890')

    def test_mask_phone_short_input_is_fully_masked(self):
        self.assertEqual(mask_phone('123'), '***')

    def test_mask_email_keeps_first_char_and_domain(self):
        self.assertEqual(mask_email('saied@gmail.com'), 's****@gmail.com')

    def test_mask_email_handles_invalid(self):
        self.assertEqual(mask_email('not-an-email'), '—')


# ---------------------------------------------------------------------------
# 3. Reveal policy
# ---------------------------------------------------------------------------
class RevealPolicyTests(TestCase):
    def setUp(self):
        self.seller = _make_customer(is_verified=True, email='seller@x.com')
        self.buyer = _make_customer(is_verified=True, email='buyer@x.com')
        self.stranger = _make_customer(is_verified=True)
        UserVerification.objects.create(customer=self.seller, id_status='approved')
        UserVerification.objects.create(customer=self.buyer)

        self.make = PartCarMake.objects.create(
            name=f'M-{uuid.uuid4().hex[:6]}', slug=f's-{uuid.uuid4().hex[:6]}',
        )
        self.listing = PartListing.objects.create(
            seller_customer=self.seller,
            title='X', description='Y',
            car_make=self.make, price_egp=Decimal('100'), warranty_days=3,
            status='active', moderation_status='approved',
        )

    def _make_order(self, status):
        return PartOrder.objects.create(
            listing=self.listing,
            buyer_customer=self.buyer,
            amount_paid=Decimal('100'),
            commission_amount=Decimal('8'),
            seller_payout=Decimal('92'),
            warranty_days=3,
            status=status,
        )

    def test_seller_can_always_see_own_contact(self):
        self.assertTrue(can_reveal_contact(self.seller, self.seller))

    def test_stranger_cannot_see_seller_contact(self):
        self.assertFalse(can_reveal_contact(self.stranger, self.seller))

    def test_buyer_with_pending_payment_order_still_masked(self):
        order = self._make_order('pending_payment')
        self.assertFalse(can_reveal_contact(self.buyer, self.seller, order))

    def test_buyer_with_paid_held_order_can_reveal(self):
        order = self._make_order('paid_held')
        self.assertTrue(can_reveal_contact(self.buyer, self.seller, order))
        # Also true for the seller looking at the buyer.
        self.assertTrue(can_reveal_contact(self.seller, self.buyer, order))

    def test_contact_view_returns_masked_for_stranger(self):
        view = contact_view(self.seller, viewer=self.stranger)
        self.assertFalse(view['is_revealed'])
        self.assertNotEqual(view['phone'], self.seller.phone)
        self.assertIn('*', view['phone'])

    def test_contact_view_returns_real_for_paying_buyer(self):
        order = self._make_order('paid_held')
        view = contact_view(self.seller, viewer=self.buyer, order=order)
        self.assertTrue(view['is_revealed'])
        self.assertEqual(view['phone'], self.seller.phone)
        self.assertEqual(view['email'], self.seller.email)

    def test_badge_reflects_seller_tier(self):
        badge = get_trust_badge(self.seller)
        self.assertEqual(badge['tier'], 'id')
        self.assertEqual(badge['score'], 70)
