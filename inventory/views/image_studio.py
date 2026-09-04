"""
🎨 AI Image Studio — Views (HTTP adapters)
=====================================================================
واجهات استوديو الخلفيات الذكي. الـ views هنا مجرد طبقة HTTP رفيعة بتنادي
inventory.services.image_studio — كل منطق المعالجة في الـ service layer.

الصلاحيات: تسجيل دخول + tenant + دور إداري/مخزن (admin/manager/stock).
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.http import require_POST

from ..models import Product
from ..services import image_studio as studio
from .utils import (
    _get_branch_for_user, _json_response_safe, role_required, tenant_required,
)

_STUDIO_ROLES = ('admin', 'manager', 'stock')


def _payload(request) -> dict:
    """يقرأ POST body سواء form-encoded أو JSON."""
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body or b'{}')
        except (ValueError, TypeError):
            return {}
    return request.POST.dict()


@login_required(login_url='/login/')
@tenant_required
@role_required(*_STUDIO_ROLES)
def image_studio(request):
    """صفحة الاستوديو — اختيار قطعة وتغيير خلفيتها بالـ AI."""
    q = (request.GET.get('q') or '').strip()
    qs = Product.objects.filter(is_active=True, image__isnull=False).exclude(image='')
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(part_number__icontains=q)
            | Q(brand__icontains=q) | Q(car_model__icontains=q)
        )
    qs = qs.order_by('-image_ai_bg_applied', 'name')
    page = Paginator(qs, 24).get_page(request.GET.get('page'))

    focus = None
    focus_id = request.GET.get('product')
    if focus_id:
        focus = Product.objects.filter(id=focus_id).first()

    return render(request, 'inventory/image_studio.html', {
        'page': page,
        'products': page.object_list,
        'focus': focus,
        'q': q,
        'presets': studio.list_presets(),
        'branch': _get_branch_for_user(request.user),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required(*_STUDIO_ROLES)
@require_POST
def image_studio_generate(request):
    """يولّد معاينة خلفية جديدة لقطعة (بدون تطبيق)."""
    data = _payload(request)
    product = Product.objects.filter(id=data.get('product_id')).first()
    if product is None:
        return _json_response_safe({'error': 'القطعة غير موجودة.'}, status=404)

    result = studio.generate_preview(
        product,
        preset_key=(data.get('preset') or studio.DEFAULT_PRESET),
        custom_prompt=(data.get('custom_prompt') or ''),
    )
    if not result.get('ok'):
        return _json_response_safe(
            {'error': result.get('detail') or 'فشل توليد الخلفية.'},
            status=502 if result.get('error') != 'no_image' else 400,
        )
    return _json_response_safe(result)


@login_required(login_url='/login/')
@tenant_required
@role_required(*_STUDIO_ROLES)
@require_POST
def image_studio_apply(request):
    """يطبّق المعاينة كصورة رسمية للقطعة (مع نسخة احتياطية للأصل)."""
    data = _payload(request)
    product = Product.objects.filter(id=data.get('product_id')).first()
    if product is None:
        return _json_response_safe({'error': 'القطعة غير موجودة.'}, status=404)

    result = studio.apply_preview(
        product,
        preview_path=(data.get('preview_path') or ''),
        preset_key=(data.get('preset') or ''),
    )
    if not result.get('ok'):
        return _json_response_safe(
            {'error': result.get('detail') or 'فشل تطبيق الصورة.'}, status=400)
    return _json_response_safe(result)


@login_required(login_url='/login/')
@tenant_required
@role_required(*_STUDIO_ROLES)
@require_POST
def image_studio_revert(request):
    """يرجّع الصورة الأصلية المحفوظة قبل معالجة الـ AI."""
    data = _payload(request)
    product = Product.objects.filter(id=data.get('product_id')).first()
    if product is None:
        return _json_response_safe({'error': 'القطعة غير موجودة.'}, status=404)

    result = studio.revert(product)
    if not result.get('ok'):
        return _json_response_safe(
            {'error': result.get('detail') or 'فشل استرجاع الصورة.'}, status=400)
    return _json_response_safe(result)
