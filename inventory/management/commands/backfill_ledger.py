# تكميل قيود دفتر الأستاذ (GL) بأثر رجعي للفواتير القديمة.
#
# الفواتير اللي اتعملت قبل ما يشتغل تقييد المبيعات/المشتريات في الدفتر
# اتقيّد منها طرف الدفع بس (نقدية/بنك مقابل الذمم)، من غير الإيراد وتكلفة
# البضاعة والمخزون — فميزان المراجعة والمركز المالي بيطلعوا ناقصين.
#
# الأمر ده بينادي AccountingService.post_sale_invoice / post_purchase_invoice
# لكل فاتورة، وهي idempotent (بتتخطى اللي ليها قيد بالفعل) فآمنة للتكرار.
#
# وكمان بيصلّح حاجتين اتقيّدوا غلط قبل كده:
#   • تحويلات الخزائن اللي اتقيّدت مصروف + إيراد آخر → بتتشال وتتقيّد على
#     الحساب الوسيط (١٠٩٠).
#   • فواتير البيع اللي اتغيّر إجماليها بعد الترحيل (خصم على المتبقّي، رد
#     تأمين كور…) → قيد تسوية بالفرق بس (sync_sale_invoice).
#   • مصروفات البنود اللي اتقيّدت على حساب نظامي بالغلط (بند ١ «رواتب» كان
#     بيتقيّد على ٥٠٠١ تكلفة البضاعة المباعة…) → بتتنقل لحساب البند ٥٧xxx
#     (في الفترات المفتوحة بس — الفترة المقفولة مابتتعدّلش).
#   • حساب «عمولات مستحقة» (٢١١٠) بيتطابق مع أرصدة عمولات الموظفين: عمولات
#     البائعين القديمة ماكانش ليها قيد، وصرف العمولات كان بيتقيّد مصروف
#     عمومي بدل ما يسدّد الالتزام → قيد تسوية واحد بالفرق.
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

        # ── تحويلات الخزائن المتقيّدة مصروف/إيراد ─────────────────────────
        from inventory.models import AccountingEntry, FinancialTransaction
        from inventory.services.accounting_service import TRANSFER_TAG, _is_transfer
        clearing = AccountingService.account('treasury_transfer')
        transfers_fixed = 0
        for ft in (FinancialTransaction.objects.filter(description__startswith=TRANSFER_TAG)
                   .select_related('treasury').order_by('id')):
            if not _is_transfer(ft):
                continue
            jes = JournalEntry.objects.filter(financial_transaction=ft).exclude(status='reversed')
            if jes.exists() and AccountingEntry.objects.filter(
                    journal_entry__in=jes, account=clearing).exists():
                continue   # already on the clearing account
            if not dry_run:
                try:
                    AccountingService.unpost(jes, reason=f"إعادة تقييد تحويل #{ft.pk} على الحساب الوسيط")
                    AccountingService.post_payment(ft)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"  ⚠️ تحويل #{ft.pk}: {exc}")
                    continue
            transfers_fixed += 1

        # ── فواتير اتغيّرت بعد الترحيل ─────────────────────────────────
        synced = 0
        for inv in SaleInvoice.objects.filter(status='posted').order_by('id'):
            if dry_run:
                continue
            try:
                if AccountingService.sync_sale_invoice(inv):
                    synced += 1
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  ⚠️ تسوية فاتورة #{inv.id}: {exc}")

        rerouted = self._reroute_category_expenses(dry_run)
        trued_up = self._true_up_commissions(dry_run)

        mark = "🟡 (DRY-RUN) " if dry_run else "✅ "
        self.stdout.write(f"  {mark}قيود مبيعات: {sales_done} · قيود مشتريات: {purch_done}"
                          f" · تحويلات اتصلّحت: {transfers_fixed} · فواتير اتسوّت: {synced}"
                          f" · سطور مصروفات اتنقلت لحساب بندها: {rerouted}"
                          f" · تسوية العمولات: {trued_up}")
        return sales_done + purch_done + transfers_fixed + synced + rerouted + (1 if trued_up else 0)

    def _reroute_category_expenses(self, dry_run):
        """Move expense lines posted under the old «5 + category id» code onto
        the category's own 57xxx account (open periods only)."""
        from django.db.models import Q
        from inventory.models import (
            AccountingEntry, AccountingPeriod, ExpenseCategory, JournalEntry,
            PurchaseInvoiceExtraCost,
        )
        from inventory.services.accounting_service import AccountingService

        closed = list(AccountingPeriod.objects.filter(is_closed=True)
                      .values_list('start_date', 'end_date'))

        def _is_closed(d):
            return any(a <= d <= b for a, b in closed)

        moved = 0
        for cat in ExpenseCategory.objects.all().order_by('pk'):
            old_code = f'5{cat.pk:03d}'
            new_code = AccountingService.category_account_code(cat)
            if old_code == new_code:
                continue
            accrual_refs = [f"PINV-{ec.invoice_id}-EC-{ec.pk}" for ec in
                            PurchaseInvoiceExtraCost.objects.filter(expense_category=cat,
                                                                    treasury__isnull=True)]
            jes = (JournalEntry.objects
                   .filter(Q(financial_transaction__category=cat,
                             financial_transaction__purchase_extra_cost__isnull=True)
                           | Q(financial_transaction__purchase_extra_cost__expense_category=cat)
                           | Q(reference__in=accrual_refs))
                   .exclude(status='reversed').filter(reversal_of__isnull=True))
            je_ids = [je.pk for je in jes.only('pk', 'date', 'period')
                      if not (je.period_id and je.period.is_closed) and not _is_closed(je.date)]
            lines = AccountingEntry.objects.filter(journal_entry_id__in=je_ids, account__code=old_code)
            count = lines.count()
            if count and not dry_run:
                lines.update(account=AccountingService.category_account(cat))
            moved += count
        return moved

    def _true_up_commissions(self, dry_run):
        """Make the commission-payable control account equal the employees'
        outstanding commission balances (one adjustment entry, dated today)."""
        from decimal import Decimal
        from django.db.models import Sum
        from inventory.models import AccountingEntry, EmployeeProfile
        from inventory.services.accounting_service import AccountingService

        outstanding = (EmployeeProfile.objects.aggregate(t=Sum('commission_balance'))['t']
                       or Decimal('0'))
        payable = AccountingService.account('commission_payable')
        agg = AccountingEntry.objects.filter(account=payable).aggregate(d=Sum('debit'), c=Sum('credit'))
        in_ledger = (agg['c'] or Decimal('0')) - (agg['d'] or Decimal('0'))
        diff = (Decimal(str(outstanding)) - in_ledger).quantize(Decimal('0.01'))
        if diff == 0:
            return Decimal('0')
        if not dry_run:
            try:
                AccountingService.post_journal(
                    description="تسوية العمولات المستحقة مع أرصدة الموظفين",
                    lines=[
                        {'account': 'commission_expense', 'debit': diff if diff > 0 else 0,
                         'credit': -diff if diff < 0 else 0},
                        {'account': 'commission_payable', 'debit': -diff if diff < 0 else 0,
                         'credit': diff if diff > 0 else 0},
                    ],
                    journal_type='adjustment',
                    reference='COMM-TRUEUP',
                )
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  ⚠️ تسوية العمولات: {exc}")
                return Decimal('0')
        return diff

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
