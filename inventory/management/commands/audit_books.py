# 🔍 فحص الدفاتر لكل شركة (قراءة بس — مابيعدّلش حاجة):
#   المخزون مع حركاته، قيمة المخزون مع الدفتر، الموردين، الخزن، توازن الدفتر،
#   وفواتير الشراء المتعدّلة اللي مخزونها مارجعش.
#
# الاستخدام (django-tenants):
#   python manage.py audit_books                  # كل الشركات
#   python manage.py audit_books --schema=gom     # شركة واحدة
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "فحص اتّساق الدفاتر (مخزون/دفتر/موردين/خزن) — قراءة بس"

    def add_arguments(self, parser):
        parser.add_argument("--schema", default=None, help="schema شركة واحدة (الافتراضي: كل الشركات)")

    def handle(self, *args, **options):
        from django_tenants.utils import get_tenant_model, schema_context
        from inventory.services.books_audit import books_problems

        tenants = get_tenant_model().objects.exclude(schema_name='public')
        if options['schema']:
            tenants = tenants.filter(schema_name=options['schema'])
        total = 0
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                problems = books_problems()
            mark = "✅" if not problems else f"⚠️ {len(problems)}"
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n🏢 {tenant.schema_name} — {mark}"))
            for p in problems:
                self.stdout.write(f"  • {p}")
            total += len(problems)
        self.stdout.write(self.style.SUCCESS("\nكله مظبوط ✅") if not total
                          else self.style.WARNING(f"\n{total} ملاحظة — راجعها مع المحاسب."))
