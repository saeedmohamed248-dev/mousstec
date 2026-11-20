"""🚨 لوحة مالك المنصة — الأخطاء، مركز الأمان، سجل الأحداث، حالة النظام، الزوار اللايف."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models import (
    SystemErrorLog,
    PlatformEvent,
)
from clients.permissions import widget_required
from ._common import _log_event, saas_admin_required

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 🚨 System Error Log Viewer
# ─────────────────────────────────────────────────────────────────────
_ERR_WINDOWS = {'1h': timedelta(hours=1), '24h': timedelta(hours=24), '7d': timedelta(days=7), '30d': timedelta(days=30)}


def _errors_filters(params):
    """الفلاتر من الرابط (GET) أو من فورم الإجراء الجماعي — نفس المنطق في العرض والتصدير والحل الجماعي."""
    f = {
        'show': params.get('show', 'open') if params.get('show') in ('open', 'resolved', 'all') else 'open',
        'level': params.get('level') if params.get('level') in ('warning', 'error', 'critical') else '',
        'tenant': (params.get('tenant') or '').strip()[:100],
        'q': (params.get('q') or '').strip()[:200],
        'code': params.get('code') if params.get('code') in ('5xx', '4xx', '404', '403', '400') else '',
        'window': params.get('window') if params.get('window') in _ERR_WINDOWS else '',
        'hide_bots': params.get('hide_bots') == '1',
    }
    return f


def _errors_queryset(f):
    from clients.services.error_insight import SCANNER_PATH_SQL
    qs = SystemErrorLog.objects.all()
    if f['show'] == 'open':
        qs = qs.filter(is_resolved=False)
    elif f['show'] == 'resolved':
        qs = qs.filter(is_resolved=True)
    if f['level']:
        qs = qs.filter(level=f['level'])
    if f['tenant']:
        qs = qs.filter(tenant_schema=f['tenant'])
    if f['window']:
        qs = qs.filter(created_at__gte=timezone.now() - _ERR_WINDOWS[f['window']])
    code = f['code']
    if code == '5xx':
        qs = qs.filter(status_code__gte=500)
    elif code == '4xx':
        qs = qs.filter(status_code__gte=400, status_code__lt=500)
    elif code:
        qs = qs.filter(status_code=int(code))
    if f['hide_bots']:
        # سكانرات + زوار مش داخلين على صفحات مش موجودة
        qs = qs.exclude(path__iregex=SCANNER_PATH_SQL).exclude(user_id__isnull=True, status_code=404)
    if f['q']:
        q = f['q']
        cond = (Q(path__icontains=q) | Q(message__icontains=q) | Q(exception_class__icontains=q)
                | Q(tenant_name__icontains=q) | Q(tenant_schema__icontains=q) | Q(username__icontains=q))
        if q.isdigit():
            cond |= Q(status_code=int(q)) | Q(pk=int(q))
        from erp_core.client_ip import valid_ip
        if valid_ip(q):
            cond |= Q(ip_address=valid_ip(q))
        qs = qs.filter(cond)
    return qs


def _errors_qs_string(f, **override):
    from urllib.parse import urlencode
    d = {**f, **override}
    out = {k: v for k, v in d.items() if v and k != 'hide_bots'}
    if d.get('hide_bots'):
        out['hide_bots'] = '1'
    return urlencode(out)


def _errors_overview():
    """أرقام حقيقية للوحة: آخر ساعة/٢٤ ساعة، الاتجاه، الشركات المتأثرة، الرسم بالساعة، أكتر المشاكل."""
    from django.db.models import Max
    from django.db.models.functions import TruncHour
    now = timezone.now()
    d24, d48 = now - timedelta(hours=24), now - timedelta(hours=48)
    last24 = SystemErrorLog.objects.filter(created_at__gte=d24)
    real24 = last24.filter(Q(is_resolved=False) | Q(resolved_by__isnull=False))   # من غير البوتات اللي اتقفلت لوحدها
    prev24 = SystemErrorLog.objects.filter(created_at__gte=d48, created_at__lt=d24).filter(
        Q(is_resolved=False) | Q(resolved_by__isnull=False))
    n24, n_prev = real24.count(), prev24.count()
    trend = None if not n_prev else round((n24 - n_prev) * 100 / n_prev)
    # 📈 بالساعة (آخر ٢٤ ساعة): أعطال الكود (5xx) والباقي
    buckets = {}
    for r in real24.annotate(h=TruncHour('created_at')).values('h').annotate(
            server=Count('id', filter=Q(status_code__gte=500)), other=Count('id', filter=Q(status_code__lt=500))):
        buckets[r['h']] = (r['server'], r['other'])
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    hours = []
    for i in range(24):
        h = start + timedelta(hours=i)
        server, other = buckets.get(h, (0, 0))
        hours.append({'label': timezone.localtime(h).strftime('%H:00'), 'server': server, 'other': other,
                      'total': server + other})
    peak = max((h['total'] for h in hours), default=0) or 1
    for h in hours:
        h['pct'] = round(h['total'] * 100 / peak)
        h['server_pct'] = round(h['server'] * 100 / peak)
    top_pages = list(real24.values('status_code', 'fingerprint').annotate(
        n=Count('id'), last=Max('created_at'), path=Max('path'),
        tenants=Count('tenant_schema', distinct=True)).order_by('-n')[:8])
    top_tenants = list(real24.exclude(tenant_schema='').values('tenant_schema', 'tenant_name').annotate(
        n=Count('id'), server=Count('id', filter=Q(status_code__gte=500)), last=Max('created_at'))
        .order_by('-n')[:8])
    return {
        'last_1h': real24.filter(created_at__gte=now - timedelta(hours=1)).count(),
        'last_24h': n24, 'prev_24h': n_prev, 'trend': trend,
        'server_24h': real24.filter(status_code__gte=500).count(),
        'tenants_24h': real24.exclude(tenant_schema='').values('tenant_schema').distinct().count(),
        'users_24h': real24.exclude(user_id__isnull=True).values('tenant_schema', 'user_id').distinct().count(),
        'hours': hours, 'peak': peak, 'top_pages': top_pages, 'top_tenants': top_tenants,
    }


def _errors_csv(qs):
    import csv
    from django.http import HttpResponse
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="errors-{timezone.localdate():%Y%m%d}.csv"'
    resp.write('\ufeff')   # Excel يقرا العربي
    w = csv.writer(resp)
    w.writerow(['id', 'time', 'level', 'status', 'tenant', 'schema', 'method', 'path', 'user', 'ip',
                'exception', 'message', 'resolved', 'resolved_by'])
    for e in qs.select_related('resolved_by').order_by('-created_at')[:5000]:
        w.writerow([e.pk, timezone.localtime(e.created_at).strftime('%Y-%m-%d %H:%M:%S'), e.level, e.status_code,
                    e.tenant_name, e.tenant_schema, e.method, e.path, e.username, e.ip_address or '',
                    e.exception_class, (e.message or '')[:500], 'yes' if e.is_resolved else 'no',
                    getattr(e.resolved_by, 'username', '') if e.is_resolved else ''])
    return resp


@saas_admin_required
@widget_required('errors')
def system_errors_list(request):
    f = _errors_filters(request.GET)
    qs = _errors_queryset(f)
    if request.GET.get('export') == 'csv':
        return _errors_csv(qs)
    show, level, tenant_schema = f['show'], f['level'], f['tenant']

    # 🧬 تجميع بالبصمة (view + كود + استثناء): نفس المشكلة = صف واحد (الأحدث) بعدّاد تكرار.
    # 🐛 [FIX]: كل تكرار كان صف لوحده — ٥ صفوف «/login/ 403» ورا بعض بيخبّوا المشاكل التانية،
    #    والتجميع القديم كان بالمسار بالحرف (/orders/12/ ≠ /orders/99/).
    from clients.services.error_insight import explain, fingerprint_of
    since = timezone.now() - timedelta(days=30)
    errors, seen = [], set()
    for e in qs.order_by('-created_at')[:2000]:
        fp = fingerprint_of(e)
        group_key = (e.tenant_schema, fp)
        if group_key in seen:
            continue
        seen.add(group_key)
        e.group_fp = fp
        errors.append(e)
        if len(errors) >= 200:
            break
    stored = {e.fingerprint for e in errors if e.fingerprint}
    repeats = {
        (r['tenant_schema'], r['fingerprint']): r['n']
        for r in SystemErrorLog.objects.filter(created_at__gte=since, fingerprint__in=stored)
        .values('tenant_schema', 'fingerprint').annotate(n=Count('id'))
    }
    # صفوف قديمة قبل عمود البصمة: العدّ بالمسار زي الأول
    legacy_repeats = {
        (r['tenant_schema'], r['status_code'], r['path']): r['n']
        for r in SystemErrorLog.objects.filter(
            created_at__gte=since, fingerprint='', path__in={e.path for e in errors if not e.fingerprint},
        ).values('tenant_schema', 'status_code', 'path').annotate(n=Count('id'))
    }
    from clients.models import Domain
    hosts_by_schema = {}
    for schema, dom in Domain.objects.filter(
            tenant__schema_name__in={e.tenant_schema for e in errors}).values_list('tenant__schema_name', 'domain'):
        hosts_by_schema.setdefault(schema, set()).add(dom)
    from clients.services import autofix
    from clients.services.error_autoheal import signature_of
    for e in errors:
        e.insight = explain(e, hosts_by_schema.get(e.tenant_schema, ()))
        e.autofix_sent = autofix.sent_at(signature_of(e)) if e.insight['kind'] != 'bot' else None
        e.repeat_count = (repeats.get((e.tenant_schema, e.fingerprint), 1) if e.fingerprint
                          else legacy_repeats.get((e.tenant_schema, e.status_code, e.path), 1))

    match_open = qs.filter(is_resolved=False).count() if show != 'resolved' else 0
    return render(request, 'clients/saas_admin/system_errors.html', {
        'errors': errors,
        'show': show,
        'level': level,
        'tenant_schema': tenant_schema,
        'f': f,
        'qs_base': _errors_qs_string(f),
        'qs_no_show': _errors_qs_string(f, show=''),
        'qs_no_level': _errors_qs_string(f, level=''),
        'match_total': qs.count(),
        'match_open': match_open,
        'ov': _errors_overview(),
        'count_open': SystemErrorLog.objects.filter(is_resolved=False).count(),
        'count_critical': SystemErrorLog.objects.filter(is_resolved=False, level='critical').count(),
        'autofix_enabled': autofix.enabled(),
        'count_auto_closed': SystemErrorLog.objects.filter(
            is_resolved=True, resolved_by__isnull=True, created_at__gte=since).count(),
    })


@saas_admin_required
@widget_required('errors')
@require_POST
def system_errors_bulk(request):
    """☑️ إجراء جماعي: «تم الحل» / «إعادة فتح» للمحدد — أو «حل كل اللي في الفلتر ده».

    المحدد = مجموعات (نفس البصمة عند نفس الشركة) زي زرار «تم الحل» العادي.
    """
    from urllib.parse import parse_qs
    action = request.POST.get('action')
    back = request.POST.get('next', '')
    back = back if back.startswith('/superadmin/errors/') else reverse('saas_system_errors')
    if action not in ('resolve', 'reopen', 'resolve_filter'):
        messages.error(request, "إجراء غير معروف.")
        return redirect(back)
    now = timezone.now()
    if action == 'resolve_filter':
        params = {k: v[0] for k, v in parse_qs(request.POST.get('filter_qs', '')).items()}
        f = _errors_filters(params)
        f['show'] = 'open'
        target = _errors_queryset(f).filter(is_resolved=False)
        expected = request.POST.get('expected', '')
        n_now = target.count()
        # 🛡️ لو الرقم اتغيّر من ساعة ما شفته (أخطاء جديدة دخلت) — ماتقفلش حاجة ماشفتهاش
        if expected.isdigit() and n_now > int(expected):
            messages.warning(request, f"دخل {n_now - int(expected)} خطأ جديد على الفلتر ده من ساعة ما فتحت الصفحة — "
                                      f"راجعهم الأول وبعدين اضغط تاني.")
            return redirect(back)
        n = target.update(is_resolved=True, resolved_by=request.user, resolved_at=now)
        desc = f"☑️ حل جماعي لـ {n} خطأ (فلتر: {request.POST.get('filter_qs', '')[:200] or 'كل المفتوح'})"
    else:
        ids = [int(i) for i in request.POST.getlist('ids') if str(i).isdigit()][:500]
        if not ids:
            messages.error(request, "اختار خطأ واحد على الأقل.")
            return redirect(back)
        picked = list(SystemErrorLog.objects.filter(pk__in=ids).values('pk', 'tenant_schema', 'fingerprint'))
        cond = Q(pk__in=[p['pk'] for p in picked])
        for p in picked:
            if p['fingerprint']:
                cond |= Q(tenant_schema=p['tenant_schema'], fingerprint=p['fingerprint'])
        if action == 'resolve':
            n = SystemErrorLog.objects.filter(cond, is_resolved=False).update(
                is_resolved=True, resolved_by=request.user, resolved_at=now)
            desc = f"☑️ حل {n} خطأ ({len(picked)} مجموعة)"
        else:
            n = SystemErrorLog.objects.filter(cond, is_resolved=True).update(
                is_resolved=False, resolved_by=None, resolved_at=None)
            desc = f"↩️ إعادة فتح {n} خطأ ({len(picked)} مجموعة)"
    _log_event('other', user=request.user, description=desc)
    messages.success(request, desc.lstrip('☑️↩️ '))
    return redirect(back)


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Security Center — محاولات دخول، سكانرات، حظر IP
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('errors')
def security_center(request):
    from clients.services import security_center as sc
    from erp_core.client_ip import client_ip
    hours = 24 if request.GET.get('window') != '7d' else 24 * 7
    logins, failures_total, locked_count = sc.failed_logins(hours)
    scanners, scanner_hits = sc.scanner_ips(hours)
    return render(request, 'clients/saas_admin/security_center.html', {
        'window': '7d' if hours > 24 else '24h',
        'logins': logins[:100], 'failures_total': failures_total, 'locked_count': locked_count,
        'scanners': scanners, 'scanner_hits': scanner_hits,
        'noisy': sc.noisy_ips(hours),
        'blocked': sc.blocked_list(),
        'events': sc.recent_security_events(),
        'my_ip': client_ip(request),
        'durations': [('1h', 'ساعة'), ('24h', '٢٤ ساعة'), ('7d', 'أسبوع'), ('forever', 'دايماً')],
    })


@saas_admin_required
@widget_required('errors')
@require_POST
def security_action(request):
    from clients.services import security_center as sc
    from erp_core.client_ip import client_ip
    action = request.POST.get('action')
    ip = (request.POST.get('ip') or '').strip()
    me = client_ip(request)
    if action == 'block':
        ok, msg = sc.block_ip(ip, duration=request.POST.get('duration', '24h'),
                              reason=request.POST.get('reason', ''), user=request.user, actor_ip=me)
        (messages.success if ok else messages.error)(request, msg)
    elif action == 'unblock':
        n = sc.unblock_ip(ip, user=request.user, actor_ip=me)
        (messages.success if n else messages.error)(request, f"اترفع الحظر عن {ip}." if n else "الـ IP ده مش محظور.")
    elif action == 'unlock':
        schema = (request.POST.get('schema') or 'public').strip()
        n = sc.unlock_login(schema, ip=ip or None, username=(request.POST.get('username') or '').strip() or None)
        _log_event('other', user=request.user,
                   description=f"🛡️ فك قفل الدخول ({schema}) ip={ip} user={request.POST.get('username', '')}")
        messages.success(request, f"اتفك القفل ({n} سجل محاولات اتمسح).")
    else:
        messages.error(request, "إجراء غير معروف.")
    back = request.POST.get('next', '')
    return redirect(back if back.startswith('/superadmin/security/') else 'saas_security_center')


@saas_admin_required
@widget_required('errors')
def system_error_resolve(request, error_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    err = get_object_or_404(SystemErrorLog, pk=error_id)
    # 🧬 «تم الحل» بيقفل المجموعة كلها (نفس البصمة عند نفس الشركة) — الصف المعروض هو الأحدث بس.
    group = SystemErrorLog.objects.filter(pk=err.pk)
    if err.fingerprint:
        group = SystemErrorLog.objects.filter(
            Q(pk=err.pk) | Q(is_resolved=False, fingerprint=err.fingerprint, tenant_schema=err.tenant_schema))
    n = group.update(is_resolved=True, resolved_by=request.user, resolved_at=timezone.now())
    messages.success(request, "تم تعليم الخطأ كمحلول." if n <= 1 else f"تم تعليم {n} تكرار لنفس الخطأ كمحلولين.")
    # 🐛 [FIX]: كان بيرجّع على «مفتوحة» من غير فلتر الشركة اللي كنت فاتحه
    back = request.POST.get('next', '')
    if back.startswith('/superadmin/errors/'):
        return redirect(back)
    return redirect('saas_system_errors')


@saas_admin_required
@widget_required('errors')
def system_error_autofix(request, error_id):
    """🤖 «اطلب تصليح» — يبعت الخطأ لبوت التصليح (GitHub Actions ⇒ PR) حتى لو مش 500 جديد."""
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    from clients.services import autofix
    err = get_object_or_404(SystemErrorLog, pk=error_id)
    ok, msg = autofix.dispatch(err, force=True)
    (messages.success if ok else messages.error)(request, msg)
    back = request.POST.get('next', '')
    return redirect(back if back.startswith('/superadmin/errors/') else 'saas_system_errors')


# ─────────────────────────────────────────────────────────────────────
# 📜 Audit Log — full PlatformEvent viewer
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('errors')
def audit_log_list(request):
    """
    سجل أحداث المنصة الكامل (PlatformEvent) — فلاتر + بحث + pagination.
    GET params:
      - event_type, tenant, user → exact match
      - q → بحث في description
      - from, to → نطاق زمني (YYYY-MM-DD)
      - page
    """
    from django.core.paginator import Paginator
    from datetime import datetime

    qs = PlatformEvent.objects.all().order_by('-timestamp')

    event_type = (request.GET.get('event_type') or '').strip()
    tenant = (request.GET.get('tenant') or '').strip()
    user_q = (request.GET.get('user') or '').strip()
    q = (request.GET.get('q') or '').strip()
    date_from = (request.GET.get('from') or '').strip()
    date_to = (request.GET.get('to') or '').strip()

    if event_type:
        qs = qs.filter(event_type=event_type)
    if tenant:
        qs = qs.filter(tenant_schema__iexact=tenant)
    if user_q:
        qs = qs.filter(user_name__icontains=user_q)
    if q:
        qs = qs.filter(
            Q(description__icontains=q)
            | Q(tenant_name__icontains=q)
            | Q(user_name__icontains=q)
        )
    if date_from:
        try:
            d = datetime.strptime(date_from, '%Y-%m-%d').date()
            qs = qs.filter(timestamp__date__gte=d)
        except ValueError:
            pass
    if date_to:
        try:
            d = datetime.strptime(date_to, '%Y-%m-%d').date()
            qs = qs.filter(timestamp__date__lte=d)
        except ValueError:
            pass

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    # تجميعات سريعة للسايد بانل
    total = PlatformEvent.objects.count()
    by_type = (
        PlatformEvent.objects.values('event_type')
        .annotate(n=Count('id')).order_by('-n')
    )
    top_tenants = (
        PlatformEvent.objects.exclude(tenant_schema='')
        .values('tenant_schema', 'tenant_name')
        .annotate(n=Count('id')).order_by('-n')[:10]
    )
    top_users = (
        PlatformEvent.objects.exclude(user_name='')
        .values('user_name').annotate(n=Count('id')).order_by('-n')[:10]
    )

    return render(request, 'clients/saas_admin/audit_log.html', {
        'page_obj': page_obj,
        'filters': {
            'event_type': event_type, 'tenant': tenant, 'user': user_q,
            'q': q, 'from': date_from, 'to': date_to,
        },
        'event_types': PlatformEvent.EVENT_TYPES,
        'total': total,
        'filtered_count': qs.count(),
        'by_type': list(by_type),
        'top_tenants': list(top_tenants),
        'top_users': list(top_users),
    })


# ─────────────────────────────────────────────────────────────────────
# ⚙️ System Status — DB / Redis / Celery / app health
# ─────────────────────────────────────────────────────────────────────
def _check_database():
    """Ping the DB with `SELECT 1` and time it."""
    from django.db import connection as _conn
    import time
    t0 = time.perf_counter()
    try:
        with _conn.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        ms = (time.perf_counter() - t0) * 1000
        if ms > 500:
            return {'name': 'PostgreSQL', 'status': 'degraded', 'detail': f'{ms:.0f}ms (بطيء)', 'icon': 'database'}
        return {'name': 'PostgreSQL', 'status': 'ok', 'detail': f'{ms:.1f}ms', 'icon': 'database'}
    except Exception as e:
        return {'name': 'PostgreSQL', 'status': 'down', 'detail': str(e)[:120], 'icon': 'database'}


def _check_cache():
    """Round-trip set/get from the default cache backend."""
    from django.core.cache import cache
    import time, secrets
    key = f'__health_probe_{secrets.token_hex(4)}__'
    val = secrets.token_hex(8)
    t0 = time.perf_counter()
    try:
        cache.set(key, val, 10)
        got = cache.get(key)
        ms = (time.perf_counter() - t0) * 1000
        if got != val:
            return {'name': 'Redis Cache', 'status': 'degraded', 'detail': 'set/get mismatch', 'icon': 'bolt'}
        cache.delete(key)
        if ms > 200:
            return {'name': 'Redis Cache', 'status': 'degraded', 'detail': f'{ms:.0f}ms (بطيء)', 'icon': 'bolt'}
        return {'name': 'Redis Cache', 'status': 'ok', 'detail': f'{ms:.1f}ms', 'icon': 'bolt'}
    except Exception as e:
        return {'name': 'Redis Cache', 'status': 'down', 'detail': str(e)[:120], 'icon': 'bolt'}


def _check_celery():
    """Ping live Celery workers."""
    try:
        from erp_core.celery import app as celery_app
    except Exception:
        try:
            from celery import current_app as celery_app
        except Exception as e:
            return {'name': 'Celery Workers', 'status': 'unknown', 'detail': f'لا يمكن استيراد celery: {e}', 'icon': 'gears'}
    try:
        replies = celery_app.control.ping(timeout=1.0) or []
        n = len(replies)
        if n == 0:
            return {'name': 'Celery Workers', 'status': 'down', 'detail': 'لا يوجد عمال نشطون', 'icon': 'gears'}
        return {'name': 'Celery Workers', 'status': 'ok', 'detail': f'{n} عامل نشط', 'icon': 'gears'}
    except Exception as e:
        return {'name': 'Celery Workers', 'status': 'unknown', 'detail': str(e)[:120], 'icon': 'gears'}


def _check_resources():
    """Disk + memory via psutil if available."""
    out = []
    try:
        import psutil
    except ImportError:
        return [{'name': 'Disk / Memory', 'status': 'unknown', 'detail': 'psutil غير مثبت — pip install psutil', 'icon': 'microchip'}]
    try:
        du = psutil.disk_usage('/')
        pct = du.percent
        free_gb = du.free / (1024 ** 3)
        status = 'ok' if pct < 80 else ('degraded' if pct < 92 else 'down')
        out.append({
            'name': 'Disk', 'status': status,
            'detail': f'{pct:.1f}% مستخدم — {free_gb:.1f} GB متاح', 'icon': 'hard-drive',
        })
    except Exception as e:
        out.append({'name': 'Disk', 'status': 'unknown', 'detail': str(e)[:80], 'icon': 'hard-drive'})
    try:
        vm = psutil.virtual_memory()
        status = 'ok' if vm.percent < 85 else ('degraded' if vm.percent < 95 else 'down')
        out.append({
            'name': 'Memory', 'status': status,
            'detail': f'{vm.percent:.1f}% مستخدم — {vm.available / (1024**3):.1f} GB متاح', 'icon': 'memory',
        })
    except Exception as e:
        out.append({'name': 'Memory', 'status': 'unknown', 'detail': str(e)[:80], 'icon': 'memory'})
    return out


@saas_admin_required
@widget_required('visitors')
def live_market(request):
    """🟢 /superadmin/live/ — مين أونلاين دلوقتي في كل سوق (بالاسم لو معروف) + زوار النهارده + الأرقام.
    ?tab=online|today|stats  ·  ?area=printing  ·  ?q=بحث
    ?partial=1 بيرجّع جزء الصفحة بس (التحديث التلقائي كل ١٥ ثانية)."""
    from clients.services.live_market import TABS, snapshot
    from django.conf import settings as dj_settings
    tab = request.GET.get('tab') if request.GET.get('tab') in TABS else 'online'
    ctx = snapshot(tab=tab, area=request.GET.get('area', ''), q=request.GET.get('q', '')[:60])
    ctx['admin_url'] = dj_settings.ADMIN_URL
    tpl = 'clients/saas_admin/_live_body.html' if request.GET.get('partial') else 'clients/saas_admin/live_market.html'
    return render(request, tpl, ctx)


@saas_admin_required
@widget_required('visitors')
def live_visitor(request, token):
    """🧭 /superadmin/live/v/<token>/ — رحلة زائر واحد بالتفصيل: كل زيارة، كل صفحة والساعة كام وقعد قد إيه،
    كل عملية عملها، ووقف فين. ?days=1|7|30  ·  ?bg=1 يظهر طلبات الخلفية كمان."""
    from clients.services.live_market import visitor_journey
    try:
        days = max(1, min(int(request.GET.get('days', 1)), 30))
    except (TypeError, ValueError):
        days = 1
    journey = visitor_journey(token, days=days, show_background=request.GET.get('bg') == '1')
    if journey is None:
        return render(request, 'clients/saas_admin/live_visitor.html', {'expired': True}, status=404)
    tpl = 'clients/saas_admin/_live_visitor_body.html' if request.GET.get('partial') else \
        'clients/saas_admin/live_visitor.html'
    return render(request, tpl, {**journey, 'bg': request.GET.get('bg') == '1',
                                 'day_options': (1, 7, 30)})


@saas_admin_required
@widget_required('errors')
def system_status(request):
    """
    /superadmin/system/ — لقطة لحظية لصحة المنصة.
    تشمل: DB ping, Redis cache, Celery workers, disk/memory, error rate, response time.
    """
    from clients.models import VisitorLog
    from django.db.models import Avg
    now = timezone.now()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)
    five_min_ago = now - timedelta(minutes=5)

    checks = [_check_database(), _check_cache(), _check_celery()] + _check_resources()

    # تحديد الحالة الإجمالية
    statuses = [c['status'] for c in checks]
    # 🐛 [FIX]: فحص «غير معروف» (مثلاً psutil مش متثبتة) كان بيقلب الحالة كلها «في تدهور أداء»
    #    والمكونات كلها شغالة. اللي مقدرناش نقيسه مايتحسبش عطل.
    if 'down' in statuses:
        overall = 'down'
    elif 'degraded' in statuses:
        overall = 'degraded'
    else:
        overall = 'ok'

    # App-level metrics
    try:
        # 🐛 [FIX]: كان `timestamp__gte` (عمود مش موجود) ⇒ FieldError ⇒ الأرقام دايماً «—»
        errors_hour = SystemErrorLog.objects.filter(created_at__gte=hour_ago).count()
        errors_day = SystemErrorLog.objects.filter(created_at__gte=day_ago).count()
        unresolved = SystemErrorLog.objects.filter(is_resolved=False).count()
    except Exception:
        errors_hour = errors_day = unresolved = None

    try:
        avg_resp_hour = VisitorLog.objects.filter(
            timestamp__gte=hour_ago, response_time_ms__isnull=False,
        ).aggregate(avg=Avg('response_time_ms'))['avg'] or 0
        slowest = VisitorLog.objects.filter(
            timestamp__gte=hour_ago, response_time_ms__isnull=False,
        ).order_by('-response_time_ms')[:10].values(
            'timestamp', 'path', 'response_time_ms', 'status_code', 'tenant_schema',
        )
        active_5m = VisitorLog.objects.filter(timestamp__gte=five_min_ago).values('ip_address').distinct().count()
        reqs_hour = VisitorLog.objects.filter(timestamp__gte=hour_ago).count()
    except Exception:
        avg_resp_hour = reqs_hour = active_5m = 0
        slowest = []

    return render(request, 'clients/saas_admin/system_status.html', {
        'checks': checks,
        'overall': overall,
        'errors_hour': errors_hour,
        'errors_day': errors_day,
        'unresolved_errors': unresolved,
        'avg_resp_hour': int(avg_resp_hour or 0),
        'reqs_hour': reqs_hour,
        'active_5m': active_5m,
        'slowest': list(slowest),
        'now': now,
    })
