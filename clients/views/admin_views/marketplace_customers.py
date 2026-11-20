"""🛍️ أدوات السوبر أدمن — عملاء السوق: التفاصيل، الحذف/الهدية/الإشعار، الإشعارات، الحظر."""
from __future__ import annotations

import logging
from decimal import Decimal

from clients.permissions import platform_owner_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render

from clients.models import (
    CustomerNotification,
    MarketplaceCustomer,
    PlatformEvent,
)

from ._common import PAID_PURCHASE_STATUSES, _require_superadmin

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()


# =====================================================================
# 👤 تفاصيل عميل السوق (Marketplace Customer Detail - AJAX HTML)
# =====================================================================

@login_required
@platform_owner_required
def super_admin_customer_detail(request, customer_id):
    """يُرجع HTML بتفاصيل العميل + كل نشاطه (designs, purchases, requests, chats)."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    designs = list(customer.designs.select_related('purchase__package').order_by('-created_at')[:50])
    purchases = list(customer.design_purchases.select_related('package').order_by('-created_at')[:20])
    requests_list = list(customer.requests.order_by('-created_at')[:20])

    # إجماليات
    # 🐛 [FIX]: الباقة اللي اتدفعت واتصرفت كلها (exhausted) ماكانتش بتتحسب في «إجمالي المدفوع»
    total_spent = sum((p.price_paid for p in purchases if p.status in PAID_PURCHASE_STATUSES), Decimal('0'))
    total_designs = len(designs)
    total_purchases = len(purchases)

    # 🧭 رحلة العميل على الموقع (الصفحات اللي فتحها وآخر صفحة وقف عندها)
    from clients.services.customer_journey import build_customer_journey
    journey = build_customer_journey(customer)

    return render(request, 'clients/super_admin_customer_detail.html', {
        'journey': journey,
        'customer': customer,
        'designs': designs,
        'purchases': purchases,
        'requests_list': requests_list,
        'total_spent': total_spent,
        'total_designs': total_designs,
        'total_purchases': total_purchases,
    })


# =====================================================================
# 🛍️ Marketplace Customer admin actions — Delete / Gift / Notify
# =====================================================================

@login_required
@platform_owner_required
def super_admin_customer_delete(request, customer_id):
    """
    🗑️ Hard-delete a MarketplaceCustomer + all related data.
    Requires POST with confirm_phone matching the customer's phone number.
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    confirm_phone = (request.POST.get('confirm_phone') or '').strip()
    if confirm_phone != customer.phone:
        return JsonResponse({
            'error': 'رقم التأكيد لا يطابق رقم العميل.',
        }, status=400)

    label = customer.company_name or customer.full_name
    phone = customer.phone
    try:
        with transaction.atomic():
            customer.delete()  # cascades through notifications, designs, purchases, requests
            PlatformEvent.objects.create(
                event_type='other',
                tenant_schema='public',
                tenant_name='marketplace',
                user_name=request.user.username,
                description=f"🗑️ حذف عميل سوق «{label}» ({phone}) نهائياً.",
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("[SUPER ADMIN] Failed to delete customer %s: %s", customer_id, exc)
        return JsonResponse({'error': f'فشل الحذف: {exc}'}, status=500)

    return JsonResponse({
        'ok': True,
        'message': f'تم حذف العميل «{label}» نهائياً.',
    })


@login_required
@platform_owner_required
def super_admin_customer_gift(request, customer_id):
    """
    🎁 Grant free design credits to a marketplace customer.
    POST: designs=<int>, reason=<str>
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    try:
        designs = max(int(request.POST.get('designs', 0) or 0), 0)
    except ValueError:
        designs = 0
    reason = (request.POST.get('reason') or '').strip()

    if designs <= 0:
        return JsonResponse({'error': 'لازم تحدد عدد تصاميم أكبر من صفر.'}, status=400)

    with transaction.atomic():
        # Grant: increase total free designs (does not affect already-used count).
        customer.free_designs_total = (customer.free_designs_total or 0) + designs
        customer.save(update_fields=['free_designs_total'])

        # Notify the customer in-app
        CustomerNotification.objects.create(
            customer=customer,
            title=f'🎁 هدية: {designs} تصميم مجاني',
            body=reason or f'تم منحك {designs} تصميم مجاني من إدارة منصة Mouss Tec. اضغط للتصميم الآن.',
            level='success',
            icon='fa-gift',
            action_url='/marketplace/design-store/',
            action_label='ابدأ التصميم',
            sent_by=request.user,
        )

        PlatformEvent.objects.create(
            event_type='other', tenant_schema='public', tenant_name='marketplace',
            user_name=request.user.username,
            description=f"🎁 هدية {designs} تصميم لعميل سوق «{customer.full_name}» ({customer.phone})",
        )

    return JsonResponse({
        'ok': True,
        'message': f'تم منح «{customer.full_name}» هدية {designs} تصميم مجاني.',
        'new_total': customer.free_designs_total,
        'remaining': customer.free_designs_remaining,
    })


@login_required
@platform_owner_required
def super_admin_customer_notify(request, customer_id):
    """
    🔔 Send an in-app notification to a marketplace customer.
    POST: title, body, level (info|success|warning|danger), icon, action_url, action_label
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    title = (request.POST.get('title') or '').strip()
    body = (request.POST.get('body') or '').strip()
    level = (request.POST.get('level') or 'info').strip()
    icon = (request.POST.get('icon') or 'fa-bell').strip()
    action_url = (request.POST.get('action_url') or '').strip()
    action_label = (request.POST.get('action_label') or '').strip()

    if not title or not body:
        return JsonResponse({'error': 'العنوان والنص مطلوبين.'}, status=400)
    if level not in {'info', 'success', 'warning', 'danger'}:
        level = 'info'

    notif = CustomerNotification.objects.create(
        customer=customer,
        title=title[:200],
        body=body,
        level=level,
        icon=icon[:50] or 'fa-bell',
        action_url=action_url[:300],
        action_label=action_label[:80],
        sent_by=request.user,
    )

    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='marketplace',
        user_name=request.user.username,
        description=f"🔔 إشعار «{title[:60]}» إلى عميل «{customer.full_name}» ({customer.phone})",
    )

    return JsonResponse({
        'ok': True,
        'message': 'تم إرسال الإشعار.',
        'notification_id': notif.pk,
    })


# =====================================================================
# 🔔 Customer-facing notifications API — used by /marketplace/ dashboard
# =====================================================================

def customer_notifications_list(request):
    """Return the current marketplace customer's notifications as JSON."""
    from clients.views._shared import _marketplace_auth
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'not_authenticated'}, status=401)

    qs = customer.notifications.order_by('-created_at')[:30]
    items = [{
        'id': n.pk,
        'title': n.title,
        'body': n.body,
        'level': n.level,
        'icon': n.icon or 'fa-bell',
        'action_url': n.action_url,
        'action_label': n.action_label,
        'created_at': n.created_at.isoformat(),
        'is_read': n.is_read,
    } for n in qs]
    unread = customer.notifications.filter(read_at__isnull=True).count()
    return JsonResponse({'items': items, 'unread': unread})


def customer_notification_read(request, notification_id):
    """Mark a single notification as read."""
    from clients.views._shared import _marketplace_auth
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'not_authenticated'}, status=401)
    try:
        n = customer.notifications.get(pk=notification_id)
    except CustomerNotification.DoesNotExist:
        return JsonResponse({'error': 'not_found'}, status=404)
    n.mark_read()
    return JsonResponse({'ok': True})


# =====================================================================
# 🚫 حظر / فك حظر عميل سوق من اللوحة
# =====================================================================
@login_required
@platform_owner_required
def super_admin_customer_block(request, customer_id):
    """🐛 [FIX]: اللوحة بتعرض «محظور» وعدد المحظورين، بس الحظر نفسه كان من Django admin بس."""
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    customer.is_blocked = not customer.is_blocked
    customer.save(update_fields=['is_blocked'])
    label = customer.company_name or customer.full_name
    PlatformEvent.objects.create(
        event_type='fraud_flag' if customer.is_blocked else 'other', tenant_schema='public',
        tenant_name='marketplace', user_name=request.user.username,
        description=f"{'🚫 حظر' if customer.is_blocked else '✅ فك حظر'} عميل سوق «{label}» ({customer.phone})",
    )
    return JsonResponse({'ok': True, 'is_blocked': customer.is_blocked,
                         'message': (f'تم حظر «{label}» — مش هيقدر يدخل السوق.' if customer.is_blocked
                                     else f'تم فك حظر «{label}».')})
