# تنظيف حركات مالية «يتيمة» ناتجة عن الحذف القديم للفواتير.
#
# الحذف القديم للفاتورة كان بيعمل «حركة تسوية» عكسية (out) بدل ما يمسح الدفعة،
# وبعد حذف الفاتورة الحركتين (الدفعة الأصلية in + التسوية out) بيفضلوا في الخزنة
# بـ sale_invoice=NULL — والتسوية (out) بتظهر غلط في شاشة المصاريف.
#
# الأمر ده بيلمّ الحركات اليتيمة اللي بيانها بيشير لفاتورة معيّنة وفيها «تسوية»
# (دليل إنها من حذف قديم)، وبيتأكد إن مجموع الـ in = مجموع الـ out لكل خزنة
# (يعني صافي أثرها على الرصيد = صفر)، وبعدين يمسحها هي وقيودها من غير ما يلمس
# رصيد الخزنة (لأنه أصلاً صافيها صفر). لو الصافي مش صفر بيتخطّى ويبلّغ (أماناً).
#
# الاستخدام:
#   python manage.py purge_orphan_invoice_txns --schema=fixit --dry-run
#   python manage.py purge_orphan_invoice_txns --schema=fixit
import re
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

_INV_RE = re.compile(r"فاتورة\s*#\s*(\d+)")


class Command(BaseCommand):
    help = "تنظيف الحركات المالية اليتيمة الناتجة عن الحذف القديم للفواتير"

    def add_arguments(self, parser):
        parser.add_argument("--schema", default=None,
                            help="schema شركة واحدة (الافتراضي: كل الشركات)")
        parser.add_argument("--dry-run", action="store_true",
                            help="عرض بس من غير حذف")

    def _purge_current_schema(self, dry_run):
        from inventory.models.finance import FinancialTransaction, AccountingEntry
        from inventory.models import JournalEntry

        # الحركات اليتيمة = مش مرتبطة بفاتورة بيع/شراء وبيانها بيشير لرقم فاتورة
        orphans = FinancialTransaction.objects.filter(
            sale_invoice__isnull=True, purchase_invoice__isnull=True,
        )
        # نجمّع حسب رقم الفاتورة المذكور في البيان
        by_inv = defaultdict(list)
        for ft in orphans:
            m = _INV_RE.search(ft.description or "")
            if m:
                by_inv[m.group(1)].append(ft)

        removed = 0
        for inv_no, fts in sorted(by_inv.items(), key=lambda x: int(x[0])):
            # لازم يكون فيهم حركة «تسوية» (دليل إنها من حذف قديم)
            has_settlement = any("تسوية" in (f.description or "") or "حذف فاتورة" in (f.description or "")
                                 for f in fts)
            if not has_settlement:
                continue
            # تأكّد إن الصافي لكل خزنة = صفر (in == out) قبل الحذف
            net = defaultdict(Decimal)
            for f in fts:
                net[f.treasury_id] += f.amount if f.transaction_type == "in" else -f.amount
            if any(v != Decimal("0") for v in net.values()):
                self.stdout.write(self.style.WARNING(
                    f"  ⚠️ فاتورة #{inv_no}: الصافي مش صفر {dict(net)} — تم التخطّي للأمان"))
                continue
            mark = "🟡 DRY-RUN" if dry_run else "🗑️"
            self.stdout.write(
                f"  {mark} فاتورة #{inv_no}: مسح {len(fts)} حركة يتيمة (صافي=0)")
            if not dry_run:
                with transaction.atomic():
                    for f in fts:
                        JournalEntry.objects.filter(financial_transaction=f).delete()
                        AccountingEntry.objects.filter(financial_transaction=f).delete()
                        f.delete()
            removed += len(fts)
        if removed == 0:
            self.stdout.write("  ✓ مفيش حركات يتيمة تحتاج تنظيف")
        return removed

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
                grand += self._purge_current_schema(dry_run)

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n🟡 (DRY-RUN) حركات هتتمسح: {grand} — لم يتم حذف أي شيء"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\n✅ تم مسح {grand} حركة يتيمة"))
