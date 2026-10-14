# 🧾 فواتير عقود الصيانة القديمة: القطع خرجت من المخزن بس مااتقيّدش ليها قيد
#   (المنطق القديم كان بيتخطّى الفاتورة كلها) — فحساب المخزون في الدفتر شايل قطع
#   مش موجودة، وتكلفتها ماظهرتش في قائمة الدخل. بنقيّد لكل فاتورة منهم:
#   مدين تكلفة البضاعة المباعة / دائن المخزون، بتاريخها (أو النهارده لو فترتها اتقفلت).
#   الفواتير اللي قبل آخر «تسوية مخزون» (reconcile_inventory_ledger): فرقها اتسوّى خلاص
#   على «رصيد افتتاحي»، فالدائن بيبقى «رصيد افتتاحي» عشان المخزون مايتخصمش مرتين.
from datetime import datetime, time
from decimal import Decimal

from django.db import migrations
from django.db.models import Q
from django.utils import timezone


def _account(ChartOfAccount, code, name, kind):
    acct, _ = ChartOfAccount.objects.get_or_create(code=code, defaults={'name': name, 'account_type': kind})
    return acct


def backfill(apps, schema_editor):
    SaleInvoice = apps.get_model('inventory', 'SaleInvoice')
    JournalEntry = apps.get_model('inventory', 'JournalEntry')
    AccountingEntry = apps.get_model('inventory', 'AccountingEntry')
    ChartOfAccount = apps.get_model('inventory', 'ChartOfAccount')
    AccountingPeriod = apps.get_model('inventory', 'AccountingPeriod')

    invoices = SaleInvoice.objects.filter(Q(status='posted') | Q(is_applied=True),
                                          maintenance_contract__isnull=False, is_return=False,
                                          total_cost__gt=0)
    if not invoices.exists():
        return
    cogs = _account(ChartOfAccount, '5001', 'تكلفة البضاعة المباعة', 'expense')
    stock = _account(ChartOfAccount, '1200', 'المخزون', 'asset')
    # لو «تسوية المخزون» (reconcile_inventory_ledger) اتعملت بعد الفاتورة، فرق القطع
    # دي اتسوّى خلاص على «رصيد افتتاحي» — فبننقل التكلفة منه بدل ما نخصم المخزون تاني.
    opening = _account(ChartOfAccount, '3005', 'رصيد افتتاحي (حقوق ملكية)', 'equity')
    last_reconcile = (JournalEntry.objects.filter(reference='INV-RECONCILE')
                      .exclude(status='reversed').order_by('-created_at').first())

    def period_for(d):
        return AccountingPeriod.objects.filter(start_date__lte=d, end_date__gte=d).first()

    for inv in invoices:
        if (JournalEntry.objects.filter(sale_invoice=inv, journal_type='sales')
                .exclude(status='reversed').exists()):
            continue
        cost = Decimal(str(inv.total_cost)).quantize(Decimal('0.01'))
        dt = inv.date_created or timezone.now()
        settled = last_reconcile is not None and dt <= last_reconcile.created_at
        credit_account = opening if settled else stock
        day = timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()
        period = period_for(day)
        if period is not None and period.is_closed:
            day = timezone.localdate()
            period = period_for(day)
        je = JournalEntry.objects.create(
            date=day, journal_type='sales', reference=f"SINV-{inv.pk}",
            description=f"فاتورة مبيعات #{inv.pk} (عقد صيانة — تكلفة القطع)"[:255],
            status='posted', period=period, branch_id=inv.branch_id, sale_invoice=inv,
            posted_at=timezone.now())
        je.number = f"JE-{day:%Y%m}-{je.pk:06d}"
        je.save(update_fields=['number'])
        when = timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())
        AccountingEntry.objects.bulk_create([
            AccountingEntry(journal_entry=je, entry_date=when, reference=je.number, account=cogs,
                            debit=cost, credit=Decimal('0'), sale_invoice=inv,
                            description=f"تكلفة بضاعة مباعة #{inv.pk}"),
            AccountingEntry(journal_entry=je, entry_date=when, reference=je.number, account=credit_account,
                            debit=Decimal('0'), credit=cost, sale_invoice=inv,
                            description=("تكلفة قطع اتسوّت قبل كده في تسوية المخزون" if settled
                                         else "خصم قيمة المخزون المباع")),
        ])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0061_settle_old_in_transit_transfers"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
