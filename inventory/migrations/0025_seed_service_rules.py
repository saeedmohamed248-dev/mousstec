"""
Seed sensible default service-reminder rules so a fresh tenant has the
predictive-nudge engine working out of the box. The admin can prune or
tweak any of these (or add their own) from the standard admin UI.

Intervals are conservative — workshops can shorten them per fleet mix.
"""
from django.db import migrations


_DEFAULTS = [
    # (name, category, km, months, severity, template)
    ("تغيير زيت المحرك", 'engine_oil', 10000, 6, 'high',
     "مرحباً {customer} 👋\n\n"
     "حسب آخر زيارة، حان موعد *{rule}* لسيارة *{vehicle}*.\n"
     "نسعد بحجز موعد لك في {workshop}."),

    ("فحص الفرامل", 'brake_pads', 20000, 12, 'high',
     "مرحباً {customer} 👋\n\n"
     "صيانة الفرامل لسيارة *{vehicle}* مستحقة. نوصي بالفحص في أقرب وقت."),

    ("تغيير البوجيهات", 'spark_plugs', 40000, 24, 'medium', ""),

    ("تغيير زيت الفتيس", 'transmission_oil', 60000, 36, 'medium', ""),

    ("فحص مياه التبريد", 'coolant', 30000, 24, 'medium', ""),

    ("تغيير فلتر الهواء", 'air_filter', 15000, 12, 'low', ""),

    ("تغيير فلتر مكيف الكابينة", 'cabin_filter', 15000, 12, 'low', ""),

    ("فحص البطارية", 'battery', None, 18, 'medium', ""),

    ("استبدال المساحات", 'wipers', None, 12, 'low', ""),

    ("فحص سير الكاتينة / التيمنج", 'timing_belt', 100000, 60, 'high',
     "تحذير دوري: قطع *{rule}* قبل التلف يحمي محرك *{vehicle}* من ضرر بالغ. "
     "تواصل مع {workshop} لحجز فحص."),
]


def seed_rules(apps, schema_editor):
    """Idempotent: only create rules whose name doesn't already exist."""
    ServiceReminderRule = apps.get_model('inventory', 'ServiceReminderRule')
    existing = set(ServiceReminderRule.objects.values_list('name', flat=True))
    new_rows = [
        ServiceReminderRule(
            name=name, category=cat,
            interval_km=km, interval_months=months,
            severity=sev, whatsapp_template=tpl,
            applies_to_brands=[], is_active=True,
        )
        for (name, cat, km, months, sev, tpl) in _DEFAULTS
        if name not in existing
    ]
    if new_rows:
        ServiceReminderRule.objects.bulk_create(new_rows)


def unseed_rules(apps, schema_editor):
    """Reverse: remove only the rules whose names match our seed set."""
    ServiceReminderRule = apps.get_model('inventory', 'ServiceReminderRule')
    names = [row[0] for row in _DEFAULTS]
    ServiceReminderRule.objects.filter(name__in=names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0024_service_reminder_rules'),
    ]

    operations = [
        migrations.RunPython(seed_rules, reverse_code=unseed_rules),
    ]
