"""Register the diagnostics feature codes and enable them on existing plans.

Audit (June 2026) found that ``smart_diagnostics`` checks four feature
codes that don't exist in the Feature catalog and aren't enabled on
any Plan:

    diagnostics_guided_tests
    diagnostics_live_data
    diagnostics_smart_parts_finder
    diagnostics_external_api_scans

Because :class:`clients.services.entitlements.EntitlementService` falls
back to ``False`` on unknown codes, every diagnostics API endpoint
silently returned "feature not in plan" — even for paying Gold/Empire
tenants. The integer quotas on ``Plan`` (``monthly_diagnostics_scans_quota``,
``monthly_diagnostics_bot_quota``) are present but they only meter
*how many*, they don't unlock the feature.

This migration is purely additive:

1. ``get_or_create`` the four codes in the Feature catalog so admin
   validation passes and they can be enabled.
2. Enable them on the automotive plans that already advertise
   diagnostics in their marketing copy:
     * auto-silver  → guided_tests + smart_parts_finder
     * auto-gold    → + live_data
     * auto-empire  → + external_api_scans (paid pay-per-call tier)
3. Printing plans (print-*) keep ``monthly_diagnostics_scans_quota=0``
   — diagnostics isn't part of the printing product; no entitlement
   change needed.

Reverse: clear the added entitlement keys from those plans. Feature
catalog rows are left intact because other data (snapshots, addons)
may reference them.
"""
from django.db import migrations


DIAG_FEATURES_SEED = [
    # (code, name_ar, name_en, category, is_quantitative, unit_label_ar, sort_order)
    ('diagnostics_guided_tests',
     'فحوصات تشخيص موجّهة', 'Guided Diagnostic Tests',
     'workshop', True, 'فحص/شهر', 40),
    ('diagnostics_live_data',
     'بيانات حية من السيارة', 'Live Vehicle Data',
     'workshop', False, '', 41),
    ('diagnostics_smart_parts_finder',
     'باحث القطع الذكي', 'Smart Parts Finder',
     'workshop', False, '', 42),
    ('diagnostics_external_api_scans',
     'فحوصات API خارجية', 'External API Scans',
     'workshop', True, 'فحص/شهر', 43),
]


# Which plans get which diagnostics features (additive — does not
# touch existing keys).
DIAG_PLAN_GRANTS = {
    'auto-silver': [
        'diagnostics_guided_tests',
        'diagnostics_smart_parts_finder',
    ],
    'auto-gold': [
        'diagnostics_guided_tests',
        'diagnostics_smart_parts_finder',
        'diagnostics_live_data',
    ],
    'auto-empire': [
        'diagnostics_guided_tests',
        'diagnostics_smart_parts_finder',
        'diagnostics_live_data',
        'diagnostics_external_api_scans',
    ],
}


def seed(apps, schema_editor):
    Feature = apps.get_model('clients', 'Feature')
    Plan = apps.get_model('clients', 'Plan')
    db = schema_editor.connection.alias

    # 1) Feature catalog ─────────────────────────────────────────────
    added = 0
    for code, name_ar, name_en, category, is_quant, unit, order in DIAG_FEATURES_SEED:
        _, created = Feature.objects.using(db).get_or_create(
            code=code,
            defaults={
                'name_ar': name_ar,
                'name_en': name_en,
                'category': category,
                'is_quantitative': is_quant,
                'unit_label_ar': unit,
                'sort_order': order,
                'is_active': True,
            },
        )
        if created:
            added += 1
    print(f"  ✅ [0069] Diagnostics features added to catalog: {added}/{len(DIAG_FEATURES_SEED)}")

    # 2) Enable on the existing automotive plans ────────────────────
    touched = 0
    for slug, codes in DIAG_PLAN_GRANTS.items():
        plan = Plan.objects.using(db).filter(slug=slug).first()
        if plan is None:
            print(f"  ⚠️  [0069] plan slug='{slug}' not found — skipping")
            continue
        ents = dict(plan.entitlements or {})
        changed = False
        for code in codes:
            if code not in ents:
                ents[code] = {'enabled': True}
                changed = True
        if changed:
            plan.entitlements = ents
            plan.save(update_fields=['entitlements'])
            touched += 1
            print(f"  ✅ [0069] '{slug}' diagnostics entitlements enabled ({len(codes)} codes)")
        else:
            print(f"  ↺ [0069] '{slug}' already has all diagnostics entitlements — skipping")
    print(f"\n[0069] Summary: {added} catalog rows added, {touched} plans updated")


def unseed(apps, schema_editor):
    """Remove the added diagnostics entitlements from the seeded plans.

    Feature rows are kept (defensive — locked_entitlements snapshots on
    existing TenantSubscriptions may reference them).
    """
    Plan = apps.get_model('clients', 'Plan')
    db = schema_editor.connection.alias
    for slug, codes in DIAG_PLAN_GRANTS.items():
        plan = Plan.objects.using(db).filter(slug=slug).first()
        if plan is None:
            continue
        ents = dict(plan.entitlements or {})
        before = len(ents)
        for code in codes:
            ents.pop(code, None)
        if len(ents) != before:
            plan.entitlements = ents
            plan.save(update_fields=['entitlements'])
    print("  [0069 reverse] Diagnostics entitlements removed from seeded plans.")


class Migration(migrations.Migration):
    dependencies = [
        ('clients', '0068_enable_watermark_on_d100'),
    ]
    operations = [migrations.RunPython(seed, reverse_code=unseed)]
