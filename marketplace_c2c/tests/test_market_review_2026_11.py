"""
مراجعة سوق قطع الغيار + سوق المطابع/الخدمات (2026-11):

* التقييم على عرض المناقصة مرة واحدة بس — مايكتبش فوق القديم.
* العميل يقدر يلغي طلبه قبل قبول عرض (العروض المعلّقة بتترفض).
* حد الطلبات المفتوحة بيحسب الطلبات اللي مستنية موافقة الإدارة.
* ميزانية "Infinity" بترجع رسالة خطأ مش 500.
* البائع لو سحب القطعة الخاصة بطلب "مطلوب قطعة"، الطلب بيرجع مفتوح بالعروض
  المنافسة والمشتري بيتبلّغ.
"""
from __future__ import annotations

import json
from decimal import Decimal

from django.test import TestCase

from clients.models import (
    CustomerNotification, PartWantedOffer, PartWantedRequest, ServiceRequest,
)
from marketplace_c2c.tests.test_parts_order_lifecycle import (
    _PublicDomainMixin, _customer as _parts_customer, _login, _make,
)
from marketplace_c2c.tests.test_tender_offers import _customer, _merchant, _offer, _request


class TenderReviewTests(_PublicDomainMixin, TestCase):
    def _accepted(self):
        customer = _customer()
        req = _request(customer)
        offer = _offer(req, _merchant('r'))
        c = _login(customer)
        self.assertEqual(c.post(f'/marketplace/offer/{offer.offer_code}/accept/').status_code, 200)
        return c, req, offer

    def _rate(self, c, offer, rating):
        return c.post(f'/marketplace/offer/{offer.offer_code}/rate/',
                      data=json.dumps({'rating': rating, 'review': 'ok'}),
                      content_type='application/json')

    def test_rating_is_once_only(self):
        c, req, offer = self._accepted()
        self.assertEqual(self._rate(c, offer, 5).status_code, 200)
        self.assertEqual(self._rate(c, offer, 1).status_code, 400)
        offer.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(offer.customer_rating, 5)
        self.assertEqual(req.status, 'completed')

    def test_rating_rejects_non_object_body(self):
        c, _req, offer = self._accepted()
        res = c.post(f'/marketplace/offer/{offer.offer_code}/rate/', data='[1]',
                     content_type='application/json')
        self.assertEqual(res.status_code, 400)

    def test_customer_can_cancel_open_request(self):
        customer = _customer()
        req = _request(customer)
        offer = _offer(req, _merchant('c'))
        res = _login(customer).post(f'/marketplace/request/{req.request_code}/cancel/')
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db(); offer.refresh_from_db()
        self.assertEqual(req.status, 'cancelled')
        self.assertEqual(offer.status, 'rejected')
        # والعرض مايتقبلش بعد الإلغاء
        self.assertEqual(
            _login(customer).post(f'/marketplace/offer/{offer.offer_code}/accept/').status_code, 400)

    def test_cannot_cancel_after_accept_or_someone_elses(self):
        c, req, _offer_ = self._accepted()
        self.assertEqual(c.post(f'/marketplace/request/{req.request_code}/cancel/').status_code, 400)
        other = _request(_customer())
        self.assertEqual(c.post(f'/marketplace/request/{other.request_code}/cancel/').status_code, 404)

    def test_open_limit_counts_pending_approval(self):
        customer = _customer()
        for _ in range(10):
            _request(customer, status='pending_approval')
        res = _login(customer).post('/marketplace/request/create/', {
            'title': 'طباعة كروت شخصية', 'description': '500 كارت مطبوع وش وضهر',
        })
        self.assertEqual(res.status_code, 429)

    def test_infinite_budget_is_a_clean_400(self):
        customer = _customer()
        res = _login(customer).post('/marketplace/request/create/', {
            'title': 'طباعة كروت شخصية', 'description': '500 كارت مطبوع وش وضهر',
            'max_budget': 'Infinity',
        })
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ServiceRequest.objects.filter(customer=customer).exists())


class WantedWithdrawReopenTests(_PublicDomainMixin, TestCase):
    def test_seller_withdrawing_private_listing_restores_other_offers(self):
        buyer, s1, s2 = _parts_customer(), _parts_customer(), _parts_customer()
        req = PartWantedRequest.objects.create(
            buyer_customer=buyer, car_make=_make(), car_model='X5', car_year=2012, part_name='Pump',
        )
        o1 = PartWantedOffer.objects.create(request=req, seller_customer=s1,
                                            price_egp=Decimal('500'), warranty_days=5)
        o2 = PartWantedOffer.objects.create(request=req, seller_customer=s2,
                                            price_egp=Decimal('600'), warranty_days=5)
        self.assertEqual(_login(buyer).post(f'/marketplace/parts/wanted/offer/{o1.pk}/accept/').status_code, 200)
        o1.refresh_from_db(); o2.refresh_from_db()
        self.assertEqual(o2.status, 'rejected')

        res = _login(s1).post(f'/marketplace/parts/{o1.linked_listing.listing_code}/withdraw/')
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db(); o1.refresh_from_db(); o2.refresh_from_db()
        self.assertEqual(req.status, 'open')
        self.assertEqual(o1.status, 'withdrawn')
        self.assertEqual(o2.status, 'pending')
        self.assertTrue(CustomerNotification.objects.filter(
            customer=buyer, title__contains='سحب عرضه').exists())
        # المشتري يقدر يقبل العرض التاني
        self.assertEqual(_login(buyer).post(f'/marketplace/parts/wanted/offer/{o2.pk}/accept/').status_code, 200)
