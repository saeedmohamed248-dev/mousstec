"""
Second review pass (2026-09) — payout center, ratings, smart matching,
seller privacy.

* Every escrow settlement queues the transfer(s) it creates; an admin marks
  them paid with a reference; customers see it in their wallet.
* Buyers rate sellers once, after delivery; the average shows on listings.
* Approving a listing alerts buyers whose wanted request it fits; posting a
  wanted request alerts sellers who already list it and returns matches.
* The public listing page no longer shows an individual seller's real name.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import Client as DjangoClient, TestCase
from django.utils import timezone

from clients.models import (
    Client, CustomerNotification, MarketplacePayout, PartListing, PartOrder,
    PartWantedRequest,
)
from clients.services import escrow as escrow_svc
from marketplace_b2b.services import matching, payouts as payouts_svc
from marketplace_b2b.services import parts_orders as orders_svc
from marketplace_c2c.tests.test_parts_order_lifecycle import (
    SHIPPING, _PublicDomainMixin, _customer, _listing, _login, _make, _pending_order,
)


def _paid_delivered_order(seller=None, buyer=None, **listing_kw):
    seller = seller or _customer('S')
    buyer = buyer or _customer('B')
    order = _pending_order(_listing(seller, **listing_kw), buyer)
    orders_svc.mark_paid(order)
    order.refresh_from_db()
    order.mark_delivered()
    order.refresh_from_db()
    return order


class PayoutQueueTests(TestCase):
    def test_release_queues_seller_payout_with_net_amount(self):
        order = _paid_delivered_order()
        escrow_svc.release_to_seller(order)
        payout = MarketplacePayout.objects.get(order=order)
        self.assertEqual(payout.kind, 'seller_payout')
        self.assertEqual(payout.amount, order.seller_payout)
        self.assertEqual(payout.customer_id, order.listing.seller_customer_id)
        self.assertEqual(payout.status, 'pending')

    def test_seller_without_payout_details_is_told_to_add_them(self):
        order = _paid_delivered_order()
        escrow_svc.release_to_seller(order)
        n = CustomerNotification.objects.filter(
            customer=order.listing.seller_customer, title__contains='ليك فلوس',
        ).first()
        self.assertIsNotNone(n)
        self.assertIn('سجّل', n.body)

    def test_refund_queues_buyer_refund(self):
        order = _paid_delivered_order()
        escrow_svc.refund_to_buyer(order, return_reason='defective')
        payout = MarketplacePayout.objects.get(order=order)
        self.assertEqual(payout.kind, 'buyer_refund')
        self.assertEqual(payout.amount, order.amount_paid)
        self.assertEqual(payout.customer_id, order.buyer_customer_id)

    def test_split_queues_both_legs(self):
        order = _paid_delivered_order()
        escrow_svc.split_settlement(order, refund_amount=Decimal('200'), return_reason='not_as_described')
        kinds = set(MarketplacePayout.objects.filter(order=order).values_list('kind', flat=True))
        self.assertEqual(kinds, {'seller_payout', 'buyer_refund'})

    def test_queue_is_idempotent(self):
        order = _paid_delivered_order()
        hold = escrow_svc.release_to_seller(order)
        payouts_svc.queue_for_hold(hold)
        self.assertEqual(MarketplacePayout.objects.filter(order=order).count(), 1)

    def test_mark_paid_needs_destination_then_snapshots_it(self):
        order = _paid_delivered_order()
        escrow_svc.release_to_seller(order)
        payout = MarketplacePayout.objects.get(order=order)
        with self.assertRaises(ValidationError):
            payouts_svc.mark_paid(payout, reference='TX-1')

        seller = order.listing.seller_customer
        seller.payout_method, seller.payout_account, seller.payout_account_name = 'vodafone_cash', '01011112222', 'Seller'
        seller.save()
        payouts_svc.mark_paid(payout, reference='TX-1')
        payout.refresh_from_db()
        self.assertEqual(payout.status, 'paid')
        self.assertEqual(payout.account, '01011112222')
        self.assertTrue(CustomerNotification.objects.filter(customer=seller, title__contains='تم تحويل').exists())

        # Editing the profile later never rewrites the paid record.
        seller.payout_account = '01099998888'
        seller.save()
        payout.refresh_from_db()
        self.assertEqual(payout.destination[1], '01011112222')

        with self.assertRaises(ValidationError):
            payouts_svc.mark_paid(payout, reference='TX-2')

    def test_auto_release_sweep_queues_payout(self):
        order = _paid_delivered_order()
        PartOrder.objects.filter(pk=order.pk).update(warranty_ends_at=timezone.now() - timezone.timedelta(hours=1))
        from clients.views.parts_marketplace_views import auto_release_expired_warranties
        self.assertEqual(auto_release_expired_warranties(), 1)
        self.assertTrue(MarketplacePayout.objects.filter(order=order, kind='seller_payout').exists())

    def test_wallet_summary(self):
        order = _paid_delivered_order()
        seller = order.listing.seller_customer
        summary = payouts_svc.wallet_summary(customer=seller)
        self.assertEqual(summary['in_escrow'], order.seller_payout)
        escrow_svc.release_to_seller(order)
        PartOrder.objects.filter(pk=order.pk).update(status='released')
        summary = payouts_svc.wallet_summary(customer=seller)
        self.assertEqual(summary['in_escrow'], Decimal('0.00'))
        self.assertEqual(summary['pending'], order.seller_payout)


class WalletViewTests(_PublicDomainMixin, TestCase):
    def test_wallet_page_and_payout_details_validation(self):
        customer = _customer()
        c = _login(customer)
        self.assertEqual(c.get('/marketplace/parts/wallet/').status_code, 200)
        bad = c.post('/marketplace/parts/wallet/', {
            'payout_method': 'vodafone_cash', 'payout_account': '123', 'payout_account_name': 'Name',
        })
        self.assertEqual(bad.status_code, 400)
        ok = c.post('/marketplace/parts/wallet/', {
            'payout_method': 'vodafone_cash', 'payout_account': '01012345678', 'payout_account_name': 'Name X',
        })
        self.assertEqual(ok.status_code, 200, ok.content)
        customer.refresh_from_db()
        self.assertTrue(customer.has_payout_details)


class RatingTests(_PublicDomainMixin, TestCase):
    def test_rate_once_after_delivery_and_show_on_listing(self):
        order = _paid_delivered_order()
        buyer = order.buyer_customer
        url = f'/marketplace/parts/order/{order.order_code}/rate/'
        self.assertEqual(_login(buyer).post(url, {'rating': '9'}).status_code, 400)
        res = _login(buyer).post(url, {'rating': '4', 'review': 'Fast shipping'})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(_login(buyer).post(url, {'rating': '5'}).status_code, 400)  # once only
        order.refresh_from_db()
        self.assertEqual(order.buyer_rating, 4)

        other = _listing(order.listing.seller_customer, title='Another part by same seller')
        body = DjangoClient().get(f'/marketplace/parts/{other.listing_code}/').content.decode()
        self.assertIn('تقييم البائع', body)
        self.assertIn('(1 تقييم)', body)

    def test_cannot_rate_before_delivery(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        orders_svc.mark_paid(order)
        res = _login(buyer).post(f'/marketplace/parts/order/{order.order_code}/rate/', {'rating': '5'})
        self.assertEqual(res.status_code, 400)


class SellerPrivacyTests(_PublicDomainMixin, TestCase):
    def test_individual_seller_name_is_masked_until_paid_order(self):
        seller = _customer('Mahmoud Abdelrahman')
        listing = _listing(seller)
        body = DjangoClient().get(f'/marketplace/parts/{listing.listing_code}/').content.decode()
        self.assertNotIn('Mahmoud Abdelrahman', body)

        buyer = _customer('Buyer')
        order = _pending_order(listing, buyer)
        orders_svc.mark_paid(order)
        body = _login(buyer).get(f'/marketplace/parts/{listing.listing_code}/').content.decode()
        self.assertIn('Mahmoud Abdelrahman', body)
        self.assertIn(seller.phone, body)


class MatchingTests(_PublicDomainMixin, TestCase):
    def _wanted(self, buyer, make, **kw):
        defaults = dict(buyer_customer=buyer, car_make=make, car_model='F30', car_year=2015,
                        part_name='مراية جانبية يمين')
        defaults.update(kw)
        return PartWantedRequest.objects.create(**defaults)

    def test_tokens_normalise_arabic(self):
        self.assertTrue(matching.tokens('المراية الأمامية') & matching.tokens('مرايه اماميه'))

    def test_approving_listing_alerts_matching_buyer_only(self):
        make, other_make = _make(), _make()
        buyer, unrelated, seller = _customer('B'), _customer('U'), _customer('S')
        self._wanted(buyer, make)
        self._wanted(unrelated, other_make)
        listing = _listing(seller, car_make=make, title='مرايه جانبيه يمين BMW F30', car_model='F30',
                           car_year_from=2012, car_year_to=2018,
                           status='draft', moderation_status='pending_approval')
        listing.approve(by_user=None)
        self.assertTrue(CustomerNotification.objects.filter(customer=buyer, title__contains='لقينا قطعة').exists())
        self.assertFalse(CustomerNotification.objects.filter(customer=unrelated, title__contains='لقينا قطعة').exists())

    def test_year_outside_listing_range_does_not_match(self):
        make = _make()
        buyer, seller = _customer(), _customer()
        req = self._wanted(buyer, make, car_year=2020)
        _listing(seller, car_make=make, title='مراية جانبية يمين', car_year_from=2010, car_year_to=2014)
        self.assertEqual(matching.listings_matching_request(req), [])

    def test_posting_wanted_request_returns_matches_and_pings_seller(self):
        make = _make()
        buyer, seller = _customer(), _customer()
        _listing(seller, car_make=make, title='كمبروسر تكييف', part_number='64-52-9-222-306')
        res = _login(buyer).post('/marketplace/parts/wanted/new/', {
            'car_make': make.pk, 'car_model': 'X5', 'car_year': '2014',
            'part_name': 'Compressor', 'part_number_oem': '64529222306',
        })
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(len(res.json()['matches']), 1)  # same OEM, different wording
        self.assertTrue(CustomerNotification.objects.filter(customer=seller, title__contains='بيدور على قطعة').exists())

    def test_price_guide(self):
        make = _make()
        seller = _customer()
        for price in ('800', '1000', '1200'):
            _listing(seller, car_make=make, title='طرمبة بنزين', price_egp=Decimal(price))
        g = matching.price_guide(car_make_id=make.pk, title='طرمبة بنزين أصلية')
        self.assertEqual(g['count'], 3)
        self.assertEqual(g['min'], Decimal('800'))
        self.assertEqual(g['median'], Decimal('1000'))
        res = DjangoClient().get('/marketplace/parts/price-guide/', {'make': make.pk, 'title': 'طرمبة'})
        self.assertEqual(res.json()['guide']['max'], '1200')
        self.assertIsNone(matching.price_guide(car_make_id=make.pk, title='شكمان'))


class B2BStockSyncTests(TestCase):
    """clients.tasks.async_sync_b2b_marketplace_product must never publish on
    its own and never touch the admin-approved price."""

    def _run(self, *, published=True, active=True, qty=7):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest import mock
        product = SimpleNamespace(part_number='PN-1', name='Filter', brand='Mann', condition='new',
                                  is_b2b_published=published, is_active=active)
        Product = mock.MagicMock()
        Product.objects.filter.return_value.first.return_value = product
        Inventory = mock.MagicMock()
        Inventory.objects.filter.return_value.aggregate.return_value = {'s': qty}
        models = {'Product': Product, 'Inventory': Inventory}
        from clients.tasks import async_sync_b2b_marketplace_product
        with mock.patch('django_tenants.utils.schema_context', lambda *_a, **_k: nullcontext()), \
             mock.patch('django.apps.apps.get_model', side_effect=lambda app, name: models[name]):
            async_sync_b2b_marketplace_product.apply(args=[self.tenant.schema_name, 1]).get()

    def setUp(self):
        from clients.models import GlobalB2BMarketplace
        self.GM = GlobalB2BMarketplace
        self.tenant = Client(schema_name='b2bsync_t', name='Sync T', owner_name='O', phone='0100')
        self.tenant.auto_create_schema = False
        self.tenant.save()

    def test_unlisted_product_is_never_auto_published(self):
        self._run()
        self.assertFalse(self.GM.objects.filter(tenant=self.tenant).exists())

    def test_existing_listing_gets_qty_but_keeps_approved_price(self):
        self.GM.objects.create(tenant=self.tenant, part_number='PN-1', product_name='Old', condition='new',
                               wholesale_price=Decimal('150'), available_qty=1)
        self._run(qty=9)
        row = self.GM.objects.get(tenant=self.tenant)
        self.assertEqual(row.available_qty, 9)
        self.assertEqual(row.wholesale_price, Decimal('150.00'))
        self.assertEqual(row.product_name, 'Filter')

    def test_unpublished_or_out_of_stock_is_removed(self):
        for kw in ({'published': False}, {'qty': 0}, {'active': False}):
            self.GM.objects.create(tenant=self.tenant, part_number='PN-1', product_name='X', condition='new',
                                   wholesale_price=Decimal('10'), available_qty=1)
            self._run(**kw)
            self.assertFalse(self.GM.objects.filter(tenant=self.tenant).exists(), kw)


class ImportRegressionTests(TestCase):
    def test_passport_share_token_roundtrip(self):
        from inventory.views.vehicles import _sign_passport_share, _unsign_passport_share
        token = _sign_passport_share('wba123', 'shop_a')
        self.assertIsNotNone(_unsign_passport_share(token, 'shop_a'))
        self.assertIsNone(_unsign_passport_share(token, 'shop_b'))

    def test_admin_dashboard_has_models_module(self):
        import inventory.admin.dashboard as dash
        self.assertTrue(hasattr(dash.models, 'Q'))


class AdminPayoutQueueTests(_PublicDomainMixin, TestCase):
    def test_superadmin_sees_queue_and_records_transfer(self):
        from django.contrib.auth import get_user_model
        admin = get_user_model().objects.create_superuser('payout_admin', 'a@x.com', 'pw-Strong-123')
        order = _paid_delivered_order()
        seller = order.listing.seller_customer
        seller.payout_method, seller.payout_account, seller.payout_account_name = 'instapay', 'seller@instapay', 'S'
        seller.save()
        escrow_svc.release_to_seller(order)
        payout = MarketplacePayout.objects.get(order=order)

        c = DjangoClient()
        c.force_login(admin)
        page = c.get('/superadmin/parts/payouts/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('seller@instapay', page.content.decode())

        res = c.post(f'/superadmin/parts/payouts/{payout.pk}/paid/', {'reference': 'IPN-778899'})
        self.assertEqual(res.status_code, 302)
        payout.refresh_from_db()
        self.assertEqual(payout.status, 'paid')
        self.assertEqual(payout.reference, 'IPN-778899')

    def test_customer_cannot_reach_queue(self):
        res = _login(_customer()).get('/superadmin/parts/payouts/')
        self.assertEqual(res.status_code, 302)


class SellerEditAndWatchTests(_PublicDomainMixin, TestCase):
    def _form(self, listing, **over):
        data = {
            'car_make': listing.car_make_id, 'title': listing.title, 'description': listing.description,
            'price_egp': str(listing.price_egp), 'warranty_days': str(listing.warranty_days),
            'condition': listing.condition, 'car_model': listing.car_model, 'city': listing.city,
            'engine_code': listing.engine_code, 'part_number': listing.part_number,
        }
        data.update(over)
        return data

    def test_price_drop_stays_live_and_alerts_watchers(self):
        seller, fan = _customer('S'), _customer('F')
        listing = _listing(seller)
        self.assertEqual(_login(fan).post(f'/marketplace/parts/{listing.listing_code}/watch/').json()['watching'], True)
        res = _login(seller).post(f'/marketplace/parts/{listing.listing_code}/edit/',
                                  self._form(listing, price_egp='750'))
        self.assertEqual(res.status_code, 200, res.content)
        listing.refresh_from_db()
        self.assertEqual(listing.moderation_status, 'approved')
        self.assertEqual(listing.price_egp, Decimal('750.00'))
        self.assertTrue(CustomerNotification.objects.filter(customer=fan, title__contains='السعر نزل').exists())
        self.assertEqual(_login(fan).get('/marketplace/parts/saved/').status_code, 200)

    def test_content_change_goes_back_to_review(self):
        seller = _customer()
        listing = _listing(seller)
        _login(seller).post(f'/marketplace/parts/{listing.listing_code}/edit/',
                            self._form(listing, title='Different part entirely'))
        listing.refresh_from_db()
        self.assertEqual(listing.moderation_status, 'pending_approval')
        self.assertEqual(listing.status, 'draft')

    def test_rejected_listing_can_be_fixed_and_resubmitted(self):
        seller = _customer()
        listing = _listing(seller, status='draft', moderation_status='rejected', rejection_reason='blurry')
        res = _login(seller).post(f'/marketplace/parts/{listing.listing_code}/edit/', self._form(listing))
        self.assertEqual(res.status_code, 200, res.content)
        listing.refresh_from_db()
        self.assertEqual(listing.moderation_status, 'pending_approval')
        self.assertEqual(listing.rejection_reason, '')

    def test_cannot_edit_someone_elses_or_reserved_listing(self):
        seller, other, buyer = _customer(), _customer(), _customer()
        listing = _listing(seller)
        self.assertEqual(_login(other).post(f'/marketplace/parts/{listing.listing_code}/edit/',
                                            self._form(listing)).status_code, 403)
        private = _listing(seller, reserved_for=buyer)
        self.assertEqual(_login(seller).post(f'/marketplace/parts/{private.listing_code}/edit/',
                                             self._form(private, price_egp='1')).status_code, 400)

    def test_my_listings_edit_button_renders_json(self):
        seller = _customer()
        _listing(seller, title='Pump "Special" <A&B>')
        body = _login(seller).get('/marketplace/parts/my-listings/').content.decode()
        self.assertIn('openEdit({&quot;code&quot;', body)
        self.assertNotIn('<A&B>', body)


class LifecycleTimeoutTests(TestCase):
    def test_shipped_order_auto_delivered_after_14_days(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        orders_svc.mark_paid(order)
        PartOrder.objects.filter(pk=order.pk).update(
            status='shipped', shipped_at=timezone.now() - timezone.timedelta(days=15))
        self.assertEqual(orders_svc.auto_confirm_stale_deliveries(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertIsNotNone(order.warranty_ends_at)

    def test_recently_shipped_or_disputed_is_left_alone(self):
        from clients.models import DisputeTicket
        seller, buyer = _customer(), _customer()
        fresh = _pending_order(_listing(seller), buyer)
        orders_svc.mark_paid(fresh)
        PartOrder.objects.filter(pk=fresh.pk).update(status='shipped', shipped_at=timezone.now())
        disputed = _pending_order(_listing(seller), buyer)
        orders_svc.mark_paid(disputed)
        PartOrder.objects.filter(pk=disputed.pk).update(
            status='shipped', shipped_at=timezone.now() - timezone.timedelta(days=20))
        DisputeTicket.objects.create(order=disputed, opened_by_role='buyer', opened_by_customer=buyer,
                                     category='item_not_received', description='where is it')
        self.assertEqual(orders_svc.auto_confirm_stale_deliveries(), 0)

    def test_unshipped_paid_order_is_refunded_after_deadline(self):
        seller, buyer = _customer(), _customer()
        listing = _listing(seller)
        order = _pending_order(listing, buyer)
        orders_svc.mark_paid(order)
        PartOrder.objects.filter(pk=order.pk).update(paid_at=timezone.now() - timezone.timedelta(days=6))
        self.assertEqual(orders_svc.refund_unshipped_orders(), 1)
        order.refresh_from_db(); listing.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
        self.assertEqual(order.return_shipping_payer, 'seller')
        self.assertEqual(listing.status, 'removed')
        refund = MarketplacePayout.objects.get(order=order)
        self.assertEqual((refund.kind, refund.amount), ('buyer_refund', order.amount_paid))

    def test_paid_within_deadline_not_refunded(self):
        seller, buyer = _customer(), _customer()
        order = _pending_order(_listing(seller), buyer)
        orders_svc.mark_paid(order)
        self.assertEqual(orders_svc.refund_unshipped_orders(), 0)
