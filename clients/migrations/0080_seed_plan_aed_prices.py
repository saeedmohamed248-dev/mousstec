"""Seed monthly_price_aed for plans so the UAE regional site shows AED prices.

All plans shipped with monthly_price_aed = 0, so the UAE site fell back to the
Egyptian price *numbers* labelled with the AED symbol. This seeds a realistic
AED price (EGP ÷ 13, the prevailing EGP→AED rate) for the known plans, and for
any other active plan still at 0 it derives one the same way — so no Egyptian
figure is ever shown on the UAE site.

Admin can override any of these later from the Super Admin panel; this only
fills values that are currently unset (0), never overwrites a real AED price.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations

# Explicit AED prices agreed with the owner (EGP ÷ 13, rounded to clean figures).
AED_BY_SLUG = {
    'auto-silver': Decimal('42'),
    'auto-gold': Decimal('65'),
    'auto-empire': Decimal('190'),
    'print-starter': Decimal('67'),
    'print-pro': Decimal('96'),
    'print-enterprise': Decimal('154'),
    'premium_diagnostics': Decimal('460'),
}
_EGP_TO_AED = Decimal('13')


def seed_aed(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')
    for plan in Plan.objects.all():
        current = plan.monthly_price_aed or Decimal('0')
        if current and current > 0:
            continue  # never overwrite a real AED price
        aed = AED_BY_SLUG.get(plan.slug)
        if aed is None:
            base = plan.monthly_price or Decimal('0')
            if base <= 0:
                continue
            aed = (Decimal(base) / _EGP_TO_AED).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        plan.monthly_price_aed = aed
        plan.save(update_fields=['monthly_price_aed'])


def noop_reverse(apps, schema_editor):
    # Reversing would just re-zero prices the owner may have since tuned; leave as-is.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0079_strip_hardcoded_addon_price_from_plan_features'),
    ]

    operations = [
        migrations.RunPython(seed_aed, noop_reverse),
    ]
