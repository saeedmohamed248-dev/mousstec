# مزامنة كاملة مع موقع FixIt: رفع/تحديث كل المنتجات النشطة دفعة واحدة
# الاستخدام: python manage.py fixit_sync_all
# (مع django-tenants: python manage.py tenant_command fixit_sync_all --schema=<schema_name>)
from django.core.management.base import BaseCommand

from inventory.services import fixit_sync


class Command(BaseCommand):
    help = 'رفع كل المنتجات النشطة لموقع FixIt الإلكتروني (إنشاء أو تحديث حسب الـ part_number)'

    def handle(self, *args, **options):
        if not fixit_sync.is_enabled():
            self.stderr.write(self.style.ERROR(
                'الربط مش مفعّل — اضبط FIXIT_SYNC_URL و FIXIT_SYNC_SECRET في البيئة أو settings.py'
            ))
            return
        self.stdout.write('🔄 جاري المزامنة الكاملة مع موقع FixIt...')
        total = fixit_sync.push_all_products(stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS(f'✅ تمت مزامنة {total} منتج مع الموقع'))
