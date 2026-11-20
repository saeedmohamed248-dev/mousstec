"""
🤖 API وكيل المتابعة (Dify / n8n)
=================================
GET /api/agent/followups/   — Header: Authorization: Bearer <AGENT_API_TOKEN>

بيرجّع الشركات اللي محتاجة متابعة النهارده — نفس منطق صفحتي
/superadmin/trials/ و /superadmin/churn-risk/ بالظبط (مش حساب جديد)، عشان
اللي الوكيل بيقوله للعميل يطابق اللي صاحب المنصة شايفه في الأدمن.

- trials: كل التجريبي + اللي تجربته خلصت من قريب (verdict ≠ active).
- at_risk: المشتركين بفلوس (status=active) اللي درجة صحتهم high/critical.

أمان: لو AGENT_API_TOKEN مش متظبط الـ endpoint مقفول (404) — مايتفتحش بالغلط.
مفيش بيانات حساسة: لا فلوس، ولا إيميلات، ولا بيانات عملاء الشركة نفسها.
"""
from __future__ import annotations

import hmac
import os

from django.conf import settings
from django.db import connection
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

AT_RISK_LEVELS = ('high', 'critical')


def _authorized(request) -> bool:
    token = getattr(settings, 'AGENT_API_TOKEN', None) or os.environ.get('AGENT_API_TOKEN', '')
    if not token:
        raise Http404
    header = request.headers.get('Authorization', '')
    provided = header[7:] if header.startswith('Bearer ') else ''
    return bool(provided) and hmac.compare_digest(provided.encode(), token.encode())


def _iso(value):
    return value.isoformat() if value else None


def _wa(phone: str) -> str:
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if digits.startswith('01') and len(digits) == 11:   # موبايل مصري محلي ⇒ دولي
        digits = '2' + digits
    return digits


def _tenant_base(t) -> dict:
    return {
        'id': t.id, 'name': t.name, 'schema': t.schema_name,
        'industry': getattr(t, 'industry', ''), 'status': t.status,
        'wa_phone': _wa(t.phone),
    }


@require_GET
def followups(request):
    if connection.schema_name != 'public':
        raise Http404
    if not _authorized(request):
        return JsonResponse({'error': 'unauthorized'}, status=401)

    from clients.models import Client
    from clients.services.tenant_health import bulk_tenant_health
    from clients.services.trial_watch import trial_rows

    trials = []
    for r in trial_rows():
        if r['verdict'] == 'active':
            continue
        trials.append({
            **_tenant_base(r['tenant']),
            'verdict': r['verdict'], 'verdict_label': r['verdict_label'],
            'days_left': r['days_left'],
            'last_login': _iso(r['last_login']), 'last_doc': _iso(r['last_doc']),
            'docs_7d': r['docs_7d'], 'docs_total': r['docs_total'],
            'data_error': bool(r['error']),
        })

    paying = list(Client.objects.exclude(schema_name='public').filter(is_deleted=False, status='active'))
    healths = bulk_tenant_health(paying)
    at_risk = []
    for t in paying:
        h = healths.get(t.id) or {}
        if h.get('risk') not in AT_RISK_LEVELS:
            continue
        at_risk.append({
            **_tenant_base(t),
            'score': h['score'], 'grade': h['grade'], 'risk': h['risk'],
            'reasons': h['reasons'],
            'subscription_end_date': _iso(t.subscription_end_date),
        })
    at_risk.sort(key=lambda x: x['score'])

    return JsonResponse({
        'generated_at': timezone.now().isoformat(),
        'counts': {'trials': len(trials), 'at_risk': len(at_risk)},
        'trials': trials,
        'at_risk': at_risk,
    }, json_dumps_params={'ensure_ascii': False})
