"""
💎 باقات «لايت» الجديدة + مراجعة مميزات كل الباقات (نوفمبر 2026)
======================================================================
* باقة Lite جديدة لكل قطاع — أرخص من مدخل السوق المصري (البرامج السحابية بتبدأ ~300–450 ج):
      سيارات/قطع غيار  auto-lite   249 ج/شهر
      مطابع            print-lite  299 ج/شهر
  مستخدم واحد · فرع واحد · خزنة واحدة، والأساسيات اللي بتربط العميل بالسيستم كاملة
  (فواتير، مخزون، آجل، خزينة، فاتورة إلكترونية)، والحد على **حجم الشغل** مش على المميزات:
  300 فاتورة بيع/شهر (سيارات) و100 طلب طباعة/شهر (مطابع). من غير ذكاء اصطناعي (بيتكلّف
  فلوس على كل سؤال) ومن غير الورشة/أوامر الشراء/لوحة الإنتاج (دي سبب الترقية).
* مميزات جديدة في الكتالوج (كانت موجودة في السيستم ومش معروضة): الفاتورة الإلكترونية،
  الجرد، حد الائتمان، أوامر الشراء، تحويلات الفروع، لوحة الإنتاج، عروض الأسعار والبروفات،
  وحد فواتير البيع الشهري.
* الباقات الحالية أسعارها زي ما هي — اتضافلها المميزات الجديدة، وسيلفر اتشال منها حد
  الـ 500 صنف (المعيار العالمي: الحد على العمليات/المستخدمين مش على الأصناف).
المشتركين الحاليين: لقطة مميزاتهم (locked_entitlements) بتتزوّد بالمميزات الجديدة اللي باقتهم
خدتها هنا — أوامر الشراء ولوحة الإنتاج وتحويلات الفروع كانت مفتوحة للكل قبل المهاجرة دي،
فمحدش بيخسر حاجة كانت عنده (نفس فكرة 0087). القاعدة العامة زي ما هي: ميزة تتضاف لباقة بعد
كده مش بتوصل للمشترك المقفول غير عند التجديد.
"""
from decimal import Decimal

from django.db import migrations

ON = {'enabled': True}

NEW_FEATURES = (
    # (code, name_ar, name_en, category, is_quantitative, unit_label_ar, sort_order)
    ('sales_invoices', 'فواتير البيع في الشهر', 'Sales invoices per month', 'core', True, 'فاتورة', 5),
    ('e_invoice_eta', 'الفاتورة الإلكترونية (مصلحة الضرائب)', 'E-invoice (Egyptian Tax Authority)', 'core', False, '', 6),
    ('stock_count', 'الجرد الفعلي وفروقات الجرد', 'Stock count & variances', 'core', False, '', 7),
    ('customer_credit_limit', 'حد الائتمان للعملاء', 'Customer credit limits', 'core', False, '', 8),
    ('purchase_orders', 'أوامر الشراء من النواقص', 'Purchase orders from shortages', 'core', False, '', 9),
    ('stock_transfers', 'تحويلات المخزون بين الفروع', 'Stock transfers between branches', 'core', False, '', 10),
    ('print_production_board', 'لوحة الإنتاج (تصميم ← بروفة ← طباعة ← تشطيب)',
     'Production board (design → proof → print → finishing)', 'printing', False, '', 40),
    ('print_quotes_proofs', 'عروض الأسعار وبروفات العميل بلينك', 'Quotes & customer proof links', 'printing', False, '', 41),
)

AUTO_CORE = {
    'core_invoicing': ON, 'core_inventory': ON, 'core_treasury': ON, 'reports_basic': ON,
    'open_marketplace': ON, 'e_invoice_eta': ON, 'stock_count': ON, 'customer_credit_limit': ON,
}
PRINT_CORE = {
    'core_invoicing': ON, 'core_treasury': ON, 'reports_basic': ON, 'print_auto_pricing': ON,
    'print_quotes_proofs': ON, 'e_invoice_eta': ON, 'customer_credit_limit': ON,
}

NEW_PLANS = {
    'auto-lite': dict(
        name='Lite — لايت', name_en='Lite', industry='automotive',
        monthly_price=Decimal('249.00'), monthly_price_aed=Decimal('19.00'),
        max_branches=1, max_users=1, max_treasuries=1, sort_order=5,
        monthly_ai_designs_quota=0, monthly_diagnostics_scans_quota=0, monthly_diagnostics_bot_quota=0,
        entitlements={**AUTO_CORE, 'sales_invoices': {'enabled': True, 'monthly_limit': 300}},
        features=[
            'الكاشير وفواتير البيع والشراء والمرتجعات',
            '300 فاتورة بيع/شهر',
            'مخزون بأصناف غير محدودة + الباركود والجرد',
            'العملاء والآجل وحد الائتمان وكشوف الحساب',
            'الموردين والمشتريات والمصروفات',
            'تجهيز الفاتورة الإلكترونية (ETA)',
            'تقارير المبيعات والأرباح والنواقص',
            'السوق المفتوح',
        ],
    ),
    'print-lite': dict(
        name='Lite — لايت', name_en='Lite', industry='printing',
        monthly_price=Decimal('299.00'), monthly_price_aed=Decimal('23.00'),
        max_branches=1, max_users=1, max_treasuries=1, sort_order=35,
        monthly_ai_designs_quota=0, monthly_diagnostics_scans_quota=0, monthly_diagnostics_bot_quota=0,
        entitlements={**PRINT_CORE, 'print_orders': {'enabled': True, 'monthly_limit': 100}},
        features=[
            'فاتورة سريعة لطلبات الطباعة',
            '100 طلب طباعة/شهر',
            'عروض أسعار وبروفات للعميل بلينك',
            'تسعير بتكلفة الماكينة والحبر والخامات',
            'العملاء والآجل والتحصيل وكشوف الحساب',
            'المشتريات والمصروفات',
            'تجهيز الفاتورة الإلكترونية (ETA)',
            'تقارير المبيعات والأرباح',
        ],
    ),
}

# الباقات الحالية: مميزات بتتضاف (الأسعار والحدود التانية زي ما هي) + نص كارت الباقة
UPGRADES = {
    'auto-silver': (
        {**AUTO_CORE, 'sales_invoices': ON, 'purchase_orders': ON, 'inventory_items': ON},
        ['كل مميزات Lite', 'فواتير بيع غير محدودة', '150 كرت صيانة/شهر (الورشة)',
         'أوامر الشراء من النواقص', 'خطط الفحص الموجّه والبحث الذكي عن القطع',
         '10 فحص تشخيص/شهر', '20 سؤال بوت تشخيص/شهر', 'المستشار الذكي: 50 سؤال/شهر'],
    ),
    'auto-gold': (
        {**AUTO_CORE, 'sales_invoices': ON, 'purchase_orders': ON, 'stock_transfers': ON},
        ['كل مميزات Silver', 'كروت صيانة غير محدودة',
         'تحويلات المخزون بين الفروع', 'سوق B2B', 'تحليل أعطال DTC والبيانات الحية',
         'تقارير أرباح متقدمة', 'المرتبات والعمولات', '40 فحص تشخيص/شهر',
         '40 سؤال بوت تشخيص/شهر', 'المستشار الذكي: 300 سؤال/شهر'],
    ),
    'auto-empire': (
        {**AUTO_CORE, 'sales_invoices': ON, 'purchase_orders': ON, 'stock_transfers': ON},
        ['كل مميزات Gold', 'المستشار الذكي بدون حد',
         'عقود الأساطيل', 'فحوصات API خارجية', 'دعم فني أولوية',
         '70 فحص تشخيص/شهر', '70 سؤال بوت تشخيص/شهر'],
    ),
    'print-starter': (
        {**PRINT_CORE, 'print_production_board': ON},
        ['كل مميزات Lite', 'طلبات طباعة غير محدودة', 'لوحة الإنتاج (تصميم ← بروفة ← طباعة ← تشطيب)', 'المستشار الذكي: 100 سؤال/شهر'],
    ),
    'print-pro': (
        {**PRINT_CORE, 'print_production_board': ON},
        ['كل مميزات Starter', 'سجل أعمال المصممين',
         'تقارير متقدمة', 'المرتبات والعمولات', 'المستشار الذكي: 300 سؤال/شهر'],
    ),
    'print-enterprise': (
        {**PRINT_CORE, 'print_production_board': ON},
        ['كل مميزات Pro', 'المستشار الذكي بدون حد', 'دعم فني أولوية'],
    ),
}


def _on_public_schema(schema_editor):
    """🛡️ مهاجرة بيانات المنصة — تشتغل مرة على السكيمة العامة بس (نفس حارس 0087)."""
    if schema_editor is None:
        return True
    from django_tenants.utils import get_public_schema_name
    return getattr(schema_editor.connection, 'schema_name', get_public_schema_name()) == get_public_schema_name()


def apply(apps, schema_editor):
    if not _on_public_schema(schema_editor):
        return
    Feature = apps.get_model('clients', 'Feature')
    Plan = apps.get_model('clients', 'Plan')
    PlanRevision = apps.get_model('clients', 'PlanRevision')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')

    for code, ar, en, cat, quant, unit, order in NEW_FEATURES:
        Feature.objects.update_or_create(code=code, defaults=dict(
            name_ar=ar, name_en=en, category=cat, is_quantitative=quant,
            unit_label_ar=unit, sort_order=order, is_active=True))

    for slug, data in NEW_PLANS.items():
        data = dict(data)
        plan, created = Plan.objects.get_or_create(slug=slug, defaults={**data, 'is_active': True})
        if created:
            PlanRevision.objects.create(plan=plan, monthly_price=plan.monthly_price,
                                        entitlements=plan.entitlements,
                                        change_reason='0104: باقة Lite جديدة')

    for slug, (add, features) in UPGRADES.items():
        plan = Plan.objects.filter(slug=slug).first()
        if plan is None:
            continue
        ents = dict(plan.entitlements or {})
        for code, cfg in add.items():
            if code == 'inventory_items':
                ents[code] = dict(cfg)         # سيلفر: من غير حد الأصناف
            elif code not in ents or not (ents[code] or {}).get('enabled'):
                ents[code] = dict(cfg)
        plan.entitlements, plan.features = ents, features
        plan.save(update_fields=['entitlements', 'features'])
        PlanRevision.objects.create(plan=plan, monthly_price=plan.monthly_price, entitlements=ents,
                                    change_reason='0104: مراجعة مميزات الباقات + باقات Lite')

        # 🛡️ المشتركين المقفولين على الباقة دي: المميزات الجديدة بتتضاف للقطة (من غير ما نلمس
        #    أي حد أو ميزة كانت عندهم)
        for sub in TenantSubscription.objects.filter(plan=plan).exclude(locked_at=None):
            locked = dict(sub.locked_entitlements or {})
            if not locked:
                continue
            missing = {c: dict(cfg) for c, cfg in add.items() if c != 'inventory_items' and c not in locked}
            if missing:
                sub.locked_entitlements = {**locked, **missing}
                sub.save(update_fields=['locked_entitlements'])


class Migration(migrations.Migration):

    dependencies = [
        ("clients", "0103_blocked_ip"),
    ]

    operations = [
        migrations.RunPython(apply, migrations.RunPython.noop),
    ]
