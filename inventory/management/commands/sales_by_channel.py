# 📊 تقرير مبيعات الشهر مقسّمة حسب القناة (المحل / الموقع الإلكتروني)
#   python manage.py sales_by_channel                # الشهر الحالي
#   python manage.py sales_by_channel --month 2026-08
#   مع التينانتس: python manage.py tenant_command sales_by_channel --schema=<schema>
from datetime import date

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from inventory.models import SaleInvoice


class Command(BaseCommand):
    help = 'تقرير مبيعات الشهر مقسّمة حسب قناة البيع (المحل / الموقع)'

    def add_arguments(self, parser):
        parser.add_argument('--month', help="YYYY-MM (افتراضي: الشهر الحالي)")

    def handle(self, *args, **o):
        if o.get('month'):
            y, m = map(int, o['month'].split('-'))
        else:
            now = timezone.now()
            y, m = now.year, now.month

        start = date(y, m, 1)
        end = date(y + (m // 12), (m % 12) + 1, 1)

        qs = SaleInvoice.objects.filter(
            status='posted', is_return=False,
            date_created__gte=start, date_created__lt=end,
        )
        rows = (qs.values('sales_channel')
                  .annotate(count=Count('id'), total=Sum('total_amount'),
                            profit=Sum('net_profit'))
                  .order_by('-total'))

        labels = dict(SaleInvoice.SALES_CHANNEL_CHOICES)
        self.stdout.write(self.style.SUCCESS(f"\n📊 مبيعات شهر {y}-{m:02d} حسب القناة:\n"))
        self.stdout.write(f"{'القناة':<22}{'عدد الفواتير':>14}{'الإجمالي':>16}{'صافي الربح':>16}")
        self.stdout.write("-" * 68)
        g_count = g_total = g_profit = 0
        for r in rows:
            label = labels.get(r['sales_channel'], r['sales_channel'])
            total = r['total'] or 0
            profit = r['profit'] or 0
            g_count += r['count']; g_total += total; g_profit += profit
            self.stdout.write(f"{label:<22}{r['count']:>14}{total:>16,.2f}{profit:>16,.2f}")
        self.stdout.write("-" * 68)
        self.stdout.write(self.style.SUCCESS(
            f"{'الإجمالي':<22}{g_count:>14}{g_total:>16,.2f}{g_profit:>16,.2f}\n"))
