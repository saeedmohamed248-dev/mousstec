"""Remove the hardcoded EGP add-on price bullet from Plan.features.

Some plans carried a feature bullet with a currency baked into the string,
e.g. "إضافة موظف/فرع/خزينة: 125 ج.م/شهر". On the UAE regional site this showed
Egyptian pounds, and the add-on price is already rendered dynamically (in the
region's currency) elsewhere on the pricing page — so the bullet was both wrong
on non-EG regions and redundant. Strip any such bullet from every plan.

Idempotent: running again is a no-op once the bullets are gone.
"""
from django.db import migrations


def _is_hardcoded_addon_bullet(text) -> bool:
    s = str(text)
    # The redundant add-on bullet: mentions the add-on AND a hardcoded currency.
    return 'إضافة موظف' in s and ('ج.م' in s or 'د.إ' in s)


def strip_addon_bullets(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')
    for plan in Plan.objects.all():
        feats = plan.features
        if not isinstance(feats, list):
            continue
        cleaned = [f for f in feats if not _is_hardcoded_addon_bullet(f)]
        if len(cleaned) != len(feats):
            plan.features = cleaned
            plan.save(update_fields=['features'])


def noop_reverse(apps, schema_editor):
    # We do not restore the hardcoded bullet — the add-on price is shown
    # dynamically, so re-adding a currency-baked string would reintroduce the bug.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0078_wix_connection'),
    ]

    operations = [
        migrations.RunPython(strip_addon_bullets, noop_reverse),
    ]
