"""
Customer ↔ merchant tender flow regressions (review 2026-09).

* Accepting an offer locks the request — the second accept is refused and
  the merchant deal counter isn't bumped twice.
* Merchant's own B2B requests are resolvable through the shared accept
  helper (the merchant had no way to accept offers before).
* Admin approve/reject notifies the customer; a rejected request can be
  edited and resubmitted.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import Client as DjangoClient, TestCase, override_settings
from django.utils import timezone

from clients.models import (
    Client, CustomerNotification, MarketplaceCustomer, ServiceRequest, TenderOffer,
)
from clients.views.marketplace_core_views import _accept_tender_offer, _b2b_buyer_phone
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin


def _merchant(tag):
    c = Client(schema_name=f'tender_{tag}_{uuid.uuid4().hex[:6]}', name=f'Merchant {tag}',
               owner_name='O', phone='01000000000', industry='automotive')
    c.auto_create_schema = False
    c.save()
    return c


def _customer():
    return MarketplaceCustomer.objects.create(
        customer_type='individual', full_name='Cust', phone=f'+2010{uuid.uuid4().int % 10**8:08d}',
        sector='automotive', is_verified=True,
    )


def _request(customer, status='open'):
    return ServiceRequest.objects.create(
        customer=customer, sector='automotive', title='Need brake pads',
        description='For BMW F30 2015', status=status, is_approved=status == 'open',
        expires_at=timezone.now() + timedelta(days=3),
    )


def _offer(req, merchant, price='500'):
    return TenderOffer.objects.create(
        service_request=req, merchant=merchant, price=Decimal(price),
        description='OEM pads', merchant_city='Cairo',
    )


class AcceptOfferTests(_PublicDomainMixin, TestCase):
    def test_accept_rejects_others_and_second_accept_is_refused(self):
        customer = _customer()
        req = _request(customer)
        m1, m2 = _merchant('a'), _merchant('b')
        o1, o2 = _offer(req, m1, '500'), _offer(req, m2, '450')

        c = DjangoClient(); c.cookies['mp_session'] = str(customer.session_token)
        res = c.post(f'/marketplace/offer/{o2.offer_code}/accept/')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()['merchant_name'], m2.name)

        res2 = c.post(f'/marketplace/offer/{o1.offer_code}/accept/')
        self.assertEqual(res2.status_code, 400)

        o1.refresh_from_db(); req.refresh_from_db(); m1.refresh_from_db(); m2.refresh_from_db()
        self.assertEqual(o1.status, 'rejected')
        self.assertEqual(req.accepted_offer_id, o2.pk)
        # 🛒 السوق مجاناً في البداية (MARKETPLACE_SERVICE_COMMISSION_PCT = 0)
        self.assertEqual(req.platform_commission_earned, Decimal('0.00'))
        self.assertEqual(m2.successful_deals, 1)
        self.assertEqual(m1.successful_deals, 0)

    @override_settings(MARKETPLACE_SERVICE_COMMISSION_PCT='5')
    def test_commission_math_when_switched_back_on(self):
        customer = _customer()
        req = _request(customer)
        offer = _offer(req, _merchant('d'), '450')
        c = DjangoClient(); c.cookies['mp_session'] = str(customer.session_token)
        self.assertEqual(c.post(f'/marketplace/offer/{offer.offer_code}/accept/').status_code, 200)
        req.refresh_from_db()
        self.assertEqual(req.platform_commission_earned, Decimal('22.50'))

    def test_other_customer_cannot_accept(self):
        owner, stranger = _customer(), _customer()
        req = _request(owner)
        offer = _offer(req, _merchant('c'))
        _, err = _accept_tender_offer(offer.offer_code, stranger)
        self.assertEqual(err.status_code, 403)

    def test_merchant_can_accept_offer_on_own_b2b_request(self):
        buyer_merchant, seller_merchant = _merchant('buyer'), _merchant('seller')
        proxy = MarketplaceCustomer.objects.create(
            phone=_b2b_buyer_phone(buyer_merchant), customer_type='company',
            full_name='Proxy', company_name=buyer_merchant.name, sector='automotive', is_verified=True,
        )
        req = _request(proxy)
        offer = _offer(req, seller_merchant)
        accepted, err = _accept_tender_offer(offer.offer_code, proxy)
        self.assertIsNone(err)
        self.assertEqual(accepted.status, 'accepted')


class RequestModerationTests(_PublicDomainMixin, TestCase):
    def test_rejected_request_can_be_edited_and_resubmitted(self):
        customer = _customer()
        req = _request(customer, status='rejected_by_admin')
        c = DjangoClient(); c.cookies['mp_session'] = str(customer.session_token)
        res = c.post(f'/marketplace/request/{req.request_code}/edit/',
                     {'title': 'Need brake pads for F30', 'description': 'Front pads, OEM preferred'})
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db()
        self.assertEqual(req.status, 'pending_approval')

    def test_rate_offer_with_bad_rating_is_400(self):
        customer = _customer()
        req = _request(customer)
        offer = _offer(req, _merchant('r'))
        _accept_tender_offer(offer.offer_code, customer)
        c = DjangoClient(); c.cookies['mp_session'] = str(customer.session_token)
        res = c.post(f'/marketplace/offer/{offer.offer_code}/rate/',
                     data=json.dumps({'rating': 'five'}), content_type='application/json')
        self.assertEqual(res.status_code, 400)


@override_settings(WHATSAPP_ACCESS_TOKEN='t', WHATSAPP_PHONE_NUMBER_ID='p',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AcceptNotifiesMerchantTests(_PublicDomainMixin, TestCase):
    """✅ العميل قبل العرض ⇒ الشركة بيوصلها إيميل + واتساب فيه رقم العميل (مش عدّاد في لوحتها بس)."""

    def test_merchant_gets_email_and_whatsapp_with_customer_phone(self):
        from unittest import mock

        from django.core import mail

        from clients.models import Domain
        customer = _customer()
        req = _request(customer)
        merchant = _merchant('notify')
        Client.objects.filter(pk=merchant.pk).update(email='shop@notify.test', phone='01012121212')
        Domain.objects.create(tenant=merchant, domain='notify-shop.test', is_primary=True)
        loser = _merchant('loser')
        offer, other = _offer(req, merchant, '400'), _offer(req, loser, '450')
        c = DjangoClient(); c.cookies['mp_session'] = str(customer.session_token)
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa, \
                mock.patch('clients.tasks.notify_merchant_offer_accepted.delay', side_effect=Exception('no broker')), \
                self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(c.post(f'/marketplace/offer/{offer.offer_code}/accept/').status_code, 200)
        msgs = [m for m in mail.outbox if 'shop@notify.test' in m.to]
        self.assertEqual(len(msgs), 1)
        self.assertIn(customer.phone, msgs[0].body)
        self.assertIn('https://notify-shop.test/marketplace/merchant/feed/', msgs[0].body)
        self.assertEqual([call.args[0] for call in wa.call_args_list], ['01012121212'])


@override_settings(OWNER_NOTIFY_SYNC=True, CATALOG_NOTIFY_EMAIL='owner@mousstec.test',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class NewRequestNotifiesOwnerTests(_PublicDomainMixin, TestCase):
    """🔔 طلب جديد مستني موافقة ⇒ صاحب المنصة بيتبلّغ (المطابع مش بتشوفه قبل موافقته)."""

    def test_owner_gets_email_with_approval_link(self):
        from django.core import mail
        customer = _customer()
        c = DjangoClient(); c.cookies['mp_session'] = str(customer.session_token)
        with self.captureOnCommitCallbacks(execute=True):
            r = c.post('/marketplace/request/create/', {'title': 'محتاج 1000 كارت', 'description': 'كروت شخصية وشين مط'})
        self.assertEqual(r.status_code, 200, r.content)
        owner = [m for m in mail.outbox if 'owner@mousstec.test' in m.to]
        self.assertEqual(len(owner), 1)
        self.assertIn('مستني موافقتك', owner[0].subject)
        self.assertIn('/superadmin/marketplace/requests/?status=pending_approval', owner[0].body)
