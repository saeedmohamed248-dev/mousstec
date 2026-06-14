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



# AI diagnostic check + prompt engineering.

from .utils import *  # noqa: F401, F403



@login_required
def ai_diagnostic_check(request):
    """
    🔬 Diagnostic endpoint — Tests Gemini API key directly with raw HTTP call.
    Returns full error details so we can debug the actual failure.
    Only admins can access this.
    """
    if not request.user.is_superuser:
        try:
            if request.user.employee_profile.role != 'admin':
                return JsonResponse({'error': 'Admin only'}, status=403)
        except Exception:
            return JsonResponse({'error': 'Admin only'}, status=403)

    import requests as _req

    api_key = getattr(settings, 'AI_VISION_API_KEY', None)
    ai_enabled = getattr(settings, 'ENABLE_AI_PREDICTIONS', False)

    diagnostics = {
        'ai_enabled': ai_enabled,
        'key_set': bool(api_key),
        'key_length': len(api_key) if api_key else 0,
        'key_prefix': api_key[:10] + '...' if api_key and len(api_key) > 10 else None,
        'tests': [],
    }

    if not api_key:
        diagnostics['verdict'] = 'API key is empty in settings — check .env file'
        return JsonResponse(diagnostics)

    clean_key = str(api_key).strip()

    # Test 1: List available models (cheapest call)
    try:
        list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={clean_key}"
        r = _req.get(list_url, timeout=10)
        test1 = {
            'test': 'list_models',
            'status_code': r.status_code,
            'success': r.status_code == 200,
        }
        if r.status_code == 200:
            data = r.json()
            test1['models_found'] = len(data.get('models', []))
            test1['sample_models'] = [m['name'] for m in data.get('models', [])[:5]]
        else:
            test1['error'] = r.text[:500]
        diagnostics['tests'].append(test1)
    except Exception as e:
        diagnostics['tests'].append({'test': 'list_models', 'error': str(e)})

    # Test 2: Try a minimal generation request
    for model in ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-pro']:
        try:
            gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={clean_key}"
            r = _req.post(
                gen_url,
                json={"contents": [{"role": "user", "parts": [{"text": "Say hello"}]}]},
                timeout=15,
            )
            test = {
                'test': f'generate_{model}',
                'status_code': r.status_code,
                'success': r.status_code == 200,
            }
            if r.status_code == 200:
                try:
                    out = r.json()['candidates'][0]['content']['parts'][0]['text']
                    test['response_snippet'] = out[:100]
                except Exception:
                    test['warning'] = 'Got 200 but could not parse response'
            else:
                test['error'] = r.text[:500]
            diagnostics['tests'].append(test)
            if r.status_code == 200:
                break  # success — no need to test more models
        except Exception as e:
            diagnostics['tests'].append({'test': f'generate_{model}', 'error': str(e)})

    # Verdict
    any_success = any(t.get('success') for t in diagnostics['tests'])
    if any_success:
        diagnostics['verdict'] = '✅ Gemini API is reachable. Check the application logs for the actual failure.'
    else:
        first_err = next((t.get('error', '') for t in diagnostics['tests'] if t.get('error')), '')
        if 'API_KEY_INVALID' in first_err or 'invalid' in first_err.lower():
            diagnostics['verdict'] = '❌ API key is INVALID. Get a new key from https://aistudio.google.com/apikey'
        elif 'PERMISSION_DENIED' in first_err or '403' in str(first_err):
            diagnostics['verdict'] = '❌ API key is restricted. Check key restrictions in Google Cloud Console.'
        elif 'QUOTA_EXCEEDED' in first_err or 'quota' in first_err.lower():
            diagnostics['verdict'] = '❌ API quota exceeded. Wait or upgrade your Gemini plan.'
        else:
            diagnostics['verdict'] = f'❌ Unknown error. First error: {first_err[:200]}'

    return JsonResponse(diagnostics, json_dumps_params={'indent': 2, 'ensure_ascii': False})


@csrf_exempt
@login_required
@require_POST
def ai_prompt_engineer(request):
    """
    🎨 AI Prompt Engineer Agent — migrated to Together AI (Phase N.6 follow-up).

    Takes casual Arabic/English design description → returns cinematic
    FLUX/Ideogram prompt + design_category + recommended_size.

    Was on OpenAI gpt-4o-mini; now uses the same Together fallback chain as
    compose_mega_prompt. Removes the last OpenAI dependency in the tenant
    AI Studio entry points.
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'ai_generation')
    if not allowed:
        return JsonResponse({'status': 'error', 'error': error}, status=403)

    raw_input = request.POST.get('prompt', '').strip()
    if not raw_input:
        return JsonResponse({
            'status': 'error',
            'error': 'يرجى كتابة وصف التصميم المطلوب.',
        }, status=400)

    try:
        from erp_core.ai.design_engine import _call_together_llm
    except ImportError as e:
        logger.error(f'[PROMPT ENGINEER] design_engine import failed: {e}')
        return JsonResponse({
            'status': 'error',
            'error': 'محرك الذكاء غير متاح حالياً. تواصل مع الإدارة.',
        }, status=500)

    llm = _call_together_llm(_PROMPT_ENGINEER_SYSTEM, raw_input, temperature=0.4)
    if not llm.get('success'):
        err_code = llm.get('error', 'llm_failed')
        # Map common Together failures to user-friendly Arabic messages.
        friendly = {
            'together_key_missing':       'مفتاح Together AI غير مُعد. تواصل مع مسؤول المنصة.',
            'together_llm_http_429':      'تم تجاوز حدود محرك الذكاء. حاول بعد دقيقة.',
            'together_llm_invalid_json':  'خطأ في تحليل رد المحرك. حاول صياغة الوصف بشكل مختلف.',
        }.get(err_code, f'تعذرت صياغة البرومبت ({err_code}). حاول مرة أخرى.')
        status = 429 if err_code == 'together_llm_http_429' else 502
        logger.warning(
            f'[PROMPT ENGINEER] llm failed: {err_code} — '
            f'{(llm.get("detail") or "")[:200]}'
        )
        return JsonResponse({'status': 'error', 'error': friendly}, status=status)

    result = llm.get('data') or {}

    # The system prompt may instruct the LLM to refuse out-of-scope requests
    if result.get('status') == 'rejected':
        return JsonResponse(result, status=400)

    if not result.get('engineered_prompt'):
        return JsonResponse({
            'status': 'error',
            'error': 'لم يتم توليد البرومبت. حاول وصف التصميم بشكل أوضح.',
        }, status=502)

    # Defensive defaults — keep response shape identical to the legacy
    # OpenAI version so the frontend doesn't need any change.
    result.setdefault('status', 'success')
    result.setdefault('original_intent', raw_input)
    result.setdefault('design_category', 'other')
    result.setdefault(
        'negative_prompt',
        'blurry, low quality, distorted text, artifacts, watermark, '
        'cropped, jpeg artifacts, low resolution, pixelated',
    )
    result.setdefault('recommended_size', '1024x1024')
    result.setdefault('recommended_quality', 'hd')

    logger.info(
        f'🎨 [PROMPT ENGINEER]: {getattr(tenant, "name", "?")} — '
        f'engine=together model={llm.get("model_used")} '
        f'category={result["design_category"]} by {request.user.username}'
    )
    return JsonResponse(result)
