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

from .design_engine import analyze_idea, compose_mega_prompt, describe_reference_image, _llm_model
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
            # Primary path: mp_session cookie (matches _marketplace_auth in
            # clients.views._shared — the source of truth for marketplace auth)
            token = request.COOKIES.get('mp_session')
            if token:
                cust = MarketplaceCustomer.objects.filter(
                    session_token=token, is_verified=True, is_blocked=False,
                ).first()
                if cust:
                    return None, cust
            # Legacy fallback: Django session (rarely populated for marketplace)
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
# ⚠️ مفيش @login_required — الـ analyze endpoint مفتوح للأنواع الثلاثة:
#   • Django staff users (tenant context)
#   • Marketplace customers (mp_session cookie)
#   • Public visitors (للـ try-before-buy flow)
# الـ generate endpoint هو اللي بيـ enforce credit gate حسب الـ audience.
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
        err_code = str(result.get('error', ''))
        if 'key_missing' in err_code:
            msg = '🔑 خدمة الذكاء الاصطناعي مش مفعّلة — كلّم الإدارة.'
        elif 'timeout' in err_code:
            msg = '⏱️ التحليل بياخد وقت — جرب تاني.'
        elif 'http_400' in err_code or 'http_403' in err_code or result.get('all_models_failed'):
            msg = '🤖 موديل الذكاء مش متاح على حسابك حالياً — كلّم الإدارة (Together AI model access).'
        elif 'http_429' in err_code:
            msg = '⏳ الخدمة مشغولة (rate limit) — استنى ثانيتين وحاول تاني.'
        elif 'too_few' in err_code or 'no_valid_fields' in err_code or 'invalid_schema' in err_code:
            msg = '💡 الفكرة محتاجة تفاصيل أكتر — مثال: "تيشرت قطن أبيض عليه اسم خالد" بدل "تيشرت" بس.'
        else:
            msg = '⚠️ مقدرناش نحلل الفكرة — جرب تصيغها بتفاصيل أكتر (نوع المنتج + الاستخدام + التفاصيل).'
        return JsonResponse({
            'success': False, 'message': msg, 'error': err_code,
            'detail': result.get('detail', '')[:300] if result.get('detail') else '',
        }, status=200)

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
# ⚠️ مفيش @login_required — الـ identity بتيتحدد عبر audience param +
# _resolve_actor (tenant schema أو mp_session cookie). الـ credit gate
# فـ _check_balance بيرفض أي طلب بدون hookup صحيح.
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
    reference_images = body.get('reference_images') or []  # list of {data_url, hint}

    if audience not in _VALID_AUDIENCES:
        return JsonResponse({'success': False, 'error': 'invalid_audience'}, status=400)
    if not raw_idea:
        return JsonResponse({'success': False, 'message': 'الفكرة فاضية.'}, status=400)
    if not isinstance(selections, dict):
        return JsonResponse({'success': False, 'message': 'الاختيارات بصيغة غلط.'}, status=400)
    if not isinstance(reference_images, list) or len(reference_images) > 3:
        return JsonResponse({'success': False, 'message': 'مرجع الصور غلط (أقصى 3 صور).'}, status=400)

    # Sanitize selections
    clean_selections = {
        str(k)[:40]: str(v)[:120]
        for k, v in selections.items()
        if k and v
    }

    # 🖼️ Analyze reference images (vision LLM) — قبل ما نخصم credit
    ref_descriptions = []
    for ref in reference_images[:3]:
        if not isinstance(ref, dict):
            continue
        data_url = (ref.get('data_url') or '').strip()
        hint = (ref.get('hint') or '')[:60]
        if not data_url.startswith('data:image/'):
            continue
        # Limit size: data URLs > 2MB are too large for vision API
        if len(data_url) > 2_800_000:  # ~2MB base64 ≈ 2.1MB raw
            return JsonResponse({
                'success': False,
                'message': '⚠️ صورة مرفوعة كبيرة جداً (الحد الأقصى 2 ميجا لكل صورة).',
            }, status=400)
        vis = describe_reference_image(data_url, hint=hint)
        if vis.get('success'):
            label = f'[{hint}] ' if hint else ''
            ref_descriptions.append(label + vis['description'])
        else:
            logger.warning(f'[DESIGN GENERATE] vision failed: {vis.get("error")}')

    # Credit pre-check
    tenant, customer = _resolve_actor(request, audience)
    balance, gate_msg = _check_balance(audience, tenant, customer)
    if gate_msg:
        return JsonResponse({
            'success': False, 'need_topup': True,
            'message': gate_msg, 'balance': balance or {}, 'audience': audience,
        }, status=200)

    # Stage A: compose mega prompt via LLM (مع أوصاف الصور المرجعية لو موجودة)
    try:
        mega = compose_mega_prompt(raw_idea, domain, clean_selections,
                                   reference_descriptions=ref_descriptions or None)
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

    # 🛡️ DEFENSIVE: لو هنعمل overlay، نمسح أي حروف عربية + علامات اقتباس من
    # الـ prompt قبل ما يوصل لـ FLUX (عشان لو الـ LLM ضمّن النص الأصلي).
    # كمان نضيف بنود سلبية صريحة ضد كتابة أي نص أو حروف.
    if mega.get('text_overlay'):
        import re as _re
        # امسح Arabic ranges + الاقتباسات + أرقام عربية
        mega_prompt = _re.sub(r'[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+', '', mega_prompt)
        mega_prompt = _re.sub(r'["«»“”]', '', mega_prompt)
        mega_prompt = _re.sub(r'\s{2,}', ' ', mega_prompt).strip()
        # نقوي negative prompt
        forbid = 'any text, any letters, any characters, any words, any writing, lorem ipsum, garbled text, fake text, gibberish, calligraphy'
        negative = (negative + ', ' + forbid)[:600]
        logger.info(f'[DESIGN GENERATE] text overlay active → stripped Arabic + reinforced negative')

    # Stage B: generate image via Together FLUX
    try:
        img = generate_flux_image(mega_prompt, size=size, negative_prompt=negative)
    except Exception as e:
        logger.exception('[DESIGN GENERATE] image crashed')
        return JsonResponse({'success': False, 'message': '⚠️ تعذر توليد الصورة.', 'error': str(e)}, status=200)

    if not img.get('success'):
        err_code = str(img.get('error', ''))
        detail = str(img.get('detail', ''))[:300]
        msg = '⚠️ توليد الصورة فشل — جرب تاني.'
        if 'timeout' in err_code:
            msg = '⏱️ التوليد ياخد وقت أطول من المتوقع — جرب تاني.'
        elif 'key_missing' in err_code:
            msg = '🔑 خدمة توليد الصور مش مفعّلة.'
        elif 'all_models_failed' in err_code:
            msg = f'🤖 كل موديلات الصور رفضت — كلّم الإدارة. السبب: {detail[:120]}'
        elif 'http_400' in err_code:
            msg = f'⚠️ الـ FLUX رفض الـ prompt — جرب تعدل الفكرة. التفصيل: {detail[:120]}'
        elif 'http_429' in err_code:
            msg = '⏳ الخدمة مشغولة (rate limit) — استنى ثانيتين وحاول تاني.'
        else:
            msg = f'⚠️ توليد الصورة فشل ({err_code}) — جرب تاني.'
        logger.warning(f'[DESIGN GENERATE] image failed: error={err_code} detail={detail}')
        return JsonResponse({
            'success': False, 'message': msg,
            'error': err_code, 'detail': detail,
        }, status=200)

    image_url = img.get('url')

    # 🅰️ Post-processing: overlay Arabic/text onto image if LLM specified text_overlay
    text_overlay_info = mega.get('text_overlay')
    overlay_applied = False
    if text_overlay_info and image_url:
        try:
            from .text_overlay import overlay_text_on_image_url, has_arabic
            # نطبق الـ overlay دايماً لو فيه text_overlay (حتى للنص الإنجليزي عشان نضمن وضوح)
            overlay_result = overlay_text_on_image_url(
                image_url=image_url,
                text=text_overlay_info['text'],
                position=text_overlay_info.get('position', 'center'),
                color=text_overlay_info.get('color', '#000000'),
                font_size_ratio=float(text_overlay_info.get('font_ratio', 0.08)),
            )
            if overlay_result.get('success'):
                # نبني absolute URL لو الـ storage path نسبي
                new_url = overlay_result['url']
                if new_url and new_url.startswith('/'):
                    new_url = request.build_absolute_uri(new_url)
                image_url = new_url
                overlay_applied = True
                logger.info(f'[DESIGN GENERATE] text overlay applied → {image_url}')
            else:
                logger.warning(f'[DESIGN GENERATE] overlay failed (non-fatal): {overlay_result.get("error")}')
        except Exception as e:
            logger.warning(f'[DESIGN GENERATE] overlay exception (non-fatal): {e}')

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

    # 📄 PDF download URL — متاح فقط لو في log_id (يفترض كده دايماً)
    print_spec_pdf_url = None
    if log_id:
        try:
            from django.urls import reverse
            print_spec_pdf_url = request.build_absolute_uri(
                reverse('design_print_spec_pdf', args=[log_id])
            )
        except Exception:
            print_spec_pdf_url = f'/ai/design/{log_id}/print-spec.pdf'

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
        'text_overlay_applied': overlay_applied,
        'print_spec_pdf_url': print_spec_pdf_url,
        'provider': img.get('provider'),
        'model': img.get('model'),
        'balance': credit_info.get('balance') if credit_info else None,
        'credit': credit_info,
    })


# ─────────────────────────────────────────────────────────────────────────────
# 3) Feedback — mark log as successful or not
# ─────────────────────────────────────────────────────────────────────────────
# ⚠️ مفيش @login_required — marketplace customers لازم يقدروا يقيّموا التصميم
# اللي ولّدوه. الـ log_id ownership المفروض يتم التحقق منه جوه (TODO: add it).
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


# ─────────────────────────────────────────────────────────────────────────────
# 📄 Print-Ready Spec PDF — download endpoint
# ─────────────────────────────────────────────────────────────────────────────
@login_required
def design_print_spec_pdf(request, log_id: int):
    """يـ generate و يـ return PDF بمواصفات الطباعة لتصميم معين.

    الـ access control: لازم الـ user يكون authenticated، ولو الـ log مرتبط
    بـ MarketplaceCustomer، لازم الـ user الحالي هو نفس الـ customer أو superuser.
    """
    from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
    from clients.models import AIPromptLearningLog, CustomerDesign
    from .print_spec_pdf import build_print_spec_pdf

    log = AIPromptLearningLog.objects.filter(id=log_id).first()
    if not log:
        # نـ render HTML بسيط بدل text/plain — أوضح للـ user لما يـ open في tab جديد
        logger.warning(f'[PDF] log_id={log_id} not found — requested by user={request.user.id}')
        return HttpResponseNotFound(
            '<!DOCTYPE html><html dir="rtl"><head><meta charset="utf-8">'
            '<title>التصميم غير موجود</title>'
            '<style>body{font-family:sans-serif;text-align:center;padding:40px;color:#475569;}</style>'
            '</head><body>'
            '<h2>⚠️ التصميم المطلوب غير موجود</h2>'
            '<p>الـ design log رقم {} مش متوفر — يمكن اتحذف، أو الـ link قديم.</p>'
            '<p><a href="/marketplace/design-store/my-designs/">← رجوع لتصاميمي</a></p>'
            '</body></html>'.format(log_id)
        )

    # Access check
    is_owner = (
        request.user.is_superuser
        or (log.user_id and log.user_id == request.user.id)
        or (log.customer_id and request.session.get('marketplace_customer_id') == log.customer_id)
    )
    if not is_owner:
        logger.warning(f'[PDF] access denied for log_id={log_id}, user={request.user.id}')
        return HttpResponseForbidden(
            '<!DOCTYPE html><html dir="rtl"><head><meta charset="utf-8">'
            '<title>غير مصرح</title>'
            '<style>body{font-family:sans-serif;text-align:center;padding:40px;color:#475569;}</style>'
            '</head><body>'
            '<h2>🛡️ غير مصرح بالوصول</h2>'
            '<p>التصميم ده مش بتاعك — مينفعش تحمّل الـ PDF بتاعه.</p>'
            '<p><a href="/marketplace/design-store/my-designs/">← رجوع لتصاميمي</a></p>'
            '</body></html>'
        )

    # ── Gather data ──
    text = ''
    text_color = '#000000'
    text_position = 'center'
    if isinstance(log.selections, dict):
        for k, v in log.selections.items():
            kl = (k or '').lower()
            if any(t in kl for t in ('text_on_design', 'text', 'النص', 'كتابة')) and v and not text:
                text = str(v)
            if 'color' in kl and isinstance(v, str) and v.startswith('#'):
                text_color = v

    # Category — نـ derive من detected_domain أو من ('category', ...)
    cat = (log.detected_domain or '').lower()
    if not cat or cat == 'universal_ai_design':
        cat = 'other'

    # Customer info
    customer_name = '—'
    customer_phone = '—'
    if log.customer_id:
        try:
            from clients.models import MarketplaceCustomer
            cust = MarketplaceCustomer.objects.filter(id=log.customer_id).first()
            if cust:
                customer_name = cust.full_name or cust.phone or '—'
                customer_phone = cust.phone or '—'
        except Exception:
            pass

    # Design code (نـ link لـ CustomerDesign لو موجود)
    design_code = f'AIL-{log.id}'
    cd = CustomerDesign.objects.filter(customer_id=log.customer_id, image_url=log.image_url).first() if log.customer_id else None
    if cd:
        design_code = str(cd.design_code)

    try:
        pdf_bytes = build_print_spec_pdf(
            design_code=design_code,
            image_url=log.image_url,
            text=text,
            text_color=text_color,
            text_position=text_position,
            category=cat,
            customer_name=customer_name,
            customer_phone=customer_phone,
            quantity=1,
            notes='',
            raw_idea=log.raw_input or '',
        )
    except ImportError as e:
        # ReportLab مش متثبت — مشكلة infrastructure
        logger.exception(f'[PDF] reportlab import failed for log_id={log_id}: {e}')
        return HttpResponse(
            '<!DOCTYPE html><html dir="rtl"><head><meta charset="utf-8"><title>الـ PDF service مش مفعّل</title>'
            '<style>body{font-family:sans-serif;text-align:center;padding:40px;color:#475569;}</style>'
            '</head><body>'
            '<h2>🔧 خدمة الـ PDF مش مفعّلة على السيرفر</h2>'
            '<p>كلّم الإدارة — مكتبة ReportLab محتاجة تتثبت.</p>'
            f'<p style="color:#94a3b8;font-size:12px;">Technical: {e}</p>'
            '</body></html>',
            content_type='text/html', status=503,
        )
    except Exception as e:
        logger.exception(f'[PDF] generation failed for log_id={log_id}: {e}')
        return HttpResponse(
            '<!DOCTYPE html><html dir="rtl"><head><meta charset="utf-8"><title>خطأ في توليد PDF</title>'
            '<style>body{font-family:sans-serif;text-align:center;padding:40px;color:#475569;}</style>'
            '</head><body>'
            '<h2>⚠️ خطأ في توليد ملف الـ PDF</h2>'
            '<p>حصل خطأ تقني أثناء بناء الـ PDF. الإدارة هتـ check.</p>'
            f'<p style="color:#94a3b8;font-size:12px;">Technical: {type(e).__name__}: {str(e)[:200]}</p>'
            '<p><a href="/marketplace/design-store/my-designs/">← رجوع لتصاميمي</a></p>'
            '</body></html>',
            content_type='text/html', status=500,
        )

    resp = HttpResponse(pdf_bytes, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="print-spec-{design_code}.pdf"'
    return resp
