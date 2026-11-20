"""🧭 رحلة عميل السوق في السوبر أدمن: الزيارات بتتربط بالعميل، والقدام بيتعرضوا «تقديري»."""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from clients.models import MarketplaceCustomer, VisitorLog
from clients.services.customer_journey import build_customer_journey


def _customer():
    return MarketplaceCustomer.objects.create(
        customer_type='individual', full_name='عميل رحلة', sector='printing', is_verified=True,
        phone=f'+2010{uuid.uuid4().int % 100000000:08d}')


class CustomerJourneyTests(TestCase):
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
        cache.clear()
        self.customer = _customer()

    def test_middleware_tags_visits_with_marketplace_customer(self):
        c = Client()
        c.cookies['mp_session'] = str(self.customer.session_token)
        c.get('/marketplace/')
        c.get('/marketplace/printing/')
        logs = VisitorLog.objects.filter(marketplace_customer=self.customer).order_by('timestamp')
        self.assertEqual([lg.path for lg in logs], ['/marketplace/', '/marketplace/printing/'])

        # زائر من غير كوكي (أو كوكي بايظة) مابيتربطش بحد
        anon = Client()
        anon.cookies['mp_session'] = 'not-a-uuid'
        anon.get('/marketplace/automotive/')
        self.assertIsNone(VisitorLog.objects.get(path='/marketplace/automotive/').marketplace_customer)

    def test_journey_sessions_durations_and_last_page(self):
        t0 = timezone.now() - timedelta(hours=3)
        rows = [(0, '/marketplace/', 200), (40, '/marketplace/printing/', 200),
                (100, '/marketplace/design-store/x/', 500),
                # فاصل ساعة = زيارة جديدة
                (3700, '/marketplace/dashboard/', 200)]
        for sec, path, st in rows:
            lg = VisitorLog.objects.create(path=path, status_code=st, tenant_schema='public',
                                           marketplace_customer=self.customer, ip_address='1.2.3.4')
            VisitorLog.objects.filter(pk=lg.pk).update(timestamp=t0 + timedelta(seconds=sec))

        j = build_customer_journey(self.customer)
        self.assertFalse(j['estimated'])
        self.assertEqual(len(j['sessions']), 2)
        self.assertEqual(j['last_page']['path'], '/marketplace/dashboard/')
        first = j['sessions'][1]   # الأحدث فوق
        self.assertEqual([p['seconds'] for p in first['pages']], [40, 60, None])
        self.assertTrue(first['pages'][2]['is_error'])
        self.assertEqual(first['pages'][1]['label'], 'قسم الطباعة')

    def test_old_customer_estimated_by_ip_and_browser_from_login(self):
        now = timezone.now()
        MarketplaceCustomer.objects.filter(pk=self.customer.pk).update(last_login_at=now)
        self.customer.refresh_from_db()

        def log(path, sec, ip='5.5.5.5', ua='Safari', method='GET'):
            lg = VisitorLog.objects.create(path=path, method=method, status_code=200, tenant_schema='public',
                                           ip_address=ip, user_agent=ua)
            VisitorLog.objects.filter(pk=lg.pk).update(timestamp=now + timedelta(seconds=sec))

        log('/marketplace/login/', 0, method='POST')
        log('/marketplace/dashboard/', 5)
        log('/marketplace/printing/', 30)
        log('/marketplace/parts/', 40, ip='9.9.9.9')          # حد تاني
        log('/marketplace/automotive/', 50, ua='Chrome')     # متصفح تاني

        j = build_customer_journey(self.customer)
        self.assertTrue(j['estimated'])
        paths = [p['path'] for s in j['sessions'] for p in s['pages']]
        self.assertIn('/marketplace/printing/', paths)
        self.assertNotIn('/marketplace/parts/', paths)
        self.assertNotIn('/marketplace/automotive/', paths)
        self.assertEqual(j['last_page']['path'], '/marketplace/printing/')

    def test_detail_modal_shows_journey(self):
        VisitorLog.objects.create(path='/marketplace/printing/', status_code=200, tenant_schema='public',
                                  marketplace_customer=self.customer)
        owner = User.objects.create_user('journey_owner@test.com', password='x', is_staff=True, is_superuser=True)
        admin = Client()
        admin.force_login(owner)
        r = admin.get(f'/superadmin/customer/{self.customer.pk}/detail/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('رحلة العميل على الموقع', html)
        self.assertIn('آخر صفحة وقف عندها', html)
        self.assertNotIn('لم يقم العميل بأي نشاط', html)
