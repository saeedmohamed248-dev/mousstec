"""📊 حدود كل باقة ظاهرة للعميل + 📚 صفحات المميزات فيها كل اللي في السيستم.

قبل التصليح:
  • كارت الباقة كان بيعرض أسماء المزايا من غير أرقامها، ومكررة جنب السطور المكتوبة
    («فاتورة بيع وشراء» + «فواتير بيع وشراء») — حد زي «50 سؤال للمستشار» أو «150 كرت صيانة»
    يظهر بس لو المالك كتبه بإيده، ولو غيّر الحد من المحرر الكلام يفضل قديم.
  • صفحة «المميزات» كانت لورش بس (من غير مطابع/موارد بشرية/سوق/دعم)، وجدولها ثابت وغلط
    («موظفين غير محدود» في Empire) وفيها وعود مش متنفذة («دعم 24/7»، «توفر 99.9%»).
"""
import re
from decimal import Decimal

from django.test import Client as HttpClient, SimpleTestCase, TestCase, override_settings

from clients.models import Plan, PlanPromotion
from clients.services.feature_catalog import ADDON, DEVICE, SECTIONS, build_sections
from clients.services.plan_compare import compare_table
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

ARABIC = re.compile(r'[؀-ۿ]')


def _plan(**kw):
    defaults = dict(industry='automotive', max_users=2, max_branches=1, max_treasuries=1,
                    monthly_diagnostics_scans_quota=0, monthly_diagnostics_bot_quota=0,
                    monthly_ai_designs_quota=0, entitlements={})
    defaults.update(kw)
    return Plan(**defaults)


class CompareTableTests(SimpleTestCase):

    def setUp(self):
        self.silver = _plan(name='Silver', monthly_diagnostics_scans_quota=10, entitlements={
            'core_invoicing': {'enabled': True}, 'core_inventory': {'enabled': True},
            'inventory_items': {'enabled': True, 'monthly_limit': 500},
            'workshop_repair_cards': {'enabled': True, 'monthly_limit': 150},
            'ai_copilot': {'enabled': True, 'monthly_limit': 50}})
        self.gold = _plan(name='Gold', max_users=4, monthly_diagnostics_scans_quota=40, entitlements={
            'core_invoicing': {'enabled': True}, 'core_inventory': {'enabled': True},
            'workshop_repair_cards': {'enabled': True, 'monthly_limit': 0},
            'ai_copilot': {'enabled': True, 'monthly_limit': 300},
            'hr_payroll': {'enabled': True}, 'new_quota_feature': {'enabled': True, 'monthly_limit': 7}})
        self.labels = {'core_invoicing': 'فواتير', 'hr_payroll': 'المرتبات', 'new_quota_feature': 'ميزة جديدة',
                       'b2b_marketplace': 'سوق B2B'}
        self.rows = {r['label']: r['cells'] for r in compare_table([self.silver, self.gold], self.labels)['rows']}

    def test_limits_come_from_the_same_values_that_are_enforced(self):
        self.assertEqual(self.rows['المستخدمين'], [{'n': 2}, {'n': 4}])
        self.assertEqual(self.rows['كروت الصيانة في الشهر'], [{'n': 150}, {'unlimited': True}])
        self.assertEqual(self.rows['أسئلة المستشار الذكي في الشهر'], [{'n': 50}, {'n': 300}])
        self.assertEqual(self.rows['فحوصات التشخيص في الشهر'], [{'n': 10}, {'n': 40}])

    def test_items_limit_is_unlimited_when_inventory_is_on_without_a_cap(self):
        self.assertEqual(self.rows['أصناف المخزون'], [{'n': 500}, {'unlimited': True}])

    def test_features_show_tick_cross_and_new_quota_features_show_their_number(self):
        self.assertEqual(self.rows['فواتير'], [{'yes': True}, {'yes': True}])
        self.assertEqual(self.rows['المرتبات'], [{'no': True}, {'yes': True}])
        self.assertEqual(self.rows['ميزة جديدة'], [{'no': True}, {'n': 7}])

    def test_rows_no_plan_has_are_hidden(self):
        for label in ('سوق B2B', 'أسئلة بوت التشخيص في الشهر', 'تصاميم AI في الشهر', 'طلبات الطباعة في الشهر'):
            self.assertNotIn(label, self.rows)

    def test_features_in_every_plan_come_before_upper_plan_features(self):
        labels = list(self.rows)
        self.assertLess(labels.index('فواتير'), labels.index('المرتبات'))


class FeatureCatalogTests(SimpleTestCase):

    def test_every_line_has_english_without_arabic(self):
        """الموقع الإنجليزي بيقع في e2e لو ظهر أي عربي."""
        for sid, _icon, _color, title, intro, _ind, items in SECTIONS:
            texts = [title[1], intro[1]] + [t for it in items for t in (it[1][1], it[2][1])]
            for text in texts + [ADDON[1], DEVICE[1]]:
                self.assertFalse(ARABIC.search(text), f'{sid}: {text}')

    def test_tier_is_the_lowest_plan_and_absent_when_in_every_plan(self):
        silver = _plan(name='Silver — سيلفر', name_en='Silver', entitlements={'ai_copilot': {'enabled': True}})
        gold = _plan(name='Gold — جولد', name_en='Gold', entitlements={
            'ai_copilot': {'enabled': True}, 'hr_payroll': {'enabled': True}})
        items = {i['title']: i for s in build_sections(is_en=False, plans=[silver, gold]) for i in s['items']}
        self.assertEqual(items['المرتبات والعمولات']['tier'], 'Gold')
        self.assertIsNone(items['المستشار الذكي']['tier'])
        self.assertNotIn('عقود الأساطيل', items)        # مش في أي باقة ⇒ مانعلنش عنها
        self.assertEqual(items['Social Studio']['badge'], ADDON[0])


@override_settings(ALLOWED_HOSTS=['*'])
class PublicFeaturePagesTests(_PublicDomainMixin, TestCase):

    def setUp(self):
        Plan.objects.update(is_active=False)
        common = {'core_invoicing': {'enabled': True}, 'core_inventory': {'enabled': True}}
        self.silver = Plan.objects.update_or_create(slug='auto-silver', defaults=dict(
            name='Silver — سيلفر', name_en='Silver', industry='automotive', monthly_price=Decimal('550'),
            max_users=2, max_branches=1, max_treasuries=1, sort_order=10, is_active=True,
            monthly_diagnostics_scans_quota=10, features=['فاتورة بيع وشراء'],
            entitlements={**common, 'workshop_repair_cards': {'enabled': True, 'monthly_limit': 150},
                          'ai_copilot': {'enabled': True, 'monthly_limit': 50}}))[0]
        Plan.objects.update_or_create(slug='auto-gold', defaults=dict(
            name='Gold — جولد', name_en='Gold', industry='automotive', monthly_price=Decimal('850'),
            max_users=4, max_branches=2, max_treasuries=2, sort_order=20, is_active=True,
            monthly_diagnostics_scans_quota=40,
            entitlements={**common, 'workshop_repair_cards': {'enabled': True},
                          'ai_copilot': {'enabled': True, 'monthly_limit': 300}, 'hr_payroll': {'enabled': True}}))
        Plan.objects.update_or_create(slug='print-starter', defaults=dict(
            name='Starter — ستارتر', name_en='Starter', industry='printing', monthly_price=Decimal('875'),
            max_users=2, sort_order=10, is_active=True, monthly_ai_designs_quota=50,
            entitlements={'print_orders': {'enabled': True}, 'ai_copilot': {'enabled': True, 'monthly_limit': 100}}))

    def test_pricing_page_shows_every_limit_per_plan(self):
        r = HttpClient().get('/pricing/')
        self.assertEqual(r.status_code, 200)
        auto = r.context['compare_by_industry']['automotive']
        self.assertEqual(auto['plans'], ['Silver — سيلفر', 'Gold — جولد'])
        rows = {row['label']: row['cells'] for row in auto['rows']}
        self.assertEqual(rows['أسئلة المستشار الذكي في الشهر'], [{'n': 50}, {'n': 300}])
        self.assertEqual(rows['كروت الصيانة في الشهر'], [{'n': 150}, {'unlimited': True}])
        self.assertContains(r, 'id="automotive_compare"')
        self.assertContains(r, 'id="printing_compare"')
        self.assertContains(r, 'قارن الباقات — كل المميزات والحدود')

    def test_owner_limit_change_shows_up_immediately(self):
        self.silver.entitlements['ai_copilot']['monthly_limit'] = 80
        self.silver.save()
        r = HttpClient().get('/pricing/')
        rows = {row['label']: row['cells'] for row in r.context['compare_by_industry']['automotive']['rows']}
        self.assertEqual(rows['أسئلة المستشار الذكي في الشهر'][0], {'n': 80})

    def test_card_no_longer_repeats_feature_names_next_to_written_lines(self):
        r = HttpClient().get('/pricing/')
        card = r.context['plans_by_industry']['automotive'][0]
        self.assertNotIn('entitlement_labels', card)
        self.assertEqual(card['features'], ['فاتورة بيع وشراء'])

    def test_features_page_covers_every_area_with_live_tiers_and_no_false_promises(self):
        r = HttpClient().get('/features/')
        self.assertEqual(r.status_code, 200)
        ids = [s['id'] for s in r.context['sections']]
        for sid in ('sales', 'inventory', 'finance', 'team', 'ai', 'automotive', 'printing', 'market', 'platform'):
            self.assertIn(sid, ids)
        self.assertContains(r, 'من باقة Gold')                 # المرتبات من Gold — محسوبة من الباقات
        self.assertContains(r, 'زرار «مشكلة؟ كلّمنا»')
        self.assertContains(r, 'id="compare"')
        for promise in ('دعم 24/7', '99.9%'):
            self.assertNotContains(r, promise)

    def test_features_page_in_english(self):
        c = HttpClient()
        c.cookies['django_language'] = 'en'
        r = c.get('/features/')
        self.assertContains(r, 'Payroll &amp; commissions')
        self.assertContains(r, 'From Gold')
        self.assertEqual(r.context['compare_by_industry']['automotive']['plans'], ['Silver', 'Gold'])

    def test_sector_landing_pages_list_all_features_and_their_plan_limits(self):
        r = HttpClient().get('/automotive/')
        self.assertContains(r, 'id="all-features"')
        self.assertContains(r, 'كروت الصيانة')
        self.assertContains(r, 'المرتبات والعمولات')
        self.assertContains(r, 'أسئلة المستشار الذكي في الشهر')
        self.assertNotContains(r, 'Gold / ')                    # صفحة الورش: باقات السيارات بس
        r = HttpClient().get('/printing/')
        self.assertContains(r, 'بروفة بموافقة العميل')
        self.assertContains(r, 'تصاميم AI في الشهر')

    def test_arabic_promo_name_is_hidden_on_the_english_site(self):
        from datetime import timedelta
        from django.utils import timezone
        PlanPromotion.objects.create(plan=self.silver, percent_off=25, label='عرض الافتتاح',
                                     starts_at=timezone.now() - timedelta(hours=1),
                                     ends_at=timezone.now() + timedelta(days=3))
        c = HttpClient()
        c.cookies['django_language'] = 'en'
        r = c.get('/pricing/')
        self.assertEqual(r.context['plans_by_industry']['automotive'][0]['promo']['label'], '')
        r = HttpClient().get('/pricing/')
        self.assertEqual(r.context['plans_by_industry']['automotive'][0]['promo']['label'], 'عرض الافتتاح')
