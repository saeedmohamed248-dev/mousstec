"""
🔩 واجهات الفك التدريجي + قوالب الفك (Reverse BOM)
====================================================
كل الردود JSON، وتسجيل الدخول مطلوب. الفلو المقصود من الواجهة:

  1) GET  /inventory/disassembly/templates/            → قائمة القوالب
  2) POST /inventory/disassembly/load-template/        → يبني مسودة حدث فك
     من قالب (auto-populate) ويرجّع الأبناء قابلين للتعديل
  3) POST /inventory/disassembly/result/<id>/update/   → تعديل سعر بند
  4) POST /inventory/disassembly/result/<id>/remove/   → شيل بند تالف
  5) POST /inventory/disassembly/<event_id>/execute/   → اعتماد وتوزيع التكلفة
"""
import json
import logging
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from ..models import (DisassemblyEvent, DisassemblyResult, DisassemblyTemplate,
                      InventoryItem)
from ..services import DisassemblyService

logger = logging.getLogger('mouss_tec_core')


@login_required(login_url='/login/')
def workspace(request):
    """شاشة فلو الفك بالقوالب — تختار أب + قالب، تعدّل، وتعتمد."""
    parents = (InventoryItem.objects
               .filter(status=InventoryItem.STATUS_IN_STOCK)
               .order_by('-created_at')[:200])
    templates = (DisassemblyTemplate.objects
                 .filter(is_active=True).order_by('name'))
    return render(request, 'inventory/disassembly_workspace.html', {
        'parents': parents,
        'templates': templates,
    })


def _body(request):
    try:
        return json.loads(request.body.decode('utf-8')) if request.body else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _event_json(event):
    return {
        'event_id': event.pk,
        'parent_item': {'id': event.parent_item_id, 'sku': event.parent_item.sku,
                        'name': event.parent_item.name,
                        'cost': str(event.parent_item.cost)},
        'total_scrap_revenue': str(event.total_scrap_revenue),
        'is_executed': event.is_executed,
        'total_estimated_sales': str(event.total_estimated_sales),
        'results': [
            {'result_id': r.pk, 'child_item_id': r.child_item_id,
             'sku': r.child_item.sku, 'name': r.child_item.name,
             'estimated_sales_price': str(r.estimated_sales_price),
             'allocated_cost': str(r.allocated_cost)}
            for r in event.results.select_related('child_item')
        ],
    }


@login_required
@require_GET
def list_templates(request):
    """قائمة قوالب الفك النشطة (تصفية اختيارية بكود المحرك ?engine=N20)."""
    qs = DisassemblyTemplate.objects.filter(is_active=True)
    engine = (request.GET.get('engine') or '').strip()
    if engine:
        qs = qs.filter(engine_code__icontains=engine)
    data = [{
        'id': t.pk, 'name': t.name, 'engine_code': t.engine_code,
        'default_scrap_revenue': str(t.default_scrap_revenue),
        'items_count': t.items.count(),
    } for t in qs]
    return JsonResponse({'ok': True, 'templates': data})


@login_required
@require_POST
def load_template(request):
    """يبني مسودة حدث فك من قالب ويملأها بالأبناء تلقائياً (auto-populate)."""
    body = _body(request)
    parent_id = body.get('parent_item_id')
    template_id = body.get('template_id')
    if not parent_id or not template_id:
        return JsonResponse({'error': 'missing parent_item_id/template_id'}, status=400)

    parent = get_object_or_404(InventoryItem, pk=parent_id)
    template = get_object_or_404(DisassemblyTemplate, pk=template_id)

    scrap = body.get('scrap_revenue')
    try:
        scrap = Decimal(str(scrap)) if scrap is not None else None
    except (InvalidOperation, ValueError):
        return JsonResponse({'error': 'invalid scrap_revenue'}, status=400)

    try:
        event = DisassemblyService.load_template(
            parent, template, created_by=request.user,
            override_scrap_revenue=scrap)
    except ValidationError as exc:
        return JsonResponse({'error': getattr(exc, 'message', str(exc))}, status=400)

    return JsonResponse({'ok': True, **_event_json(event)}, status=201)


@login_required
@require_POST
def update_result(request, result_id):
    """تعديل السعر التقديري لبند في مسودة (قبل الاعتماد)."""
    result = get_object_or_404(DisassemblyResult, pk=result_id)
    if result.event.is_executed:
        return JsonResponse({'error': 'event already executed'}, status=400)

    body = _body(request)
    try:
        price = Decimal(str(body.get('estimated_sales_price')))
    except (InvalidOperation, ValueError, TypeError):
        return JsonResponse({'error': 'invalid estimated_sales_price'}, status=400)
    if price < 0:
        return JsonResponse({'error': 'price must be >= 0'}, status=400)

    result.estimated_sales_price = price
    result.save(update_fields=['estimated_sales_price'])
    # نحدّث العنصر الابن نفسه كمان لو لسه ماتنفّذش (سعر تقديري مرجعي)
    result.child_item.estimated_sales_price = price
    result.child_item.save(update_fields=['estimated_sales_price', 'updated_at'])
    return JsonResponse({'ok': True, **_event_json(result.event)})


@login_required
@require_POST
def remove_result(request, result_id):
    """شيل بند (قطعة تالفة) من المسودة + امسح الابن لو اتولّد تلقائياً ومش مستخدم."""
    result = get_object_or_404(DisassemblyResult, pk=result_id)
    event = result.event
    if event.is_executed:
        return JsonResponse({'error': 'event already executed'}, status=400)

    child = result.child_item
    result.delete()
    # الابن اتولّد للمسودة دي بس، لسه في المخزن، ومش ناتج من حدث تاني → امسحه
    if (child.status == InventoryItem.STATUS_IN_STOCK
            and not child.produced_as.exists()
            and not child.disassembly_events.exists()):
        child.delete()
    return JsonResponse({'ok': True, **_event_json(event)})


@login_required
@require_POST
def execute_event(request, event_id):
    """اعتماد المسودة → توزيع التكلفة بنزاهة صفرية وإقفال الأب."""
    event = get_object_or_404(DisassemblyEvent, pk=event_id)
    try:
        report = DisassemblyService.execute_disassembly(event)
    except ValidationError as exc:
        return JsonResponse({'error': getattr(exc, 'message', str(exc))}, status=400)
    report = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in report.items()}
    if 'children' in report:
        report['children'] = [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in c.items()}
            for c in report['children']
        ]
    return JsonResponse({'ok': True, 'report': report})
