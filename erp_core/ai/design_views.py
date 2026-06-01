"""
🧠 Universal AI Design — Unified endpoints for Customers AND Tenants
=====================================================================
Three endpoints:
  POST /ai/design/analyze/   → raw idea → dynamic JSON schema (dropdowns)
  POST /ai/design/generate/  → idea + selections → mega prompt + image + log
  POST /ai/design/feedback/  → mark a log entry as is_successful (true/false)

No hardcoded categories. The schema is produced by Together LLM for whichever
domain the user is working in.
"""
from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .design_engine import analyze_idea, compose_mega_prompt, _llm_model
from .printing_copilot import generate_flux_image
from .credits import (
    get_tenant_balance, get_customer_balance,
    consume_tenant_credit, consume_customer_credit,
)

logger = logging.getLogger('mouss_tec_core')

_VALID_AUDIENCES = {'customer', 'tenant'}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — actor resolution
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_actor(request, audience: str):
    """يرجع (tenant, customer) tuple based on audience. واحد بس يكون موجود."""
    if audience == 'tenant':
        try:
            from django.db import connection
            from clients.models import Client
            schema = getattr(connection, 'schema_name', 'public')
            if schema == 'public':
                return None, None
            tenant = Client.objects.filter(schema_name=schema).first()
            return tenant, None
        except Exception:
            return None, None

    if audience == 'customer':
        try:
            from clients.models import MarketplaceCustomer
            cust_id = request.session.get('marketplace_customer_id')
            if cust_id:
                return None, MarketplaceCustomer.objects.filter(id=cust_id).first()
        except Exception:
            pass
    return None, None


def _check_balance(audience: str, tenant, customer):
    if audience == 'tenant':
        if not tenant:
            return None, '⚠️ مش قادرين نحدد شركتك. سجل دخول من حساب الشركة.'
        bal = get_tenant_balance(tenant)
        if bal.get('total', 0) <= 0:
            return bal, '💳 رصيد التصاميم خلص. اشحن باقتك.'
        return bal, None
    if not customer:
        return None, '⚠️ سجل دخول كعميل عشان تولد تصميم.'
    bal = get_customer_balance(customer)
    if bal.get('total', 0) <= 0:
        return bal, '💳 رصيد التصاميم خلص. اشحن باقتك.'
    return bal, None


def _read_json(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}'), None
    except json.JSONDecodeError:
        return None, JsonResponse({'success': False, 'error': 'invalid_json'}, status=400)


# ─────────────────────────────────────────────────────────────────────────────
# 1) Analyze — raw idea → dynamic schema
# ─────────────────────────────────────────────────────────────────────────────
@login_required
@require_POST
def design_analyze(request):
    body, err = _read_json(request)
    if err:
        return err

    raw_idea = (body.get('raw_idea') or '').strip()
    if not raw_idea:
        return JsonResponse({'success': False, 'message': 'اكتب فكرة التصميم الأول.'}, status=400)
    if len(raw_idea) > 800:
        return JsonResponse({'success': False, 'message': 'الفكرة طويلة جداً (800 حرف كحد أقصى).'}, status=400)

    try:
        result = analyze_idea(raw_idea)
    except Exception as e:
        logger.exception('[DESIGN ANALYZE] crashed')
        return JsonResponse({'success': False, 'message': '⚠️ تعذر تحليل الفكرة الآن.', 'error': str(e)}, status=200)

    if not result.get('success'):
        err_code = result.get('error', '')
        if 'key_missing' in err_code:
            msg = '🔑 خدمة الذكاء الاصطناعي مش مفعّلة — كلّم الإدارة.'
        elif 'timeout' in err_code:
            msg = '⏱️ التحليل بياخد وقت — جرب تاني.'
        else:
            msg = '⚠️ مقدرناش نحلل الفكرة — جرب تصيغها بشكل تاني.'
        return JsonResponse({'success': False, 'message': msg, 'error': err_code}, status=200)

    return JsonResponse({
        'success': True,
        'raw_idea': raw_idea,
        'domain': result['domain'],
        'domain_ar': result.get('domain_ar', ''),
        'fields': result['fields'],
    })


# ─────────────────────────────────────────────────────────────────────────────
# 2) Generate — idea + selections → mega prompt + image + log
# ─────────────────────────────────────────────────────────────────────────────
@login_required
@require_POST
def design_generate(request):
    body, err = _read_json(request)
    if err:
        return err

    raw_idea = (body.get('raw_idea') or '').strip()
    domain = (body.get('domain') or '').strip()
    selections = body.get('selections') or {}
    audience = (body.get('audience') or 'customer').strip()
    size_hint = (body.get('size') or '').strip() or None

    if audience not in _VALID_AUDIENCES:
        return JsonResponse({'success': False, 'error': 'invalid_audience'}, status=400)
    if not raw_idea:
        return JsonResponse({'success': False, 'message': 'الفكرة فاضية.'}, status=400)
    if not isinstance(selections, dict):
        return JsonResponse({'success': False, 'message': 'الاختيارات بصيغة غلط.'}, status=400)

    # Sanitize selections
    clean_selections = {
        str(k)[:40]: str(v)[:120]
        for k, v in selections.items()
        if k and v
    }

    # Credit pre-check
    tenant, customer = _resolve_actor(request, audience)
    balance, gate_msg = _check_balance(audience, tenant, customer)
    if gate_msg:
        return JsonResponse({
            'success': False, 'need_topup': True,
            'message': gate_msg, 'balance': balance or {}, 'audience': audience,
        }, status=200)

    # Stage A: compose mega prompt via LLM
    try:
        mega = compose_mega_prompt(raw_idea, domain, clean_selections)
    except Exception as e:
        logger.exception('[DESIGN GENERATE] mega compose crashed')
        return JsonResponse({'success': False, 'message': '⚠️ تعذر صياغة البرومبت.', 'error': str(e)}, status=200)

    if not mega.get('success'):
        return JsonResponse({
            'success': False,
            'message': '⚠️ مقدرناش نصيغ البرومبت — جرب تعدل اختياراتك.',
            'error': mega.get('error', ''),
        }, status=200)

    mega_prompt = mega['mega_prompt']
    negative = mega['negative_prompt']
    size = size_hint or mega['recommended_size']

    # Stage B: generate image via Together FLUX
    try:
        img = generate_flux_image(mega_prompt, size=size, negative_prompt=negative)
    except Exception as e:
        logger.exception('[DESIGN GENERATE] image crashed')
        return JsonResponse({'success': False, 'message': '⚠️ تعذر توليد الصورة.', 'error': str(e)}, status=200)

    if not img.get('success'):
        err_code = img.get('error', '')
        msg = '⚠️ توليد الصورة فشل — جرب تاني.'
        if 'timeout' in err_code:
            msg = '⏱️ التوليد ياخد وقت أطول من المتوقع — جرب تاني.'
        elif 'key_missing' in err_code:
            msg = '🔑 خدمة توليد الصور مش مفعّلة.'
        return JsonResponse({'success': False, 'message': msg, 'error': err_code}, status=200)

    image_url = img.get('url')

    # Consume credit
    credit_info = None
    try:
        meta = {'category': domain or 'universal_ai_design', 'size': size, 'model': img.get('model')}
        if audience == 'tenant' and tenant:
            credit_info = consume_tenant_credit(tenant, meta)
        elif audience == 'customer' and customer:
            credit_info = consume_customer_credit(customer, meta)
    except Exception as e:
        logger.warning(f'[DESIGN GENERATE] credit consume failed (non-fatal): {e}')

    # Log to Data Flywheel
    log_id = None
    try:
        from clients.models import AIPromptLearningLog
        log = AIPromptLearningLog.objects.create(
            audience=audience,
            user=request.user if request.user.is_authenticated else None,
            tenant=tenant,
            customer=customer,
            raw_input=raw_idea[:2000],
            detected_domain=(domain or '')[:80],
            dynamic_schema=body.get('schema') or {},
            selections=clean_selections,
            mega_prompt=mega_prompt[:2000],
            negative_prompt=negative[:500],
            image_url=(image_url or '')[:600],
            image_size=size[:20],
            llm_model=_llm_model()[:80],
            image_model=(img.get('model') or '')[:80],
        )
        log_id = log.id
    except Exception as e:
        logger.warning(f'[DESIGN GENERATE] flywheel log failed (non-fatal): {e}')

    return JsonResponse({
        'success': True,
        'log_id': log_id,
        'audience': audience,
        'domain': domain,
        'raw_idea': raw_idea,
        'selections': clean_selections,
        'mega_prompt': mega_prompt,
        'negative_prompt': negative,
        'image_url': image_url,
        'image_b64': img.get('b64_json'),
        'size': size,
        'provider': img.get('provider'),
        'model': img.get('model'),
        'balance': credit_info.get('balance') if credit_info else None,
        'credit': credit_info,
    })


# ─────────────────────────────────────────────────────────────────────────────
# 3) Feedback — mark log as successful or not
# ─────────────────────────────────────────────────────────────────────────────
@login_required
@require_POST
def design_feedback(request):
    """يسجّل تقييم المستخدم. لو is_successful=True والمستخدم عميل، بنحفظ التصميم
    تلقائياً في الـ Portfolio (CustomerDesign) عشان يقدر يبعته للمطبعة لاحقاً."""
    body, err = _read_json(request)
    if err:
        return err

    log_id = body.get('log_id')
    is_successful = body.get('is_successful')

    if not log_id or not isinstance(is_successful, bool):
        return JsonResponse({'success': False, 'error': 'invalid_payload'}, status=400)

    try:
        from clients.models import AIPromptLearningLog, CustomerDesign
        log = AIPromptLearningLog.objects.filter(id=log_id).first()
        if not log:
            return JsonResponse({'success': False, 'error': 'log_not_found'}, status=404)

        log.is_successful = is_successful
        log.feedback_at = timezone.now()
        log.save(update_fields=['is_successful', 'feedback_at'])

        result = {'success': True, 'log_id': log.id, 'is_successful': is_successful}

        # 🖼️ لو إيجابي + عميل → احفظ في الـ Portfolio (CustomerDesign)
        if is_successful and log.customer_id and log.image_url:
            existing = CustomerDesign.objects.filter(
                customer_id=log.customer_id, image_url=log.image_url,
            ).first()
            if existing:
                design = existing
                created = False
            else:
                # CustomerDesign.size_preset مقيد بـ CHOICES — نطبّعه
                valid_sizes = dict(CustomerDesign.SIZE_PRESETS).keys()
                size_preset = log.image_size if log.image_size in valid_sizes else 'auto'

                title = (log.raw_input or 'تصميم AI')[:60]
                # نضم اختيارات المستخدم في الـ description
                sel_lines = '\n'.join(f'• {k}: {v}' for k, v in (log.selections or {}).items())
                desc = (
                    f'المجال: {log.detected_domain}\n'
                    f'الفكرة: {log.raw_input}\n\n'
                    f'الاختيارات:\n{sel_lines}'
                )[:2000]

                design = CustomerDesign.objects.create(
                    customer_id=log.customer_id,
                    is_free_trial=True,
                    title=title,
                    description=desc,
                    category='other',  # universal flow — مفيش mapping للـ CHOICES
                    raw_input=(log.raw_input or '')[:2000],
                    engineered_prompt=(log.mega_prompt or '')[:2000],
                    negative_prompt=(log.negative_prompt or '')[:1000],
                    image_url=log.image_url[:600],
                    model_used=(log.image_model or 'flux')[:50],
                    size_preset=size_preset,
                )
                created = True

            result['saved_to_portfolio'] = True
            result['design_id'] = design.id
            result['design_code'] = str(design.design_code)
            result['portfolio_was_existing'] = not created

        return JsonResponse(result)
    except Exception as e:
        logger.exception('[DESIGN FEEDBACK] failed')
        return JsonResponse({'success': False, 'error': str(e)}, status=200)
