"""
🧾 الفاتورة الإلكترونية (ETA) — صفحة المراجعة وتحميل ملفات JSON.

* /system/eta/: فواتير الفترة المعتمدة، وجنب كل واحدة «جاهزة» أو الأخطاء اللي
  ناقصة (inventory/services/eta_einvoice.py)، و«تحميل الجاهزة» ملف واحد
  {"documents": [...]} — نفس الشكل اللي أدوات التوقيع والبوابة بتاخده.
* /system/invoices/<pk>/eta.json: ملف فاتورة واحدة (أو 400 بالأخطاء).
الإرسال المباشر للبوابة مش متفعّل لسه (محتاج Client Secret وتوكن التوقيع).
"""
import json
from datetime import datetime, time, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from .models import ETASettings, SaleInvoice
from .services.eta_einvoice import build_document
from .views import _get_branch_for_user, tenant_required
from .views.utils import role_required

ETA_ROLES = ('admin', 'manager', 'accountant')
MAX_ROWS = 500


def _parse(value, default):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return default


def _invoices(request):
    today = timezone.localdate()
    d_from = _parse(request.GET.get('from'), today.replace(day=1))
    d_to = _parse(request.GET.get('to'), today)
    tz = timezone.get_current_timezone()
    qs = (SaleInvoice.objects.filter(
        status='posted', is_return=False,
        date_created__gte=timezone.make_aware(datetime.combine(d_from, time.min), tz),
        date_created__lt=timezone.make_aware(datetime.combine(d_to + timedelta(days=1), time.min), tz))
        .select_related('customer').order_by('date_created', 'id'))
    branch = _get_branch_for_user(request.user)
    if branch is not None:
        qs = qs.filter(branch=branch)
    return qs, d_from, d_to


def _download(payload, filename):
    resp = HttpResponse(json.dumps(payload, ensure_ascii=False, indent=2),
                        content_type='application/json; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@login_required(login_url='/login/')
@tenant_required
@role_required(*ETA_ROLES)
def eta_overview(request):
    settings = ETASettings.load()
    qs, d_from, d_to = _invoices(request)
    rows = []
    for inv in qs[:MAX_ROWS]:
        doc, errors = build_document(inv, settings)
        rows.append({'inv': inv, 'doc': doc, 'errors': errors})
    if request.GET.get('download') == '1':
        docs = [r['doc'] for r in rows if not r['errors']]
        return _download({'documents': docs}, f'eta-{d_from}-{d_to}.json')
    return render(request, 'inventory/eta_overview.html', {
        'settings': settings, 'rows': rows, 'd_from': d_from, 'd_to': d_to,
        'ready': sum(1 for r in rows if not r['errors']), 'truncated': qs.count() > MAX_ROWS,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required(*ETA_ROLES)
def eta_invoice_json(request, pk):
    qs = SaleInvoice.objects.select_related('customer')
    branch = _get_branch_for_user(request.user)
    if branch is not None:
        qs = qs.filter(branch=branch)
    inv = get_object_or_404(qs, pk=pk)
    doc, errors = build_document(inv)
    if errors:
        return JsonResponse({'errors': errors}, status=400, json_dumps_params={'ensure_ascii': False})
    return _download({'documents': [doc]}, f'eta-invoice-{inv.pk}.json')
