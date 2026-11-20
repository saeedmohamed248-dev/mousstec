"""🔢 فحص تسلسل أرقام الفواتير — كل رقم من ١ لآخر رقم: موجود، ولا اتمسح (مين وإمتى)، ولا فجوة قديمة.

المراجع/الضرايب بيسألوا عن أي رقم ناقص — الصفحة دي بترد: الرقم ده اتمسح يوم كذا بواسطة فلان،
أو كان فجوة قبل تفعيل الترقيم المتسلسل (DocumentSequence).
"""
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Max
from django.shortcuts import render

from inventory.models import AuditLog, DocumentSequence, SaleInvoice
from inventory.views import tenant_required
from inventory.views.utils import module_required, role_required

MAX_LISTED = 500


def missing_invoice_numbers(limit=MAX_LISTED):
    """(أرقام ناقصة لحد ``limit``، عددها الكلي، آخر رقم) — من generate_series على Postgres."""
    top = SaleInvoice.objects.aggregate(m=Max('pk'))['m'] or 0
    seq = DocumentSequence.objects.filter(name='sale_invoice').first()
    last = max(top, seq.last_value if seq else 0)
    if not last:
        return [], 0, 0
    table = connection.ops.quote_name(SaleInvoice._meta.db_table)
    with connection.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM generate_series(1, %s) n "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {table} i WHERE i.id = n)", [last])
        total = cur.fetchone()[0]
        cur.execute(f"SELECT n FROM generate_series(1, %s) n "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {table} i WHERE i.id = n) ORDER BY n DESC LIMIT %s",
                    [last, limit])
        numbers = [r[0] for r in cur.fetchall()]
    return numbers, total, last


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def invoice_sequence_report(request):
    numbers, total_missing, last = missing_invoice_numbers()
    seq = DocumentSequence.objects.filter(name='sale_invoice').first()
    start = seq.start_value if seq else last
    deletions = {
        log.object_id: log for log in
        AuditLog.objects.filter(model_name='SaleInvoice', action='delete',
                                object_id__in=[str(n) for n in numbers])
        .select_related('user').order_by('timestamp')
    }
    rows = []
    for n in numbers:
        log = deletions.get(str(n))
        if log is not None:
            kind = 'deleted'
        elif n <= start:
            kind = 'legacy'
        else:
            kind = 'unknown'
        prev = rows[-1] if rows else None
        # فجوات ورا بعض من غير سبب (زي نطّة الـ ٣٢ رقم بعد ما Postgres يقع) ⇒ سطر واحد «من … لـ …»
        if (log is None and prev is not None and prev['log'] is None and prev['kind'] == kind
                and prev['low'] == n + 1):
            prev['low'] = n
            prev['count'] += 1
            continue
        rows.append({'number': n, 'low': n, 'count': 1, 'kind': kind, 'log': log})
    return render(request, 'inventory/invoice_sequence.html', {
        'last_number': last,
        'invoices_count': SaleInvoice.objects.count(),
        'total_missing': total_missing,
        'rows': rows,
        'listed_limit': MAX_LISTED,
        'start_value': start,
        'started_at': seq.created_at if seq else None,
        'unknown_count': sum(r['count'] for r in rows if r['kind'] == 'unknown'),
    })
