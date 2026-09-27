"""
Parts marketplace — order lifecycle regression tests (review 2026-09).

Pins the bugs fixed in this pass:

* A Vodafone-Cash receipt approval now notifies the seller (it never did)
  and a rejected receipt frees the reserved listing (it stayed reserved
  forever).
* Abandoned checkouts are expired so the listing goes back on sale.
* Suspended / private listings can't be bought through a direct URL.
* Paymob callback: amount must match, missing order → 404 not 500.
* Escrow settlement self-heals a missing hold on a paid order.
* The "Part Wanted" loop is closed end to end: seller offers → buyer
  accepts → private listing → payment → request fulfilled.
* Sellers can list / withdraw; buyers can cancel an unpaid checkout.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import Client as DjangoClient, TestCase
from django.utils import timezone

from clients.models import (
    CustomerNotification, EscrowHold, ManualPaymentReceipt, MarketplaceCustomer,
    PartCarMake, PartListing, PartOrder, PartWantedOffer, PartWantedRequest,
)
from clients.services import escrow as escrow_svc
from marketplace_b2b.services import parts_orders as orders_svc


class _PublicDomainMixin:
    """Marketplace URLs live on the public schema — map `testserver` to it."""
    _provisioned_domain = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        public = Tenant.objects.filter(schema_name='public').first()
        if public is None:
            public = Tenant(schema_name='public', name='Public', owner_name='T', phone='000')
            public.auto_create_schema = False
            public.save(verbosity=0)
        if not Domain.objects.filter(domain='testserver').exists():
            cls._provisioned_domain = Domain.objects.create(
                tenant=public, domain='testserver', is_primary=False,
            )

    @classmethod
    def tearDownClass(cls):
        if cls._provisioned_domain is not None:
            try:
                cls._provisioned_domain.delete()
            except Exception:
                pass
        super().tearDownClass()


def _phone():
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


def _customer(name='C'):
    return MarketplaceCustomer.objects.create(
        customer_type='individual', full_name=name, phone=_phone(),
        sector='automotive', is_verified=True,
    )


def _make():
    tag = uuid.uuid4().hex[:6]
    return PartCarMake.objects.create(name=f'M-{tag}', slug=f'm-{tag}')


def _listing(seller, **kw):
    defaults = dict(
        seller_customer=seller, title='Fuel pump', description='Good condition pump',
        car_make=_make(), price_egp=Decimal('1000.00'), warranty_days=7,
        status='active', moderation_status='approved',
    )
    defaults.update(kw)
    return PartListing.objects.create(**defaults)


def _pending_order(listing, buyer, **kw):
    PartListing.objects.filter(pk=listing.pk).update(status='reserved')
    defaults = dict(
        listing=listing, buyer_customer=buyer,
        amount_paid=listing.price_egp, commission_amount=listing.commission_amount,
        seller_payout=listing.seller_payout, warranty_days=listing.warranty_days,
        status='pending_payment', shipping_name='B', shipping_phone='01000000000',
        shipping_address='12 Street, Cairo', shipping_city='Cairo',
    )
    defaults.update(kw)
    return PartOrder.objects.create(**defaults)


def _login(customer):
    c = DjangoClient()
    c.cookies['mp_session'] = str(customer.session_token)
    return c


SHIPPING = {
    'shipping_name': 'Buyer', 'shipping_phone': '01012345678',
    'shipping_city': 'Cairo', 'shipping_address': '10 Tahrir street, Downtown',
}


# ─────────────────────────────────────────────────────────────────────
class MarkPaidServiceTests(TestCase):
    def test_mark_paid_sells_listing_holds_escrow_and_notifies_both(self):
        seller, buyer = _customer('S'), _customer('B')
        listing = _listing(seller)
        order = _pending_order(listing, buyer)

        self.assertTrue(orders_svc.mark_paid(order, txn_id='T1'))

        order.refresh_from_db(); listing.refresh_from_db()
        self.assertEqual(order.status, 'paid_held')
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(listing.status, 'sold')
        self.assertTrue(EscrowHold.objects.filter(order=order, status='held').exists())
        self.assertTrue(CustomerNotification.objects.filter(customer=seller).exists())
        self.assertTrue(CustomerNotification.objects.filter(customer=buyer).exists())

    def test_mark_paid_is_idempotent(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        self.assertTrue(orders_svc.mark_paid(order))
        self.assertFalse(orders_svc.mark_paid(order))
        self.assertEqual(EscrowHold.objects.filter(order=order).count(), 1)

    def test_cancel_unpaid_frees_listing(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer)
        self.assertTrue(orders_svc.cancel_unpaid(order, reason='x'))
        order.refresh_from_db(); listing.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(listing.status, 'active')


class ManualReceiptTests(TestCase):
    def _receipt(self, order, **kw):
        defaults = dict(
            purchase_type='parts', purchase_id=order.pk, amount=order.amount_paid,
            customer=order.buyer_customer, sender_phone='01000000000', txn_reference='REF-1',
        )
        defaults.update(kw)
        return ManualPaymentReceipt.objects.create(**defaults)

    def test_confirm_notifies_seller(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        self._receipt(order).confirm()
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid_held')
        self.assertTrue(
            CustomerNotification.objects.filter(customer=seller, title__contains='تم بيع').exists()
        )

    def test_reject_frees_reserved_listing(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer)
        self._receipt(order).reject(notes='no transfer found')
        order.refresh_from_db(); listing.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(listing.status, 'active')

    def test_diagnostics_receipt_confirm_upgrades_tier(self):
        # Regression: CustomerDiagnosticsSubscription wasn't importable from
        # the billing module, so this confirm silently did nothing.
        from clients.models import CustomerDiagnosticsSubscription
        customer = _customer()
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='diagnostics', purchase_id=sub.pk, amount=Decimal('100'),
            customer=customer, sender_phone='010', txn_reference='R', notes='pro',
        )
        self.assertIsNotNone(receipt.get_purchase_object())
        receipt.confirm()
        sub.refresh_from_db()
        self.assertEqual(sub.tier, 'pro')

    def test_cannot_reject_confirmed_receipt(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        receipt = self._receipt(order)
        receipt.confirm()
        with self.assertRaises(ValidationError):
            receipt.reject()

    def test_confirm_after_expiry_revives_order_if_listing_still_free(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer)
        receipt = self._receipt(order)
        orders_svc.cancel_unpaid(order)
        receipt.refresh_from_db()
        receipt.confirm()
        order.refresh_from_db(); listing.refresh_from_db()
        self.assertEqual(order.status, 'paid_held')
        self.assertEqual(listing.status, 'sold')

    def test_confirm_after_expiry_fails_loudly_when_listing_resold(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer)
        receipt = self._receipt(order)
        orders_svc.cancel_unpaid(order)
        PartListing.objects.filter(pk=listing.pk).update(status='sold')
        with self.assertRaises(ValidationError):
            receipt.confirm()
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, 'pending')  # rolled back


class ExpiryTests(TestCase):
    def _age(self, order, hours):
        PartOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=hours))

    def test_stale_paymob_checkout_is_cancelled(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer)
        self._age(order, 3)
        self.assertEqual(orders_svc.expire_stale_orders(), 1)
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'active')

    def test_manual_checkout_with_uploaded_receipt_is_kept(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        ManualPaymentReceipt.objects.create(
            purchase_type='parts', purchase_id=order.pk, amount=order.amount_paid,
            sender_phone='010', txn_reference='UPLOADED',
        )
        self._age(order, 48)
        self.assertEqual(orders_svc.expire_stale_orders(), 0)

    def test_manual_checkout_without_receipt_gets_24h(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='parts', purchase_id=order.pk, amount=order.amount_paid,
            sender_phone='', txn_reference='',
        )
        self._age(order, 5)
        self.assertEqual(orders_svc.expire_stale_orders(), 0)
        self._age(order, 25)
        self.assertEqual(orders_svc.expire_stale_orders(), 1)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, 'rejected')

    def test_expired_wanted_requests_are_closed(self):
        buyer = _customer()
        req = PartWantedRequest.objects.create(
            buyer_customer=buyer, car_make=_make(), car_model='F30', car_year=2015,
            part_name='Mirror', expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(orders_svc.expire_wanted_requests(), 1)
        req.refresh_from_db()
        self.assertEqual(req.status, 'expired')


class EscrowSelfHealTests(TestCase):
    def test_refund_creates_missing_hold_for_paid_order(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        PartOrder.objects.filter(pk=order.pk).update(status='refund_requested', paid_at=timezone.now())
        order.refresh_from_db()
        hold = escrow_svc.refund_to_buyer(order, return_reason='defective')
        self.assertEqual(hold.status, 'refunded_to_buyer')
        self.assertEqual(hold.buyer_refund_amount, order.amount_paid)

    def test_unpaid_order_without_hold_still_rejected(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        with self.assertRaises(ValidationError):
            escrow_svc.release_to_seller(order)


# ─────────────────────────────────────────────────────────────────────
class CheckoutGuardTests(_PublicDomainMixin, TestCase):
    def _start(self, buyer, listing):
        return _login(buyer).post(f'/payment/manual/parts/{listing.listing_code}/start/', SHIPPING)

    def test_manual_checkout_reserves_listing(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        res = self._start(buyer, listing)
        self.assertEqual(res.status_code, 200, res.content)
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'reserved')
        self.assertTrue(PartOrder.objects.filter(listing=listing, status='pending_payment').exists())

    def test_suspended_listing_cannot_be_bought(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller, moderation_status='suspended')
        self.assertEqual(self._start(buyer, listing).status_code, 400)

    def test_private_listing_only_for_its_buyer(self):
        seller, buyer, stranger = _customer(), _customer(), _customer()
        listing = _listing(seller, reserved_for=buyer)
        self.assertEqual(self._start(stranger, listing).status_code, 400)
        self.assertEqual(self._start(buyer, listing).status_code, 200)

    def test_feed_hides_private_listing(self):
        seller, buyer = _customer(), _customer()
        public = _listing(seller, title='PUBLIC-PART')
        _listing(seller, title='PRIVATE-PART', reserved_for=buyer)
        body = DjangoClient().get('/marketplace/parts/').content.decode()
        self.assertIn('PUBLIC-PART', body)
        self.assertNotIn('PRIVATE-PART', body)
        self.assertEqual(public.status, 'active')

    def test_buyer_can_cancel_unpaid_order(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer)
        res = _login(buyer).post(f'/marketplace/parts/order/{order.order_code}/cancel/')
        self.assertEqual(res.status_code, 200, res.content)
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'active')

    def test_create_listing_bad_year_is_400_not_500(self):
        seller = _customer()
        make = _make()
        res = _login(seller).post('/marketplace/parts/sell/', {
            'car_make': make.pk, 'title': 'Pump', 'description': 'A good working pump',
            'price_egp': '100', 'warranty_days': '7', 'car_year_from': 'abc',
        })
        self.assertEqual(res.status_code, 400)

    def test_seller_can_withdraw_active_listing(self):
        seller = _customer()
        listing = _listing(seller)
        res = _login(seller).post(f'/marketplace/parts/{listing.listing_code}/withdraw/')
        self.assertEqual(res.status_code, 200, res.content)
        listing.refresh_from_db()
        self.assertEqual(listing.status, 'removed')

    def test_my_listings_page_renders(self):
        seller = _customer()
        _listing(seller, title='MINE-1', moderation_status='rejected', rejection_reason='blurry photos')
        res = _login(seller).get('/marketplace/parts/my-listings/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('blurry photos', res.content.decode())

    def test_sales_page_shows_shipping_address_to_seller(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        orders_svc.mark_paid(order)
        res = _login(seller).get('/marketplace/parts/sales/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('12 Street, Cairo', res.content.decode())


@mock.patch('clients.services.paymob.verify_paymob_hmac', return_value=(True, 'ok'))
class PaymobCallbackTests(_PublicDomainMixin, TestCase):
    def _post(self, payload):
        return DjangoClient().post(
            '/marketplace/parts/paymob-callback/', data=json.dumps(payload),
            content_type='application/json',
        )

    def test_unknown_order_is_404_not_500(self, _hmac):
        res = self._post({'obj': {'success': True, 'id': 1, 'order': {'id': 'nope'}}})
        self.assertEqual(res.status_code, 404)

    def test_amount_mismatch_is_rejected(self, _hmac):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer, paymob_order_id='PM-1')
        res = self._post({'obj': {'success': True, 'id': 9, 'amount_cents': 100,
                                  'order': {'id': 'PM-1'}}})
        self.assertEqual(res.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending_payment')

    def test_success_marks_paid(self, _hmac):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer, paymob_order_id='PM-2')
        res = self._post({'obj': {'success': True, 'id': 10, 'amount_cents': 100000,
                                  'order': {'id': 'PM-2'}}})
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid_held')

    def test_failure_frees_listing(self, _hmac):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer, paymob_order_id='PM-3')
        self._post({'obj': {'success': False, 'id': 11, 'order': {'id': 'PM-3'}}})
        order.refresh_from_db(); listing.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(listing.status, 'active')


class WantedLoopTests(_PublicDomainMixin, TestCase):
    def test_full_wanted_loop(self):
        buyer, seller, other_seller = _customer('B'), _customer('S'), _customer('S2')
        make = _make()
        req = PartWantedRequest.objects.create(
            buyer_customer=buyer, car_make=make, car_model='F30', car_year=2015,
            part_name='Side mirror',
        )

        # 1) Two sellers offer.
        for s, price in ((seller, '900'), (other_seller, '950')):
            res = _login(s).post(f'/marketplace/parts/wanted/{req.request_code}/offer/', {
                'price_egp': price, 'warranty_days': '10', 'condition': 'used_excellent',
                'notes': 'Original part',
            })
            self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(CustomerNotification.objects.filter(customer=buyer).exists())

        # Buyer can't offer on own request.
        res = _login(buyer).post(f'/marketplace/parts/wanted/{req.request_code}/offer/', {
            'price_egp': '1', 'warranty_days': '3',
        })
        self.assertEqual(res.status_code, 400)

        # 2) Buyer sees them and accepts the cheaper one.
        page = _login(buyer).get('/marketplace/parts/wanted/mine/')
        self.assertEqual(page.status_code, 200)
        offer = PartWantedOffer.objects.get(request=req, seller_customer=seller)
        res = _login(buyer).post(f'/marketplace/parts/wanted/offer/{offer.pk}/accept/')
        self.assertEqual(res.status_code, 200, res.content)

        offer.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(offer.status, 'accepted')
        self.assertEqual(req.status, 'matched')
        self.assertEqual(
            PartWantedOffer.objects.get(request=req, seller_customer=other_seller).status, 'rejected',
        )
        listing = offer.linked_listing
        self.assertEqual(listing.reserved_for_id, buyer.pk)
        self.assertEqual(listing.seller_customer_id, seller.pk)
        self.assertEqual(listing.price_egp, Decimal('900.00'))

        # 3) Only the buyer can open / buy it.
        self.assertEqual(_login(other_seller).get(f'/marketplace/parts/{listing.listing_code}/').status_code, 403)
        self.assertEqual(_login(buyer).get(f'/marketplace/parts/{listing.listing_code}/').status_code, 200)

        # 4) Pay → request fulfilled.
        res = _login(buyer).post(f'/payment/manual/parts/{listing.listing_code}/start/', SHIPPING)
        self.assertEqual(res.status_code, 200, res.content)
        order = PartOrder.objects.get(listing=listing)
        orders_svc.mark_paid(order)
        req.refresh_from_db()
        self.assertEqual(req.status, 'fulfilled')

    def test_cancel_matched_request_removes_private_listing(self):
        buyer, seller = _customer(), _customer()
        req = PartWantedRequest.objects.create(
            buyer_customer=buyer, car_make=_make(), car_model='X5', car_year=2012, part_name='Pump',
        )
        offer = PartWantedOffer.objects.create(
            request=req, seller_customer=seller, price_egp=Decimal('500'), warranty_days=5,
        )
        _login(buyer).post(f'/marketplace/parts/wanted/offer/{offer.pk}/accept/')
        offer.refresh_from_db()
        res = _login(buyer).post(f'/marketplace/parts/wanted/{req.request_code}/cancel/')
        self.assertEqual(res.status_code, 200, res.content)
        offer.linked_listing.refresh_from_db()
        self.assertEqual(offer.linked_listing.status, 'removed')
