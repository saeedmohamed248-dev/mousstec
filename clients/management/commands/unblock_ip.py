"""🚑 طوارئ: رفع حظر IP (أو كل الحظر) من السيرفر — لو اتحظر IP غلط وقفل الدخول.

    python manage.py unblock_ip 1.2.3.4
    python manage.py unblock_ip --all
"""
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context


class Command(BaseCommand):
    help = 'رفع حظر IP من «مركز الأمان» (أو كله بـ --all).'

    def add_arguments(self, parser):
        parser.add_argument('ips', nargs='*')
        parser.add_argument('--all', action='store_true', help='يشيل كل الحظر')

    def handle(self, *args, **opts):
        from clients.models import BlockedIP
        if not opts['ips'] and not opts['all']:
            raise CommandError('اكتب IP أو --all')
        with schema_context('public'):
            qs = BlockedIP.objects.all() if opts['all'] else BlockedIP.objects.filter(ip_address__in=opts['ips'])
            n, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(f'اترفع الحظر عن {n} IP — بيسري خلال ٣٠ ثانية.'))
