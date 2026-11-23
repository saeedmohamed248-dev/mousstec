"""
✅ سوق الطلبات — موافقة/رفض مالك المنصة على طلبات العملاء.

اتفصلت من marketplace_core_views.py لما زاد عن حد حجم الملفات؛
بتتصدّر من clients/views/__init__.py زي ما هي، فالـ URLs ماتغيّرتش.
"""
from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from clients.models import ServiceRequest
from clients.permissions import platform_owner_required

from ._shared import _notify_merchants_of_new_request
from .marketplace_core_views import _notify_customer


# ------------------------------------------------------------------
# ✅ Super Admin: Approve / Reject marketplace requests
# ------------------------------------------------------------------

@login_required
@platform_owner_required
def marketplace_admin_approve(request, request_id):
    """موافقة سوبر أدمن على طلب سوق."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    svc_request = get_object_or_404(ServiceRequest, pk=request_id)
    if svc_request.status != 'pending_approval':
        return JsonResponse({"error": "الطلب ليس في انتظار الموافقة"}, status=400)

    # Approve and make it live
    svc_request.status = 'open'
    svc_request.is_approved = True
    # Reset expiry from now (since it was waiting for approval)
    expiry_map = {'urgent': 1, 'soon': 3, 'normal': 7}
    days = expiry_map.get(svc_request.urgency, 7)
    svc_request.expires_at = timezone.now() + timedelta(days=days)
    svc_request.save(update_fields=['status', 'is_approved', 'expires_at'])

    # 🔔 Now notify merchants
    _notify_merchants_of_new_request(svc_request)
    _notify_customer(
        svc_request.customer,
        title='✅ طلبك اتنشر للتجار',
        body=f'«{svc_request.title[:80]}» اتوافق عليه وبقى ظاهر للتجار — هتوصلك العروض هنا.',
        level='success', icon='fa-bullhorn',
        action_url=f'/marketplace/request/{svc_request.request_code}/', action_label='متابعة الطلب',
    )

    return JsonResponse({"status": "success", "message": "تم الموافقة على الطلب ونشره للتجار."})


@login_required
@platform_owner_required
def marketplace_admin_reject(request, request_id):
    """رفض سوبر أدمن لطلب سوق."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    svc_request = get_object_or_404(ServiceRequest, pk=request_id)
    if svc_request.status != 'pending_approval':
        return JsonResponse({"error": "الطلب ليس في انتظار الموافقة"}, status=400)

    try:
        data = json.loads(request.body) if request.body else {}
        reason = str(data.get('reason', '') if isinstance(data, dict) else '')
    except (json.JSONDecodeError, UnicodeDecodeError):
        reason = request.POST.get('reason', '')
    svc_request.status = 'rejected_by_admin'
    svc_request.admin_notes = reason[:2000]
    svc_request.save(update_fields=['status', 'admin_notes'])
    _notify_customer(
        svc_request.customer,
        title='❌ طلبك مااتنشرش',
        body=f'«{svc_request.title[:80]}» اترفض من الإدارة' + (f': {reason[:200]}' if reason else '.')
             + ' تقدر تعدّله وتبعته تاني.',
        level='warning', icon='fa-circle-xmark',
        action_url='/marketplace/dashboard/', action_label='طلباتي',
    )

    return JsonResponse({"status": "success", "message": "تم رفض الطلب."})
