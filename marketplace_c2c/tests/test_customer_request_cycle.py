"""
دورة طلب عميل السوق (سيارات + مطابع) — مراجعة ٢٠٢٦-١١.

* العميل يقدر يلغي طلبه قبل قبول عرض، والعروض المعلّقة تتقفل.
* التقييم مرة واحدة بس (الـ API كان بيقبل تقييم متكرر).
* الطلبات المستنية موافقة بتتحسب في حد الـ ١٠ طلبات.
* ميزانية «Infinity» ترجع 400 مش «فشل».
* صفحة التفاصيل بتعرض الحالة وسبب الرفض، واللوحة بتسمح بتعديل المرفوض.
"""
from __future__ import annotations

import json

from django.test import Client as DjangoClient, TestCase

from clients.models import ServiceRequest
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin
from marketplace_c2c.tests.test_tender_offers import _customer, _merchant, _offer, _request


def _c(customer):
    c = DjangoClient()
    c.cookies['mp_session'] = str(customer.session_token)
    return c


class CustomerRequestCycleTests(_PublicDomainMixin, TestCase):
    def test_cancel_open_request_rejects_pending_offers(self):
        cust = _customer()
        req = _request(cust)
        o = _offer(req, _merchant('x'))
        res = _c(cust).post(f'/marketplace/request/{req.request_code}/cancel/')
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db(); o.refresh_from_db()
        self.assertEqual(req.status, 'cancelled')
        self.assertEqual(o.status, 'rejected')

    def test_cannot_cancel_after_accept_or_others_request(self):
        cust = _customer()
        req = _request(cust)
        o = _offer(req, _merchant('y'))
        self.assertEqual(_c(_customer()).post(f'/marketplace/request/{req.request_code}/cancel/').status_code, 404)
        _c(cust).post(f'/marketplace/offer/{o.offer_code}/accept/')
        res = _c(cust).post(f'/marketplace/request/{req.request_code}/cancel/')
        self.assertEqual(res.status_code, 400)
        req.refresh_from_db()
        self.assertEqual(req.status, 'accepted')

    def test_cancel_requires_login_and_post(self):
        req = _request(_customer())
        self.assertEqual(DjangoClient().post(f'/marketplace/request/{req.request_code}/cancel/').status_code, 401)
        self.assertEqual(_c(req.customer).get(f'/marketplace/request/{req.request_code}/cancel/').status_code, 405)

    def test_rating_only_once(self):
        cust = _customer()
        req = _request(cust)
        o = _offer(req, _merchant('z'))
        c = _c(cust)
        c.post(f'/marketplace/offer/{o.offer_code}/accept/')
        url = f'/marketplace/offer/{o.offer_code}/rate/'
        self.assertEqual(c.post(url, json.dumps({'rating': 5}), content_type='application/json').status_code, 200)
        res = c.post(url, json.dumps({'rating': 1}), content_type='application/json')
        self.assertEqual(res.status_code, 400)
        o.refresh_from_db()
        self.assertEqual(o.customer_rating, 5)

    def test_pending_approval_counts_toward_open_limit(self):
        cust = _customer()
        for _ in range(10):
            _request(cust, status='pending_approval')
        res = _c(cust).post('/marketplace/request/create/',
                            {'title': 'طلب جديد هنا', 'description': 'تفاصيل كافية للطلب'})
        self.assertEqual(res.status_code, 429)

    def test_infinite_budget_is_400(self):
        cust = _customer()
        res = _c(cust).post('/marketplace/request/create/',
                            {'title': 'طلب جديد هنا', 'description': 'تفاصيل كافية للطلب',
                             'max_budget': 'Infinity'})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ServiceRequest.objects.filter(customer=cust).exists())

    def test_detail_shows_rejection_reason_and_dashboard_allows_edit(self):
        cust = _customer()
        req = _request(cust, status='rejected_by_admin')
        req.admin_notes = 'الصور مش واضحة'
        req.save()
        c = _c(cust)
        detail = c.get(f'/marketplace/request/{req.request_code}/').content.decode()
        self.assertIn('الصور مش واضحة', detail)
        self.assertIn('cancelRequest', detail)
        dash = c.get('/marketplace/dashboard/').content.decode()
        self.assertIn('الصور مش واضحة', dash)
        self.assertIn('openEditModal', dash)
        self.assertIn(f'/marketplace/request/{req.request_code}/', dash)

    def test_printing_customer_full_cycle(self):
        cust = _customer()
        cust.sector = 'printing'; cust.save()
        c = _c(cust)
        res = c.post('/marketplace/request/create/',
                     {'title': 'كروت شخصية ١٠٠٠', 'description': 'ورق كوشيه ٣٥٠ جرام وشين'})
        self.assertEqual(res.status_code, 200, res.content)
        req = ServiceRequest.objects.get(request_code=res.json()['request_code'])
        self.assertEqual(req.sector, 'printing')
        self.assertEqual(c.get('/marketplace/dashboard/').status_code, 200)
        self.assertEqual(c.get(f'/marketplace/request/{req.request_code}/').status_code, 200)


class CustomerLoginAndContactTests(_PublicDomainMixin, TestCase):
    def test_printing_customer_returns_to_printing_login_after_logout(self):
        cust = _customer()
        cust.sector = 'printing'; cust.save()
        c = _c(cust)
        res = c.get('/marketplace/logout/')
        self.assertEqual(res.cookies['mp_sector'].value, 'printing')
        res = c.get('/marketplace/login/')
        self.assertTrue(res['Location'].startswith('/marketplace/printing/'), res['Location'])

    def test_login_defaults_to_automotive_and_design_next_goes_to_printing(self):
        c = DjangoClient()
        self.assertTrue(c.get('/marketplace/login/')['Location'].startswith('/marketplace/automotive/'))
        loc = c.get('/marketplace/login/?next=/marketplace/design-store/')['Location']
        self.assertTrue(loc.startswith('/marketplace/printing/'), loc)

    def test_accepted_offer_shows_call_and_whatsapp(self):
        cust = _customer()
        req = _request(cust)
        o = _offer(req, _merchant('w'))
        c = _c(cust)
        c.post(f'/marketplace/offer/{o.offer_code}/accept/')
        html = c.get(f'/marketplace/request/{req.request_code}/').content.decode()
        self.assertIn('https://wa.me/201000000000', html)
        self.assertIn('tel:01000000000', html)


class ExpiryAndMerchantAlertTests(_PublicDomainMixin, TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_expired_request_notifies_customer_once(self):
        from datetime import timedelta
        from django.utils import timezone
        from clients.models import CustomerNotification
        cust = _customer()
        req = _request(cust)
        ServiceRequest.objects.filter(pk=req.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        c = _c(cust)
        c.get('/marketplace/dashboard/')
        c.get('/marketplace/dashboard/')
        req.refresh_from_db()
        self.assertEqual(req.status, 'expired')
        notes = CustomerNotification.objects.filter(customer=cust, icon='fa-clock')
        self.assertEqual(notes.count(), 1)
        self.assertIn('من غير عروض', notes.first().body)

    def test_accepted_offer_flags_merchant_until_feed_seen(self):
        from clients.views.marketplace_core_views import (
            _accepted_offers_cache_key, _expire_overdue_requests,
        )
        from django.core.cache import cache
        cust = _customer()
        req = _request(cust)
        m = _merchant('n')
        o = _offer(req, m)
        _c(cust).post(f'/marketplace/offer/{o.offer_code}/accept/')
        self.assertEqual(cache.get(_accepted_offers_cache_key(m.pk)), [o.pk])
        self.assertEqual(_expire_overdue_requests(), 0)
