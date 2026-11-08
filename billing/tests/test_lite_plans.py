"""💎 باقات Lite (أرخص باقة سيارات/مطابع) ومراجعة مميزات الباقات (clients.0104).

* الباقات والمميزات الجديدة اتسجّلت بالأسعار والحدود الصح.
* حد فواتير البيع الشهري (Lite سيارات) وحد طلبات الطباعة الشهري (Lite مطابع) متطبّقين.
* Lite من غير الورشة وأوامر الشراء ولوحة الإنتاج — والباقات الأعلى فيها.
* المشترك القديم (لقطة مميزات قبل الميزة الجديدة) بياخد الميزة الجديدة اللي باقته خدتها.
"""
import json
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client as HttpClient, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import Feature, Plan, PlatformInvoice, TenantSubscription
from clients.services import platform_billing as pb
from clients.services.entitlements import EntitlementService
from inventory.models import SaleInvoice
from inventory.tests.base import ERPTenantTestCase
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin
from inventory.tests.factories import make_branch, make_customer, make_inventory, make_product, make_treasury
from tenancy.services.plan_limits import print_orders_block, repair_cards_block, sales_invoices_block

User = get_user_model()
MIGRATION = 'clients.migrations.0104_lite_plans_and_feature_matrix'


def _reset_plans():
    """باقات المهاجرة بحالتها بالظبط — اختبارات تانية في نفس القاعدة بتعدّل الباقات."""
    mig = import_module(MIGRATION)
    existing = {
        'auto-silver': ('automotive', '550', 2, 1, 10), 'auto-gold': ('automotive', '850', 4, 2, 20),
        'auto-empire': ('automotive', '2500', 10, 5, 30), 'print-starter': ('printing', '875', 2, 1, 40),
        'print-pro': ('printing', '1250', 4, 2, 50), 'print-enterprise': ('printing', '2000', 6, 4, 60),
    }
    for slug, (industry, price, users, branches, order) in existing.items():
        Plan.objects.get_or_create(slug=slug, defaults=dict(
            name=slug, industry=industry, monthly_price=Decimal(price), max_users=users,
            max_branches=branches, max_treasuries=branches, sort_order=order, is_active=True))
    codes = {c for _s, (add, _f) in mig.UPGRADES.items() for c in add} | {'workshop_repair_cards', 'print_orders'}
    for code in codes:
        Feature.objects.get_or_create(code=code, defaults=dict(
            name_ar=code, name_en=code, category='core', is_active=True,
            is_quantitative=code in ('workshop_repair_cards', 'print_orders', 'inventory_items')))
    for slug, data in mig.NEW_PLANS.items():
        Plan.objects.update_or_create(slug=slug, defaults={**data, 'is_active': True})
    for slug, (add, features) in mig.UPGRADES.items():
        plan = Plan.objects.filter(slug=slug).first()
        if plan is None:
            continue
        plan.entitlements = {**{c: dict(v) for c, v in add.items()},
                             **({'workshop_repair_cards': {'enabled': True, 'monthly_limit': 150}}
                                if slug == 'auto-silver' else {})}
        plan.features, plan.is_active = features, True
        plan.save(update_fields=['entitlements', 'features', 'is_active'])
    Feature.objects.filter(code__in=codes).update(is_active=True)
    mig.apply(apps, None)


@override_settings(ALLOWED_HOSTS=['*'])
class LitePlansTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        from django.core.cache import caches
        caches['local_tier'].clear()
        connection.set_schema_to_public()
        _reset_plans()
        self.plans = {p.slug: p for p in Plan.objects.filter(is_active=True)}
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            industry='automotive', status='active', subscription_end_date=timezone.localdate(),
            extra_users_purchased=0, extra_branches_purchased=0, extra_treasuries_purchased=0)
        TenantSubscription.objects.filter(tenant=self.tenant).delete()
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain

    def tearDown(self):
        connection.set_schema_to_public()
        PlatformInvoice.objects.filter(tenant=self.tenant).delete()
        TenantSubscription.objects.filter(tenant=self.tenant).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def subscribe(self, slug, limit_override=None):
        connection.set_schema_to_public()
        plan = Plan.objects.get(slug=slug)
        if limit_override:
            code, n = limit_override
            ents = dict(plan.entitlements)
            ents[code] = {'enabled': True, 'monthly_limit': n}
            plan.entitlements = ents
            plan.save(update_fields=['entitlements'])
        tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        pb.issue_subscription_invoice(tenant, plan, 1)
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        connection.set_tenant(self.tenant)

    def owner_client(self):
        c = HttpClient()
        c.force_login(User.objects.create_user(f'lite_owner_{User.objects.count()}', password='x',
                                               is_superuser=True, is_staff=True))
        return c

    # ── البيانات ─────────────────────────────────────────────────────
    def test_lite_plans_are_the_cheapest_with_core_features(self):
        auto, prn = self.plans['auto-lite'], self.plans['print-lite']
        self.assertEqual(auto.monthly_price, Decimal('249.00'))
        self.assertEqual(prn.monthly_price, Decimal('299.00'))
        for p in (auto, prn):
            self.assertEqual((p.max_users, p.max_branches, p.max_treasuries), (1, 1, 1))
            sector = [q for q in self.plans.values()
                      if q.industry == p.industry and q.slug.split('-')[0] in ('auto', 'print')]
            self.assertEqual(min(sector, key=lambda q: q.monthly_price).slug, p.slug)
            self.assertNotIn('ai_copilot', p.entitlements)          # الذكاء الاصطناعي بيتكلّف فلوس
            self.assertTrue(p.entitlements['e_invoice_eta']['enabled'])
        self.assertEqual(auto.entitlements['sales_invoices']['monthly_limit'], 300)
        self.assertEqual(prn.entitlements['print_orders']['monthly_limit'], 100)
        self.assertNotIn('workshop_repair_cards', auto.entitlements)
        self.assertNotIn('purchase_orders', auto.entitlements)
        self.assertNotIn('print_production_board', prn.entitlements)
        # كل كود في الباقات موجود في الكتالوج (الأدمن بيرفض غير كده)
        codes = set(Feature.objects.filter(is_active=True).values_list('code', flat=True))
        for p in self.plans.values():
            self.assertFalse(set(p.entitlements) - codes, p.slug)

    def test_existing_plans_gain_the_new_features_and_keep_prices(self):
        silver, gold = self.plans['auto-silver'], self.plans['auto-gold']
        self.assertEqual(silver.monthly_price, Decimal('550.00'))
        self.assertTrue(silver.entitlements['purchase_orders']['enabled'])
        self.assertNotIn('monthly_limit', silver.entitlements['inventory_items'])   # من غير حد أصناف
        self.assertTrue(gold.entitlements['stock_transfers']['enabled'])
        for slug in ('print-starter', 'print-pro', 'print-enterprise'):
            self.assertTrue(self.plans[slug].entitlements['print_production_board']['enabled'], slug)

    def test_migration_is_idempotent(self):
        connection.set_schema_to_public()
        import_module(MIGRATION).apply(apps, None)
        self.assertEqual(Plan.objects.filter(slug__in=('auto-lite', 'print-lite')).count(), 2)

    # ── حد فواتير البيع (Lite سيارات) ─────────────────────────────────
    def test_lite_sales_invoice_monthly_cap_blocks_the_pos(self):
        self.subscribe('auto-lite', limit_override=('sales_invoices', 1))
        branch = make_branch()
        till = make_treasury(branch, balance='100.00')
        part = make_product(part_number='LITE-1', retail_price='50.00', average_cost='20.00')
        make_inventory(part, branch, 10)
        c = self.owner_client()
        sale = lambda: c.post('/system/lightning-pos/checkout/', json.dumps({  # noqa: E731
            'branch_id': branch.pk, 'items': [{'product_id': part.pk, 'qty': 1, 'price': 50}],
            'treasury_id': till.pk, 'paid_amount': 50}), content_type='application/json',
            HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(sale().status_code, 200)
        r = sale()
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()['code'], 'plan_limit')
        self.assertEqual(SaleInvoice.objects.count(), 1)
        # 🐛 ترجيع تاريخ الفاتورة لشهر فات مابيعدّيش الحد (الحد بوقت الإنشاء الفعلي)
        SaleInvoice.objects.update(date_created=timezone.now() - timedelta(days=45))
        self.assertEqual(sale().status_code, 402)
        # الباقة الأعلى من غير حد
        self.subscribe('auto-silver')
        self.assertIsNone(sales_invoices_block(self.tenant))

    def test_usage_warning_from_80_percent_and_banner_in_the_portal(self):
        from tenancy.services.plan_limits import usage_warning
        self.subscribe('auto-lite', limit_override=('sales_invoices', 5))
        branch, cust = make_branch(), make_customer()
        for _ in range(3):
            SaleInvoice.objects.create(customer=cust, branch=branch, invoice_type='sale')
        self.assertIsNone(usage_warning(self.tenant))                    # 60% — لسه
        SaleInvoice.objects.create(customer=cust, branch=branch, invoice_type='sale')
        w = usage_warning(self.tenant)
        self.assertEqual((w['used'], w['limit'], w['left'], w['full']), (4, 5, 1, False))
        r = self.owner_client().get(reverse('inventory:product_list'), HTTP_HOST=self.host)
        self.assertContains(r, 'planUsageBanner')
        self.assertContains(r, 'فاضل 1')
        SaleInvoice.objects.create(customer=cust, branch=branch, invoice_type='sale')
        self.assertTrue(usage_warning(self.tenant)['full'])
        self.subscribe('auto-silver')                                    # من غير حد ⇒ مفيش بانر
        self.assertIsNone(usage_warning(self.tenant))

    def test_lite_has_no_workshop_and_silver_does(self):
        self.subscribe('auto-lite')
        self.assertIn('الورشة', repair_cards_block(self.tenant))
        self.subscribe('auto-silver')
        self.assertIsNone(repair_cards_block(self.tenant))

    def test_purchase_orders_are_silver_and_up(self):
        self.subscribe('auto-lite')
        c = self.owner_client()
        self.assertEqual(c.get(reverse('inventory:purchase_order_list'), HTTP_HOST=self.host).status_code, 403)
        self.subscribe('auto-silver')
        self.assertEqual(c.get(reverse('inventory:purchase_order_list'), HTTP_HOST=self.host).status_code, 200)

    # ── المطابع ──────────────────────────────────────────────────────
    def test_print_lite_order_cap_and_production_board(self):
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.subscribe('print-lite', limit_override=('print_orders', 1))
        from printing.models import PrintOrder, PrintTreasury
        from printing.quick_invoice import create_quick_invoice
        PrintTreasury.objects.create(name='الخزنة')
        user = User.objects.create_user('print_lite_boss', password='x', is_superuser=True, is_staff=True)
        create_quick_invoice(user=user, lines=[('كروت', '1', '100')], customer_name='عميل', delivered=False)
        self.assertIsNotNone(print_orders_block(self.tenant))
        PrintOrder.objects.update(date_created=timezone.now() - timedelta(days=45))
        self.assertIsNotNone(print_orders_block(self.tenant))      # نفس الحماية في المطابع
        with self.assertRaises(ValidationError):
            create_quick_invoice(user=user, lines=[('ستيكر', '1', '50')], customer_name='عميل', delivered=False)
        c = HttpClient()
        c.force_login(user)
        self.assertEqual(c.get(reverse('printing:production_board'), HTTP_HOST=self.host).status_code, 403)
        self.subscribe('print-starter')
        self.assertIsNone(print_orders_block(self.tenant))
        self.assertEqual(c.get(reverse('printing:production_board'), HTTP_HOST=self.host).status_code, 200)

    # ── المشترك المقفول قبل المهاجرة ميخسرش اللي كان مفتوح ──────────
    def test_migration_adds_new_features_to_locked_subscribers(self):
        self.subscribe('auto-silver')
        connection.set_schema_to_public()
        sub = TenantSubscription.objects.get(tenant=self.tenant)
        old = {k: v for k, v in sub.locked_entitlements.items() if k != 'purchase_orders'}
        TenantSubscription.objects.filter(pk=sub.pk).update(locked_entitlements=old)
        import_module(MIGRATION).apply(apps, None)
        sub.refresh_from_db()
        self.assertTrue(sub.locked_entitlements['purchase_orders']['enabled'])
        # القاعدة العامة: ميزة تتضاف للباقة بعد كده مش بتوصل للمقفول غير عند التجديد
        plan = Plan.objects.get(slug='auto-silver')
        plan.entitlements = {**plan.entitlements, 'b2b_marketplace': {'enabled': True}}
        plan.save(update_fields=['entitlements'])
        connection.set_tenant(self.tenant)
        self.tenant = type(self.tenant).objects.get(pk=self.tenant.pk)
        self.assertTrue(EntitlementService.has(self.tenant, 'purchase_orders'))
        self.assertFalse(EntitlementService.has(self.tenant, 'b2b_marketplace'))


@override_settings(ALLOWED_HOSTS=['*'])
class LitePlansPagesTests(_PublicDomainMixin, TestCase):
    """صفحة الأسعار وصفحة المطابع: 4 كروت بمميزات كل باقة + سطور المقارنة الجديدة."""

    def setUp(self):
        _reset_plans()

    def test_pricing_page_shows_the_lite_plan_and_new_rows(self):
        r = HttpClient().get('/pricing/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '300 فاتورة بيع/شهر')
        self.assertContains(r, 'xl:grid-cols-4')
        self.assertContains(r, 'الفاتورة الإلكترونية')       # سطر في جدول المقارنة
        self.assertContains(r, 'أوامر الشراء من النواقص')

    def test_print_landing_cards_show_each_plan_features(self):
        r = HttpClient().get(reverse('printing_landing'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '100 طلب طباعة/شهر')
        self.assertContains(r, 'لوحة الإنتاج')
