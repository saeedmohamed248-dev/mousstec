"""
Phase 1 — Foundation tests.

Covers the two "critical" bugs reported by the user:

1. Soft delete keeps historical FKs intact (no cascade crash on customers
   that have part listings / part orders).
2. New PartListing entries are NOT publicly visible until a Super Admin
   approves them; the public feed + detail view both respect this.

These tests deliberately use the plain Django ``TestCase`` because the
marketplace models live in SHARED_APPS (public schema). No tenant create/
drop dance — keeps the suite fast and deterministic.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.urls import reverse

from clients.models import (
    MarketplaceCustomer,
    PartCarMake,
    PartListing,
    PartOrder,
)


def _fresh_phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


def _make_customer(**overrides) -> MarketplaceCustomer:
    defaults = dict(
        customer_type='individual',
        full_name='Deep Test',
        phone=_fresh_phone(),
        sector='automotive',
        is_verified=True,
    )
    defaults.update(overrides)
    return MarketplaceCustomer.objects.create(**defaults)


def _make_make() -> PartCarMake:
    return PartCarMake.objects.create(
        name=f'BMW-{uuid.uuid4().hex[:6]}',
        slug=f'bmw-{uuid.uuid4().hex[:6]}',
    )


def _make_listing(customer, make, **overrides) -> PartListing:
    defaults = dict(
        seller_customer=customer,
        title='Front headlight assembly',
        description='Used, excellent condition. OEM.',
        car_make=make,
        car_model='F30',
        price_egp=Decimal('1500.00'),
        warranty_days=7,
        status='active',
        moderation_status='approved',
    )
    defaults.update(overrides)
    return PartListing.objects.create(**defaults)


# ---------------------------------------------------------------------------
# 1. Soft-delete keeps FKs intact (no crash on deletion of "deep" customer)
# ---------------------------------------------------------------------------
class SoftDeleteCustomerTests(TestCase):
    def test_customer_with_listings_can_be_soft_deleted_without_crash(self):
        cust = _make_customer(full_name='deep')
        make = _make_make()
        listing = _make_listing(cust, make)

        # This is the exact scenario that used to crash (PROTECT on FK).
        cust.soft_delete(reason='requested by user')

        cust.refresh_from_db()
        listing.refresh_from_db()
        self.assertTrue(cust.is_deleted)
        self.assertEqual(listing.seller_customer_id, cust.pk)  # FK preserved
        # Default manager still returns the row (mixin keeps `objects` open)
        # — the filtering is done explicitly in views via alive_objects/
        # is_deleted=False.
        self.assertTrue(
            MarketplaceCustomer.alive_objects.filter(pk=cust.pk).count() == 0,
            "alive_objects must hide soft-deleted customers",
        )
        self.assertTrue(
            MarketplaceCustomer.all_objects.filter(pk=cust.pk).exists(),
            "all_objects must still expose soft-deleted customers for admin",
        )

    def test_restore_revives_customer(self):
        cust = _make_customer()
        cust.soft_delete()
        cust.restore()
        cust.refresh_from_db()
        self.assertFalse(cust.is_deleted)
        self.assertIsNone(cust.deleted_at)

    def test_force_delete_requires_superuser(self):
        cust = _make_customer()
        User = get_user_model()
        regular = User.objects.create_user(username='reg', password='x')
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            cust.force_delete(regular)


# ---------------------------------------------------------------------------
# 2. Listing moderation gate
# ---------------------------------------------------------------------------
class ListingModerationTests(TestCase):
    def test_new_listings_default_to_pending_approval(self):
        cust = _make_customer()
        make = _make_make()
        listing = PartListing.objects.create(
            seller_customer=cust,
            title='Brake disc',
            description='New OEM',
            car_make=make,
            price_egp=Decimal('500.00'),
            warranty_days=3,
        )
        self.assertEqual(listing.moderation_status, 'pending_approval')
        self.assertFalse(listing.is_publicly_visible)

    def test_pending_listing_hidden_from_public_feed(self):
        cust = _make_customer()
        make = _make_make()
        pending = _make_listing(
            cust, make, status='draft', moderation_status='pending_approval',
        )
        approved = _make_listing(cust, make, title='Approved one')

        rf = RequestFactory()
        # _marketplace_auth + public schema gate — patch the schema check.
        from clients.views import parts_marketplace_views as pmv
        from unittest.mock import patch
        request = rf.get('/marketplace/parts/')
        with patch.object(pmv, 'connection') as mock_conn:
            mock_conn.schema_name = 'public'
            resp = pmv.parts_feed(request)
        body = resp.content.decode('utf-8', errors='ignore')
        self.assertIn('Approved one', body)
        self.assertNotIn(pending.title, body)

    def test_approve_flips_status_and_moderation(self):
        cust = _make_customer()
        make = _make_make()
        listing = PartListing.objects.create(
            seller_customer=cust, title='X', description='Y',
            car_make=make, price_egp=Decimal('100'), warranty_days=3,
        )
        User = get_user_model()
        admin = User.objects.create_superuser(
            username=f'admin-{uuid.uuid4().hex[:6]}', password='x',
        )
        ok = listing.approve(by_user=admin)
        listing.refresh_from_db()
        self.assertTrue(ok)
        self.assertEqual(listing.moderation_status, 'approved')
        self.assertEqual(listing.status, 'active')  # draft auto-promoted
        self.assertEqual(listing.moderated_by_id, admin.pk)
        self.assertTrue(listing.is_publicly_visible)

    def test_reject_stores_reason_and_hides_listing(self):
        cust = _make_customer()
        make = _make_make()
        listing = _make_listing(
            cust, make, status='draft', moderation_status='pending_approval',
        )
        User = get_user_model()
        admin = User.objects.create_superuser(
            username=f'adm-{uuid.uuid4().hex[:6]}', password='x',
        )
        listing.reject(by_user=admin, reason='Photos blurry')
        listing.refresh_from_db()
        self.assertEqual(listing.moderation_status, 'rejected')
        self.assertEqual(listing.rejection_reason, 'Photos blurry')
        self.assertFalse(listing.is_publicly_visible)

    def test_soft_deleted_listing_is_not_visible(self):
        cust = _make_customer()
        make = _make_make()
        listing = _make_listing(cust, make)  # approved + active
        self.assertTrue(listing.is_publicly_visible)
        listing.soft_delete()
        listing.refresh_from_db()
        self.assertFalse(listing.is_publicly_visible)
