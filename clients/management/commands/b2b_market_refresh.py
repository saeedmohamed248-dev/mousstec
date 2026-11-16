"""🌐 تحديث أصناف الشركات المعتمدة في سوق قطع الغيار (صورة + موديلات + كمية).

🐛 [FIX]: أصناف الشركات اللي اتوافق عليها قبل كده:
  * اللي اتنشرت كانت من غير صورة/موديلات (الحقول اتضافت بعدين).
  * واللي رصيدها كان صفر وقت الموافقة اتشالت ومارجعتش لما المخزون رجع.
الأمر ده بيلف على كل صنف منشور ونشط عند كل شركة ويعمله نفس مزامنة حركة المخزون:
يحدّث الموجود، ويرجّع اللي اتوافق عليه قبل كده ورصيده رجع، ويشيل اللي رصيده صفر.
آمن يتعاد.

    python manage.py b2b_market_refresh                 # كل الشركات
    python manage.py b2b_market_refresh --schema fixit  # شركة واحدة
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context


class Command(BaseCommand):
    help = 'يحدّث أصناف الشركات المنشورة في سوق قطع الغيار (صورة/موديلات/كمية) ويرجّع المعتمد اللي رصيده رجع.'

    def add_arguments(self, parser):
        parser.add_argument('--schema', default='', help='schema شركة واحدة بس')

    def handle(self, *args, schema='', **opts):
        from clients.models import Client
        from clients.tasks import async_sync_b2b_marketplace_product
        from inventory.models import Product

        tenants = Client.objects.exclude(schema_name='public').order_by('name')
        if schema:
            tenants = tenants.filter(schema_name=schema)
        totals = Counter()
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                ids = list(Product.objects.filter(is_b2b_published=True, is_active=True)
                           .values_list('pk', flat=True))
            counts = Counter()
            for pid in ids:
                res = async_sync_b2b_marketplace_product.apply(args=[tenant.schema_name, pid])
                counts[res.result if res.successful() else 'failed'] += 1
            if ids:
                self.stdout.write(f"{tenant.name}: " + ' · '.join(f"{k}={v}" for k, v in sorted(counts.items(), key=str)))
            totals.update(counts)
        self.stdout.write(self.style.SUCCESS(
            'خلص: ' + (' · '.join(f"{k}={v}" for k, v in sorted(totals.items(), key=str)) or 'مفيش أصناف منشورة')))
