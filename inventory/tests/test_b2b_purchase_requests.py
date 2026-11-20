"""🛒 طلب شراء مباشر من تاجر في سوق B2B (B2BPurchaseRequest).

كانت نتايج البحث بتوري السعر والرصيد بس من غير أي طريقة تتعامل بيها مع التاجر.
"""
import json
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.urls import reverse

from clients.models import B2BPurchaseRequest, Client, GlobalB2BMarketplace
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee

IN_PLAN = patch('clients.services.entitlements.EntitlementService.has', return_value=True)


def _other_company(name='تاجر الجملة', phone='01155554444'):
    c = Client(schema_name=f'b2breq_{uuid.uuid4().hex[:8]}', name=name, owner_name='صاحب', phone=phone,
               email='seller@example.com', industry='automotive')
    c.auto_create_schema = False
    tenant = connection.tenant
    connection.set_schema_to_public()   # django-tenants: الشركة بتتعمل من الـ public بس
    c.save()
    connection.set_tenant(tenant)
    return c


@override_settings(ALLOWED_HOSTS=['*'])
class B2BPurchaseRequestTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        IN_PLAN.start()
        self.addCleanup(IN_PLAN.stop)
        self.branch = make_branch()
        self.host = self.domain.domain
        self.boss = User.objects.create_user('b2breq_boss', password='x', is_staff=True, is_superuser=True)
        self.c = HttpClient()
        self.c.force_login(self.boss)
        # جداول الـ public مابتتمسحش بين اختبارات الـ TransactionTestCase — نبدأ نضاف
        B2BPurchaseRequest.objects.all().delete()
        GlobalB2BMarketplace.objects.all().delete()
        self.other = _other_company()
        self.listing = GlobalB2BMarketplace.objects.create(
            tenant=self.other, part_number='B2B-PLUG', product_name='بوجيه', wholesale_price=Decimal('195.00'),
            available_qty=40)
        self.notify = patch('inventory.views.b2b_requests._notify').start()
        self.addCleanup(patch.stopall)

    def create(self, listing=None, qty=6, note=''):
        return self.c.post(reverse('inventory:b2b_request_create'),
                           json.dumps({'listing_id': (listing or self.listing).pk, 'quantity': qty, 'note': note}),
                           content_type='application/json', HTTP_HOST=self.host)

    def respond(self, req, action, reply=''):
        return self.c.post(reverse('inventory:b2b_request_respond', args=[req.pk]),
                           {'action': action, 'reply': reply}, HTTP_HOST=self.host)

    def test_buyer_sends_request_at_the_published_price_and_seller_is_told(self):
        r = self.create(qty=6, note='الدفع كاش')   # TransactionTestCase ⇒ on_commit بيشتغل على طول
        self.assertEqual(r.status_code, 200, r.content)
        req = B2BPurchaseRequest.objects.get()
        self.assertEqual((req.buyer_id, req.seller_id, req.quantity, req.unit_price, req.status),
                         (self.tenant.pk, self.other.pk, 6, Decimal('195.00'), 'pending'))
        self.assertEqual(req.total, Decimal('1170.00'))
        self.notify.assert_called_once()
        self.assertEqual(self.notify.call_args.args[0], self.other)

        # نفس الصنف تاني وهو لسه مستني ⇒ تحديث مش طلب مكرر
        r = self.create(qty=8)
        self.assertEqual(json.loads(r.content)['status'], 'updated')
        self.assertEqual(B2BPurchaseRequest.objects.get().quantity, 8)

        page = self.c.get(reverse('inventory:b2b_requests') + '?tab=out', HTTP_HOST=self.host)
        self.assertContains(page, 'بوجيه')
        self.assertNotContains(page, '01155554444')          # التليفون بعد موافقة التاجر بس

    def test_bad_requests_are_refused(self):
        self.assertEqual(self.create(qty=0).status_code, 400)
        self.assertEqual(self.create(qty=41).status_code, 400)         # أكتر من المتاح
        mine = GlobalB2BMarketplace.objects.create(tenant=self.tenant, part_number='MINE', wholesale_price=10,
                                                   available_qty=5)
        self.assertEqual(self.create(listing=mine).status_code, 400)   # صنفي أنا
        GlobalB2BMarketplace.objects.filter(pk=self.listing.pk).update(available_qty=0)
        self.assertEqual(self.create().status_code, 404)               # خلص عند التاجر
        Client.objects.filter(pk=self.other.pk).update(is_fraud_flagged=True)
        GlobalB2BMarketplace.objects.filter(pk=self.listing.pk).update(available_qty=40)
        self.assertEqual(self.create().status_code, 404)               # تاجر متعلّم عليه
        self.assertFalse(B2BPurchaseRequest.objects.exists())

    def test_seller_accepts_and_both_sides_see_contacts(self):
        req = B2BPurchaseRequest.objects.create(
            buyer=self.other, seller=self.tenant, listing=None, part_number='X-1', product_name='فلتر',
            unit_price=Decimal('50'), quantity=3)
        page = self.c.get(reverse('inventory:b2b_requests'), HTTP_HOST=self.host)
        self.assertEqual(page.context['incoming_pending'], 1)
        self.assertNotContains(page, '01155554444')
        r = self.respond(req, 'accept', 'جاهزين بكرة')
        self.assertEqual(r.status_code, 200, r.content)
        req.refresh_from_db()
        self.assertEqual((req.status, req.seller_reply), ('accepted', 'جاهزين بكرة'))
        self.assertIsNotNone(req.responded_at)
        self.assertEqual(self.notify.call_args.args[0], self.other)        # المشتري اتبلّغ
        page = self.c.get(reverse('inventory:b2b_requests'), HTTP_HOST=self.host)
        self.assertContains(page, '01155554444')
        self.assertContains(page, 'wa.me/201155554444')
        self.assertEqual(self.respond(req, 'reject').status_code, 400)     # اترد عليه خلاص

    def test_only_the_right_side_can_act(self):
        incoming = B2BPurchaseRequest.objects.create(
            buyer=self.other, seller=self.tenant, part_number='X-2', unit_price=Decimal('10'), quantity=1)
        outgoing = B2BPurchaseRequest.objects.create(
            buyer=self.tenant, seller=self.other, part_number='X-3', unit_price=Decimal('10'), quantity=1)
        self.assertEqual(self.respond(incoming, 'cancel').status_code, 404)  # البائع مايلغيش طلب المشتري
        self.assertEqual(self.respond(outgoing, 'accept').status_code, 404)  # المشتري مايوافقش على طلبه
        self.assertEqual(self.respond(outgoing, 'cancel').status_code, 200)
        outgoing.refresh_from_db()
        self.assertEqual(outgoing.status, 'cancelled')
        self.assertEqual(self.respond(incoming, 'reject', 'خلص').status_code, 200)
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, 'rejected')

    def test_cashier_cannot_buy_or_answer(self):
        user, _p = make_employee(username='b2breq_cashier', role='cashier', branch=self.branch)
        c = HttpClient()
        c.force_login(user)
        r = c.post(reverse('inventory:b2b_request_create'), json.dumps({'listing_id': self.listing.pk, 'quantity': 1}),
                   content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(B2BPurchaseRequest.objects.exists())

    def test_search_marks_own_listings(self):
        GlobalB2BMarketplace.objects.create(tenant=self.tenant, part_number='B2B-PLUG', wholesale_price=180,
                                            available_qty=5, product_name='بوجيه')
        Client.objects.filter(pk__in=[self.tenant.pk, self.other.pk]).update(is_marketplace_active=True)
        from django.core.cache import cache
        cache.clear()
        r = self.c.get('/system/api/v1/b2b/market/search/?q=B2B-PLUG', HTTP_HOST=self.host)
        rows = {row['seller_id']: row for row in json.loads(r.content)['results']}
        self.assertTrue(rows[self.tenant.pk]['is_mine'])
        self.assertFalse(rows[self.other.pk]['is_mine'])
        self.assertEqual(rows[self.other.pk]['listing_id'], self.listing.pk)
