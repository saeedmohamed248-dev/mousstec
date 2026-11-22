"""📲 لوحة مالك المنصة — التواصل مع الشركات المسجّلة على واتساب ومتابعتهم.

* زرار لكل شركة بيفتح واتساب من موبايل المالك والرسالة جاهزة (ويتسجّل).
* «ابعت للكل من موبايلي» — الشركات واحدة ورا التانية.
* «ابعت للكل أوتوماتيك» — من رقم المنصة (WhatsApp Cloud API) في الخلفية.
* تسجيل نتيجة كل متابعة والميعاد الجاي — والصفحة بتقدّم اللي ميعاده جه.
المنطق كله في clients/services/whatsapp_outreach.py.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from clients.models import BroadcastCampaign, Client, Plan, TenantOutreach
from clients.permissions import widget_required
from clients.services import whatsapp_outreach as wa
from erp_core.http_utils import safe_back as _safe_back
from ._common import _log_event, saas_admin_required

logger = logging.getLogger('mouss_tec_core')

# نفس فلاتر الإيميل الجماعي (من غير «مخصص»)
AUDIENCES = tuple(a for a in BroadcastCampaign.AUDIENCE_CHOICES if a[0] != 'custom')
_FILTER_KEYS = ('audience', 'plan', 'industry', 'q', 'due')


def _filters(params):
    f = {k: (params.get(k) or '').strip() for k in _FILTER_KEYS}
    f['audience'] = f['audience'] or 'all'
    return f


def _tenants(f):
    from clients.services.broadcast import resolve_audience
    qs = resolve_audience(f['audience'], plan=f['plan'],
                          extra={'industry': f['industry']} if f['industry'] else None)
    if f['q']:
        q = f['q']
        qs = qs.filter(Q(name__icontains=q) | Q(owner_name__icontains=q)
                       | Q(phone__icontains=q) | Q(schema_name__icontains=q))
    return qs


def _message(params):
    """نص الرسالة: المكتوب في الصندوق، وإلا الرسالة الجاهزة المختارة."""
    preset = (params.get('preset') or wa.DEFAULT_PRESET).strip()
    text = (params.get('msg') or '').strip()
    if not text:
        text = wa.PRESETS.get(preset, wa.PRESETS[wa.DEFAULT_PRESET])[1]
    return preset, text[:2000]


def _qs(f, **extra):
    return urlencode({k: v for k, v in {**f, **extra}.items() if v})


@saas_admin_required
@widget_required('tenants')
def whatsapp_outreach(request):
    """/superadmin/whatsapp/ — كل الشركات + زرار واتساب لكل واحدة + المتابعة."""
    f = _filters(request.GET)
    preset, text = _message(request.GET)
    rows = wa.outreach_rows(_tenants(f).order_by('-created_on', '-pk'), due_only=f['due'] == '1')
    today = timezone.localdate()
    for r in rows:
        r['preview'] = wa.render_message(text, r['tenant'], today)
    counts = {
        'total': len(rows),
        'due': sum(1 for r in rows if r['due']),
        'never': sum(1 for r in rows if r['last'] is None),
        'no_phone': sum(1 for r in rows if not r['wa_phone']),
    }
    # آخر دفعات الإرسال الأوتوماتيك ونتيجتها
    recent = {}
    for o in (TenantOutreach.objects.filter(channel='api').exclude(batch='')
              .order_by('-created_at').values('batch', 'status', 'created_at')[:500]):
        b = recent.setdefault(o['batch'], {'batch': o['batch'], 'at': o['created_at'],
                                           'sent': 0, 'failed': 0, 'queued': 0})
        if o['status'] in b:
            b[o['status']] += 1
    return render(request, 'clients/saas_admin/whatsapp_outreach.html', {
        'rows': rows, 'counts': counts, 'f': f, 'filter_qs': _qs(f),
        'audiences': AUDIENCES, 'plans': Plan.objects.filter(is_active=True).values('slug', 'name').order_by('name'),
        'industries': Client.INDUSTRY_CHOICES,
        'presets': [(k, v[0]) for k, v in wa.PRESETS.items()], 'preset': preset, 'msg': text,
        'outcomes': [(k, v) for k, v in TenantOutreach.STATUS_CHOICES if k in TenantOutreach.OUTCOME_STATUSES],
        'api_ready': wa.api_ready(), 'api_template': bool(settings.WHATSAPP_OUTREACH_TEMPLATE),
        'recent_batches': list(recent.values())[:5], 'today': today,
    })


@saas_admin_required
@widget_required('tenants')
def whatsapp_open(request, tenant_id):
    """POST: يسجّل التواصل ويحوّل على wa.me والرسالة جاهزة (بيتفتح في تاب جديدة)."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    tenant = get_object_or_404(Client.objects.exclude(schema_name='public'), pk=tenant_id)
    _, text = _message(request.POST)
    url = wa.log_phone_open(tenant, wa.render_message(text, tenant), user=request.user,
                            batch=(request.POST.get('batch') or '')[:32])
    if not url:
        messages.error(request, f"رقم «{tenant.name}» مش صالح للواتساب — عدّله من صفحة الشركة.")
        return redirect(_safe_back(request, reverse('saas_whatsapp_outreach')))
    return HttpResponseRedirect(url)


def _parse_next(post):
    """الميعاد الجاي: تاريخ مكتوب، أو «بعد N يوم»."""
    raw = (post.get('next_follow_up') or '').strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    try:
        days = int(post.get('in_days') or 0)
    except ValueError:
        days = 0
    return timezone.localdate() + timedelta(days=days) if days > 0 else None


@saas_admin_required
@widget_required('tenants')
def whatsapp_outcome(request, tenant_id):
    """POST: نتيجة المتابعة (ردّ/مهتم/اشترك…) + ملاحظات + الميعاد الجاي."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    tenant = get_object_or_404(Client.objects.exclude(schema_name='public'), pk=tenant_id)
    status = (request.POST.get('status') or '').strip()
    if status not in TenantOutreach.OUTCOME_STATUSES:
        messages.error(request, "اختار النتيجة.")
    else:
        wa.record_outcome(tenant, status=status, notes=request.POST.get('notes') or '',
                          next_follow_up=_parse_next(request.POST), user=request.user)
        messages.success(request, f"✅ اتسجّلت متابعة «{tenant.name}».")
    return redirect(_safe_back(request, reverse('saas_whatsapp_outreach')))


@saas_admin_required
@widget_required('tenants')
def whatsapp_queue(request):
    """/superadmin/whatsapp/queue/ — «ابعت للكل من موبايلي»: شركة واحدة في الصفحة، وبعد ما تفتح
    واتساب الصفحة بتنقل للي بعدها. الترتيب ثابت (بالـ id) عشان التسجيل مايلخبطش العدّاد."""
    f = _filters(request.GET)
    preset, text = _message(request.GET)
    tenants = [t for t in _tenants(f).order_by('pk') if wa.wa_phone(t)]
    if f['due'] == '1':
        due_ids = {r['tenant'].pk for r in wa.outreach_rows(tenants, due_only=True)}
        tenants = [t for t in tenants if t.pk in due_ids]
    try:
        i = max(int(request.GET.get('i') or 0), 0)
    except ValueError:
        i = 0
    batch = (request.GET.get('batch') or '')[:32] or timezone.now().strftime('m%Y%m%d%H%M%S')
    base = {**f, 'preset': preset, 'msg': text, 'batch': batch}
    current = tenants[i] if i < len(tenants) else None
    return render(request, 'clients/saas_admin/whatsapp_queue.html', {
        'current': current, 'i': i, 'n': i + 1, 'total': len(tenants), 'msg': text, 'batch': batch,
        'preview': wa.render_message(text, current) if current else '',
        'wa_phone': wa.wa_phone(current) if current else '',
        'next_url': f"{reverse('saas_whatsapp_queue')}?{_qs(base, i=str(i + 1))}",
        'back_url': f"{reverse('saas_whatsapp_outreach')}?{_qs(f, preset=preset, msg=text)}",
    })


@saas_admin_required
@widget_required('tenants')
def whatsapp_send_api(request):
    """POST: «ابعت للكل أوتوماتيك» من رقم المنصة — في الخلفية."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    f = _filters(request.POST)
    _, text = _message(request.POST)
    back = f"{reverse('saas_whatsapp_outreach')}?{_qs(f)}"
    if (request.POST.get('confirm') or '').strip().upper() != 'SEND':
        messages.error(request, "اكتب SEND في خانة التأكيد عشان الإرسال الجماعي يبدأ.")
        return redirect(back)
    if not wa.api_ready():
        messages.error(request, "مفاتيح واتساب المنصة (WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID) مش متظبطة — "
                                "استخدم «ابعت للكل من موبايلي».")
        return redirect(back)
    tenants = list(_tenants(f))
    if f['due'] == '1':
        due_ids = {r['tenant'].pk for r in wa.outreach_rows(tenants, due_only=True)}
        tenants = [t for t in tenants if t.pk in due_ids]
    if not tenants:
        messages.error(request, "مفيش شركات في الفلتر ده.")
        return redirect(back)
    batch, queued, bad = wa.queue_api_batch(tenants, text, user=request.user)
    try:
        from clients.tasks import send_whatsapp_outreach_batch
        send_whatsapp_outreach_batch.delay(batch)
    except Exception as exc:  # noqa: BLE001 — Redis/Celery واقع
        logger.exception('whatsapp outreach enqueue failed')
        TenantOutreach.objects.filter(batch=batch, status='queued').update(
            status='failed', error=f'الإرسال مابدأش: {type(exc).__name__}'[:300])
        messages.error(request, "الإرسال في الخلفية مابدأش (Celery/Redis) — جرّب تاني بعد شوية.")
        return redirect(back)
    _log_event('other', user=request.user,
               description=f"📲 واتساب جماعي من رقم المنصة: {queued} شركة (دفعة {batch})"
                           + (f" — {bad} رقم مش صالح" if bad else ''))
    msg = f"✅ بدأ إرسال {queued} رسالة من رقم المنصة في الخلفية — النتيجة هتظهر في «آخر دفعات الإرسال»."
    if bad:
        msg += f" ({bad} شركة رقمها مش صالح — اتسجّلت ماتبعتتش)"
    messages.success(request, msg)
    return redirect(back)
