"""
🧠 Cognitive Advisor — Views & API endpoints.
"""
from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_POST, require_GET

from .advisor_agent import run_advisor_pipeline
from ._safety import check_ai_rate_limit

logger = logging.getLogger('mouss_tec_core')

# Session key prefix for advisor history
_SESSION_KEY = 'advisor_history_v1'
_MAX_HISTORY = 20  # رسائل max في الـ session — أي حاجة فوق كده تتقطع


# ---------------------------------------------------------------------------
# Page views (sector-specific)
# ---------------------------------------------------------------------------
@login_required
def advisor_page_printing(request):
    """صفحة المستشار الذكي لقطاع التصاميم والمطابع."""
    return render(request, 'erp_core/cognitive_advisor.html', {
        'sector': 'printing',
        'sector_label': 'التصاميم والمطابع',
        'sector_emoji': '🖨️',
        'tenant_schema': getattr(connection, 'schema_name', 'public'),
    })


@login_required
def advisor_page_automotive(request):
    """صفحة المستشار الذكي لقطاع السيارات وقطع الغيار."""
    return render(request, 'erp_core/cognitive_advisor.html', {
        'sector': 'automotive',
        'sector_label': 'السيارات وقطع الغيار',
        'sector_emoji': '🚗',
        'tenant_schema': getattr(connection, 'schema_name', 'public'),
    })


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------
@login_required
@require_POST
def advisor_chat_api(request):
    """
    Endpoint رئيسي للمحادثة — يستقبل JSON {query, sector} ويرجع JSON {answer, ...}.
    بيستخدم الـ session لحفظ السياق التاريخي.
    """
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'invalid_json'}, status=400)

    query = (body.get('query') or '').strip()
    sector = body.get('sector', 'printing')

    if sector not in ('printing', 'automotive'):
        return JsonResponse({'success': False, 'error': 'invalid_sector'}, status=400)

    if not query:
        return JsonResponse({
            'success': True,
            'answer': 'أهلاً! اسألني عن الكاش، الراكد، الأرباح، أو أي تقرير عاوزه — ورد عليك بأرقام حقيقية من شغلك.',
            'tool_calls': [],
        })

    if len(query) > 2000:
        return JsonResponse({
            'success': False,
            'error': 'query_too_long',
            'answer': 'السؤال طويل جداً — اختصره شوية.',
        }, status=400)

    # 🛡️ Rate limit per tenant+user (multi-tool calls are expensive)
    schema = getattr(connection, 'schema_name', 'public')
    rl_key = f'advisor_chat:{schema}:{request.user.id}'
    ok, msg = check_ai_rate_limit(rl_key, per_minute=15, per_hour=250)
    if not ok:
        return JsonResponse({'success': False, 'error': 'rate_limited', 'answer': msg}, status=429)

    # تحميل تاريخ المحادثة من الـ session
    session_key = f'{_SESSION_KEY}_{sector}'
    history = request.session.get(session_key, [])

    try:
        result = run_advisor_pipeline(query, sector=sector, history=history)
    except Exception as e:
        logger.exception('[ADVISOR VIEW] pipeline crashed')
        return JsonResponse({
            'success': False,
            'answer': '⚠️ حصل عطل غير متوقع — جرب تاني بعد لحظات.',
            'error': str(e),
        }, status=200)  # 200 عشان الـ UI يعرض الرسالة الأنيقة

    # حدّث الـ session history (مع capping)
    if result.get('success'):
        history.append({'role': 'user', 'text': query})
        history.append({'role': 'model', 'text': result['answer']})
        request.session[session_key] = history[-_MAX_HISTORY:]
        request.session.modified = True

    return JsonResponse(result)


@login_required
@require_POST
def advisor_reset_api(request):
    """يصفّر تاريخ المحادثة في الـ session."""
    sector = (request.POST.get('sector') or 'printing').strip()
    if sector not in ('printing', 'automotive'):
        return HttpResponseBadRequest('invalid sector')
    request.session.pop(f'{_SESSION_KEY}_{sector}', None)
    request.session.modified = True
    return JsonResponse({'success': True})
