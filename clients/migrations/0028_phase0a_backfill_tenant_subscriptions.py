"""
Phase 0a — Data Hygiene for Subscription Refactor
==================================================
هدف: نضمن إن كل Client عنده TenantSubscription صحيح قبل ما نشيل الـ
hardcoded if/elif من Client.save() في Phase 0b.

ما بنعمله:
  1. لكل Client مفيهوش TenantSubscription، بننشئ واحد بـ Plan مشتق من
     mapping الـ legacy Client.plan CharField → Plan.slug.
  2. لكل Client عنده TenantSubscription بس plan الـ subscription
     مش متناسق مع الـ mapping → بنـ log warning (مش بنـ overwrite،
     لأن الـ subscription على الأرجح الأصح والـ source of truth الجاي).
  3. additive و idempotent — آمن لإعادة التشغيل وآمن للـ rollback
     (الـ reverse no-op؛ ما حدش يحذف tenant subscriptions تلقائياً).

النتاج المتوقع على production (بناءً على Counter({'gold': 5, 'print_pro': 1})):
  - حد أقصى 6 TenantSubscription rows جديدة لو مش موجودين أصلاً.
  - صفر تعديلات على rows موجودة.
"""
from django.db import migrations


# Mapping للـ legacy Client.plan CharField → Plan.slug في DB
# مأكد على prod: Counter({'gold': 5, 'print_pro': 1}) — كل القيم متعرف عليها.
LEGACY_TO_PLAN_SLUG = {
    'silver':           'auto-silver',
    'gold':             'auto-gold',
    'empire':           'auto-empire',
    'print_basic':      'print-starter',
    'print_pro':        'print-pro',
    'print_enterprise': 'print-enterprise',
}


def backfill_subscriptions(apps, schema_editor):
    """Forward: ينشئ TenantSubscription لكل Client مفيهوش واحد."""
    Client = apps.get_model('clients', 'Client')
    Plan = apps.get_model('clients', 'Plan')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')

    # هنشتغل بس على public schema — TenantSubscription موجود في public.
    db_alias = schema_editor.connection.alias

    created_count = 0
    skipped_existing = 0
    mismatched_count = 0
    missing_plan_count = 0
    unknown_legacy_count = 0

    for client in Client.objects.using(db_alias).all():
        legacy_plan = (client.plan or '').strip()
        target_slug = LEGACY_TO_PLAN_SLUG.get(legacy_plan)

        if not target_slug:
            # Plan قديم مش في الـ mapping — نـ log ونتخطى. Phase 0a additive only.
            unknown_legacy_count += 1
            print(
                f"  ⚠️  [Phase 0a] Skipping tenant '{client.schema_name}' — "
                f"unknown legacy plan='{legacy_plan}' (not in LEGACY_TO_PLAN_SLUG)"
            )
            continue

        target_plan = Plan.objects.using(db_alias).filter(slug=target_slug).first()
        if not target_plan:
            missing_plan_count += 1
            print(
                f"  ⚠️  [Phase 0a] Skipping tenant '{client.schema_name}' — "
                f"Plan with slug='{target_slug}' does not exist. "
                f"Run migration 0011 first."
            )
            continue

        # هل عنده subscription بالفعل؟
        existing_sub = TenantSubscription.objects.using(db_alias).filter(
            tenant=client
        ).first()

        if existing_sub:
            skipped_existing += 1
            # Sanity check — هل الـ plan متناسق؟
            if existing_sub.plan_id and existing_sub.plan_id != target_plan.id:
                mismatched_count += 1
                existing_slug = existing_sub.plan.slug if existing_sub.plan else 'None'
                print(
                    f"  ℹ️  [Phase 0a] Tenant '{client.schema_name}' has "
                    f"subscription with plan slug='{existing_slug}' but legacy "
                    f"Client.plan='{legacy_plan}' maps to '{target_slug}'. "
                    f"Leaving subscription untouched (it's the source of truth)."
                )
            continue

        # مفيش subscription → ننشئ واحد
        TenantSubscription.objects.using(db_alias).create(
            tenant=client,
            plan=target_plan,
            billing_cycle_months=1,
            is_active=True,
        )
        created_count += 1
        print(
            f"  ✅ [Phase 0a] Created subscription for '{client.schema_name}' "
            f"with plan '{target_slug}'"
        )

    print(
        f"\n[Phase 0a] Summary:\n"
        f"  - Subscriptions created:  {created_count}\n"
        f"  - Already had subscription: {skipped_existing}\n"
        f"  - Plan mismatches (warning only): {mismatched_count}\n"
        f"  - Unknown legacy plans (skipped): {unknown_legacy_count}\n"
        f"  - Missing Plan rows (skipped): {missing_plan_count}\n"
    )


def noop_reverse(apps, schema_editor):
    """
    Reverse: no-op متعمد. ما حدش يحذف TenantSubscriptions تلقائياً —
    ده عمل خطر على بيانات الـ tenants. لو احتجت rollback اعمله يدوياً.
    """
    print(
        "  [Phase 0a reverse] No-op. TenantSubscription rows created by this "
        "migration will NOT be auto-deleted (data safety)."
    )


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0027_add_ai_prompt_learning_log'),
    ]

    operations = [
        migrations.RunPython(backfill_subscriptions, reverse_code=noop_reverse),
    ]
