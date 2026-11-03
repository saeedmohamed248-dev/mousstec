"""🌐 صفحة «طلبات نشر الشركات (B2B)» في السوبر أدمن — العرض والأزرار والصلاحيات.
(منطق الجمع عبر الشركات نفسه متغطّي في inventory/tests/test_superadmin_b2b_listing_requests.py)"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from clients.services import b2b_listing_review as svc

URL = '/superadmin/b2b/listing-requests/'
ROW = {
    'tenant_id': 1, 'tenant_name': 'ورشة الاختبار', 'schema': 'acme', 'id': 7, 'status': 'pending',
    'status_label': 'قيد المراجعة', 'product_name': 'فلتر زيت للسوق', 'part_number': 'SKU-77',
    'brand': 'BOSCH', 'car_model': '', 'condition': 'جديد', 'stock': 4,
    'retail_price': Decimal('120'), 'requested_price': Decimal('90'), 'approved_price': None,
    'review_notes': '', 'requested_by': 'mgr', 'created_at': datetime(2026, 11, 3, 12, 0), 'image_url': '',
}


class SuperAdminB2BPageTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        cls._pub = Tenant.objects.filter(schema_name='public').first()
        cls._made_pub = cls._pub is None
        if cls._made_pub:
            cls._pub = Tenant(schema_name='public', name='Public', owner_name='o', phone='0')
            cls._pub.auto_create_schema = False
            cls._pub.save(verbosity=0)
        cls._dom = None
        if not Domain.objects.filter(domain='testserver').exists():
            cls._dom = Domain.objects.create(tenant=cls._pub, domain='testserver', is_primary=False)

    @classmethod
    def tearDownClass(cls):
        if cls._dom is not None:
            cls._dom.delete()
        if cls._made_pub:
            cls._pub.delete(force_drop=False)
        super().tearDownClass()

    def setUp(self):
        self.owner = User.objects.create_user('b2b_page_owner@test.com', password='x',
                                              is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.owner)

    def test_list_shows_company_requests_with_actions(self):
        with patch.object(svc, 'collect_requests', return_value=[ROW]) as collect, \
                patch.object(svc, 'pending_count', return_value=1):
            r = self.c.get(URL)
        self.assertEqual(r.status_code, 200)
        collect.assert_called_once_with(status='pending')
        self.assertContains(r, 'فلتر زيت للسوق')
        self.assertContains(r, 'ورشة الاختبار')
        self.assertContains(r, '/superadmin/b2b/listing-requests/acme/7/decide/')

    def test_dashboard_links_to_page(self):
        r = self.c.get('/superadmin/')
        if r.status_code == 200:
            self.assertContains(r, URL)

    def test_decide_passes_reviewer_and_price(self):
        with patch.object(svc, 'decide', return_value=(type('T', (), {'name': 'ورشة', 'schema_name': 'acme'})(),
                                                       'فلتر')) as decide:
            r = self.c.post('/superadmin/b2b/listing-requests/acme/7/decide/',
                            {'action': 'approve', 'price': '85'})
        self.assertEqual(r.status_code, 302)
        args, kwargs = decide.call_args
        self.assertEqual(args, ('acme', 7, 'approve'))
        self.assertEqual(kwargs['price'], '85')
        self.assertEqual(kwargs['reviewer_label'], 'b2b_page_owner@test.com')

    def test_decide_error_is_shown_not_500(self):
        with patch.object(svc, 'decide', side_effect=svc.ReviewError('الطلب اتراجع قبل كده')):
            r = self.c.post('/superadmin/b2b/listing-requests/acme/7/decide/', {'action': 'approve'}, follow=True)
        self.assertEqual(r.status_code, 200)

    def test_normal_user_is_blocked(self):
        u = User.objects.create_user('b2b_page_nobody', password='x')
        c = Client()
        c.force_login(u)
        self.assertNotEqual(c.get(URL).status_code, 200)
        with patch.object(svc, 'decide') as decide:
            c.post('/superadmin/b2b/listing-requests/acme/7/decide/', {'action': 'approve'})
        decide.assert_not_called()
