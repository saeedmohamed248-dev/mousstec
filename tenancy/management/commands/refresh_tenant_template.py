"""⚡ يعمل/يحدّث قالب الـ schema اللي الشركات الجديدة بتتنسخ منه.

بيتشغّل في سكريبت النشر بعد migrate_schemas — من غيره أول تسجيل بعد النشر
هو اللي بيعمل القالب (ودي بتاخد وقت المهاجرات العادي مرة واحدة).
"""
import time

from django.core.management.base import BaseCommand

from tenancy.services.schema_template import ensure_template


class Command(BaseCommand):
    help = 'Create or migrate the tenant template schema used to provision new tenants fast'

    def handle(self, *args, **options):
        started = time.monotonic()
        name = ensure_template(verbosity=options.get('verbosity', 1))
        if not name:
            self.stdout.write('TENANT_TEMPLATE_SCHEMA فاضي — الشركات بتتعمل بالمهاجرات العادية.')
            return
        self.stdout.write(self.style.SUCCESS(
            f'✅ قالب الشركات «{name}» جاهز ({time.monotonic() - started:.1f}s)'))
