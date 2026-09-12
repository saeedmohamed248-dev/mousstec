# مزامنة كاملة مع موقع FixIt: رفع/تحديث كل المنتجات النشطة دفعة واحدة
# الاستخدام: python manage.py fixit_sync_all [--prune]
# (مع django-tenants: python manage.py tenant_command fixit_sync_all --schema=<schema_name> [--prune])
#
# --prune : يخلّي الموقع مطابق لموس تك بالظبط — يحذف أي منتج على الموقع
#           مش موجود هنا (زي المنتجات التجريبية/القديمة). المنتجات المعمولة
#           على الموقع من غير SKU بتتساب.
from django.core.management.base import BaseCommand

from inventory.services import fixit_sync


class Command(BaseCommand):
    help = 'رفع كل المنتجات النشطة لموقع FixIt الإلكتروني (إنشاء أو تحديث حسب الـ part_number)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--prune',
            action='store_true',
            help='حذف أي منتج على الموقع مش موجود في مخزون موس تك (تنضيف التجريبي/القديم)',
        )

    def handle(self, *args, **options):
        if not fixit_sync.is_configured():
            self.stderr.write(self.style.ERROR(
                'الربط مش مفعّل — اضبط FIXIT_SYNC_URL و FIXIT_SYNC_SECRET في البيئة أو settings.py'
            ))
            return
        if not fixit_sync.is_enabled():
            self.stderr.write(self.style.ERROR(
                'الفرع (schema) الحالي مش هو الفرع المسموح بالمزامنة (FIXIT_TENANT_SCHEMA). '
                'شغّل الأمر على الفرع الصح، أو عدّل/امسح FIXIT_TENANT_SCHEMA.'
            ))
            return
        prune = options.get('prune')
        if prune:
            self.stdout.write('🔄 جاري المزامنة الكاملة + تنضيف الموقع ليطابق موس تك...')
        else:
            self.stdout.write('🔄 جاري المزامنة الكاملة مع موقع FixIt...')
        total = fixit_sync.push_all_products(stdout=self.stdout, prune=prune)
        self.stdout.write(self.style.SUCCESS(f'✅ تمت مزامنة {total} منتج مع الموقع'))
