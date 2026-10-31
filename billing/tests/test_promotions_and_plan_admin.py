"""🏷️ عروض الخصم المؤقتة + 💎 لوحة تعديل الباقة الكاملة.

قبل كده:
- مفيش أي طريقة لعمل خصم على باقة لمدة (يوم / 10 أيام / شهر) — كان لازم يتغيّر السعر نفسه
  ويرجع بإيد بعدين، والمشتركين القدام بيتقفل عليهم السعر الجديد.
- صفحة تعديل الباقة كانت السعر والصلاحيات بس: الحدود (مستخدمين/فروع/خزائن) والحصص وسعر
  الدرهم وخصومات المدة والاسم وسطور العرض وإخفاء الباقة كانوا من Django admin بس — وزيادة
  الحدود ماكانتش بتوصل للمشتركين الحاليين.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from clients.models import Client, Plan, PlanPromotion, TenantSubscription
from clients.services import platform_billing as pb
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()


def _tenant(schema, **kw):
    c = Client(name=schema, owner_name='T', phone='01000000000', schema_name=schema, industry='automotive', **kw)
    c.auto_create_schema = False
    c.save()
    return c


def _plan(slug, price, **kw):
    defaults = dict(name=slug, industry='automotive', monthly_price=Decimal(price), max_users=2, max_branches=1,
                    max_treasuries=1, quarterly_discount=10, semi_annual_discount=15, annual_discount=20,
                    is_active=True)
    defaults.update(kw)
    return Plan.objects.update_or_create(slug=slug, defaults=defaults)[0]


def _promo(plan=None, pct=20, start=-1, hours=24, **kw):
    now = timezone.now()
    return PlanPromotion.objects.create(plan=plan, percent_off=pct, starts_at=now + timedelta(hours=start),
                                        ends_at=now + timedelta(hours=start + hours), **kw)


class PromotionPricingTests(TestCase):

    def setUp(self):
        self.silver = _plan('auto-silver', '550')
        self.gold = _plan('auto-gold', '850')

    def fresh(self, plan):
        return Plan.objects.get(pk=plan.pk)       # من غير كاش العرض (دقيقة على الكائن)

    def test_running_promo_discounts_every_period_on_top_of_the_period_discount(self):
        _promo(self.silver, pct=20)
        silver = self.fresh(self.silver)
        self.assertEqual(silver.price_for_period(1), Decimal('440.00'))
        self.assertEqual(silver.price_for_period(12), Decimal('4224.00'))    # 550×12×0.8×0.8
        self.assertEqual(silver.price_for_period(1, promo=False), Decimal('550.00'))
        self.assertEqual(self.fresh(self.gold).price_for_period(1), Decimal('850.00'))   # باقة تانية

    def test_promo_only_inside_its_window(self):
        _promo(self.silver, pct=30, start=2)                        # لسه مابدأش
        _promo(self.silver, pct=40, start=-48, hours=24)            # خلص امبارح
        self.assertEqual(self.fresh(self.silver).price_for_period(1), Decimal('550.00'))
        _promo(self.silver, pct=50, is_active=False)                # اتوقف
        self.assertEqual(self.fresh(self.silver).price_for_period(1), Decimal('550.00'))

    def test_industry_wide_and_all_plans_promos_and_best_one_wins(self):
        _promo(industry='automotive', pct=10)
        _promo(pct=15)                                               # كل الباقات
        _promo(industry='printing', pct=60)                          # قطاع تاني
        self.assertEqual(self.fresh(self.gold).price_for_period(1), Decimal('722.50'))

    def test_invoice_during_promo_is_discounted_but_locked_price_stays_the_plan_price(self):
        _promo(self.silver, pct=20)
        tenant = _tenant('promo_shop')
        inv = pb.issue_subscription_invoice(tenant, self.fresh(self.silver), 1)
        self.assertEqual(inv.total, Decimal('440.00'))
        self.assertEqual(inv.subtotal, Decimal('550.00'))
        self.assertEqual(inv.discount_amount, Decimal('110.00'))
        sub = TenantSubscription.objects.get(tenant=tenant)
        self.assertEqual(sub.locked_monthly_price, Decimal('550.00'))   # التجديد بعد العرض بالسعر العادي

    def test_upgrade_credit_for_the_old_plan_ignores_a_promo_running_on_it(self):
        tenant = _tenant('promo_up')
        pb.issue_subscription_invoice(tenant, self.silver, 1)
        before = pb.quote_plan_change(tenant, self.fresh(self.gold), 1)['total']
        _promo(self.silver, pct=50)                                  # عرض على الباقة القديمة بس
        after = pb.quote_plan_change(Client.objects.get(pk=tenant.pk), self.fresh(self.gold), 1)['total']
        self.assertEqual(after, before)

    def test_percent_and_window_validation(self):
        from django.core.exceptions import ValidationError
        now = timezone.now()
        for pct, end in ((0, now + timedelta(days=1)), (95, now + timedelta(days=1)), (10, now - timedelta(days=1))):
            with self.assertRaises(ValidationError):
                PlanPromotion(plan=self.silver, percent_off=pct, starts_at=now, ends_at=end).full_clean()


@override_settings(ALLOWED_HOSTS=['*'])
class OwnerPanelPlansAndPromotionsTests(_PublicDomainMixin, TestCase):

    def setUp(self):
        self.owner = User.objects.create_user('owner@plat.test', password='x', is_superuser=True, is_staff=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)
        self.silver = _plan('auto-silver', '550')

    def test_promotion_for_10_days_from_the_owner_panel_then_stop_it(self):
        r = self.c.post('/superadmin/promotions/', {'target': f'plan:{self.silver.pk}', 'percent_off': '25',
                                                    'label': 'عرض الافتتاح', 'duration': '10d'})
        self.assertEqual(r.status_code, 302)
        promo = PlanPromotion.objects.get()
        self.assertEqual((promo.plan, promo.percent_off, promo.label), (self.silver, 25, 'عرض الافتتاح'))
        self.assertEqual(round((promo.ends_at - promo.starts_at).total_seconds()), 10 * 86400)
        self.assertEqual(Plan.objects.get(pk=self.silver.pk).price_for_period(1), Decimal('412.50'))
        page = self.c.get('/superadmin/promotions/')
        self.assertContains(page, 'عرض الافتتاح')
        self.c.post('/superadmin/promotions/', {'action': 'stop', 'promo_id': promo.pk})
        self.assertEqual(Plan.objects.get(pk=self.silver.pk).price_for_period(1), Decimal('550.00'))

    def test_bad_promotion_is_refused(self):
        self.c.post('/superadmin/promotions/', {'target': 'all', 'percent_off': '0', 'duration': '1d'})
        self.c.post('/superadmin/promotions/', {'target': 'plan:999999', 'percent_off': '10', 'duration': '1d'})
        self.assertFalse(PlanPromotion.objects.exists())

    def test_pricing_page_shows_old_price_struck_and_badge(self):
        _promo(self.silver, pct=20, label='الجمعة البيضاء')
        body = self.c.get('/pricing/').content.decode()
        self.assertIn('line-through', body)
        self.assertIn('الجمعة البيضاء', body)
        self.assertIn('data-p-monthly="440.00"', body)
        self.assertIn('data-o-monthly="550.00"', body)

    def _edit(self, **overrides):
        data = {'name': 'Silver', 'name_en': 'Silver', 'sort_order': '10', 'is_active': 'on',
                'monthly_price': '550', 'monthly_price_aed': '45', 'quarterly_discount': '10',
                'semi_annual_discount': '15', 'annual_discount': '20', 'max_users': '2', 'max_branches': '1',
                'max_treasuries': '1', 'monthly_ai_designs_quota': '0', 'monthly_diagnostics_scans_quota': '10',
                'monthly_diagnostics_bot_quota': '20', 'features_text': 'فاتورة بيع\nمخزون',
                'rollout': 'next_renewal'}
        data.update(overrides)
        return self.c.post(f'/superadmin/plans/{self.silver.pk}/edit/', data)

    def test_full_plan_edit_and_limit_increase_reaches_current_subscribers(self):
        tenant = _tenant('limits_shop')
        pb.issue_subscription_invoice(tenant, self.silver, 1)
        self._edit(max_users='5', max_branches='2', features_text='سطر 1\n\nسطر 2 ', is_active='',
                   monthly_price_aed='49.5', annual_discount='25', monthly_ai_designs_quota='20')
        plan = Plan.objects.get(pk=self.silver.pk)
        self.assertEqual((plan.max_users, plan.max_branches, plan.monthly_ai_designs_quota), (5, 2, 20))
        self.assertEqual((plan.monthly_price_aed, plan.annual_discount), (Decimal('49.50'), 25))
        self.assertEqual(plan.features, ['سطر 1', 'سطر 2'])
        self.assertFalse(plan.is_active)
        tenant.refresh_from_db()
        self.assertEqual((tenant.max_users, tenant.max_branches), (5, 2))

    def test_limit_decrease_only_with_force_apply(self):
        tenant = _tenant('limits_down')
        pb.issue_subscription_invoice(tenant, self.silver, 1)
        self._edit(max_users='1')
        tenant.refresh_from_db()
        self.assertEqual(tenant.max_users, 2)                    # الموظف التاني مايتشالش من غير قرار
        self._edit(max_users='1', rollout='force_apply', confirm_text='CONFIRM')
        tenant.refresh_from_db()
        self.assertEqual(tenant.max_users, 1)

    def test_form_saves_as_shown_even_with_localized_decimal_comma(self):
        page = self.c.get(f'/superadmin/plans/{self.silver.pk}/edit/').content.decode()
        self.assertIn('value="550.00"', page)              # مش «550,00»
        self._edit(monthly_price='1٬250٫50', monthly_price_aed='49,5')
        plan = Plan.objects.get(pk=self.silver.pk)
        self.assertEqual((plan.monthly_price, plan.monthly_price_aed), (Decimal('1250.50'), Decimal('49.50')))

    def test_bad_values_are_refused_without_saving(self):
        self._edit(max_users='0')
        self._edit(name='')
        self._edit(annual_discount='95')
        plan = Plan.objects.get(pk=self.silver.pk)
        self.assertEqual((plan.max_users, plan.name, plan.annual_discount), (2, 'auto-silver', 20))

    def test_clone_makes_a_hidden_copy(self):
        r = self.c.post(f'/superadmin/plans/{self.silver.pk}/clone/')
        copy = Plan.objects.get(slug='auto-silver-copy')
        self.assertRedirects(r, f'/superadmin/plans/{copy.pk}/edit/', fetch_redirect_response=False)
        self.assertFalse(copy.is_active)
        self.assertEqual((copy.monthly_price, copy.max_users), (self.silver.monthly_price, self.silver.max_users))
        self.c.post(f'/superadmin/plans/{self.silver.pk}/clone/')
        self.assertTrue(Plan.objects.filter(slug='auto-silver-copy2').exists())

    def test_staff_without_plans_widget_cannot_touch_plans(self):
        from clients.models import StaffRole
        agent = User.objects.create_user('agent@plat.test', password='x', is_staff=True)
        StaffRole.objects.create(user=agent, role='support')
        c = HttpClient()
        c.force_login(agent)
        c.post('/superadmin/promotions/', {'target': 'all', 'percent_off': '50', 'duration': '30d'})
        self.assertFalse(PlanPromotion.objects.exists())
        c.post(f'/superadmin/plans/{self.silver.pk}/clone/')
        self.assertFalse(Plan.objects.filter(slug__startswith='auto-silver-copy').exists())


@override_settings(ALLOWED_HOSTS=['*'])
class OwnerTeamPageTests(_PublicDomainMixin, TestCase):
    """👥 فريق المنصة: كان إضافة موظف دعم/مبيعات وصلاحياته من Django admin بس."""

    def setUp(self):
        self.owner = User.objects.create_user('owner@plat.test', password='x', is_superuser=True, is_staff=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)

    def test_add_staff_member_with_role_then_change_and_suspend(self):
        from clients.models import StaffRole
        r = self.c.post('/superadmin/team/', {'action': 'add', 'email': 'Agent@Plat.test', 'name': 'منى علي',
                                              'role': 'support'}, follow=True)
        agent = User.objects.get(username='agent@plat.test')
        self.assertTrue(agent.is_staff)
        self.assertEqual(agent.staff_role.role, 'support')
        msgs = [str(m) for m in r.context['messages']]
        temp = next(m for m in msgs if 'المؤقتة' in m).split('المؤقتة: ')[1].split(' ')[0]
        self.assertTrue(agent.check_password(temp))                     # كلمة السر بتظهر مرة للمالك
        a = HttpClient()
        a.force_login(agent)
        self.assertEqual(a.get('/superadmin/support/').status_code, 200)
        self.assertNotEqual(a.get('/superadmin/plans/').status_code, 200)   # الدعم مالوش الباقات
        self.assertNotEqual(a.get('/superadmin/team/').status_code, 200)    # والفريق للمالك بس
        self.c.post('/superadmin/team/', {'action': 'role', 'user_id': agent.pk, 'role': 'sales'})
        self.assertEqual(StaffRole.objects.get(user=agent).role, 'sales')
        self.c.post('/superadmin/team/', {'action': 'toggle', 'user_id': agent.pk})
        agent.refresh_from_db()
        self.assertFalse(agent.is_active)

    def test_owner_cannot_be_touched_and_god_role_is_not_grantable(self):
        self.c.post('/superadmin/team/', {'action': 'toggle', 'user_id': self.owner.pk})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.c.post('/superadmin/team/', {'action': 'add', 'email': 'x@plat.test', 'role': 'god'})
        self.assertFalse(User.objects.filter(username='x@plat.test').exists())
