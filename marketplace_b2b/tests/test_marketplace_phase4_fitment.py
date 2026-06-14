"""
Phase 4 — Vehicle fitment & wanted-request tests.

Verifies:
1. Engine-code matters: N13 request doesn't match N20 stock.
2. Year matters: 2015 request doesn't match 2018 request when filtered.
3. Make filter respects FK identity.
4. Expired and non-open requests are excluded from the seller feed.
5. Engine code is normalized to uppercase on save.
6. UniqueConstraint blocks duplicate offers from the same seller on
   the same request.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from clients.models import (
    MarketplaceCustomer, PartCarMake, PartWantedRequest, PartWantedOffer,
)
from clients.services.fitment import open_wanted_requests


def _phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


def _customer() -> MarketplaceCustomer:
    return MarketplaceCustomer.objects.create(
        customer_type='individual', full_name='X', phone=_phone(),
        sector='automotive', is_verified=True,
    )


def _make(name='BMW') -> PartCarMake:
    return PartCarMake.objects.create(
        name=f'{name}-{uuid.uuid4().hex[:6]}',
        slug=f'{name.lower()}-{uuid.uuid4().hex[:6]}',
    )


def _request(buyer, make, *, model='F30', year=2015, engine='N13', **k):
    return PartWantedRequest.objects.create(
        buyer_customer=buyer, car_make=make,
        car_model=model, car_year=year, engine_code=engine,
        part_name=k.pop('part_name', 'Throttle body'),
        **k,
    )


class FitmentFilterTests(TestCase):
    def setUp(self):
        self.buyer = _customer()
        self.bmw = _make('BMW')
        self.honda = _make('Honda')

    def test_engine_code_strict_match(self):
        r_n13 = _request(self.buyer, self.bmw, engine='N13')
        _request(self.buyer, self.bmw, engine='N20', part_name='Other')

        qs = open_wanted_requests(make=self.bmw, model='F30', year=2015, engine_code='N13')
        ids = list(qs.values_list('pk', flat=True))
        self.assertEqual(ids, [r_n13.pk])

    def test_engine_code_case_insensitive(self):
        r = _request(self.buyer, self.bmw, engine='N13')
        qs = open_wanted_requests(make=self.bmw, engine_code='n13')
        self.assertIn(r.pk, qs.values_list('pk', flat=True))

    def test_year_mismatch_excludes(self):
        _request(self.buyer, self.bmw, year=2015)
        qs = open_wanted_requests(make=self.bmw, year=2018)
        self.assertEqual(qs.count(), 0)

    def test_make_isolation(self):
        _request(self.buyer, self.bmw)
        r_honda = _request(self.buyer, self.honda, model='Civic', engine='K20A')
        qs = open_wanted_requests(make=self.honda)
        self.assertEqual(list(qs.values_list('pk', flat=True)), [r_honda.pk])

    def test_model_iexact(self):
        r = _request(self.buyer, self.bmw, model='F30')
        qs = open_wanted_requests(make=self.bmw, model='f30')
        self.assertIn(r.pk, qs.values_list('pk', flat=True))

    def test_no_filters_returns_all_open(self):
        r1 = _request(self.buyer, self.bmw)
        r2 = _request(self.buyer, self.honda, model='Civic', engine='K20A')
        qs = open_wanted_requests()
        self.assertSetEqual({r1.pk, r2.pk}, set(qs.values_list('pk', flat=True)))


class FeedExclusionTests(TestCase):
    def setUp(self):
        self.buyer = _customer()
        self.make = _make()

    def test_expired_request_excluded(self):
        r = _request(self.buyer, self.make)
        r.expires_at = timezone.now() - timedelta(days=1)
        r.save(update_fields=['expires_at'])
        qs = open_wanted_requests(make=self.make)
        self.assertNotIn(r.pk, qs.values_list('pk', flat=True))

    def test_non_open_status_excluded(self):
        r = _request(self.buyer, self.make)
        r.status = 'fulfilled'
        r.save(update_fields=['status'])
        self.assertNotIn(r.pk, open_wanted_requests().values_list('pk', flat=True))

    def test_soft_deleted_excluded(self):
        r = _request(self.buyer, self.make)
        r.soft_delete()
        self.assertNotIn(r.pk, open_wanted_requests().values_list('pk', flat=True))


class NormalizationTests(TestCase):
    def test_engine_code_uppercased_on_save(self):
        buyer = _customer()
        make = _make()
        r = _request(buyer, make, engine='n13 ')
        self.assertEqual(r.engine_code, 'N13')

    def test_default_expiry_14_days(self):
        r = _request(_customer(), _make())
        self.assertAlmostEqual(
            (r.expires_at - timezone.now()).days, 13, delta=1,
        )


class OfferConstraintTests(TestCase):
    def test_duplicate_offer_per_seller_blocked(self):
        buyer = _customer()
        seller = _customer()
        make = _make()
        req = _request(buyer, make)
        PartWantedOffer.objects.create(
            request=req, seller_customer=seller,
            price_egp=Decimal('500'), warranty_days=7,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PartWantedOffer.objects.create(
                    request=req, seller_customer=seller,
                    price_egp=Decimal('400'), warranty_days=7,
                )
