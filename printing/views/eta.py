"""🧾 الفاتورة الإلكترونية للمطابع — مراجعة فواتير الفترة + الإعدادات + تحميل ملفات JSON (printing/eta.py).

الإعدادات هي ETASettings بتاعة الشركة، بس صفحة أدمن inventory مقفولة على المطابع
(IndustryRoutingMiddleware) — فبتتعدّل من هنا.
"""
import json
from datetime import datetime, time, timedelta

from django import forms
from django.contrib import admin as _admin, messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from erp_core.decorators import tenant_or_404

__all__ = ['print_eta_overview', 'print_eta_order_json']
MAX_ROWS = 500


class _ETASettingsForm(forms.ModelForm):
    class Meta:
        from inventory.models import ETASettings
        model = ETASettings
        fields = ('taxpayer_rin', 'taxpayer_name', 'activity_code', 'branch_id', 'governate', 'region_city',
                  'street', 'building_number', 'service_item_code')


def _allowed(user):
    from printing.views.finance import _can_see_money
    return _can_see_money(user, 'can_view_reports') or _can_see_money(user, 'can_view_treasury')


def _parse(value, default):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return default


def _download(payload, filename):
    resp = HttpResponse(json.dumps(payload, ensure_ascii=False, indent=2), content_type='application/json; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@login_required
@tenant_or_404
def print_eta_overview(request):
    from inventory.models import ETASettings
    from printing.eta import build_document
    from printing.models import PrintOrder
    if not _allowed(request.user):
        return HttpResponseForbidden(_("لا تملك صلاحية التقارير."))
    settings = ETASettings.load()
    form = _ETASettingsForm(instance=settings)
    if request.method == 'POST':
        from printing.views.finance import _can_see_money
        if not _can_see_money(request.user, 'can_manage_treasury'):
            return HttpResponseForbidden(_("تعديل الإعدادات لصاحب المطبعة / مدير الخزينة بس."))
        form = _ETASettingsForm(request.POST, instance=settings)
        if form.is_valid():
            form.save()
            messages.success(request, _("اتحفظت إعدادات الفاتورة الإلكترونية."))
            return redirect(request.get_full_path())
    today = timezone.localdate()
    d_from = _parse(request.GET.get('from'), today.replace(day=1))
    d_to = _parse(request.GET.get('to'), today)
    tz = timezone.get_current_timezone()
    qs = (PrintOrder.objects.filter(
        status='delivered',
        date_delivered__gte=timezone.make_aware(datetime.combine(d_from, time.min), tz),
        date_delivered__lt=timezone.make_aware(datetime.combine(d_to + timedelta(days=1), time.min), tz))
        .select_related('customer').order_by('date_delivered', 'pk'))
    rows = []
    for order in qs[:MAX_ROWS]:
        doc, errors = build_document(order, settings)
        rows.append({'order': order, 'doc': doc, 'errors': errors})
    if request.GET.get('download') == '1':
        return _download({'documents': [r['doc'] for r in rows if not r['errors']]}, f'eta-print-{d_from}-{d_to}.json')
    ctx = _admin.site.each_context(request)
    ctx.update({
        'title': _("🧾 الفاتورة الإلكترونية"), 'form': form, 'rows': rows, 'd_from': d_from, 'd_to': d_to,
        'ready': sum(1 for r in rows if not r['errors']), 'truncated': qs.count() > MAX_ROWS,
    })
    return render(request, 'printing/eta_overview.html', ctx)


@login_required
@tenant_or_404
def print_eta_order_json(request, order_id):
    from printing.eta import build_document
    from printing.models import PrintOrder
    if not _allowed(request.user):
        return HttpResponseForbidden(_("لا تملك صلاحية التقارير."))
    order = get_object_or_404(PrintOrder.objects.select_related('customer'), pk=order_id)
    doc, errors = build_document(order)
    if errors:
        return JsonResponse({'errors': errors}, status=400, json_dumps_params={'ensure_ascii': False})
    return _download({'documents': [doc]}, f'eta-{order.order_number}.json')
