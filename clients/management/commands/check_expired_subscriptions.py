"""
🛡️ أمر إداري: فحص وتعطيل الاشتراكات المنتهية تلقائياً
يُشغّل يومياً عبر Celery Beat أو crontab لضمان عدم استمرار أي عميل بعد انتهاء مدته.

الاستخدام:
    python manage.py check_expired_subscriptions
    python manage.py check_expired_subscriptions --fix-trial-dates   # لإصلاح تواريخ التجربة الخاطئة
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from clients.models import Client
import logging

logger = logging.getLogger('mouss_tec_core')


class Command(BaseCommand):
    help = 'فحص وتعطيل الاشتراكات المنتهية والفترات التجريبية المنقضية'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fix-trial-dates',
            action='store_true',
            help='إصلاح تواريخ التجربة الخاطئة (أكثر من 3 أيام من تاريخ الإنشاء)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='عرض ما سيتم فعله بدون تنفيذ فعلي',
        )

    def handle(self, *args, **options):
        today = timezone.now().date()
        dry_run = options['dry_run']
        fix_dates = options['fix_trial_dates']

        self.stdout.write(self.style.NOTICE(f'📅 تاريخ اليوم: {today}'))
        self.stdout.write('=' * 60)

        # ─── 1. إصلاح تواريخ التجربة الخاطئة ───
        if fix_dates:
            self.stdout.write(self.style.WARNING('\n🔧 إصلاح تواريخ الفترة التجريبية...'))
            trial_clients = Client.objects.filter(status='trial')
            fixed_count = 0
            for client in trial_clients:
                if client.created_on:
                    correct_end = client.created_on + timedelta(days=3)
                    if client.trial_ends_at != correct_end:
                        self.stdout.write(
                            f'  ⚠️ {client.name} ({client.schema_name}): '
                            f'trial_ends_at={client.trial_ends_at} → {correct_end} '
                            f'(created: {client.created_on})'
                        )
                        if not dry_run:
                            client.trial_ends_at = correct_end
                            client.save(update_fields=['trial_ends_at'])
                        fixed_count += 1
            if fixed_count == 0:
                self.stdout.write(self.style.SUCCESS('  ✅ جميع التواريخ صحيحة'))
            else:
                action = 'سيتم إصلاح' if dry_run else 'تم إصلاح'
                self.stdout.write(self.style.SUCCESS(f'  ✅ {action} {fixed_count} عميل'))

        # ─── 2. تعطيل الفترات التجريبية المنتهية ───
        self.stdout.write(self.style.WARNING('\n🔍 فحص الفترات التجريبية المنتهية...'))
        expired_trials = Client.objects.filter(
            status='trial',
            trial_ends_at__lt=today,
            is_active=True
        )
        for client in expired_trials:
            days_over = (today - client.trial_ends_at).days
            self.stdout.write(
                f'  🔴 {client.name} ({client.schema_name}): '
                f'انتهت التجربة منذ {days_over} يوم (ended: {client.trial_ends_at})'
            )
            if not dry_run:
                client.status = 'suspended'
                client.save(update_fields=['status'])
                logger.info(f'🔒 [AUTO-SUSPEND] Trial expired for {client.schema_name}, suspended.')

        if not expired_trials.exists():
            self.stdout.write(self.style.SUCCESS('  ✅ لا توجد تجارب منتهية'))
        else:
            action = 'سيتم تعطيل' if dry_run else 'تم تعطيل'
            self.stdout.write(self.style.SUCCESS(f'  ✅ {action} {expired_trials.count()} عميل'))

        # ─── 3. تعطيل الاشتراكات المنتهية (بعد فترة السماح 3 أيام) ───
        self.stdout.write(self.style.WARNING('\n🔍 فحص الاشتراكات المنتهية (بعد فترة السماح)...'))
        grace_cutoff = today - timedelta(days=3)
        expired_subs = Client.objects.filter(
            status='active',
            subscription_end_date__lt=grace_cutoff,
            is_active=True
        )
        for client in expired_subs:
            days_over = (today - client.subscription_end_date).days
            self.stdout.write(
                f'  🔴 {client.name} ({client.schema_name}): '
                f'انتهى الاشتراك منذ {days_over} يوم (ended: {client.subscription_end_date})'
            )
            if not dry_run:
                client.status = 'suspended'
                client.save(update_fields=['status'])
                logger.info(f'🔒 [AUTO-SUSPEND] Subscription expired for {client.schema_name}, suspended.')

        if not expired_subs.exists():
            self.stdout.write(self.style.SUCCESS('  ✅ لا توجد اشتراكات منتهية'))
        else:
            action = 'سيتم تعطيل' if dry_run else 'تم تعطيل'
            self.stdout.write(self.style.SUCCESS(f'  ✅ {action} {expired_subs.count()} عميل'))

        # ─── 4. ملخص الحالة العامة ───
        self.stdout.write(self.style.WARNING('\n📊 ملخص الحالة:'))
        for status_code, status_label in Client.STATUS_CHOICES:
            count = Client.objects.filter(status=status_code).count()
            self.stdout.write(f'  {status_label}: {count}')

        active_trials = Client.objects.filter(status='trial', trial_ends_at__gte=today)
        for client in active_trials:
            days_left = (client.trial_ends_at - today).days
            self.stdout.write(
                f'  ⏱️ {client.name}: باقي {days_left} يوم تجريبي '
                f'(ينتهي {client.trial_ends_at})'
            )

        self.stdout.write(self.style.SUCCESS('\n✅ اكتمل الفحص'))
