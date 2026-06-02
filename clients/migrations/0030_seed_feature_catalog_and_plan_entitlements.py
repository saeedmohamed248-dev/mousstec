"""
Phase 1 (data seed) — Feature catalog + initial Plan entitlements
==================================================================
Idempotent: get_or_create للـ Features، update_or_create للـ Plan
entitlements. آمن لإعادة التشغيل، آمن للـ rollback (reverse =
clear entitlements + delete seeded features).

NOTE: الـ entitlements اللي بنـ seed مبنية على الـ Arabic display
strings الموجودة في migration 0011، مع تخريطها لـ feature codes
حقيقية. لو فيه gap بين 0011 وبين هنا، الأولوية للـ 0011 (هي مصدر
الترويج للعملاء).
"""
from django.db import migrations


# ─── Feature Catalog Seed ────────────────────────────────────────────
# Format: (code, name_ar, name_en, category, is_quantitative, unit_label_ar, sort_order)
FEATURES_SEED = [
    # Core — أساسية
    ('core_invoicing',         'فواتير بيع وشراء',  'Sales & Purchase Invoicing', 'core',         False, '', 10),
    ('core_inventory',         'مخزون قطع غيار',    'Inventory Management',       'core',         False, '', 20),
    ('core_treasury',          'إدارة الخزائن',     'Treasury Management',        'core',         False, '', 30),

    # Workshop — مراكز صيانة (automotive)
    ('workshop_repair_cards',  'كروت صيانة',        'Repair Job Cards',           'workshop',     True,  'كرت/شهر', 10),
    ('workshop_fleet_contracts','عقود أساطيل',      'Fleet Maintenance Contracts','workshop',     False, '', 20),
    ('workshop_dtc_ai',        'تحليل أعطال DTC',   'DTC AI Diagnostic',          'workshop',     False, '', 30),

    # Printing — مطابع
    ('print_orders',           'طلبات طباعة',       'Print Orders',               'printing',     True,  'طلب/شهر', 10),
    ('print_auto_pricing',     'تسعير تلقائي',      'Auto Pricing',               'printing',     False, '', 20),
    ('print_designer_worklog', 'سجل أعمال المصممين','Designer Worklog',           'printing',     False, '', 30),

    # Marketplace — أسواق
    ('b2b_marketplace',        'سوق B2B',           'B2B Marketplace',            'marketplace',  False, '', 10),
    ('open_marketplace',       'السوق المفتوح',     'Open Marketplace',           'marketplace',  False, '', 20),

    # Analytics — تقارير
    ('reports_basic',          'تقارير أساسية',     'Basic Reports',              'analytics',    False, '', 10),
    ('reports_advanced',       'تقارير متقدمة',     'Advanced Analytics',         'analytics',    False, '', 20),

    # Integrations — تكاملات
    ('whatsapp_delivery',      'إرسال واتساب تلقائي','WhatsApp Auto-Delivery',    'integrations', True,  'رسالة/شهر', 10),

    # Support — دعم
    ('priority_support',       'دعم فني أولوية',    'Priority Support',           'support',      False, '', 10),
]


# ─── Plan Entitlements Seed ──────────────────────────────────────────
# Mapped بناءً على display strings في migration 0011.
# Format: plan_slug → {feature_code: {enabled, monthly_limit?}}
PLAN_ENTITLEMENTS = {
    # ── Automotive ──
    'auto-silver': {
        'core_invoicing':          {'enabled': True},
        'core_inventory':          {'enabled': True},
        'core_treasury':           {'enabled': True},
        'workshop_repair_cards':   {'enabled': True, 'monthly_limit': 150},
        'open_marketplace':        {'enabled': True},
        'reports_basic':           {'enabled': True},
    },
    'auto-gold': {
        'core_invoicing':          {'enabled': True},
        'core_inventory':          {'enabled': True},
        'core_treasury':           {'enabled': True},
        'workshop_repair_cards':   {'enabled': True, 'monthly_limit': 0},  # 0 = unlimited per existing convention
        'workshop_dtc_ai':         {'enabled': True},
        'b2b_marketplace':         {'enabled': True},
        'open_marketplace':        {'enabled': True},
        'reports_basic':           {'enabled': True},
        'reports_advanced':        {'enabled': True},
    },
    'auto-empire': {
        'core_invoicing':          {'enabled': True},
        'core_inventory':          {'enabled': True},
        'core_treasury':           {'enabled': True},
        'workshop_repair_cards':   {'enabled': True, 'monthly_limit': 0},
        'workshop_fleet_contracts':{'enabled': True},
        'workshop_dtc_ai':         {'enabled': True},
        'b2b_marketplace':         {'enabled': True},
        'open_marketplace':        {'enabled': True},
        'reports_basic':           {'enabled': True},
        'reports_advanced':        {'enabled': True},
        'priority_support':        {'enabled': True},
    },

    # ── Printing ──
    'print-starter': {
        'core_treasury':           {'enabled': True},
        'print_orders':            {'enabled': True, 'monthly_limit': 0},  # ما نص لـ limit في 0011، نخلي unlimited
        'print_auto_pricing':      {'enabled': True},
        'open_marketplace':        {'enabled': True},
        'reports_basic':           {'enabled': True},
    },
    'print-pro': {
        'core_treasury':           {'enabled': True},
        'print_orders':            {'enabled': True, 'monthly_limit': 0},
        'print_auto_pricing':      {'enabled': True},
        'print_designer_worklog':  {'enabled': True},
        'open_marketplace':        {'enabled': True},
        'reports_basic':           {'enabled': True},
        'reports_advanced':        {'enabled': True},
    },
    'print-enterprise': {
        'core_treasury':           {'enabled': True},
        'print_orders':            {'enabled': True, 'monthly_limit': 0},
        'print_auto_pricing':      {'enabled': True},
        'print_designer_worklog':  {'enabled': True},
        'open_marketplace':        {'enabled': True},
        'reports_basic':           {'enabled': True},
        'reports_advanced':        {'enabled': True},
        'priority_support':        {'enabled': True},
    },
}


def seed_features_and_entitlements(apps, schema_editor):
    Feature = apps.get_model('clients', 'Feature')
    Plan = apps.get_model('clients', 'Plan')
    db_alias = schema_editor.connection.alias

    # 1) Feature catalog ──────────────────────────────────────────
    features_created = 0
    for code, name_ar, name_en, category, is_quant, unit, order in FEATURES_SEED:
        obj, was_created = Feature.objects.using(db_alias).get_or_create(
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
        if was_created:
            features_created += 1
    print(f"  ✅ [Phase 1 seed] Features in catalog: {Feature.objects.using(db_alias).count()} ({features_created} new)")

    # 2) Plan entitlements ────────────────────────────────────────
    plans_updated = 0
    for slug, entitlements in PLAN_ENTITLEMENTS.items():
        plan = Plan.objects.using(db_alias).filter(slug=slug).first()
        if not plan:
            print(f"  ⚠️  [Phase 1 seed] Plan slug='{slug}' not found — skipping entitlements")
            continue
        # Idempotent: نـ replace entitlements بالكامل (آمن لإعادة التشغيل)
        plan.entitlements = entitlements
        plan.save(update_fields=['entitlements'])
        plans_updated += 1
        print(f"  ✅ [Phase 1 seed] Plan '{slug}' entitlements set ({len(entitlements)} features)")

    print(f"\n[Phase 1 seed] Summary: {features_created} features added, {plans_updated} plans configured")


def reverse_seed(apps, schema_editor):
    """Reverse: نشيل entitlements بس من الـ plans (مش نحذف الـ Features
    لأن أي data جاية ممكن تكون بتـ reference). الـ migration 0029
    reverse هو اللي بيـ drop الجدول."""
    Plan = apps.get_model('clients', 'Plan')
    db_alias = schema_editor.connection.alias

    for slug in PLAN_ENTITLEMENTS.keys():
        plan = Plan.objects.using(db_alias).filter(slug=slug).first()
        if plan:
            plan.entitlements = {}
            plan.save(update_fields=['entitlements'])
    print("  [Phase 1 seed reverse] Cleared entitlements on seeded plans (Feature rows preserved).")


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0029_feature_catalog_and_entitlements'),
    ]

    operations = [
        migrations.RunPython(seed_features_and_entitlements, reverse_code=reverse_seed),
    ]
