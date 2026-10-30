"""
🔒 قفل مزايا الباقات الأعلى — للمشتركين الجداد بس (grandfathering)

باقة Silver كانت واخدة ببلاش مزايا بنبيعها لـ Gold/Empire، لأن ماكانش فيه كود بيقفلها:
تقارير الأرباح المتقدمة (reports_advanced) وتحليل أعطال DTC (workshop_dtc_ai) لـ Gold+،
والمستشار الذكي AI Copilot لـ Empire، وحد 150 كرت صيانة/شهر و500 صنف لـ Silver.

1. الكتالوج: ميزتين جداد — ai_copilot و inventory_items (حد عدد الأصناف الكلي، مش شهري).
2. الباقات: Silver فيها حد 500 صنف، Empire فيها AI Copilot، وباقات الطباعة كمان
   (المستشار الذكي كان شغّال للمطابع ومش بيتباع كميزة منفصلة هناك).
3. الاشتراكات الموجودة دلوقتي بتحتفظ بالمزايا اللي كانت مفتوحة لها لحد أول دفع/تجديد
   (mark_paid بيعمل snapshot جديد من الباقة) — العميل القديم مايتفاجئش.
"""
from django.db import migrations
from django.utils import timezone

# المزايا اللي كانت مفتوحة للكل واتقفلت هنا — الاشتراك الحالي بياخدها لحد التجديد
GRANDFATHERED = ('reports_advanced', 'workshop_dtc_ai', 'ai_copilot')


def forward(apps, schema_editor):
    Feature = apps.get_model('clients', 'Feature')
    Plan = apps.get_model('clients', 'Plan')
    PlanRevision = apps.get_model('clients', 'PlanRevision')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')

    Feature.objects.update_or_create(code='ai_copilot', defaults=dict(
        name_ar='المستشار الذكي (AI Copilot)', name_en='AI Copilot', category='analytics',
        description='شات المستشار الذكي على بيانات الشركة + صفحة المستشار في التقارير',
        is_quantitative=False, is_active=True, sort_order=30))
    Feature.objects.update_or_create(code='inventory_items', defaults=dict(
        name_ar='حد أصناف المخزون', name_en='Inventory items limit', category='core',
        description='أقصى عدد أصناف نشطة (الحد كلي مش شهري — monthly_limit = عدد الأصناف)',
        is_quantitative=True, unit_label_ar='صنف', is_active=True, sort_order=25))

    plan_changes = {
        'auto-silver': {'inventory_items': {'enabled': True, 'monthly_limit': 500}},
        'auto-empire': {'ai_copilot': {'enabled': True}},
        'print-starter': {'ai_copilot': {'enabled': True}},
        'print-pro': {'ai_copilot': {'enabled': True}},
        'print-enterprise': {'ai_copilot': {'enabled': True}},
    }
    for slug, extra in plan_changes.items():
        plan = Plan.objects.filter(slug=slug).first()
        if plan is None:
            continue
        ents = dict(plan.entitlements or {})
        if all(ents.get(k) == v for k, v in extra.items()):
            continue
        ents.update(extra)
        plan.entitlements = ents
        plan.save(update_fields=['entitlements'])
        # الـ signal بتاع الـ revision مابيشتغلش على موديل المهاجرة — نسجّله بإيدينا
        PlanRevision.objects.create(plan=plan, monthly_price=plan.monthly_price, entitlements=ents,
                                    change_reason='0087: قفل مزايا الباقات الأعلى')

    now = timezone.now()
    for sub in TenantSubscription.objects.select_related('plan').exclude(plan=None):
        locked = sub.locked_entitlements if (sub.locked_at and isinstance(sub.locked_entitlements, dict)
                                             and sub.locked_entitlements) else None
        ents = dict(locked if locked is not None else (sub.plan.entitlements or {}))
        for code in GRANDFATHERED:
            ents[code] = {'enabled': True}
        cards = ents.get('workshop_repair_cards')
        if isinstance(cards, dict) and (cards.get('monthly_limit') or 0) > 0:
            ents['workshop_repair_cards'] = {**cards, 'monthly_limit': 0}
        sub.locked_entitlements = ents
        fields = ['locked_entitlements']
        if locked is None:
            sub.locked_at = now
            sub.locked_monthly_price = sub.locked_monthly_price or sub.plan.monthly_price
            fields += ['locked_at', 'locked_monthly_price']
        sub.save(update_fields=fields)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0086_deactivate_premium_diagnostics_plan'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
