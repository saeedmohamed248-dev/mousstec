# تسوية حساب المخزون في دفتر الأستاذ ليساوي القيمة الفعلية للمخزون.
#
# المشكلة: المخزون الافتتاحي/المُحمَّل مباشرة (من غير فاتورة شراء) كان بيزوّد
# الكمية بس مايتقيّدش على حساب المخزون (١٢٠٠). أول ما القطع دي تتباع، البيع
# بيعمل Credit مخزون فينزل الحساب بالسالب (زي -65,057).
#
# الأمر ده بيحسب القيمة الفعلية للمخزون = Σ(الكمية × متوسط التكلفة)، ويقارنها
# برصيد حساب المخزون في الأستاذ، ويقيّد فرق التسوية مقابل «رصيد افتتاحي —
# حقوق ملكية» (٣٠٠٥). بيتسّاب مرة واحدة بعد نشر إصلاح قيد المخزون الافتتاحي.
#
# الاستخدام (django-tenants):
#   python manage.py reconcile_inventory_ledger                 # كل الشركات
#   python manage.py reconcile_inventory_ledger --schema=fixit  # شركة واحدة
#   python manage.py reconcile_inventory_ledger --dry-run       # عرض الفرق فقط
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum, F, DecimalField, ExpressionWrapper


class Command(BaseCommand):
    help = "تسوية حساب المخزون في الأستاذ ليطابق القيمة الفعلية (Σ الكمية × متوسط التكلفة)"

    def add_arguments(self, parser):
        parser.add_argument("--schema", default=None,
                            help="schema شركة واحدة (الافتراضي: كل الشركات ماعدا public)")
        parser.add_argument("--dry-run", action="store_true",
                            help="عرض الفرق فقط بدون تقييد")

    def _reconcile_current_schema(self, dry_run):
        from inventory.models import Inventory, AccountingEntry, ChartOfAccount
        from inventory.services.accounting_service import AccountingService

        money = DecimalField(max_digits=18, decimal_places=2)
        # القيمة الفعلية للمخزون على الأصناف كلها.
        target = Inventory.objects.aggregate(
            v=Sum(ExpressionWrapper(F('quantity') * F('product__average_cost'),
                                    output_field=money))
        )['v'] or Decimal('0.00')

        # رصيد حساب المخزون الحالي في الأستاذ (مدين − دائن).
        inv_acct = ChartOfAccount.objects.filter(code='1200').first()
        current = Decimal('0.00')
        if inv_acct is not None:
            agg = AccountingEntry.objects.filter(account=inv_acct).aggregate(
                d=Sum('debit'), c=Sum('credit'))
            current = (agg['d'] or Decimal('0')) - (agg['c'] or Decimal('0'))

        diff = (target - current).quantize(Decimal('0.01'))
        self.stdout.write(
            f"  المخزون الفعلي={target}  |  دفتر الأستاذ={current}  |  فرق التسوية={diff}")
        if abs(diff) < Decimal('0.01'):
            self.stdout.write("  ✓ مطابق — لا حاجة لتسوية")
            return 0

        if dry_run:
            self.stdout.write(f"  🟡 DRY-RUN: هيتقيّد فرق {diff} مقابل «رصيد افتتاحي (٣٠٠٥)»")
            return 1

        if diff > 0:
            lines = [
                {'account': 'inventory', 'debit': diff, 'credit': 0},
                {'account': 'opening_equity', 'debit': 0, 'credit': diff},
            ]
        else:
            amt = -diff
            lines = [
                {'account': 'opening_equity', 'debit': amt, 'credit': 0},
                {'account': 'inventory', 'debit': 0, 'credit': amt},
            ]
        AccountingService.post_journal(
            description="تسوية رصيد المخزون الافتتاحي مع القيمة الفعلية",
            lines=lines,
            journal_type='adjustment',
            reference='INV-RECONCILE',
        )
        self.stdout.write(self.style.SUCCESS(f"  ✅ تم تقييد تسوية بمقدار {diff}"))
        return 1

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
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n🏢 {tenant.schema_name}"))
            with schema_context(tenant.schema_name):
                grand += self._reconcile_current_schema(dry_run)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n🟡 (DRY-RUN) شركات تحتاج تسوية مخزون: {grand}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\n✅ تمت تسوية المخزون في {grand} شركة"))
