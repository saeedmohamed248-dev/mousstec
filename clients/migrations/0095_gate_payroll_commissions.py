"""
🔒 المرتبات والعمولات لـ Gold/Empire و Pro/Enterprise — للمشتركين الجداد بس (grandfathering)

المرتبات (مسيرات + كشوف مرتبات) وتقرير العمولات كانوا مفتوحين لكل الباقات حتى Silver/Starter —
وده موديول بتبيعه أودو ودفترة لوحده. الحضور والانصراف والإجازات والسلف فاضلين لكل الباقات.

- الكتالوج: ميزة hr_payroll.
- الباقات: Gold / Empire / Pro / Enterprise (وسطر «المرتبات والعمولات» في المميزات).
- الاشتراكات الموجودة دلوقتي بتحتفظ بيها لحد أول دفع/تجديد بعد كده (زي 0087).
"""
from django.db import migrations
from django.utils import timezone

PLANS = ('auto-gold', 'auto-empire', 'print-pro', 'print-enterprise')
LINE = 'المرتبات والعمولات'


def _on_public_schema(schema_editor):
    """🛡️ مهاجرة بيانات المنصة — تشتغل مرة على السكيمة العامة بس (نفس حارس 0087)."""
    if schema_editor is None:   # نداء مباشر من اختبار
        return True
    from django_tenants.utils import get_public_schema_name
    return getattr(schema_editor.connection, 'schema_name', get_public_schema_name()) == get_public_schema_name()


def forward(apps, schema_editor):
    if not _on_public_schema(schema_editor):
        return
    Feature = apps.get_model('clients', 'Feature')
    Plan = apps.get_model('clients', 'Plan')
    PlanRevision = apps.get_model('clients', 'PlanRevision')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')

    Feature.objects.update_or_create(code='hr_payroll', defaults=dict(
        name_ar='المرتبات والعمولات', name_en='Payroll & commissions', category='core',
        description='مسيرات المرتبات وكشوف المرتب للموظفين + تقرير العمولات',
        is_quantitative=False, is_active=True, sort_order=40))

    for plan in Plan.objects.filter(slug__in=PLANS):
        ents = dict(plan.entitlements or {})
        features = list(plan.features or [])
        if ents.get('hr_payroll') == {'enabled': True} and LINE in features:
            continue
        ents['hr_payroll'] = {'enabled': True}
        if LINE not in features:
            features.append(LINE)
        plan.entitlements, plan.features = ents, features
        plan.save(update_fields=['entitlements', 'features'])
        # الـ signal بتاع الـ revision مابيشتغلش على موديل المهاجرة — نسجّله بإيدينا
        PlanRevision.objects.create(plan=plan, monthly_price=plan.monthly_price, entitlements=ents,
                                    change_reason='0095: المرتبات والعمولات لـ Gold/Pro وأعلى')

    now = timezone.now()
    for sub in TenantSubscription.objects.select_related('plan').exclude(plan=None):
        locked = sub.locked_entitlements if (sub.locked_at and isinstance(sub.locked_entitlements, dict)
                                             and sub.locked_entitlements) else None
        ents = dict(locked if locked is not None else (sub.plan.entitlements or {}))
        if ents.get('hr_payroll', {}).get('enabled'):
            continue
        ents['hr_payroll'] = {'enabled': True}       # كانت مفتوحة له — تفضل لحد التجديد
        sub.locked_entitlements = ents
        fields = ['locked_entitlements']
        if locked is None:
            sub.locked_at = now
            sub.locked_monthly_price = sub.locked_monthly_price or sub.plan.monthly_price
            fields += ['locked_at', 'locked_monthly_price']
        sub.save(update_fields=fields)


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0094_coupons_and_referrals'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
