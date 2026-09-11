# تكميل قيود دفتر الأستاذ (GL) بأثر رجعي للفواتير القديمة.
#
# الفواتير اللي اتعملت قبل ما يشتغل تقييد المبيعات/المشتريات في الدفتر
# اتقيّد منها طرف الدفع بس (نقدية/بنك مقابل الذمم)، من غير الإيراد وتكلفة
# البضاعة والمخزون — فميزان المراجعة والمركز المالي بيطلعوا ناقصين.
#
# الأمر ده بينادي AccountingService.post_sale_invoice / post_purchase_invoice
# لكل فاتورة، وهي idempotent (بتتخطى اللي ليها قيد بالفعل) فآمنة للتكرار.
#
# الاستخدام:
#   python manage.py backfill_ledger --schema=fixit_02e0 --dry-run
#   python manage.py backfill_ledger --schema=fixit_02e0
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "تكميل قيود دفتر الأستاذ للفواتير القديمة (مبيعات + مشتريات)"

    def add_arguments(self, parser):
        parser.add_argument("--schema", default=None, help="schema شركة واحدة")
        parser.add_argument("--dry-run", action="store_true", help="عرض بس")

    def _run_schema(self, dry_run):
        from inventory.models import SaleInvoice, PurchaseInvoice, JournalEntry
        from inventory.services.accounting_service import AccountingService

        sales_done = purch_done = 0

        sinvs = SaleInvoice.objects.exclude(status='quotation').order_by('id')
        for inv in sinvs:
            if JournalEntry.objects.filter(sale_invoice=inv, journal_type='sales').exists():
                continue
            if not dry_run:
                try:
                    AccountingService.post_sale_invoice(inv)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"  ⚠️ فاتورة بيع #{inv.id}: {exc}")
                    continue
            sales_done += 1

        pinvs = PurchaseInvoice.objects.filter(status='posted').order_by('id')
        for inv in pinvs:
            if JournalEntry.objects.filter(purchase_invoice=inv, journal_type='purchase').exists():
                continue
            if not dry_run:
                try:
                    AccountingService.post_purchase_invoice(inv)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"  ⚠️ فاتورة شراء #{inv.id}: {exc}")
                    continue
            purch_done += 1

        mark = "🟡 (DRY-RUN) " if dry_run else "✅ "
        self.stdout.write(f"  {mark}قيود مبيعات: {sales_done} · قيود مشتريات: {purch_done}")
        return sales_done + purch_done

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        schema = options["schema"]
        from django_tenants.utils import schema_context, get_tenant_model

        TenantModel = get_tenant_model()
        if schema:
            tenants = TenantModel.objects.filter(schema_name=schema)
            if not tenants:
                self.stderr.write(self.style.ERROR(f"لا توجد شركة schema = {schema}"))
                return
        else:
            tenants = TenantModel.objects.exclude(schema_name="public")

        grand = 0
        for tenant in tenants:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n🏢 {tenant.schema_name}"))
            with schema_context(tenant.schema_name):
                grand += self._run_schema(dry_run)

        if dry_run:
            self.stdout.write(self.style.WARNING(f"\n🟡 (DRY-RUN) هيتقيّد: {grand} فاتورة"))
        else:
            self.stdout.write(self.style.SUCCESS(f"\n✅ تم تكميل قيود {grand} فاتورة"))
