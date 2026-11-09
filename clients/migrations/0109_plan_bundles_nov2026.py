"""
💎 الباقات الجديدة (نوفمبر ٢٠٢٦ — قرار صاحب المنصة)
===================================================
* السوق (B2B وسوق قطع الغيار) مفتوح لكل الباقات ومن غير عمولة في البداية (العمولة في الإعدادات).
* الرد الآلي واستوديو التسويق بقوا جوه الباقات بدل ما يتباعوا لوحدهم — بعدد شهري بيكبر مع الباقة:

  | الميزة                       | لايت | سيلفر/ستارتر | جولد/برو | إمباير/إنتربرايز |
  |------------------------------|------|-------------|----------|-----------------|
  | ردود الرد الآلي/شهر          | 100  | 1,500       | 4,000    | 10,000          |
  | ماسنجر/إنستجرام/الكومنتات     | ❌   | ❌          | ✅       | ✅              |
  | بوستات بالذكاء الاصطناعي/شهر  | 4    | 20          | 60       | من غير حد       |
  | إعلانات ممولة بتتعلّم         | ❌   | ❌          | ✅       | ✅              |
  | المستشار الذكي (أسئلة/شهر)    | 10   | زي ما هو    | زي ما هو | زي ما هو        |
  | كشف أعطال (عربية/شهر — سيارات)| 20   | 60          | 200      | 600             |
  | تحليل كود العطل (DTC) بالذكاء  | ✅   | ✅          | ✅       | ✅              |
  | أسئلة بوت التشخيص/شهر         | 5    | زي ما هو    | زي ما هو | زي ما هو        |
  | تصاميم AI/شهر (مطابع)         | 10   | 50          | 100      | 300             |

* اللي دافعين الإضافات لوحدها (رد آلي/تسويق) فاضلين على اشتراكهم من غير حد لحد ما يخلص.
* المشتركين المقفولين على لقطة مميزات (locked_entitlements) بياخدوا المميزات الجديدة دي كمان —
  كلها إضافة مش سحب، فمحدش بيخسر حاجة.
"""
from django.db import migrations

ON = {'enabled': True}
OFF = {'enabled': False}


def lim(n):
    return {'enabled': True, 'monthly_limit': n}


NEW_FEATURES = (
    # (code, name_ar, name_en, category, is_quantitative, unit_label_ar, sort_order)
    ('ai_auto_reply', 'الرد الآلي الذكي على واتساب', 'AI auto-reply on WhatsApp', 'integrations', True, 'رد', 60),
    ('ai_auto_reply_social', 'الرد الآلي على ماسنجر وإنستجرام والكومنتات',
     'Auto-reply on Messenger, Instagram & comments', 'integrations', False, '', 61),
    ('social_studio', 'استوديو التسويق (بوستات بالذكاء الاصطناعي)', 'Marketing studio (AI posts)',
     'marketplace', True, 'بوست', 62),
    ('ads_autopilot', 'إعلانات ممولة بتتعلّم وتتحسّن لوحدها', 'Self-optimizing paid ads', 'marketplace', False, '', 63),
)

TIERS = {
    1: ('auto-lite', 'print-lite'),
    2: ('auto-silver', 'print-starter'),
    3: ('auto-gold', 'print-pro'),
    4: ('auto-empire', 'print-enterprise'),
}
TIER_ENTS = {
    1: {'ai_auto_reply': lim(100), 'ai_auto_reply_social': OFF, 'social_studio': lim(4), 'ads_autopilot': OFF},
    2: {'ai_auto_reply': lim(1500), 'ai_auto_reply_social': OFF, 'social_studio': lim(20), 'ads_autopilot': OFF},
    3: {'ai_auto_reply': lim(4000), 'ai_auto_reply_social': ON, 'social_studio': lim(60), 'ads_autopilot': ON},
    4: {'ai_auto_reply': lim(10000), 'ai_auto_reply_social': ON, 'social_studio': ON, 'ads_autopilot': ON},
}
ALL_PLANS = {'b2b_marketplace': ON, 'open_marketplace': ON}
DIAG_VEHICLES = {'auto-lite': 20, 'auto-silver': 60, 'auto-gold': 200, 'auto-empire': 600}
DIAG_BOT = {'auto-lite': 5}              # الباقي زي ما هو (20/40/70)
# كشف الأعطال في كل الباقات: تحليل كود العطل بيتخزّن ٣٠ يوم لكل كود فتكلفته شبه صفر
AUTO_ALL = {'workshop_dtc_ai': ON}
AI_DESIGNS = {'print-lite': 10}          # الباقي زي ما هو (50/100/300)

FEATURES = {
    'auto-lite': [
        'الكاشير وفواتير البيع والشراء والمرتجعات', '300 فاتورة بيع/شهر',
        'مخزون بأصناف غير محدودة + الباركود والجرد', 'العملاء والآجل وحد الائتمان وكشوف الحساب',
        'الموردين والمشتريات والمصروفات', 'السوق (B2B وقطع الغيار) مجاناً',
        'كشف أعطال لكل الماركات: 20 عربية/شهر', 'رد آلي على واتساب: 100 رد/شهر',
        '4 بوستات بالذكاء الاصطناعي/شهر', 'المستشار الذكي: 10 أسئلة/شهر',
        'تجهيز الفاتورة الإلكترونية (ETA)',
    ],
    'auto-silver': [
        'كل مميزات Lite', 'فواتير بيع غير محدودة', '150 كرت صيانة/شهر (الورشة)',
        'أوامر الشراء من النواقص', 'كشف أعطال: 60 عربية/شهر', 'رد آلي على واتساب: 1,500 رد/شهر',
        'استوديو التسويق: 20 بوست/شهر بالنشر المجدول', 'المستشار الذكي: 50 سؤال/شهر',
    ],
    'auto-gold': [
        'كل مميزات Silver', 'فرعين · 4 مستخدمين · خزينتين', 'كروت صيانة غير محدودة',
        'تحويلات المخزون بين الفروع', 'البيانات الحية من جهاز OBD', 'كشف أعطال: 200 عربية/شهر',
        'رد آلي: 4,000 رد/شهر + ماسنجر وإنستجرام والكومنتات',
        'استوديو التسويق: 60 بوست + إعلانات بتتعلّم لوحدها', 'تقارير أرباح متقدمة',
        'المرتبات والعمولات', 'المستشار الذكي: 300 سؤال/شهر', 'نركّبلك الواتساب وندخّل بضاعتك ببلاش',
    ],
    'auto-empire': [
        'كل مميزات Gold', '5 فروع · 10 مستخدمين · 4 خزائن', 'كشف أعطال: 600 عربية/شهر',
        'رد آلي: 10,000 رد/شهر + رقم لكل فرع', 'بوستات من غير حد + تحسين الإعلانات تلقائياً وتقرير أسبوعي',
        'المستشار الذكي بدون حد', 'عقود الأساطيل', 'دعم فني أولوية ومتابعة شهرية',
    ],
    'print-lite': [
        'فاتورة سريعة لطلبات الطباعة', '100 طلب طباعة/شهر', 'عروض أسعار وبروفات للعميل بلينك',
        'تسعير بتكلفة الماكينة والحبر والخامات', 'العملاء والآجل والتحصيل وكشوف الحساب',
        'المشتريات والمصروفات', 'السوق مجاناً', '10 تصاميم AI/شهر', 'رد آلي على واتساب: 100 رد/شهر',
        '4 بوستات بالذكاء الاصطناعي/شهر', 'المستشار الذكي: 10 أسئلة/شهر', 'تجهيز الفاتورة الإلكترونية (ETA)',
    ],
    'print-starter': [
        'كل مميزات Lite', 'طلبات طباعة غير محدودة', 'لوحة الإنتاج (تصميم ← بروفة ← طباعة ← تشطيب)',
        '50 تصميم AI/شهر', 'رد آلي على واتساب: 1,500 رد/شهر', 'استوديو التسويق: 20 بوست/شهر',
        'المستشار الذكي: 100 سؤال/شهر',
    ],
    'print-pro': [
        'كل مميزات Starter', 'فرعين · 4 مستخدمين · خزينتين', 'سجل أعمال المصممين', '100 تصميم AI/شهر',
        'رد آلي: 4,000 رد/شهر + ماسنجر وإنستجرام والكومنتات',
        'استوديو التسويق: 60 بوست + إعلانات بتتعلّم لوحدها', 'تقارير متقدمة', 'المرتبات والعمولات',
        'المستشار الذكي: 300 سؤال/شهر', 'نركّبلك الواتساب وندخّل بياناتك ببلاش',
    ],
    'print-enterprise': [
        'كل مميزات Pro', '4 فروع · 6 مستخدمين · 6 خزائن', '300 تصميم AI/شهر',
        'رد آلي: 10,000 رد/شهر + رقم لكل فرع', 'بوستات من غير حد + تحسين الإعلانات تلقائياً',
        'المستشار الذكي بدون حد', 'دعم فني أولوية ومتابعة شهرية',
    ],
}


def _on_public_schema(schema_editor):
    """🛡️ مهاجرة بيانات المنصة — مرة على السكيمة العامة بس (نفس حارس 0087/0104)."""
    if schema_editor is None:
        return True
    from django_tenants.utils import get_public_schema_name
    return getattr(schema_editor.connection, 'schema_name', get_public_schema_name()) == get_public_schema_name()


def plan_additions(slug):
    tier = next((t for t, slugs in TIERS.items() if slug in slugs), None)
    add = dict(ALL_PLANS)
    if tier:
        add.update(TIER_ENTS[tier])
    if slug.startswith('auto-'):
        add.update(AUTO_ALL)
    if slug in ('auto-lite', 'print-lite'):
        add['ai_copilot'] = lim(10)
    return add


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

    for plan in Plan.objects.all():
        add = plan_additions(plan.slug)
        ents = dict(plan.entitlements or {})
        for code, cfg in add.items():
            if code == 'ai_copilot' and (ents.get(code) or {}).get('enabled'):
                continue                 # المستشار في الباقات الأكبر زي ما هو
            ents[code] = dict(cfg)
        plan.entitlements = ents
        fields = ['entitlements']
        if plan.slug in FEATURES:
            plan.features = FEATURES[plan.slug]
            fields.append('features')
        if plan.slug in DIAG_VEHICLES:
            plan.monthly_diagnostics_scans_quota = DIAG_VEHICLES[plan.slug]
            fields.append('monthly_diagnostics_scans_quota')
        if plan.slug in DIAG_BOT:
            plan.monthly_diagnostics_bot_quota = DIAG_BOT[plan.slug]
            fields.append('monthly_diagnostics_bot_quota')
        if plan.slug in AI_DESIGNS:
            plan.monthly_ai_designs_quota = AI_DESIGNS[plan.slug]
            fields.append('monthly_ai_designs_quota')
        plan.save(update_fields=fields)
        PlanRevision.objects.create(plan=plan, monthly_price=plan.monthly_price, entitlements=ents,
                                    change_reason='0109: السوق مجاناً + الرد الآلي والتسويق جوه الباقات')

        # المقفولين على لقطة: المميزات الجديدة بتتضاف (ومفيش حاجة بتتسحب)
        for sub in TenantSubscription.objects.filter(plan=plan).exclude(locked_at=None):
            locked = dict(sub.locked_entitlements or {})
            if not locked:
                continue
            changed = False
            for code, cfg in add.items():
                cur = locked.get(code) or {}
                if code not in locked or (cfg.get('enabled') and not cur.get('enabled')):
                    locked[code] = dict(cfg)
                    changed = True
            if changed:
                sub.locked_entitlements = locked
                sub.save(update_fields=['locked_entitlements'])


class Migration(migrations.Migration):

    dependencies = [
        ("clients", "0108_signup_funnel"),
    ]

    operations = [
        migrations.RunPython(apply, migrations.RunPython.noop),
    ]
