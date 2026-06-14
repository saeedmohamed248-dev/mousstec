"""
🤖 Printing AI Studio Views
==============================
AI-powered design generation and smart watermark for printing tenants.
Gated by TenantSubscription + AILimitTracker.
"""
import logging
import base64
import json
import re
from io import BytesIO
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')



# AI studio history, sessions, favorites, attachments.

from .utils import *  # noqa: F401, F403



# =====================================================================
# 💾 AI Studio History & Sessions
# =====================================================================

@login_required
def ai_studio_history(request):
    """عرض كل التصاميم اللي العميل عملها قبل كده."""
    from clients.models import AIStudioSession

    tenant = _get_tenant()
    if not tenant:
        return render(request, 'printing/ai_history.html', {'sessions': [], 'tenant': None})

    sessions = AIStudioSession.objects.filter(tenant=tenant).order_by('-created_at')[:100]

    return render(request, 'printing/ai_history.html', {
        'sessions': sessions,
        'tenant': tenant,
        'total': sessions.count(),
    })


@login_required
def ai_studio_history_api(request):
    """JSON list of past sessions for the AI Studio modal."""
    from clients.models import AIStudioSession

    tenant = _get_tenant()
    if not tenant:
        return JsonResponse({'sessions': []})

    qs = AIStudioSession.objects.filter(tenant=tenant).order_by('-created_at')[:50]
    sessions = [{
        'id': s.pk,
        'raw_input': s.raw_input[:120],
        'design_category': s.design_category,
        'image_url': s.watermarked_image_url or s.image_url,
        'logo_used': s.logo_used,
        'watermarked': s.watermarked,
        'is_favorite': s.is_favorite,
        'created_at': s.created_at.strftime('%Y-%m-%d %H:%M'),
        'model_used': s.model_used,
    } for s in qs]

    return JsonResponse({'sessions': sessions, 'count': len(sessions)})


@csrf_exempt
@login_required
@require_POST
def ai_session_toggle_favorite(request, session_id):
    """مفضّل / إلغاء مفضّل لجلسة."""
    from clients.models import AIStudioSession
    tenant = _get_tenant()
    session = get_object_or_404(AIStudioSession, pk=session_id, tenant=tenant)
    session.is_favorite = not session.is_favorite
    session.save(update_fields=['is_favorite'])
    return JsonResponse({'success': True, 'is_favorite': session.is_favorite})


@csrf_exempt
@login_required
@require_POST
def ai_session_delete(request, session_id):
    """حذف جلسة من السجل."""
    from clients.models import AIStudioSession
    tenant = _get_tenant()
    session = get_object_or_404(AIStudioSession, pk=session_id, tenant=tenant)
    session.delete()
    return JsonResponse({'success': True})


@login_required
def ai_attach_search(request):
    """🔍 بحث عن طلب طباعة لربطه بتصميم AI Studio."""
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'invoices': []})

    try:
        from printing.models import PrintOrder
    except ImportError:
        return JsonResponse({'invoices': [], 'error': 'PrintOrder model not found'})

    # ⚠️ Fixed: search through PrintOrder (which has the customer relation)
    qs = PrintOrder.objects.select_related('customer').filter(
        Q(customer__name__icontains=query) |
        Q(customer__phone__icontains=query) |
        Q(order_number__icontains=query)
    ).order_by('-date_created')[:15]

    invoices = []
    for order in qs:
        invoices.append({
            'id': order.pk,
            'code': order.order_number or f'PO-{order.pk}',
            'customer': (order.customer.name if order.customer else '—'),
            'date': order.date_created.strftime('%Y-%m-%d') if order.date_created else '',
            'total': str(order.total_amount or 0),
            'status': order.get_status_display(),
        })
    return JsonResponse({'invoices': invoices})


@csrf_exempt
@login_required
@require_POST
def ai_session_attach(request, session_id):
    """🔗 ربط جلسة AI Studio بطلب طباعة."""
    from clients.models import AIStudioSession
    tenant = _get_tenant()
    session = get_object_or_404(AIStudioSession, pk=session_id, tenant=tenant)

    invoice_id = request.POST.get('invoice_id')
    if not invoice_id:
        return JsonResponse({'success': False, 'error': 'invoice_id required'}, status=400)

    try:
        from printing.models import PrintOrder
        order = PrintOrder.objects.get(pk=invoice_id)
    except PrintOrder.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'الطلب غير موجود'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'خطأ: {e}'}, status=500)

    # Append to notes (PrintOrder has 'notes' field per migration 0001)
    attached = False
    if hasattr(order, 'notes'):
        order.notes = (order.notes or '') + f"\n\n🎨 تصميم AI Studio (جلسة #{session.pk}): {session.image_url}"
        order.save(update_fields=['notes'])
        attached = True

    logger.info(f"🔗 [AI ATTACH]: Session #{session.pk} attached to PrintOrder #{order.pk} by {request.user.username}")
    return JsonResponse({
        'success': True,
        'message': f'تم ربط التصميم بالطلب {order.order_number} بنجاح',
        'invoice_id': order.pk,
        'attached': attached,
    })
