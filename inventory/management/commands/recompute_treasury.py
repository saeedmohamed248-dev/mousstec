# إعادة احتساب رصيد كل خزنة من واقع حركاتها المالية (المصدر الوحيد للحقيقة).
#
# الخزنة لا تملك "رصيد افتتاحي" مستقل، لذا الرصيد الصحيح دائماً =
#   مجموع الإيداعات (in) − مجموع المسحوبات (out).
# يُستخدم لتصحيح أي انحراف قديم في البيانات (مثل فاتورة سُجّلت مرتين).
#
# الاستخدام (مع django-tenants):
#   python manage.py recompute_treasury                 # كل الشركات (schemas)
#   python manage.py recompute_treasury --schema=fixit  # شركة واحدة
#   python manage.py recompute_treasury --dry-run       # عرض الفروقات فقط بدون حفظ
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum


class Command(BaseCommand):
    help = "إعادة احتساب رصيد كل خزنة من حركاتها المالية (تصحيح انحرافات البيانات)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema", default=None,
            help="اسم schema لشركة واحدة فقط (الافتراضي: كل الشركات ماعدا public)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="عرض الفروقات فقط بدون حفظ أي تعديل",
        )

    def _recompute_current_schema(self, dry_run):
        from inventory.models.finance import Treasury, FinancialTransaction

        fixed = 0
        for tr in Treasury.objects.select_related("branch").all():
            total_in = FinancialTransaction.objects.filter(
                treasury=tr, transaction_type="in"
            ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
            total_out = FinancialTransaction.objects.filter(
                treasury=tr, transaction_type="out"
            ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
            correct = total_in - total_out
            old = tr.balance or Decimal("0")
            if correct != old:
                fixed += 1
                mark = "🟡 DRY-RUN" if dry_run else "✅"
                self.stdout.write(
                    f"  {mark} {tr.name} [{tr.branch.name}]: "
                    f"{old} → {correct}  (in={total_in} out={total_out}, "
                    f"فرق={old - correct})"
                )
                if not dry_run:
                    # .update() لتفادي إطلاق أي signals تعيد احتساب الرصيد
                    Treasury.objects.filter(pk=tr.pk).update(balance=correct)
            else:
                self.stdout.write(f"  ✓ {tr.name} [{tr.branch.name}]: {old} (سليم)")
        return fixed

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        schema = options["schema"]

        from django_tenants.utils import schema_context, get_tenant_model

        TenantModel = get_tenant_model()
        if schema:
            tenants = TenantModel.objects.filter(schema_name=schema)
            if not tenants:
                self.stderr.write(self.style.ERROR(f"لا توجد شركة باسم schema = {schema}"))
                return
        else:
            tenants = TenantModel.objects.exclude(schema_name="public")

        grand = 0
        for tenant in tenants:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n🏢 {tenant.schema_name}"
            ))
            with schema_context(tenant.schema_name):
                grand += self._recompute_current_schema(dry_run)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n🟡 (DRY-RUN) خزائن تحتاج تصحيح: {grand} — لم يتم حفظ أي تعديل"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n✅ تم تصحيح {grand} خزنة"
            ))
