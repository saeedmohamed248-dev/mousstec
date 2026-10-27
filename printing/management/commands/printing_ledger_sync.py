"""
📒 قيد الأرصدة الافتتاحية وكل الحركات القديمة لشركات الطباعة في الدفتر العام.

الاستخدام (مرة بعد النشر — وتتعاد بأمان، مابتكرّرش قيود):
    python manage.py printing_ledger_sync                # كل شركات الطباعة اللي لسه ماتقيّدتش
    python manage.py printing_ledger_sync --schema flez  # شركة واحدة
    python manage.py printing_ledger_sync --force        # مراجعة وتصليح أي فرق في كل الشركات
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context


class Command(BaseCommand):
    help = 'Post opening balances + sync every printing document into the general ledger'

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='schema_name لشركة واحدة')
        parser.add_argument('--force', action='store_true', help='أعد مزامنة كل المستندات حتى لو اتعملت قبل كده')

    def handle(self, *args, **options):
        from clients.models import Client
        tenants = Client.objects.exclude(schema_name='public').filter(industry='printing')
        if options.get('schema'):
            tenants = tenants.filter(schema_name=options['schema'])
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                from printing import ledger
                from inventory.services.accounting_reports import AccountingReportService
                ran = ledger.bootstrap(force=options['force'])
                tb = AccountingReportService.trial_balance()
                status = 'synced' if ran else 'already synced'
                balanced = 'balanced' if tb['is_balanced'] else 'NOT BALANCED'
                self.stdout.write(f"{tenant.schema_name}: {status} — trial balance {balanced} "
                                  f"({tb['total_debit']} / {tb['total_credit']})")
