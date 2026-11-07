"""👥 استيراد العملاء والموردين بأرصدتهم الافتتاحية (صفحة للمالك بدل لوحة Django الفنية)."""
import csv as _csv

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _t

from inventory.services.party_import import HEADERS, import_parties
from inventory.views import tenant_required
from inventory.views.utils import role_required

_TEMPLATE_ROWS = {
    'customer': [['أحمد علي', '01012345678', '1500', ''], ['شركة النور للنقل', '01198765432', '12000', '123-456-789']],
    'vendor': [['مخازن الأمل لقطع الغيار', '01022223333', '8000', ''], ['توكيل البافارية', '', '25000', '']],
}


def _load_dataset(up):
    import tablib
    raw = up.read()
    fname = (up.name or '').lower()
    ds = tablib.Dataset()
    if fname.endswith(('.xlsx', '.xlsm')):
        ds.load(raw, format='xlsx')
    elif fname.endswith('.xls'):
        ds.load(raw, format='xls')
    elif fname.endswith('.ods'):
        ds.load(raw, format='ods')
    else:
        ds.load(raw.decode('utf-8-sig'), format='csv')
    return ds


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def party_import(request):
    kind = request.POST.get('kind') or request.GET.get('kind') or 'customer'
    if kind not in ('customer', 'vendor'):
        kind = 'customer'
    ctx = {'kind': kind, 'headers': HEADERS}

    if request.method == 'GET':
        if request.GET.get('template'):
            resp = HttpResponse(content_type='text/csv; charset=utf-8')
            resp['Content-Disposition'] = f'attachment; filename="mousstec_{kind}s_template.csv"'
            resp.write('﻿')   # BOM عشان العربي يظهر صح في Excel
            w = _csv.writer(resp)
            w.writerow(HEADERS)
            for r in _TEMPLATE_ROWS[kind]:
                w.writerow(r)
            return resp
        return render(request, 'inventory/party_import.html', ctx)

    up = request.FILES.get('file')
    if not up:
        ctx['error'] = _t('اختر ملف Excel أو CSV أولاً.')
        return render(request, 'inventory/party_import.html', ctx)
    try:
        ds = _load_dataset(up)
    except Exception:
        ctx['error'] = _t('تعذّر قراءة الملف. المدعوم: Excel (.xlsx/.xls)، CSV، ODS.')
        return render(request, 'inventory/party_import.html', ctx)
    try:
        ctx['result'] = import_parties(kind, ds, user=request.user)
    except ValueError as e:
        ctx['error'] = str(e)
    return render(request, 'inventory/party_import.html', ctx)
