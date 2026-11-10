"""🎥 مسح تسجيلات الشاشة الأقدم من SCREEN_RECORDING_RETENTION_DAYS (افتراضي ٧ أيام).

بيتنادى كمان تلقائياً (بنسبة صغيرة) مع كل دفعة تسجيل، فده للتشغيل اليدوي أو من cron.
"""
from django.core.management.base import BaseCommand

from clients.views.screen_recording_views import purge_old_sessions, retention_days


class Command(BaseCommand):
    help = 'مسح تسجيلات الشاشة القديمة'

    def handle(self, *args, **options):
        n = purge_old_sessions()
        self.stdout.write(f'اتمسح {n} (جلسات + أجزاء) أقدم من {retention_days()} أيام')
