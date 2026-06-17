"""
🩹 backfill_trial_subscriptions
================================
يصلح كل tenant عنده Client.plan مظبوط (مثلاً 'empire' أو 'print_enterprise')
بس الـ TenantSubscription.plan_id = None، فبيلاقي كل المزايا مقفولة رغم إنه
في فترة التجربة المفترض إنها بكل المزايا.

السبب: signup قديم كان بيعمل TenantSubscription بدون plan FK. الفيكس الجديد
في clients/views/auth_views.py بيـ link الـ Plan وقت الـ signup، لكن الـ
tenants اللي اتعملوا قبل الفيكس لسه فيهم الـ drift.

Usage:
    python manage.py backfill_trial_subscriptions          # dry run
    python manage.py backfill_trial_subscriptions --apply  # actually save
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context

from clients.models import Client, TenantSubscription, Plan
from clients.services.plan_mapping import resolve_plan_slug


class Command(BaseCommand):
    help = 'Link TenantSubscription.plan to the right Plan row based on Client.plan'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Actually save changes (default = dry run)')

    def handle(self, *args, **options):
        apply = options['apply']
        self.stdout.write(self.style.NOTICE(
            f'🩹 Backfill trial subscriptions ({"APPLY" if apply else "dry run"})'
        ))

        fixed = created = skipped = 0
        with schema_context('public'):
            for t in Client.objects.exclude(schema_name='public').filter(is_deleted=False):
                target_slug = resolve_plan_slug(t.plan or '')
                if not target_slug:
                    self.stdout.write(f'  ⏭️  {t.schema_name}: unknown plan "{t.plan}", skip')
                    skipped += 1
                    continue
                target_plan = Plan.objects.filter(slug=target_slug, is_active=True).first()
                if not target_plan:
                    self.stdout.write(f'  ⏭️  {t.schema_name}: Plan(slug={target_slug}) not in DB, skip')
                    skipped += 1
                    continue

                sub = getattr(t, 'subscription', None)
                if sub is None:
                    self.stdout.write(
                        f'  🆕 {t.schema_name}: create TenantSubscription → plan={target_slug}'
                    )
                    if apply:
                        TenantSubscription.objects.create(
                            tenant=t, plan=target_plan, is_active=False,
                        )
                    created += 1
                elif sub.plan_id is None:
                    self.stdout.write(
                        f'  🔗 {t.schema_name}: link subscription.plan → {target_slug}'
                    )
                    if apply:
                        sub.plan = target_plan
                        sub.save(update_fields=['plan'])
                    fixed += 1
                elif sub.plan.slug != target_slug:
                    self.stdout.write(
                        f'  ℹ️  {t.schema_name}: sub.plan={sub.plan.slug}, '
                        f'Client.plan={t.plan} (target={target_slug}) — drift, NOT changing'
                    )
                    skipped += 1
                else:
                    skipped += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'✅ Done. linked={fixed}, created={created}, skipped={skipped}'
            f'{"  [dry run — pass --apply to save]" if not apply else ""}'
        ))
