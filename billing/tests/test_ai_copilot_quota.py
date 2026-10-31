"""🧠 حد أسئلة المستشار الذكي الشهري + 💸 تسجيل تكلفة الذكاء الاصطناعي لكل شركة.

قبل كده: المستشار كان من غير أي حد — شركة واحدة تقدر تسأل آلاف الأسئلة والمنصة تدفع لمزوّد الـ AI
من غير ما تعرف. ومفيش أي مكان يقول كل شركة كلّفت كام قصاد اشتراكها.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import AIBonusGrant, AILimitTracker, Plan, PlatformInvoice, TenantSubscription
from clients.services import platform_billing as pb
from inventory.tests.base import ERPTenantTestCase
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()


def _together_reply(*_a, **_kw):
    body = {'choices': [{'message': {'content': 'مبيعات النهارده 1,200 ج.م'}}],
            'usage': {'prompt_tokens': 1500, 'completion_tokens': 500}}
    return mock.Mock(status_code=200, json=lambda: body, text='')


@override_settings(ALLOWED_HOSTS=['*'], ENABLE_AI_PREDICTIONS=True, TOGETHER_API_KEY='k')
class CopilotQuotaTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        from django.core.cache import caches
        caches['local_tier'].clear()
        connection.set_schema_to_public()
        self.silver = Plan.objects.update_or_create(slug='auto-silver', defaults=dict(
            name='Silver', industry='automotive', monthly_price=Decimal('550'), max_users=5, max_branches=5,
            max_treasuries=5, is_active=True,
            entitlements={'core_invoicing': {'enabled': True}, 'ai_copilot': {'enabled': True, 'monthly_limit': 2}}))[0]
        tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        type(self.tenant).objects.filter(pk=tenant.pk).update(industry='automotive', status='active')
        TenantSubscription.objects.filter(tenant=tenant).delete()
        AIBonusGrant.objects.filter(tenant=tenant).delete()
        AILimitTracker.objects.filter(tenant=tenant).delete()
        pb.issue_subscription_invoice(tenant, self.silver, 1)
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        connection.set_tenant(self.tenant)
        self.c = HttpClient()
        self.c.force_login(User.objects.create_user('quota_owner', password='x', is_superuser=True, is_staff=True))

    def tearDown(self):
        connection.set_schema_to_public()
        AILimitTracker.objects.filter(tenant_id=self.tenant.pk).delete()
        AIBonusGrant.objects.filter(tenant_id=self.tenant.pk).delete()
        PlatformInvoice.objects.filter(tenant_id=self.tenant.pk).delete()
        TenantSubscription.objects.filter(tenant_id=self.tenant.pk).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def ask(self):
        with mock.patch('inventory.ai_services.requests.post', side_effect=_together_reply):
            return self.c.get('/system/api/v1/ai/estimate-repair/', {'query': 'مبيعات النهارده'},
                              HTTP_HOST=self.domain.domain)

    def test_monthly_limit_then_ai_credits_then_blocked(self):
        for _ in range(2):
            r = self.ask()
            self.assertEqual(r.status_code, 200)
            self.assertIn('1,200', r.json()['recommendations'])
        r = self.ask()                                                  # الحد خلص ومفيش رصيد
        self.assertEqual((r.status_code, r.json()['code']), (403, 'copilot_quota'))
        self.assertIn('رصيد AI', r.json()['message'])
        connection.set_schema_to_public()
        AIBonusGrant.objects.create(tenant_id=self.tenant.pk, granted_designs=1, reason='test')
        connection.set_tenant(self.tenant)
        for _ in range(10):                                             # رصيد واحد = 10 أسئلة
            self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(self.ask().status_code, 403)
        connection.set_schema_to_public()
        grant = AIBonusGrant.objects.get(tenant_id=self.tenant.pk)
        self.assertEqual(grant.consumed_designs, 1)
        self.assertEqual(AILimitTracker.objects.filter(tenant_id=self.tenant.pk,
                                                       action_type='copilot_question').count(), 12)
        connection.set_tenant(self.tenant)

    def test_every_ai_call_is_logged_with_tokens_for_the_company(self):
        self.ask()
        connection.set_schema_to_public()
        log = AILimitTracker.objects.get(tenant_id=self.tenant.pk, action_type='llm_text')
        connection.set_tenant(self.tenant)
        self.assertEqual((log.metadata['provider'], log.metadata['tokens_in'], log.metadata['tokens_out']),
                         ('together', 1500, 500))
        self.assertIn('ai_repair_estimator_api', log.metadata['caller'])

    def test_unlimited_plan_and_trial_are_not_capped(self):
        connection.set_schema_to_public()
        self.silver.entitlements = {'core_invoicing': {'enabled': True}, 'ai_copilot': {'enabled': True}}
        self.silver.save()
        pb.issue_subscription_invoice(type(self.tenant).objects.get(pk=self.tenant.pk), self.silver, 1)
        connection.set_tenant(self.tenant)
        for _ in range(4):
            self.assertEqual(self.ask().status_code, 200)

    def test_subscription_page_shows_usage_and_top_up(self):
        self.ask()
        r = self.c.get('/subscription/manage/', HTTP_HOST=self.domain.domain)
        self.assertContains(r, 'id="ai-credits"')
        self.assertEqual(r.context['ai_credits']['copilot'], {'limit': 2, 'used': 1})
        self.assertContains(r, 'tnt_40')


@override_settings(ALLOWED_HOSTS=['*'])
class AIUsagePageTests(_PublicDomainMixin, TestCase):

    def test_cost_per_company_against_its_plan_price(self):
        from clients.models import Client
        plan = Plan.objects.update_or_create(slug='auto-silver', defaults=dict(
            name='Silver', industry='automotive', monthly_price=Decimal('550'), max_users=2, max_branches=1,
            max_treasuries=1, is_active=True))[0]
        shop = Client(name='ورشة النور', owner_name='x', phone='0100', schema_name='ai_cost_shop',
                      max_users=2, max_branches=1, max_treasuries=1)
        shop.auto_create_schema = False
        shop.save()
        TenantSubscription.objects.create(tenant=shop, plan=plan, locked_monthly_price=Decimal('550'))
        for _ in range(100):      # 100 نداء × 2,000 توكن × 0.88$/مليون × 50 = 8.80 ج.م
            AILimitTracker.objects.create(tenant=shop, action_type='llm_text',
                                          metadata={'provider': 'together', 'tokens_in': 1500, 'tokens_out': 500})
        for _ in range(5):        # 5 صور × 0.04$ × 50 = 10 ج.م
            AILimitTracker.objects.create(tenant=shop, action_type='ai_generation')
        owner = User.objects.create_user('owner@plat.test', password='x', is_superuser=True, is_staff=True)
        c = HttpClient()
        c.force_login(owner)
        r = c.get('/superadmin/ai-usage/')
        self.assertEqual(r.status_code, 200)
        row = r.context['rows'][0]
        self.assertEqual((row['calls'], row['tokens'], row['images'], row['cost']), (100, 200000, 5, Decimal('18.80')))
        self.assertEqual(row['share'], Decimal('3.4'))
        self.assertContains(r, 'ورشة النور')

    def test_copilot_limits_migration(self):
        from importlib import import_module
        from django.apps import apps
        for slug in ('auto-silver', 'auto-gold', 'auto-empire', 'print-starter'):
            Plan.objects.update_or_create(slug=slug, defaults=dict(
                name=slug, industry='printing' if slug.startswith('print') else 'automotive',
                monthly_price=Decimal('100'), is_active=True, entitlements={}, features=[]))
        import_module('clients.migrations.0097_ai_copilot_quota_and_usage').copilot_limits(apps, None)
        ents = {p.slug: p.entitlements['ai_copilot'] for p in Plan.objects.filter(
            slug__in=['auto-silver', 'auto-gold', 'auto-empire', 'print-starter'])}
        self.assertEqual(ents, {'auto-silver': {'enabled': True, 'monthly_limit': 50},
                                'auto-gold': {'enabled': True, 'monthly_limit': 300},
                                'auto-empire': {'enabled': True},
                                'print-starter': {'enabled': True, 'monthly_limit': 100}})
        self.assertIn('المستشار الذكي: 50 سؤال/شهر', Plan.objects.get(slug='auto-silver').features)
