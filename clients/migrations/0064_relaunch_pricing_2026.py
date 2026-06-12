"""
🚀 Relaunch Pricing — 2026-06-11

Wipes (deactivates) every legacy Plan / AIAddonPackage / DiagnosticsAddon /
DesignPackage row, then seeds the fresh launch catalog the user signed off on:

Automotive plans       : Silver 550 / Gold 850 / Empire 2500
Printing plans         : Starter 875 / Pro 1250 / Enterprise 2000
Design packs (Customer): 10@60, 20@115, 50@275  (HD, personal)
Design packs (Designer): 20@149, 50@349, 100@599 (4K + source files, commercial)
Diagnostics top-up     : 150 EGP / 30 uses

Diagnostics quotas live on Plan now (no separate DiagnosticsAddon needed for
the launch tier). The old 6000 EGP Premium Diagnostics add-on is deactivated.

Legacy rows are kept (is_active=False) to preserve FK integrity from existing
TenantSubscription / TenantDesignTopUp rows. Nothing is hard-deleted.
"""
from decimal import Decimal
from django.db import migrations


def seed(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')
    AIAddon = apps.get_model('clients', 'AIAddonPackage')
    DiagAddon = apps.get_model('clients', 'DiagnosticsAddon')
    DesignPkg = apps.get_model('clients', 'DesignPackage')
    TopUp = apps.get_model('clients', 'DiagnosticsTopUpPack')

    # ── 1. Deactivate everything ──────────────────────────────────────
    Plan.objects.update(is_active=False)
    AIAddon.objects.update(is_active=False)
    DiagAddon.objects.update(is_active=False)
    DesignPkg.objects.update(is_active=False)

    # ── 2. Seed Automotive plans ──────────────────────────────────────
    auto_silver = {
        'slug': 'auto-silver',
        'defaults': dict(
            name='Silver — سيلفر',
            industry='automotive',
            monthly_price=Decimal('550.00'),
            quarterly_discount=10, semi_annual_discount=15, annual_discount=20,
            max_branches=1, max_users=1, max_treasuries=1,
            monthly_ai_designs_quota=0,
            monthly_diagnostics_scans_quota=10,
            monthly_diagnostics_bot_quota=20,
            features=[
                'فاتورة بيع وشراء', 'مخزون قطع غيار حتى 500 صنف',
                'خزينة واحدة', '150 كرت صيانة/شهر',
                'السوق المفتوح', 'تقارير أساسية',
                '10 فحص تشخيص/شهر', '20 سؤال بوت تشخيص/شهر',
            ],
            entitlements={
                'core_invoicing':        {'enabled': True},
                'core_inventory':        {'enabled': True},
                'core_treasury':         {'enabled': True},
                'workshop_repair_cards': {'enabled': True, 'monthly_limit': 150},
                'open_marketplace':      {'enabled': True},
                'reports_basic':         {'enabled': True},
            },
            is_active=True, sort_order=10,
        ),
    }
    auto_gold = {
        'slug': 'auto-gold',
        'defaults': dict(
            name='Gold — جولد',
            industry='automotive',
            monthly_price=Decimal('850.00'),
            quarterly_discount=10, semi_annual_discount=15, annual_discount=20,
            max_branches=2, max_users=4, max_treasuries=2,
            monthly_ai_designs_quota=0,
            monthly_diagnostics_scans_quota=40,
            monthly_diagnostics_bot_quota=40,
            features=[
                'كل مميزات Silver', 'فرعين · 4 مستخدمين · خزينتين',
                'كروت صيانة غير محدودة', 'مخزون غير محدود',
                'سوق B2B', 'تحليل أعطال DTC',
                'تقارير أرباح متقدمة',
                '40 فحص تشخيص/شهر', '40 سؤال بوت تشخيص/شهر',
                'إضافة موظف/فرع/خزينة: 125 ج.م/شهر',
            ],
            entitlements={
                'core_invoicing':        {'enabled': True},
                'core_inventory':        {'enabled': True},
                'core_treasury':         {'enabled': True},
                'workshop_repair_cards': {'enabled': True, 'monthly_limit': 0},
                'workshop_dtc_ai':       {'enabled': True},
                'b2b_marketplace':       {'enabled': True},
                'open_marketplace':      {'enabled': True},
                'reports_basic':         {'enabled': True},
                'reports_advanced':      {'enabled': True},
            },
            is_active=True, sort_order=20,
        ),
    }
    auto_empire = {
        'slug': 'auto-empire',
        'defaults': dict(
            name='Empire — إمباير',
            industry='automotive',
            monthly_price=Decimal('2500.00'),
            quarterly_discount=10, semi_annual_discount=15, annual_discount=20,
            max_branches=5, max_users=10, max_treasuries=4,
            monthly_ai_designs_quota=0,
            monthly_diagnostics_scans_quota=70,
            monthly_diagnostics_bot_quota=70,
            features=[
                'كل مميزات Gold', '5 فروع · 10 مستخدمين · 4 خزائن',
                'AI Copilot', 'عقود أساطيل',
                'دعم فني أولوية',
                '70 فحص تشخيص/شهر', '70 سؤال بوت تشخيص/شهر',
                'إضافة موظف/فرع/خزينة: 125 ج.م/شهر',
            ],
            entitlements={
                'core_invoicing':           {'enabled': True},
                'core_inventory':           {'enabled': True},
                'core_treasury':            {'enabled': True},
                'workshop_repair_cards':    {'enabled': True, 'monthly_limit': 0},
                'workshop_dtc_ai':          {'enabled': True},
                'workshop_fleet_contracts': {'enabled': True},
                'b2b_marketplace':          {'enabled': True},
                'open_marketplace':         {'enabled': True},
                'reports_basic':            {'enabled': True},
                'reports_advanced':         {'enabled': True},
                'priority_support':         {'enabled': True},
            },
            is_active=True, sort_order=30,
        ),
    }

    # ── 3. Seed Printing plans ────────────────────────────────────────
    print_starter = {
        'slug': 'print-starter',
        'defaults': dict(
            name='Starter — ستارتر',
            industry='printing',
            monthly_price=Decimal('875.00'),
            quarterly_discount=10, semi_annual_discount=15, annual_discount=20,
            max_branches=1, max_users=1, max_treasuries=1,
            monthly_ai_designs_quota=50,
            monthly_diagnostics_scans_quota=0,
            monthly_diagnostics_bot_quota=0,
            features=[
                'طلبات طباعة', 'تسعير تلقائي',
                'إدارة الخزائن', 'فرع واحد · مستخدم واحد',
                '50 تصميم AI/شهر',
            ],
            entitlements={
                'core_invoicing':    {'enabled': True},
                'core_treasury':     {'enabled': True},
                'print_orders':      {'enabled': True, 'monthly_limit': 0},
                'print_auto_pricing':{'enabled': True},
                'reports_basic':     {'enabled': True},
            },
            is_active=True, sort_order=40,
        ),
    }
    print_pro = {
        'slug': 'print-pro',
        'defaults': dict(
            name='Pro — برو',
            industry='printing',
            monthly_price=Decimal('1250.00'),
            quarterly_discount=10, semi_annual_discount=15, annual_discount=20,
            max_branches=2, max_users=4, max_treasuries=2,
            monthly_ai_designs_quota=100,
            monthly_diagnostics_scans_quota=0,
            monthly_diagnostics_bot_quota=0,
            features=[
                'كل مميزات Starter', 'فرعين · 4 مستخدمين · خزينتين',
                'طلبات طباعة غير محدودة', 'سجل أعمال المصممين',
                'تقارير متقدمة', '100 تصميم AI/شهر',
            ],
            entitlements={
                'core_invoicing':        {'enabled': True},
                'core_treasury':         {'enabled': True},
                'print_orders':          {'enabled': True, 'monthly_limit': 0},
                'print_auto_pricing':    {'enabled': True},
                'print_designer_worklog':{'enabled': True},
                'reports_basic':         {'enabled': True},
                'reports_advanced':      {'enabled': True},
            },
            is_active=True, sort_order=50,
        ),
    }
    print_enterprise = {
        'slug': 'print-enterprise',
        'defaults': dict(
            name='Enterprise — إنتربرايز',
            industry='printing',
            monthly_price=Decimal('2000.00'),
            quarterly_discount=10, semi_annual_discount=15, annual_discount=20,
            max_branches=4, max_users=6, max_treasuries=6,
            monthly_ai_designs_quota=300,
            monthly_diagnostics_scans_quota=0,
            monthly_diagnostics_bot_quota=0,
            features=[
                'كل مميزات Pro', '4 فروع · 6 مستخدمين · 6 خزائن',
                'دعم فني أولوية', '300 تصميم AI/شهر',
                'إضافة موظف/فرع/خزينة: 125 ج.م/شهر',
            ],
            entitlements={
                'core_invoicing':        {'enabled': True},
                'core_treasury':         {'enabled': True},
                'print_orders':          {'enabled': True, 'monthly_limit': 0},
                'print_auto_pricing':    {'enabled': True},
                'print_designer_worklog':{'enabled': True},
                'reports_basic':         {'enabled': True},
                'reports_advanced':      {'enabled': True},
                'priority_support':      {'enabled': True},
            },
            is_active=True, sort_order=60,
        ),
    }

    for spec in (auto_silver, auto_gold, auto_empire,
                 print_starter, print_pro, print_enterprise):
        Plan.objects.update_or_create(slug=spec['slug'], defaults=spec['defaults'])

    # ── 4. Seed Design packages — Customer (HD, personal) ─────────────
    cust_packs = [
        dict(slug='c10', target_audience='customer', name_ar='باقة 10 تصاميم',
             designs_count=10, designer_designs_count=0, price_egp=Decimal('60.00'),
             allows_source_files=False, allows_commercial_use=False,
             allows_watermark=True, free_regenerations_per_design=1,
             resolution_max='2048x2048', quality_level='hd',
             icon_emoji='🎯', accent_color='#3b82f6', sort_order=10,
             is_featured=False, is_active=True),
        dict(slug='c20', target_audience='customer', name_ar='باقة 20 تصميم',
             designs_count=20, designer_designs_count=0, price_egp=Decimal('115.00'),
             allows_source_files=False, allows_commercial_use=False,
             allows_watermark=False, free_regenerations_per_design=2,
             resolution_max='2048x2048', quality_level='hd',
             icon_emoji='⭐', accent_color='#8b5cf6', sort_order=20,
             is_featured=True, badge_text='الأكثر مبيعاً', is_active=True),
        dict(slug='c50', target_audience='customer', name_ar='باقة 50 تصميم',
             designs_count=50, designer_designs_count=0, price_egp=Decimal('275.00'),
             allows_source_files=False, allows_commercial_use=False,
             allows_watermark=False, free_regenerations_per_design=2,
             resolution_max='2048x2048', quality_level='hd',
             icon_emoji='💎', accent_color='#ec4899', sort_order=30,
             is_featured=False, is_active=True),
    ]
    # ── 5. Seed Design packages — Designer (4K + source, commercial) ──
    des_packs = [
        dict(slug='d20', target_audience='designer', name_ar='باقة 20 تصميم — للمصممين',
             designs_count=20, designer_designs_count=20, price_egp=Decimal('149.00'),
             allows_source_files=False, allows_commercial_use=True,
             allows_watermark=False, free_regenerations_per_design=2,
             resolution_max='2048x2048', quality_level='hd',
             icon_emoji='🏢', accent_color='#0ea5e9', sort_order=40,
             is_featured=False, is_active=True),
        dict(slug='d50', target_audience='designer', name_ar='باقة 50 تصميم — للمصممين',
             designs_count=50, designer_designs_count=50, price_egp=Decimal('349.00'),
             allows_source_files=True, allows_commercial_use=True,
             allows_watermark=False, free_regenerations_per_design=3,
             resolution_max='4096x4096', quality_level='ultra',
             icon_emoji='⭐', accent_color='#22c55e', sort_order=50,
             is_featured=True, badge_text='الأفضل قيمة',
             is_active=True),
        dict(slug='d100', target_audience='designer', name_ar='باقة 100 تصميم — للمصممين',
             designs_count=100, designer_designs_count=100, price_egp=Decimal('599.00'),
             allows_source_files=True, allows_commercial_use=True,
             allows_watermark=False, free_regenerations_per_design=5,
             resolution_max='4096x4096', quality_level='ultra',
             icon_emoji='💎', accent_color='#f59e0b', sort_order=60,
             is_featured=False, is_active=True),
    ]
    for spec in cust_packs + des_packs:
        slug = spec.pop('slug')
        # The save() override on DesignPackage computes price_per_design, but
        # migrations use the historical model without that override, so we
        # compute it explicitly here.
        if spec['designs_count'] > 0:
            spec['price_per_design'] = (
                spec['price_egp'] / Decimal(str(spec['designs_count']))
            ).quantize(Decimal('0.01'))
        DesignPkg.objects.update_or_create(slug=slug, defaults=spec)

    # ── 6. Seed Diagnostics Top-Up pack ───────────────────────────────
    TopUp.objects.update_or_create(
        slug='diag-30',
        defaults=dict(
            name='شحن 30 استخدام تشخيص',
            price_egp=Decimal('150.00'),
            uses_granted=30,
            is_active=True,
            sort_order=10,
        ),
    )


def unseed(apps, schema_editor):
    # Reverse: just deactivate the new launch rows so we can re-run cleanly.
    Plan = apps.get_model('clients', 'Plan')
    DesignPkg = apps.get_model('clients', 'DesignPackage')
    TopUp = apps.get_model('clients', 'DiagnosticsTopUpPack')

    Plan.objects.filter(slug__in=[
        'auto-silver', 'auto-gold', 'auto-empire',
        'print-starter', 'print-pro', 'print-enterprise',
    ]).update(is_active=False)
    DesignPkg.objects.filter(slug__in=[
        'c10', 'c20', 'c50', 'd20', 'd50', 'd100',
    ]).update(is_active=False)
    TopUp.objects.filter(slug='diag-30').update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ('clients', '0063_add_diagnostics_quotas_and_topup'),
    ]
    operations = [
        migrations.RunPython(seed, unseed),
    ]
