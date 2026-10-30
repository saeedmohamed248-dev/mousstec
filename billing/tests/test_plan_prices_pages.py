"""💎 أسعار الباقات في «إدارة الاشتراك» من جدول Plan، والكارت بالجنيه للشركات المصرية بس.

قبل التصليح: صفحة إدارة الاشتراك كانت بتعرض أسعار مكتوبة في الكود (550/850/2500)
— تعديل السعر من لوحة المالك مايظهرش، والشركة الإماراتية تشوف «850 د.إ» بدل سعر
الدرهم. ودفع Social Studio / Omnichannel بالكارت كان بيبعت سعر الدرهم (25) لـ Paymob
كجنيه.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import Plan
from inventory.tests.base import ERPTenantTestCase


@override_settings(ALLOWED_HOSTS=['*'])
class PlanPricesPagesTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()
        Plan.objects.update_or_create(slug='auto-gold', defaults={
            'name': 'Gold — جولد', 'industry': 'automotive', 'monthly_price': Decimal('975.00'),
            'monthly_price_aed': Decimal('77.00'), 'max_users': 4, 'is_active': True, 'sort_order': 20})
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='automotive', country='EG')
        self.tenant.refresh_from_db()
        connection.set_tenant(self.tenant)
        user = get_user_model().objects.create_user('plan_owner', password='x', is_superuser=True, is_staff=True)
        self.c = HttpClient()
        self.c.force_login(user)

    def tearDown(self):
        # الـ flush بيمسح جداول السكيمة الحالية — لو فضلنا على public الشركة نفسها بتتمسح
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _set_country(self, cc):
        connection.set_schema_to_public()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(country=cc)
        connection.set_tenant(self.tenant)

    def test_manage_page_shows_plan_table_price(self):
        r = self.c.get('/subscription/manage/', HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '975')          # السعر اللي المالك حاطه، مش 850 المكتوبة
        self.assertContains(r, 'Gold — جولد')

    def test_manage_page_shows_aed_price_for_uae_company(self):
        self._set_country('AE')
        r = self.c.get('/subscription/manage/', HTTP_HOST=self.domain.domain)
        self.assertContains(r, '77')
        self.assertNotContains(r, '975')

    def test_card_checkout_refused_for_uae_company(self):
        self._set_country('AE')
        with mock.patch('clients.services.paymob.create_iframe_url') as iframe:
            r1 = self.c.post('/social-studio/pay/', HTTP_HOST=self.domain.domain)
            r2 = self.c.post('/omnichannel/pay/', HTTP_HOST=self.domain.domain)
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(r2.status_code, 302)
        iframe.assert_not_called()             # مفيش طلب دفع اتعمل بسعر الدرهم كجنيه

    def test_company_owner_cannot_extend_own_subscription_from_pricing_page(self):
        connection.set_schema_to_public()
        before = type(self.tenant).objects.get(pk=self.tenant.pk)
        connection.set_tenant(self.tenant)
        self.c.post('/pricing/', {'plan': 'empire', 'shop': self.tenant.schema_name},
                    HTTP_HOST=self.domain.domain)
        connection.set_schema_to_public()
        after = type(self.tenant).objects.get(pk=self.tenant.pk)
        self.assertEqual(after.subscription_end_date, before.subscription_end_date)
        self.assertEqual(after.plan, before.plan)


class PremiumDiagnosticsPlanMigrationTests(TestCase):
    """باقة الـ 6,000 ج كانت بترجع تتفعّل على أي قاعدة جديدة وتظهر في صفحة الأسعار."""

    def test_migration_deactivates_premium_plan_and_keeps_the_row(self):
        from importlib import import_module
        from django.apps import apps
        Plan.objects.update_or_create(slug='premium_diagnostics', defaults={
            'name': 'Premium Diagnostics', 'industry': 'automotive',
            'monthly_price': Decimal('6000.00'), 'is_active': True})
        import_module('clients.migrations.0086_deactivate_premium_diagnostics_plan').deactivate(apps, None)
        plan = Plan.objects.get(slug='premium_diagnostics')
        self.assertFalse(plan.is_active)

    def test_fresh_database_has_no_active_premium_plan(self):
        # بعد كل المهاجرات (أياً كان ترتيب diagnostics_catalog.0002) الباقة مقفولة
        self.assertFalse(Plan.objects.filter(slug='premium_diagnostics', is_active=True).exists())


class LandingBotPricingTests(TestCase):
    """بوت الموقع كان بيقول أسعار قديمة مكتوبة (سيلفر 780، Empire 1,800، تجربة 7 أيام، خصم 9/12.5/25%)."""

    def setUp(self):
        Plan.objects.update_or_create(slug='auto-silver', defaults={
            'name': 'Silver — سيلفر', 'industry': 'automotive', 'monthly_price': Decimal('615.00'),
            'quarterly_discount': 10, 'semi_annual_discount': 15, 'annual_discount': 20,
            'max_users': 2, 'is_active': True, 'sort_order': 10})

    def test_local_reply_and_ai_prompt_use_live_prices_and_trial(self):
        from clients.models.tenancy import TRIAL_DAYS
        from clients.views._shared import _landing_bot_local_reply
        from clients.views.ai_assistant_views import landing_bot_knowledge
        for text in (_landing_bot_local_reply('الأسعار كام'), landing_bot_knowledge()):
            self.assertIn('615', text)                       # السعر اللي المالك حاطه
            self.assertNotIn('780', text)
            self.assertIn('10% ربع سنوي', text)
            self.assertNotIn('12.5%', text)
        self.assertIn(f'{TRIAL_DAYS} يوم', _landing_bot_local_reply('عايز اجرب'))
        self.assertNotIn('5 أيام مجانية', landing_bot_knowledge())

    @override_settings(PAYMOB_CARD_SUBSCRIPTIONS=False)
    def test_bot_does_not_promise_card_payment_when_it_is_off(self):
        from clients.views._shared import _landing_bot_local_reply
        reply = _landing_bot_local_reply('طرق الدفع')
        self.assertIn('إنستاباي', reply)
        self.assertNotIn('Paymob', reply)
