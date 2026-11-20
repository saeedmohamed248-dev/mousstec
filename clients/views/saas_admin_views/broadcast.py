"""📢 لوحة مالك المنصة — حملات الإيميل والبانرات."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import messages
from erp_core.http_utils import safe_back as _safe_back
from django.shortcuts import redirect, render
from django.utils import timezone

from clients.models import (
    Plan, BroadcastCampaign,
)
from clients.permissions import widget_required
from ._common import _log_event, saas_admin_required

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 📢 Email Broadcast — composer + history
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tenants')
def broadcast_list(request):
    """صفحة قائمة الحملات + composer للحملة الجديدة."""
    from clients.services.broadcast import resolve_audience

    campaigns = BroadcastCampaign.objects.order_by('-created_at')[:100]

    # القيم الافتراضية للـ composer
    preview_audience = (request.GET.get('audience') or 'all').strip()
    preview_plan = (request.GET.get('plan') or '').strip()
    try:
        preview_qs = resolve_audience(preview_audience, plan=preview_plan)
        preview_count = preview_qs.count()
        preview_sample = list(preview_qs.values_list('name', 'email', 'schema_name')[:5])
    except Exception:
        preview_count = 0
        preview_sample = []

    plans = list(
        Plan.objects.filter(is_active=True).values('slug', 'name').order_by('name')
    )

    return render(request, 'clients/saas_admin/broadcast_list.html', {
        'campaigns': campaigns,
        'audience_choices': BroadcastCampaign.AUDIENCE_CHOICES,
        'preview_audience': preview_audience,
        'preview_plan': preview_plan,
        'preview_count': preview_count,
        'preview_sample': preview_sample,
        'plans': plans,
    })


@saas_admin_required
@widget_required('tenants')
def broadcast_send(request):
    """POST: ينشئ حملة + يبعتها (sync — لـ MVP). للأحجام الكبيرة ننقلها لـ Celery لاحقاً."""
    if request.method != 'POST':
        return redirect('saas_broadcast_list')

    subject = (request.POST.get('subject') or '').strip()
    body = (request.POST.get('body') or '').strip()
    audience = (request.POST.get('audience') or 'all').strip()
    plan = (request.POST.get('plan') or '').strip()
    confirm = (request.POST.get('confirm') or '').strip()
    send_email = request.POST.get('send_email') == 'on'
    show_in_app = request.POST.get('show_in_app') == 'on'
    in_app_severity = (request.POST.get('in_app_severity') or 'info').strip()
    in_app_days = request.POST.get('in_app_days') or '14'

    if not subject or not body:
        messages.error(request, "الموضوع والنص مطلوبان.")
        return redirect('saas_broadcast_list')
    if confirm != 'SEND':
        messages.error(request, "اكتب SEND في خانة التأكيد للإرسال.")
        return redirect('saas_broadcast_list')
    if not send_email and not show_in_app:
        messages.error(request, "اختر قناة واحدة على الأقل: إيميل أو بانر داخل النظام.")
        return redirect('saas_broadcast_list')

    in_app_ends_at = None
    if show_in_app:
        try:
            days = max(1, min(int(in_app_days), 90))
        except (ValueError, TypeError):
            days = 14
        in_app_ends_at = timezone.now() + timedelta(days=days)

    campaign = BroadcastCampaign.objects.create(
        subject=subject[:200],
        body=body,
        audience=audience,
        audience_plan=plan,
        created_by=request.user,
        send_email=send_email,
        show_in_app=show_in_app,
        in_app_severity=in_app_severity if in_app_severity in ('info', 'success', 'warning', 'critical') else 'info',
        in_app_ends_at=in_app_ends_at,
    )

    # 🚀 لو محدش طلب إيميل، خلصنا — البانر بقى نشط دلوقتي
    if not send_email:
        campaign.status = 'sent'
        campaign.sent_at = timezone.now()
        campaign.save(update_fields=['status', 'sent_at'])
        _log_event(
            'other', user=request.user,
            description=f"📢 بث بانر «{subject[:80]}» (campaign #{campaign.id}) — بدون إيميل",
        )
        messages.success(request, f"✅ تم نشر البانر للشركات. مفيش إرسال إيميل.")
        return redirect('saas_broadcast_list')

    # 🚀 الإرسال في الخلفية عبر Celery — الـ view يرجع فوراً ومفيش 504
    try:
        from clients.tasks import send_broadcast_campaign
        send_broadcast_campaign.delay(campaign.id)
    except Exception as e:
        logger.exception("broadcast_send enqueue failed")
        campaign.status = 'failed'
        campaign.error_log = f"enqueue error: {type(e).__name__}: {e}"
        campaign.save(update_fields=['status', 'error_log'])
        messages.error(request, f"فشل بدء الإرسال: {e}")
        return redirect('saas_broadcast_list')

    _log_event(
        'other', user=request.user,
        description=f"📢 بدء بث جماعي «{subject[:80]}» (campaign #{campaign.id}) في الخلفية",
    )
    messages.success(
        request,
        f"✅ تم بدء إرسال الحملة «{subject[:60]}» في الخلفية. هتظهر النتيجة في القائمة لما تخلص."
    )
    return redirect('saas_broadcast_list')


@saas_admin_required
@widget_required('tenants')
def broadcast_retry(request, campaign_id):
    """إعادة إرسال حملة عالقة في sending أو فشلت. POST فقط."""
    if request.method != 'POST':
        return redirect('saas_broadcast_list')

    try:
        campaign = BroadcastCampaign.objects.get(pk=campaign_id)
    except BroadcastCampaign.DoesNotExist:
        messages.error(request, "الحملة مش موجودة.")
        return redirect('saas_broadcast_list')

    # نسمح بإعادة الإرسال للحملات العالقة (sending) أو الفاشلة فقط — مش المُرسلة
    if campaign.status not in ('sending', 'failed', 'draft'):
        messages.error(request, f"الحملة دي حالتها «{campaign.get_status_display()}» مش قابلة لإعادة الإرسال.")
        return redirect('saas_broadcast_list')

    was_stuck = campaign.is_stuck
    # نرجعها draft عشان send_campaign يقبلها
    campaign.status = 'draft'
    campaign.sent_count = 0
    campaign.failed_count = 0
    campaign.skipped_count = 0
    campaign.error_log = ''
    campaign.save(update_fields=['status', 'sent_count', 'failed_count', 'skipped_count', 'error_log'])

    # 🐛 [FIX]: لو الحملة كانت واقفة (عامل الخلفية مش شغال) إعادة الجدولة على Celery كانت هتقف
    #    تاني بنفس الشكل — فنبعتها مباشرة في thread من السيرفر نفسه.
    if was_stuck:
        import threading
        from clients.services.broadcast import send_campaign

        def _run(cid):
            from django.db import connection as _conn
            try:
                send_campaign(BroadcastCampaign.objects.get(pk=cid))
            except Exception as exc:  # noqa: BLE001
                logger.exception("broadcast direct send #%s failed", cid)
                BroadcastCampaign.objects.filter(pk=cid).update(
                    status='failed', error_log=f"{type(exc).__name__}: {exc}"[:2000])
            finally:
                _conn.close()
        threading.Thread(target=_run, args=(campaign.id,), daemon=True).start()
        messages.success(request, f"📤 بدأ إرسال «{campaign.subject[:60]}» مباشرة — حدّث الصفحة بعد دقيقة.")
        return redirect('saas_broadcast_list')

    try:
        from clients.tasks import send_broadcast_campaign
        send_broadcast_campaign.delay(campaign.id)
    except Exception as e:
        logger.exception("broadcast_retry enqueue failed")
        messages.error(request, f"فشل بدء الإرسال: {e}")
        return redirect('saas_broadcast_list')

    messages.success(request, f"🔄 تم إعادة جدولة الحملة «{campaign.subject[:60]}» للإرسال.")
    return redirect('saas_broadcast_list')


@saas_admin_required
@widget_required('tenants')
def broadcast_enable_banner(request, campaign_id):
    """🔔 حملة اتبعتت إيميل بس — اعرضها كمان كبانر جوه لوحات الشركات (14 يوم) من غير ما تتبعت تاني."""
    if request.method != 'POST':
        return redirect('saas_broadcast_list')
    from datetime import timedelta
    n = BroadcastCampaign.objects.filter(pk=campaign_id, show_in_app=False).update(
        show_in_app=True, in_app_starts_at=timezone.now(), in_app_ends_at=timezone.now() + timedelta(days=14))
    if n:
        messages.success(request, "🔔 الرسالة بقت ظاهرة كبانر في لوحات الشركات لمدة 14 يوم.")
    return redirect('saas_broadcast_list')


# =====================================================================
# 📢 Tenant-side: dismiss a broadcast banner (any logged-in tenant user)
# =====================================================================
def broadcast_banner_dismiss(request, campaign_id):
    """يستدعيها user من tenant عشان يقفل البانر — يتسجّل إخفاء واحد لكل user/campaign."""
    if not request.user.is_authenticated:
        return redirect('/')
    if request.method != 'POST':
        return redirect(_safe_back(request))

    from clients.models import BroadcastCampaign, BroadcastDismissal
    try:
        campaign = BroadcastCampaign.objects.get(pk=campaign_id, show_in_app=True)
    except BroadcastCampaign.DoesNotExist:
        return redirect(_safe_back(request))

    BroadcastDismissal.objects.get_or_create(campaign=campaign, user=request.user)
    return redirect(_safe_back(request))
