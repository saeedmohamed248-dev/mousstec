"""
Smoke-tests for the customer-facing printing & design pages.

Verifies every page in the customer journey returns 200 (or expected redirect)
and that key routes referenced from the templates actually resolve. Catches
broken links + ensures CSRF cookie gets set on the design store landing page.
"""
from __future__ import annotations

import uuid

from django.test import TestCase, Client, override_settings
from django.urls import reverse, NoReverseMatch

from clients.models import MarketplaceCustomer


class _TenantDomainMixin:
    """marketplace URLs بـ public schema — لازم نـ provision testserver domain
    عشان TenantMainMiddleware ميـ bounce-ش الـ requests بـ 404."""
    _provisioned_domain = None
    _provisioned_tenant = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_model, get_tenant_domain_model
        TenantModel = get_tenant_model()
        DomainModel = get_tenant_domain_model()
        public = TenantModel.objects.filter(schema_name='public').first()
        if public is None:
            public = TenantModel(
                schema_name='public', name='Public', owner_name='Test', phone='000',
            )
            public.auto_create_schema = False
            public.save(verbosity=0)
            cls._provisioned_tenant = public
        if not DomainModel.objects.filter(domain='testserver').exists():
            cls._provisioned_domain = DomainModel.objects.create(
                tenant=public, domain='testserver', is_primary=False,
            )

    @classmethod
    def tearDownClass(cls):
        if cls._provisioned_domain is not None:
            try: cls._provisioned_domain.delete()
            except Exception: pass
        if cls._provisioned_tenant is not None:
            try: cls._provisioned_tenant.delete(force_drop=False)
            except Exception: pass
        super().tearDownClass()


def _new_customer(sector='printing'):
    return MarketplaceCustomer.objects.create(
        customer_type='individual',
        full_name='Printing Tester',
        phone=f'+2010{uuid.uuid4().int % 100000000:08d}',
        sector=sector,
        is_verified=True,
    )


def _client_for(customer) -> Client:
    c = Client()
    c.cookies['mp_session'] = str(customer.session_token)
    return c


@override_settings(DESIGN_CHAT_ENABLED=True)
class PrintingCustomerFlowTests(_TenantDomainMixin, TestCase):
    """End-to-end smoke of all customer-facing printing pages + buttons."""

    def test_marketplace_printing_landing_page_renders(self):
        """شاشة الدخول/التسجيل لقطاع الطباعة."""
        r = self.client.get('/marketplace/printing/')
        self.assertEqual(r.status_code, 200)
        # Must contain register + login affordances
        self.assertIn('سجّل وابدأ التصميم', r.content.decode('utf-8'))

    def test_design_store_home_anonymous_renders_and_sets_csrf(self):
        """متجر التصاميم لازم يفتح للزوار + يـ set كوكي mt_csrf
        (الأزرار بتعتمد عليه في purchase API call)."""
        r = self.client.get('/marketplace/design-store/')
        self.assertEqual(r.status_code, 200)
        # ⚠️ Critical: mt_csrf cookie must be set or topup purchase 403s.
        self.assertIn('mt_csrf', r.cookies, msg='design_store_home لازم يـ set كوكي mt_csrf')

    def test_authenticated_customer_landing_pages(self):
        """العميل المسجّل يقدر يفتح كل الصفحات الأساسية."""
        cust = _new_customer()
        c = _client_for(cust)
        pages = [
            '/marketplace/design-store/',
            '/marketplace/design-store/my-designs/',
            '/marketplace/design-store/print-orders/',
            '/marketplace/brand-profile/',
            '/marketplace/brand-profile/edit/',
            '/marketplace/dashboard/',
        ]
        for url in pages:
            with self.subTest(url=url):
                r = c.get(url)
                self.assertIn(
                    r.status_code, (200, 302),
                    msg=f'{url} رجع {r.status_code}',
                )

    def test_critical_urls_resolve(self):
        """كل الـ URLs اللي بتـ get hit من زرار في الـ templates لازم تـ resolve.
        لو حد غيّر اسم/path، الـ test ده هيمسكها قبل ما العميل يقابل 404."""
        # Reverse by URL name — اللي ميشتغلش يطلع NoReverseMatch.
        for name in [
            'design_store_home', 'design_store_my_designs',
            'design_chat_page', 'design_chat_start',
            'brand_profile', 'brand_profile_page',
            'marketplace_dashboard', 'marketplace_printing',
            'copilot_topup_catalog', 'copilot_topup_purchase',
            'copilot_balance', 'copilot_send_to_print',
            'design_analyze', 'design_generate', 'design_feedback',
            'design_store_my_print_orders',
        ]:
            with self.subTest(name=name):
                try:
                    reverse(name)
                except NoReverseMatch:
                    self.fail(f'URL name {name!r} مش موجود — في زر بيرجع 404.')

    def test_design_chat_page_loads(self):
        """صفحة الـ design chat — للعميل المسجّل."""
        cust = _new_customer()
        r = _client_for(cust).get('/marketplace/design-chat/')
        self.assertEqual(r.status_code, 200)

    def test_topup_catalog_api_returns_packages(self):
        """الـ catalog endpoint اللي بيتنادى من زرار 'اشتري' لازم يرد packages."""
        cust = _new_customer()
        r = _client_for(cust).get('/printing-copilot/api/topup/catalog/?audience=customer')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data.get('success'))
        self.assertIsInstance(data.get('packages'), list)

    def test_balance_api(self):
        """الـ balance endpoint اللي بيـ refresh الـ chip في top-bar."""
        cust = _new_customer()
        r = _client_for(cust).get('/printing-copilot/api/balance/?audience=customer')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get('success'))
