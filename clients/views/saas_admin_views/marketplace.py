"""🛍️ لوحة مالك المنصة — مراجعة قطع السوق، طلبات نشر B2B، النزاعات والمدفوعات وطلبات السوق."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models import (
    PartListing, DisputeTicket,
)
from clients.permissions import widget_required
from ._common import _log_event, saas_admin_required

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Part Listings — moderation queue (admin approves before going live)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('b2b')
def parts_moderation_queue(request):
    """قائمة كل قطع الغيار اللي بتنتظر مراجعة الإدارة."""
    show = request.GET.get('show', 'pending')  # pending | rejected | all
    qs = PartListing.objects.filter(is_deleted=False).select_related(
        'car_make', 'seller_customer', 'seller_tenant',
    ).order_by('-created_at')
    if show == 'pending':
        qs = qs.filter(moderation_status='pending_approval')
    elif show == 'rejected':
        qs = qs.filter(moderation_status='rejected')
    # `all` → no extra filter
    return render(request, 'clients/saas_admin/parts_moderation_queue.html', {
        'listings': qs[:200],
        'show': show,
        'count_pending': PartListing.objects.filter(
            is_deleted=False, moderation_status='pending_approval',
        ).count(),
    })


@saas_admin_required
@widget_required('b2b')
def parts_moderation_approve(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    newly_approved = listing.approve(by_user=request.user)
    _log_event(
        'other', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"اعتماد قطعة غيار «{listing.title[:60]}» (id={listing.id})",
    )
    # 🐛 [FIX]: البائع (الفرد) ماكانش بيعرف إن قطعته اتنشرت — كان لازم يفتح «إعلاناتي» بالصدفة
    from clients.services.customer_notify import notify_customer
    if newly_approved:
        notify_customer(
            listing.seller_customer, title='✅ قطعتك اتنشرت',
            body=f'«{listing.title[:80]}» اتراجعت واتنشرت في سوق قطع الغيار.',
            level='success', icon='fa-circle-check', action_url='/marketplace/parts/my-listings/',
            action_label='إعلاناتي', sent_by=request.user)
    # 🐛 [FIX]: زرار «موافقة ونشر» في لوحة المشرف كان بيوديك لصفحة المراجعة ويسيب اللوحة —
    #    دلوقتي بيبعت AJAX (زي «رفض») والصف بيختفي مكانه.
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'message': f"تم اعتماد القطعة «{listing.title[:40]}» ونشرها."})
    messages.success(request, f"تم اعتماد القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_moderation_queue')


# ─────────────────────────────────────────────────────────────────────
# 🌐 طلبات نشر الشركات في سوق B2B (B2BListingRequest — جوه schema كل شركة)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('b2b')
def b2b_listing_requests(request):
    """🐛 [FIX]: الشركة تدوس «نشر في سوق التجار» والطلب مكانش بيظهر هنا خالص —
    لأنه بيتسجّل في schema الشركة. الصفحة دي بتجمع الطلبات من كل الشركات."""
    from clients.services import b2b_listing_review as svc
    show = request.GET.get('show', 'pending')
    if show not in ('pending', 'approved', 'rejected', 'all'):
        show = 'pending'
    return render(request, 'clients/saas_admin/b2b_listing_requests.html', {
        'rows': svc.collect_requests(status='' if show == 'all' else show),
        'show': show,
        'count_pending': svc.pending_count(),
    })


@saas_admin_required
@widget_required('b2b')
@require_POST
def b2b_listing_request_decide(request, schema_name, request_id):
    from clients.services import b2b_listing_review as svc
    action = (request.POST.get('action') or '').strip()
    label = request.user.get_full_name() or request.user.get_username()
    info = {}
    try:
        tenant, name = svc.decide(
            schema_name, request_id, action, reviewer_label=label,
            price=(request.POST.get('price') or '').strip() or None,
            notes=(request.POST.get('notes') or '')[:500], info=info,
        )
    except svc.ReviewError as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('saas_b2b_listing_requests')}?show=pending")
    verb = 'موافقة ونشر' if action == 'approve' else 'رفض'
    _log_event('other', tenant=tenant, user=request.user,
               description=f"🌐 سوق B2B: {verb} «{name[:60]}» (طلب #{request_id})")
    messages.success(request, f"تم {verb} «{name[:40]}» — {tenant.name}.")
    # 🐛 [FIX]: الموافقة على صنف رصيده صفر كانت بتعدّي بنجاح والصنف مابيظهرش في السوق
    #    من غير ولا كلمة — دلوقتي الأدمن بيعرف إنه هيظهر أول ما يدخله مخزون.
    if action == 'approve' and info.get('stock', 1) <= 0:
        messages.warning(request, f"«{name[:40]}» رصيده صفر عند {tenant.name} — "
                                  "مش هيظهر في سوق قطع الغيار غير لما يدخله مخزون (هيظهر لوحده).")
    return redirect(f"{reverse('saas_b2b_listing_requests')}?show=pending")


@saas_admin_required
@widget_required('b2b')
@require_POST
def b2b_listing_requests_approve_bulk(request):
    """✅ موافقة على المحدد (items=schema:id,...) أو على كل المعلّق (all=1) مرة واحدة —
    بسعر البيع الحالي لكل صنف (ولو صفر سعر الجملة)."""
    from clients.services import b2b_listing_review as svc
    back = f"{reverse('saas_b2b_listing_requests')}?show=pending"
    if request.POST.get('all') == '1':
        items = None
    else:
        items = []
        for tok in (request.POST.get('items') or '').split(','):
            schema, _, rid = tok.strip().partition(':')
            if schema and rid.isdigit():
                items.append((schema, int(rid)))
        if not items:
            messages.error(request, 'مفيش طلبات محددة.')
            return redirect(back)
    label = request.user.get_full_name() or request.user.get_username()
    res = svc.approve_many(items, reviewer_label=label)
    _log_event('other', user=request.user,
               description=f"🌐 سوق B2B: موافقة جماعية على {res['approved']} صنف "
                           f"({'، '.join(sorted(res['tenants']))[:200]})")
    msg = f"تمت الموافقة ونشر {res['approved']} صنف في السوق."
    if res.get('no_stock'):
        msg += f" منهم {res['no_stock']} رصيده صفر — هيظهر لوحده أول ما يدخله مخزون."
    if res['skipped']:
        msg += f" اتساب {res['skipped']} (اتراجع قبل كده أو سعره صفر — راجعه لوحده)."
    if res['failed']:
        msg += f" فشل {res['failed']} — شوف سجل الأخطاء."
    (messages.success if res['approved'] else messages.warning)(request, msg)
    return redirect(back)


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Part Listings — Active/Live marketplace control (Phase 3 #1)
#   Lists every approved & non-deleted listing with row actions:
#     • Edit  (admin override)
#     • Suspend (approved → suspended)
#     • Soft Delete (is_deleted=True)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('b2b')
def parts_active_listings(request):
    """Live marketplace — every listing currently visible to buyers."""
    q = (request.GET.get('q') or '').strip()
    qs = (
        PartListing.objects.filter(
            is_deleted=False, moderation_status='approved',
        )
        .select_related('car_make', 'seller_customer', 'seller_tenant')
        .order_by('-created_at')
    )
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(part_number__icontains=q)
            | Q(seller_tenant__name__icontains=q)
        )

    total_active = PartListing.objects.filter(
        is_deleted=False, moderation_status='approved',
    ).count()
    total_suspended = PartListing.objects.filter(
        is_deleted=False, moderation_status='suspended',
    ).count()
    return render(request, 'clients/saas_admin/parts_active_listings.html', {
        'listings': qs[:300],
        'q': q,
        'total_active': total_active,
        'total_suspended': total_suspended,
    })


@saas_admin_required
@widget_required('b2b')
def parts_listing_suspend(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    reason = (request.POST.get('reason') or '').strip()[:240]
    listing.moderation_status = 'suspended'
    listing.moderated_by = request.user
    listing.moderated_at = timezone.now()
    if hasattr(listing, 'rejection_reason'):
        listing.rejection_reason = reason or 'Suspended by admin'
    listing.save(update_fields=[
        'moderation_status', 'moderated_by', 'moderated_at', 'rejection_reason',
    ])
    logger.warning(
        "PartListing SUSPENDED id=%s by=%s reason=%s",
        listing.id, request.user.username, reason,
    )
    _log_event(
        'suspension', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"تعليق قطعة «{listing.title[:60]}» (id={listing.id}) — {reason or 'بدون سبب'}",
    )
    messages.success(request, f"تم تعليق القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_active_listings')


@saas_admin_required
@widget_required('b2b')
def parts_listing_soft_delete(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    if hasattr(listing, 'soft_delete'):
        listing.soft_delete(user=request.user, reason='Admin removed via active-listings table')
    else:
        listing.is_deleted = True
        listing.save(update_fields=['is_deleted'])
    logger.warning("PartListing SOFT-DELETED id=%s by=%s", listing.id, request.user.username)
    _log_event(
        'suspension', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"حذف قطعة «{listing.title[:60]}» (id={listing.id}) من السوق",
    )
    messages.success(request, f"تم حذف القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_active_listings')


@saas_admin_required
@widget_required('b2b')
def parts_listing_edit(request, listing_id):
    """Admin-side edit form for a single listing — covers the critical fields
    most often abused (title/price/qty/description). Heavier brand/model edits
    still go through the regular seller workflow."""
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    EDITABLE_FIELDS = ('title', 'part_number', 'price_egp', 'warranty_days', 'description', 'condition', 'city')

    if request.method == 'POST':
        for f in EDITABLE_FIELDS:
            if f in request.POST and hasattr(listing, f):
                raw = request.POST.get(f, '').strip()
                if f == 'price_egp':
                    try:
                        setattr(listing, f, Decimal(raw))
                    except (InvalidOperation, ValueError):
                        messages.error(request, f"🛑 قيمة السعر غير صالحة: {raw}")
                        return redirect('saas_parts_listing_edit', listing_id=listing.id)
                elif f == 'warranty_days':
                    try:
                        setattr(listing, f, int(raw))
                    except ValueError:
                        messages.error(request, f"🛑 فترة الضمان غير صالحة: {raw}")
                        return redirect('saas_parts_listing_edit', listing_id=listing.id)
                else:
                    setattr(listing, f, raw)
        try:
            listing.full_clean()
            listing.save()
            messages.success(request, "تم حفظ التعديلات.")
            return redirect('saas_parts_active_listings')
        except Exception as e:
            messages.error(request, f"🛑 خطأ في الحفظ: {e}")
            return redirect('saas_parts_listing_edit', listing_id=listing.id)

    return render(request, 'clients/saas_admin/parts_listing_edit.html', {
        'listing': listing,
        'editable_fields': EDITABLE_FIELDS,
    })


@saas_admin_required
@widget_required('b2b')
def parts_moderation_reject(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    import json as _json
    try:
        body = _json.loads(request.body)
        reason = (body.get('reason') or '').strip()
    except Exception:
        reason = (request.POST.get('reason') or '').strip()
    listing.reject(by_user=request.user, reason=reason)
    _log_event(
        'other', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"رفض قطعة «{listing.title[:60]}» (id={listing.id}) — {reason or 'بدون سبب'}",
    )
    # 🐛 [FIX]: البائع ماكانش بيتبلّغ بالرفض ولا سببه
    from clients.services.customer_notify import notify_customer
    notify_customer(
        listing.seller_customer, title='❌ قطعتك مااتنشرتش',
        body=f'«{listing.title[:80]}» اترفضت' + (f': {reason[:200]}' if reason else '.') + ' تقدر تعدّلها وتبعتها تاني.',
        level='warning', icon='fa-circle-xmark', action_url='/marketplace/parts/my-listings/',
        action_label='إعلاناتي', sent_by=request.user)
    if request.headers.get('Content-Type', '').startswith('application/json') or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        from django.http import JsonResponse
        return JsonResponse({'message': f"تم رفض القطعة «{listing.title[:40]}»."})
    messages.success(request, f"تم رفض القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_moderation_queue')


# ─────────────────────────────────────────────────────────────────────
# ⚖️ Dispute resolution centre
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('escrow')
def disputes_queue(request):
    """All open / under-review dispute tickets."""
    show = request.GET.get('show', 'open')
    qs = DisputeTicket.objects.filter(is_deleted=False).select_related(
        'order', 'order__listing', 'opened_by_customer', 'opened_by_tenant',
    ).order_by('-opened_at')
    if show == 'open':
        qs = qs.filter(status__in=['open', 'under_review'])
    elif show == 'resolved':
        qs = qs.filter(status__in=['resolved_refund', 'resolved_release', 'resolved_split'])
    return render(request, 'clients/saas_admin/disputes_queue.html', {
        'tickets': qs[:200],
        'show': show,
        'count_open': DisputeTicket.objects.filter(
            is_deleted=False, status__in=['open', 'under_review'],
        ).count(),
    })


@saas_admin_required
@widget_required('escrow')
def payouts_queue(request):
    """
    💸 Transfers the platform owes after escrow settlements (seller payouts
    and buyer refunds) — send the money, then record the transfer reference.
    """
    from django.db.models import Sum
    from clients.models import MarketplacePayout
    show = request.GET.get('show', 'pending')
    qs = MarketplacePayout.objects.select_related(
        'customer', 'tenant', 'order', 'order__listing', 'paid_by',
    ).order_by('created_at' if show == 'pending' else '-created_at')
    if show in ('pending', 'paid', 'cancelled'):
        qs = qs.filter(status=show)
    pending = MarketplacePayout.objects.filter(status='pending')
    return render(request, 'clients/saas_admin/payouts_queue.html', {
        'payouts': qs[:300],
        'show': show,
        'count_pending': pending.count(),
        'total_pending': pending.aggregate(s=Sum('amount'))['s'] or 0,
        'count_missing_details': pending.filter(
            customer__isnull=False, customer__payout_account='',
        ).count(),
    })


@saas_admin_required
@widget_required('escrow')
def payout_mark_paid(request, payout_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    from django.core.exceptions import ValidationError as DjVE
    from clients.models import MarketplacePayout
    from marketplace_b2b.services.payouts import mark_paid
    payout = get_object_or_404(MarketplacePayout, pk=payout_id)
    try:
        mark_paid(
            payout, by_user=request.user,
            reference=request.POST.get('reference') or '',
            notes=request.POST.get('notes') or '',
        )
    except DjVE as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect('saas_payouts_queue')
    _log_event(
        'other', tenant=payout.tenant, user=request.user,
        description=f"💸 تحويل {payout.get_kind_display()} {payout.amount} → {payout.recipient_label} "
                    f"(طلب {payout.order.order_code})",
    )
    messages.success(request, f"تم تسجيل تحويل {payout.amount} لـ {payout.recipient_label}.")
    return redirect('saas_payouts_queue')


def _notify_dispute_outcome(ticket, action, notes=''):
    from clients.models import CustomerNotification
    order = ticket.order
    outcome = {
        'refund':  'تم رد المبلغ للمشتري.',
        'release': 'تم تحرير المبلغ للبائع.',
        'split':   'تمت تسوية جزئية بين الطرفين.',
        'cancel':  'تم إلغاء النزاع ورجع الطلب لحالته.',
    }.get(action, '')
    body = f'قرار الإدارة في نزاع «{order.listing.title[:60]}»: {outcome}' + (f' ملاحظات: {notes[:200]}' if notes else '')
    for customer, url in (
        (order.buyer_customer, '/marketplace/parts/orders/'),
        (order.listing.seller_customer, '/marketplace/parts/sales/'),
    ):
        if customer is None:
            continue
        try:
            CustomerNotification.objects.create(
                customer=customer, title='⚖️ تم البت في النزاع', body=body,
                level='info', icon='fa-scale-balanced',
                action_url=url, action_label='تفاصيل الطلب',
            )
        except Exception:
            logger.exception("dispute outcome notification failed (ticket %s)", ticket.pk)


@saas_admin_required
@widget_required('escrow')
def dispute_resolve(request, ticket_id):
    """POST endpoint — action ∈ {refund, release, split, cancel}."""
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    ticket = get_object_or_404(
        DisputeTicket.objects.filter(is_deleted=False).select_related('order'),
        pk=ticket_id,
    )
    action = (request.POST.get('action') or '').strip()
    notes = (request.POST.get('notes') or '').strip()
    from clients.services import disputes as dispute_svc
    from django.core.exceptions import ValidationError as DjVE
    try:
        if action == 'refund':
            reason = (request.POST.get('return_reason') or 'defective').strip()
            dispute_svc.resolve_with_refund(
                ticket, return_reason=reason, by_user=request.user, notes=notes,
            )
        elif action == 'release':
            dispute_svc.resolve_with_release(
                ticket, by_user=request.user, notes=notes,
            )
        elif action == 'split':
            from decimal import Decimal as _D, InvalidOperation as _BadDec
            try:
                amt = _D((request.POST.get('refund_amount') or '0').strip())
            except _BadDec:
                raise DjVE("مبلغ الاسترداد لازم يكون رقم.")
            reason = (request.POST.get('return_reason') or 'not_as_described').strip()
            dispute_svc.resolve_with_split(
                ticket, refund_amount=amt, return_reason=reason,
                by_user=request.user, notes=notes,
            )
        elif action == 'cancel':
            dispute_svc.cancel_dispute(ticket, by_role=ticket.opened_by_role, notes=notes)
        else:
            messages.error(request, f"إجراء غير معروف: {action}")
            return redirect('saas_disputes_queue')
    except DjVE as exc:
        messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))
        return redirect('saas_disputes_queue')
    _log_event(
        'other', tenant=getattr(ticket, 'opened_by_tenant', None), user=request.user,
        description=f"⚖️ نزاع #{ticket.id} → إجراء «{action}»" + (f" — {notes}" if notes else ''),
    )
    # 🐛 [FIX]: الطرفين ماكانوش بيعرفوا قرار النزاع إلا لو فتحوا الطلب بالصدفة.
    _notify_dispute_outcome(ticket, action, notes)
    messages.success(request, f"تم تنفيذ '{action}' على التذكرة.")
    return redirect('saas_disputes_queue')


# ─────────────────────────────────────────────────────────────────────
# 🛍️ Marketplace Requests — full management (all statuses)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('b2b')
def marketplace_requests_list(request):
    """قائمة كل طلبات السوق مع فلتر بالحالة + بحث."""
    from clients.models import ServiceRequest
    from django.http import JsonResponse
    import json

    status_filter = request.GET.get('status', '')
    sector_filter = request.GET.get('sector', '')
    q = request.GET.get('q', '').strip()

    qs = ServiceRequest.objects.select_related('customer').order_by('-created_at')
    if status_filter:
        qs = qs.filter(status=status_filter)
    if sector_filter:
        qs = qs.filter(sector=sector_filter)
    if q:
        qs = qs.filter(
            Q(title__icontains=q) | Q(customer__full_name__icontains=q) | Q(customer__phone__icontains=q)
        )

    # Handle AJAX status-change
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except Exception:
            data = {}
        action = data.get('action')
        req_id = data.get('id')
        if not req_id:
            return JsonResponse({'error': 'id مطلوب'}, status=400)
        svc = get_object_or_404(ServiceRequest, pk=req_id)
        from datetime import timedelta as td
        from django.utils import timezone as tz

        if action == 'approve':
            svc.status = 'open'
            svc.is_approved = True
            expiry_map = {'urgent': 1, 'soon': 3, 'normal': 7}
            days = expiry_map.get(svc.urgency, 7)
            svc.expires_at = tz.now() + td(days=days)
            svc.save(update_fields=['status', 'is_approved', 'expires_at'])
            return JsonResponse({'status': 'ok', 'message': 'تم الموافقة ونشره للتجار.'})
        elif action == 'reject':
            svc.status = 'rejected_by_admin'
            svc.admin_notes = data.get('reason', '')
            svc.save(update_fields=['status', 'admin_notes'])
            return JsonResponse({'status': 'ok', 'message': 'تم رفض الطلب.'})
        elif action == 'close':
            svc.status = 'closed'
            svc.save(update_fields=['status'])
            return JsonResponse({'status': 'ok', 'message': 'تم إغلاق الطلب.'})
        elif action == 'reopen':
            svc.status = 'pending_approval'
            svc.save(update_fields=['status'])
            return JsonResponse({'status': 'ok', 'message': 'تم إعادة فتح الطلب للمراجعة.'})
        elif action == 'set_open':
            svc.status = 'open'
            svc.is_approved = True
            svc.save(update_fields=['status', 'is_approved'])
            return JsonResponse({'status': 'ok', 'message': 'تم تفعيل الطلب مباشرة.'})
        else:
            return JsonResponse({'error': 'إجراء غير معروف'}, status=400)

    counts = {
        'all': ServiceRequest.objects.count(),
        'pending_approval': ServiceRequest.objects.filter(status='pending_approval').count(),
        'open': ServiceRequest.objects.filter(status='open').count(),
        'accepted': ServiceRequest.objects.filter(status='accepted').count(),
        'completed': ServiceRequest.objects.filter(status='completed').count(),
        'rejected': ServiceRequest.objects.filter(status='rejected_by_admin').count(),
        'closed': ServiceRequest.objects.filter(status='closed').count(),
        'expired': ServiceRequest.objects.filter(status='expired').count(),
    }

    return render(request, 'clients/saas_admin/marketplace_requests.html', {
        'requests': qs[:200],
        'counts': counts,
        'status_filter': status_filter,
        'sector_filter': sector_filter,
        'q': q,
        'status_choices': [
            ('', 'كل الطلبات'),
            ('pending_approval', 'في انتظار الموافقة'),
            ('open', 'مفتوح للتجار'),
            ('accepted', 'تم قبول عرض'),
            ('completed', 'مكتمل'),
            ('rejected_by_admin', 'مرفوض'),
            ('closed', 'مغلق'),
            ('expired', 'منتهي الصلاحية'),
        ],
    })
