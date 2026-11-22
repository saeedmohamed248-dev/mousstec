"""📊 أرقام «شغل النهارده» في لوحة المطبعة (inventory/admin/dashboard.py::_printing_dashboard).

صاحب المطبعة أول ما يفتح محتاج يعرف: إيه اللي شغال، إيه اللي اتأخر، إيه اللي جاهز يتسلّم،
وفلوسه عند العملاء كام — قبل أي زرار. الأرقام دي من نفس التعريفات اللي في باقي السيستم.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Q, Sum
from django.urls import reverse

OPEN_STATUSES = ('draft', 'confirmed', 'in_progress', 'ready')


def receivables_total():
    """💳 فلوسك عند العملاء = رصيد حساب المدينين (1100) في الدفاتر — نفس «أعمار الديون»
    والميزانية (فواتير اتسلّمت ولسه ماتحصّلتش + الأرصدة الافتتاحية). رقم واحد من استعلام واحد."""
    from inventory.models import AccountingEntry
    from printing.ledger import PRINT_ACCOUNTS
    code = PRINT_ACCOUNTS['ar'][0]
    agg = (AccountingEntry.objects.filter(Q(account__code=code) | Q(account__parent__code=code))
           .aggregate(d=Sum('debit'), c=Sum('credit')))
    return (agg['d'] or Decimal('0')) - (agg['c'] or Decimal('0'))


def ops_snapshot(*, pending, delayed, show_money):
    from printing.models import PrintOrder
    changelist = reverse('admin:printing_printorder_changelist')
    snap = {
        'pending': pending, 'delayed': delayed,
        'ready': PrintOrder.objects.filter(status='ready').count(),
        'pending_url': f"{changelist}?status__in={','.join(OPEN_STATUSES)}",
        'ready_url': f"{changelist}?status__exact=ready",
        'board_url': reverse('printing:production_board'),
    }
    if show_money:
        snap['receivables'] = receivables_total()
        snap['receivables_url'] = reverse('printing:accounting_overview') + '?tab=aging'
    return snap
