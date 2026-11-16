"""
مراجعة ٢٠٢٦-١١ — قفل دورة الطلب المقبول في السوق المفتوح.

* القبول بيسجّل وقته.
* بعد ٧ أيام: تذكير واحد للعميل يقيّم.
* بعد ٣٠ يوم: الطلب بيتقفل «مكتمل» تلقائي، والعميل لسه يقدر يقيّم مرة واحدة.
* المهمة الدورية بتقفل الطلبات المفتوحة اللي مدتها خلصت (كانت بتحصل بس لما حد يفتح الصفحة).
"""
from __future__ import annotations

import json
from datetime import timedelta

from django.test import Client as DjangoClient, TestCase
from django.utils import timezone

from clients.models import CustomerNotification, ServiceRequest, TenderOffer
from clients.views.marketplace_core_views import _accept_tender_offer
from marketplace_c2c.services.tenders import close_stale_accepted_requests
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin
from marketplace_c2c.tests.test_tender_offers import _customer, _merchant, _offer, _request


def _accepted(tag):
    customer = _customer()
    req = _request(customer)
    offer = _offer(req, _merchant(tag))
    accepted, err = _accept_tender_offer(offer.offer_code, customer)
    assert err is None, err
    req.refresh_from_db()
    return customer, req, offer


class TenderCloseCycleTests(_PublicDomainMixin, TestCase):
    def test_accept_records_time(self):
        _, req, _ = _accepted('t1')
        self.assertIsNotNone(req.accepted_at)

    def test_reminder_once_then_auto_complete_and_rating_still_allowed(self):
        customer, req, offer = _accepted('t2')
        now = timezone.now()
        # لسه بدري — مفيش حاجة
        self.assertEqual(close_stale_accepted_requests(now + timedelta(days=3)), {'reminded': 0, 'closed': 0})
        # ٧ أيام ⇒ تذكير واحد بس
        self.assertEqual(close_stale_accepted_requests(now + timedelta(days=8)), {'reminded': 1, 'closed': 0})
        self.assertEqual(close_stale_accepted_requests(now + timedelta(days=9)), {'reminded': 0, 'closed': 0})
        self.assertEqual(CustomerNotification.objects.filter(customer=customer, title__contains='اتعاملت').count(), 1)
        # ٣٠ يوم ⇒ مكتمل تلقائي
        self.assertEqual(close_stale_accepted_requests(now + timedelta(days=31)), {'reminded': 0, 'closed': 1})
        req.refresh_from_db()
        self.assertEqual(req.status, 'completed')
        closed_at = req.completed_at
        self.assertIsNotNone(closed_at)

        # العميل لسه يقدر يقيّم — مرة واحدة
        c = DjangoClient()
        c.cookies['mp_session'] = str(customer.session_token)
        res = c.post(f'/marketplace/offer/{offer.offer_code}/rate/',
                     data=json.dumps({'rating': 4, 'review': 'تمام'}), content_type='application/json')
        self.assertEqual(res.status_code, 200, res.content)
        offer.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(offer.customer_rating, 4)
        self.assertEqual(req.completed_at, closed_at)
        res = c.post(f'/marketplace/offer/{offer.offer_code}/rate/',
                     data=json.dumps({'rating': 5}), content_type='application/json')
        self.assertEqual(res.status_code, 400)

    def test_rated_request_is_left_alone(self):
        customer, req, offer = _accepted('t3')
        c = DjangoClient()
        c.cookies['mp_session'] = str(customer.session_token)
        c.post(f'/marketplace/offer/{offer.offer_code}/rate/', data=json.dumps({'rating': 5}),
               content_type='application/json')
        self.assertEqual(close_stale_accepted_requests(timezone.now() + timedelta(days=40)),
                         {'reminded': 0, 'closed': 0})

    def test_legacy_accepted_without_timestamp_uses_created_at(self):
        _, req, _ = _accepted('t4')
        ServiceRequest.objects.filter(pk=req.pk).update(accepted_at=None,
                                                        created_at=timezone.now() - timedelta(days=60))
        self.assertEqual(close_stale_accepted_requests()['closed'], 1)

    def test_periodic_task_expires_overdue_open_requests(self):
        from clients.tasks import close_stale_tender_requests
        req = _request(_customer())
        ServiceRequest.objects.filter(pk=req.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        result = close_stale_tender_requests()
        self.assertEqual(result['expired'], 1)
        req.refresh_from_db()
        self.assertEqual(req.status, 'expired')
        self.assertFalse(TenderOffer.objects.filter(service_request=req).exists())
