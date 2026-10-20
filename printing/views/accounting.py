"""
📒 Printing accounting pages — read-only views over the general ledger.

/printing/accounting/                    ميزان المراجعة + الميزانية + دفتر اليومية
/printing/supplier/<id>/statement/       كشف حساب المورد
"""
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.utils import timezone


def _can_view_books(user):
    if user.is_staff or user.is_superuser:
        return True
    perms = getattr(user, 'print_permissions', None)
    try:
        return bool(perms and (perms.can_view_reports or perms.can_view_profits))
    except Exception:
        return False


def _date_param(value):
    from datetime import date as _d
    try:
        return _d.fromisoformat((value or '').strip())
    except ValueError:
        return None


@login_required
def accounting_overview(request):
    """ميزان المراجعة والميزانية ودفتر اليومية — كله من الدفتر العام (استحقاق)."""
    from inventory.models import JournalEntry
    from inventory.services.accounting_reports import AccountingReportService
    from printing import ledger

    if not _can_view_books(request.user):
        return HttpResponseForbidden("لا تملك صلاحية مشاهدة الدفاتر المحاسبية.")
    ledger.ensure_bootstrapped()

    today = timezone.localdate()
    as_of = _date_param(request.GET.get('as_of')) or today
    date_from = _date_param(request.GET.get('from')) or today.replace(day=1)
    date_to = _date_param(request.GET.get('to')) or today

    entries = (JournalEntry.objects.filter(date__gte=date_from, date__lte=date_to)
               .prefetch_related('lines__account').order_by('-date', '-id')[:200])
    return render(request, 'printing/accounting.html', {
        'tab': request.GET.get('tab') or 'trial',
        'as_of': as_of,
        'date_from': date_from,
        'date_to': date_to,
        'trial': AccountingReportService.trial_balance(as_of=as_of),
        'sheet': AccountingReportService.balance_sheet(as_of=as_of),
        'entries': entries,
    })


@login_required
def supplier_statement(request, supplier_id):
    """كشف حساب المورد: فواتير الشراء المستلمة (له) والمدفوعات (عليه) برصيد جاري."""
    from printing.models import PrintSupplier
    from printing.views.finance import _can_see_money

    if not (_can_see_money(request.user, 'can_view_reports')
            or _can_see_money(request.user, 'can_manage_treasury')):
        return HttpResponseForbidden("لا تملك صلاحية مشاهدة كشوف الحساب.")
    supplier = get_object_or_404(PrintSupplier, pk=supplier_id)

    events = []
    for p in supplier.purchases.filter(status='received'):
        events.append({
            'date': p.received_at or timezone.make_aware(
                timezone.datetime.combine(p.date, timezone.datetime.min.time())),
            'kind': 'purchase', 'ref': p.number, 'obj_id': p.pk,
            'description': f'فاتورة شراء #{p.number}' + (f' (فاتورة المورد {p.supplier_invoice_no})'
                                                        if p.supplier_invoice_no else ''),
            'credit': p.net_total, 'debit': Decimal('0'),
        })
    for t in supplier.payments_qs().select_related('treasury', 'purchase'):
        paid = t.transaction_type == 'out'
        events.append({
            'date': t.date, 'kind': 'payment' if paid else 'refund', 'ref': f'#{t.pk}', 'obj_id': t.pk,
            'description': t.description or (f'سداد للمورد — {t.treasury.name}' if paid
                                             else f'استرداد من المورد — {t.treasury.name}'),
            'debit': t.amount if paid else Decimal('0'),
            'credit': Decimal('0') if paid else t.amount,
        })
    events.sort(key=lambda e: e['date'])
    running = Decimal('0')
    for ev in events:
        running += ev['credit'] - ev['debit']
        ev['balance'] = running
    summary = supplier.account_summary()
    return render(request, 'printing/supplier_statement.html', {
        'supplier': supplier,
        'events': events,
        'summary': summary,
        'print_mode': request.GET.get('print') == '1',
    })
