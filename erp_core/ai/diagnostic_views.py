"""
🚗 BMW/MINI Auto Diagnostic — Views & API
"""
from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_POST

from .auto_diagnostic import run_diagnostic_pipeline

logger = logging.getLogger('mouss_tec_core')

_SESSION_KEY = 'diagnostic_history_v1'
_MAX_HISTORY = 16


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@login_required
def diagnostic_page_shop(request):
    """صفحة التشخيص للورشة (الفنيين المحترفين)."""
    return render(request, 'erp_core/auto_diagnostic.html', {
        'audience': 'shop',
        'audience_label': 'الفني / الورشة',
        'audience_emoji': '🔧',
        'tenant_schema': getattr(connection, 'schema_name', 'public'),
    })


@login_required
def diagnostic_page_customer(request):
    """صفحة التشخيص لصاحب السيارة (مش فني)."""
    return render(request, 'erp_core/auto_diagnostic.html', {
        'audience': 'customer',
        'audience_label': 'صاحب السيارة',
        'audience_emoji': '🚙',
        'tenant_schema': getattr(connection, 'schema_name', 'public'),
    })


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------
@login_required
@require_POST
def diagnostic_chat_api(request):
    """Endpoint رئيسي: يستقبل {query, audience} ويرد {answer, refined, ...}."""
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'invalid_json'}, status=400)

    query = (body.get('query') or '').strip()
    audience = (body.get('audience') or 'shop').strip()

    if audience not in ('shop', 'customer'):
        return JsonResponse({'success': False, 'error': 'invalid_audience'}, status=400)

    if not query:
        return JsonResponse({
            'success': True,
            'answer': 'اكتب الشكوى بالعامية أو كود العطل (مثال: P0301)، أو موديل السيارة + المحرك (مثال: F30 N20).',
        })

    if len(query) > 2000:
        return JsonResponse({
            'success': False,
            'answer': 'الشكوى طويلة جداً — اختصرها.',
            'error': 'query_too_long',
        }, status=400)

    session_key = f'{_SESSION_KEY}_{audience}'
    history = request.session.get(session_key, [])

    try:
        result = run_diagnostic_pipeline(query, audience=audience, history=history)
    except Exception as e:
        logger.exception('[DIAG VIEW] pipeline crashed')
        return JsonResponse({
            'success': False,
            'answer': '⚠️ حصل عطل غير متوقع — جرب تاني بعد لحظات.',
            'error': str(e),
        }, status=200)

    if result.get('success'):
        history.append({'role': 'user', 'text': query})
        history.append({'role': 'model', 'text': result['answer']})
        request.session[session_key] = history[-_MAX_HISTORY:]
        request.session.modified = True

    return JsonResponse(result)


@login_required
@require_POST
def diagnostic_reset_api(request):
    """يصفّر تاريخ المحادثة."""
    audience = (request.POST.get('audience') or 'shop').strip()
    if audience not in ('shop', 'customer'):
        return HttpResponseBadRequest('invalid audience')
    request.session.pop(f'{_SESSION_KEY}_{audience}', None)
    request.session.modified = True
    return JsonResponse({'success': True})
