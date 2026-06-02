"""
Phase 2 (data) — Backfill PlanRevisions + initial subscription snapshots
==========================================================================
ينفذ خطوتين على البيانات الموجودة:

  1. لكل Plan موجود، ينشئ revision أولى تمثل الحالة الحالية (السعر +
     entitlements). ده الـ "baseline" اللي كل الـ subscriptions القديمة هتـ
     reference. لو الـ Plan عنده revisions بالفعل، نـ skip.

  2. لكل TenantSubscription بدون locked_at (يعني لسة ما اتعملش snapshot)،
     نـ copy الـ price والـ entitlements من الـ plan الحالي ونـ point لـ
     latest revision.

كله idempotent — آمن لإعادة التشغيل. reverse = clear locked_* + delete
revisions اللي اتعملت من المهاجرة دي (نـ pick من change_reason).
"""
from django.db import migrations
from django.utils import timezone


BACKFILL_REASON = 'Phase 2 backfill — initial revision (current Plan state)'


def backfill_revisions_and_snapshots(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')
    PlanRevision = apps.get_model('clients', 'PlanRevision')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')
    db_alias = schema_editor.connection.alias

    # ── Step 1: Initial revision per Plan ─────────────────────────
    revisions_created = 0
    for plan in Plan.objects.using(db_alias).all():
        already_has_revision = PlanRevision.objects.using(db_alias).filter(plan=plan).exists()
        if already_has_revision:
            continue
        PlanRevision.objects.using(db_alias).create(
            plan=plan,
            monthly_price=plan.monthly_price,
            entitlements=dict(plan.entitlements or {}),
            changed_by=None,
            change_reason=BACKFILL_REASON,
        )
        revisions_created += 1
        print(f"  📜 [Phase 2 backfill] Initial revision for plan '{plan.slug}' @ {plan.monthly_price} ج.م")

    # ── Step 2: Snapshot existing subscriptions ─────────────────
    snapshots_created = 0
    snapshots_skipped = 0
    for sub in TenantSubscription.objects.using(db_alias).select_related('plan').all():
        if sub.locked_at is not None:
            snapshots_skipped += 1
            continue
        if not sub.plan_id:
            snapshots_skipped += 1
            continue

        revision = (
            PlanRevision.objects.using(db_alias)
            .filter(plan_id=sub.plan_id)
            .order_by('-effective_from').first()
        )
        if revision is None:
            # ما يفترضش يحصل بعد Step 1، لكن دفاعي
            snapshots_skipped += 1
            continue

        sub.locked_monthly_price = sub.plan.monthly_price
        sub.locked_entitlements = dict(sub.plan.entitlements or {})
        sub.locked_at = timezone.now()
        sub.locked_plan_revision = revision
        sub.save(update_fields=[
            'locked_monthly_price', 'locked_entitlements',
            'locked_at', 'locked_plan_revision',
        ])
        snapshots_created += 1
        print(
            f"  📸 [Phase 2 backfill] Snapshot for '{sub.tenant.schema_name}' "
            f"locked @ {sub.locked_monthly_price} ج.م, plan={sub.plan.slug}"
        )

    print(
        f"\n[Phase 2 backfill] Summary:\n"
        f"  - PlanRevisions created (baseline): {revisions_created}\n"
        f"  - Subscriptions snapshotted: {snapshots_created}\n"
        f"  - Subscriptions skipped (already locked or no plan): {snapshots_skipped}\n"
    )


def reverse_backfill(apps, schema_editor):
    """Reverse: نـ clear الـ locked_* على الـ subscriptions و نحذف
    revisions اللي صنعتها المهاجرة دي بس (نـ identify بـ change_reason)."""
    PlanRevision = apps.get_model('clients', 'PlanRevision')
    TenantSubscription = apps.get_model('clients', 'TenantSubscription')
    db_alias = schema_editor.connection.alias

    # Clear snapshots
    cleared = TenantSubscription.objects.using(db_alias).filter(
        locked_at__isnull=False
    ).update(
        locked_monthly_price=None,
        locked_entitlements={},
        locked_at=None,
        locked_plan_revision=None,
    )

    # Delete backfill revisions only (preserve any revisions added since)
    deleted, _ = PlanRevision.objects.using(db_alias).filter(
        change_reason=BACKFILL_REASON
    ).delete()

    print(f"  [Phase 2 reverse] Cleared {cleared} snapshots, deleted {deleted} baseline revisions.")


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0031_plan_revisions_and_locked_snapshots'),
    ]

    operations = [
        migrations.RunPython(backfill_revisions_and_snapshots, reverse_code=reverse_backfill),
    ]
