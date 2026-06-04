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

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .design_engine import (
    _classify_presentation_category,
    PRESENTATION_CATEGORIES,
    analyze_idea,
    compose_mega_prompt,
    describe_reference_image,
    _llm_model,
)
from .printing_copilot import generate_flux_image, generate_design_image, pick_design_engine
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
        'presentation_category': result.get('presentation_category', ''),
        'subtype': result.get('subtype', ''),
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
    # 🎯 Presentation category — trust client (from analyze step) if valid, else derive.
    # ده اللي بيـ dispatch الـ recipe block في _MEGA_SYSTEM (apparel vs document vs ...).
    client_category = (body.get('presentation_category') or '').strip().lower()
    if client_category in PRESENTATION_CATEGORIES:
        presentation_category = client_category
    else:
        presentation_category = _classify_presentation_category(raw_idea, domain)

    # 🎯 Subtype — forwarded from the analyze step (if client sent it) or
    # derived inside compose_mega_prompt as a fallback. Critical for
    # footwear/apparel/furniture/etc. where the recipe has subtype branches.
    client_subtype = (body.get('subtype') or '').strip().lower()

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

    # 🎨 Load customer's brand profile (if exists and active) — auto-inherit
    # برand identity في كل تصميم. الـ apply_brand_profile() داخل compose_mega_prompt
    # بـ merge الـ brand defaults مع الـ explicit selections (explicit يفوز).
    brand_context = None
    brand_logo_url = None
    if customer is not None:
        try:
            bp = getattr(customer, 'brand_profile', None)
            if bp and bp.is_active:
                brand_context = bp.as_brand_context()
                if bp.auto_inject_logo and bp.has_logo:
                    brand_context['logo_described'] = True
                    try:
                        brand_logo_url = bp.logo_image.url
                    except Exception:
                        pass
                logger.info(
                    f'[DESIGN GENERATE] brand profile applied: {bp.brand_name} '
                    f'(colors={bp.auto_inject_colors} logo={bp.auto_inject_logo})'
                )
        except Exception as e:
            logger.warning(f'[DESIGN GENERATE] brand profile lookup failed: {e}')

    # Stage A: compose mega prompt via LLM (مع أوصاف الصور المرجعية لو موجودة)
    try:
        mega = compose_mega_prompt(raw_idea, domain, clean_selections,
                                   reference_descriptions=ref_descriptions or None,
                                   presentation_category=presentation_category,
                                   subtype=client_subtype or None,
                                   brand_context=brand_context)
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

    # 🛡️ APPAREL DEFENSIVE: FLUX بيرجع للـ dressforms حتى لو الـ LLM ما طلبهاش.
    # نـ append الـ anti-mannequin negative terms قسراً — ONLY لو category=apparel.
    if presentation_category == 'apparel':
        mannequin_guards = (
            'visible mannequin, mannequin head, mannequin neck, mannequin face, '
            'dressform, dress form, tailor dummy, headed mannequin, '
            'golden mannequin bust, wooden mannequin stand, mannequin stand, '
            'showroom dummy, plastic figure, white plastic torso, '
            'exposed support stand, base pedestal, store dummy'
        )
        negative = (negative + ', ' + mannequin_guards)[:1200]
        if 'invisible mannequin' not in mega_prompt.lower() and 'ghost mannequin' not in mega_prompt.lower():
            mega_prompt = (
                'INVISIBLE GHOST-MANNEQUIN presentation (NO visible mannequin, '
                'NO head, NO neck stand, NO dressform — garment shaped by an '
                'unseen wearer with realistic 3D volume). ' + mega_prompt
            )[:2500]

    # 🛡️ DOCUMENT DEFENSIVE: FLUX بيحب يضيف pencils/marble/props للـ documents.
    # نـ append هارد block + force flat-paper hint للـ mega_prompt.
    if presentation_category == 'document':
        document_guards = (
            'pencil, pen on desk, marble surface, wooden desk, oak desk, '
            'linen cloth, coffee mug, ceramic mug, plant, botanical, magazine, '
            'editorial flat-lay, props, depth of field, bokeh, out-of-focus background, '
            'perspective tilt, curled paper, paper stack, hand holding paper, '
            'fabric, cotton, jersey, mannequin, garment, shirt, '
            'lorem ipsum, fake invoice text, garbled text, fake business name, '
            'random numbers, gibberish letters, hallucinated text'
        )
        negative = (negative + ', ' + document_guards)[:1200]
        if 'flat digital' not in mega_prompt.lower() and 'flat document' not in mega_prompt.lower():
            mega_prompt = (
                'FLAT DIGITAL DOCUMENT TEMPLATE on pure white background (NO photography, '
                'NO marble, NO desk, NO pencils, NO props, NO depth-of-field — '
                'straight-on 90° flat scan view, document fills 90% of canvas). '
                + mega_prompt
            )[:2500]

    # 🛡️ FOOTWEAR DEFENSIVE: avoid mannequin/foot/leg.
    if presentation_category == 'footwear':
        footwear_guards = (
            'foot, leg, ankle, sock, person, model, human, mannequin, '
            'fabric texture, garment language, ghost mannequin'
        )
        negative = (negative + ', ' + footwear_guards)[:1200]

    # 🛡️ LOGO DEFENSIVE: pure mark, no product context.
    if presentation_category == 'logo':
        logo_guards = (
            'product, t-shirt, mannequin, photograph, mockup, scene, background props, '
            'fabric, paper texture, depth, shadow on surface, 3d object'
        )
        negative = (negative + ', ' + logo_guards)[:1200]

    # ═══════════════════════════════════════════════════════════════════
    # Stage B: Smart engine routing — Ideogram for text-critical, FLUX for photo
    # ─────────────────────────────────────────────────────────────────
    # text_overlay اللي رجع من compose_mega_prompt بيدلنا على وجود نص مطلوب.
    # لو الـ engine = ideogram، النص لازم يدخل الـ prompt مباشرة (Ideogram
    # بيرسمه بدقة)، والـ post-overlay بيتـ skip لأنه مش محتاج.
    # ═══════════════════════════════════════════════════════════════════
    text_overlay_info = mega.get('text_overlay')
    has_text_content = bool(text_overlay_info and text_overlay_info.get('text'))
    overlay_text_raw = text_overlay_info.get('text', '') if has_text_content else ''
    import re as _re_arabic
    has_arabic_in_text = bool(_re_arabic.search(r'[؀-ۿ]', overlay_text_raw))

    chosen_engine = pick_design_engine(
        category=presentation_category,
        has_text_content=has_text_content,
        has_arabic=has_arabic_in_text,
    )

    # ─── Ideogram-specific prompt adaptation ──────────────────────────
    # لو راحنا Ideogram، الـ prompt لازم يحوي النص نفسه (مش "blank zone").
    # نستبدل التعليمات الـ "leave clean zone for overlay" بـ instruction
    # صريحة "render the exact text X in [position]".
    ideogram_prompt = mega_prompt
    if chosen_engine == 'ideogram' and has_text_content:
        text_val = overlay_text_raw.strip()
        position = text_overlay_info.get('position', 'center')
        text_color = text_overlay_info.get('color', '#000000')
        # Strip the "leave blank zone for overlay" instructions — Ideogram renders text directly
        ideogram_prompt = _re_arabic.sub(
            r'(?i)(clean blank|empty rectangular|printable zone|ready for (?:text )?overlay|leave a clean[^.]*)',
            '',
            ideogram_prompt,
        )
        position_phrase = {
            'chest': 'centered on the upper chest area',
            'back': 'centered on the upper back panel',
            'top': 'at the top of the design',
            'bottom': 'at the bottom of the design',
            'center': 'centered prominently',
        }.get(position, 'centered prominently')
        # Inject explicit text render instruction at the start (highest priority for Ideogram)
        text_instruction = (
            f'IMPORTANT TEXT RENDERING: Render the exact text "{text_val}" '
            f'{position_phrase} in color {text_color}, using a professional, '
            f'highly legible font. The text MUST be spelled exactly as given. '
        )
        ideogram_prompt = (text_instruction + ideogram_prompt)[:1800]
        # Strip "any text, any letters" from negative_prompt — we WANT text now
        negative = _re_arabic.sub(
            r'(?i)(any text|any letters|any words|any numbers|fake text|'
            r'lorem ipsum|gibberish|placeholder text|garbled writing|'
            r'calligraphy attempts|typography),?\s*',
            '',
            negative,
        )
        negative = _re_arabic.sub(r',\s*,', ', ', negative).strip(', ')

    # Run the smart router
    try:
        active_prompt = ideogram_prompt if chosen_engine == 'ideogram' else mega_prompt
        img = generate_design_image(
            prompt=active_prompt,
            size=size,
            negative_prompt=negative,
            category=presentation_category,
            has_text_content=has_text_content,
            has_arabic=has_arabic_in_text,
            force_engine=chosen_engine,
            block_schnell_fallback=True,
        )
    except Exception as e:
        logger.exception('[DESIGN GENERATE] image crashed')
        return JsonResponse({'success': False, 'message': '⚠️ تعذر توليد الصورة.', 'error': str(e)}, status=200)

    active_engine = img.get('engine', 'flux')
    logger.info(
        f'[DESIGN GENERATE] category={presentation_category} '
        f'engine={active_engine} has_text={has_text_content} '
        f'has_arabic={has_arabic_in_text} '
        f'fallback_from={img.get("fallback_from", "")}'
    )

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
    # ⚠️ Skip overlay لو الـ engine = ideogram لأن النص بقى مدمج داخل الصورة الأصلية.
    # Ideogram بيرسم النص بدقة أعلى من أي PIL overlay، فلا داعي نـ double-render.
    overlay_applied = False
    overlay_skipped_reason = ''
    if active_engine == 'ideogram' and text_overlay_info:
        overlay_skipped_reason = 'ideogram_rendered_in_image'
        # نـ mark النص كـ "applied" عشان الـ UI يعرض metadata صحيح
        overlay_applied = True
        logger.info('[DESIGN GENERATE] overlay skipped — Ideogram rendered text in-image')
    elif text_overlay_info and image_url:
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

    # ═══════════════════════════════════════════════════════════════════
    # 🔍 QUALITY GATE — Vision-based verification + Auto-regenerate
    # ─────────────────────────────────────────────────────────────────
    # نـ verify الصورة المولّدة ضد الـ brief. لو الـ verdict ضعيف،
    # نعيد التوليد مرة واحدة بـ prompt augmented بالـ correction.
    # ده الـ safety net النهائي اللي يمنع وصول design غلط للعميل.
    # ═══════════════════════════════════════════════════════════════════
    quality_report = None
    quality_regenerated = False
    quality_gate_enabled = bool(
        getattr(settings, 'DESIGN_QUALITY_GATE_ENABLED', True)
        and image_url
    )

    if quality_gate_enabled:
        try:
            from .design_engine import verify_design_quality
            expected_text = (
                text_overlay_info.get('text', '') if text_overlay_info else ''
            )
            quality_report = verify_design_quality(
                image_url=image_url,
                raw_idea=raw_idea,
                category=presentation_category,
                subtype=(mega.get('subtype') or body.get('subtype') or '').strip() or None,
                expected_text=expected_text or None,
            )
            logger.info(
                f'[QUALITY GATE] success={quality_report.get("success")} '
                f'score={quality_report.get("score")} '
                f'verdict={quality_report.get("verdict")} '
                f'subtype_match={quality_report.get("subtype_match")} '
                f'issues={quality_report.get("key_issues")}'
            )

            # 🔄 Auto-regenerate لو verdict ضعيف
            if (quality_report.get('success')
                    and quality_report.get('should_regenerate')
                    and not quality_regenerated):
                suggestion = quality_report.get('correction_suggestion') or ''
                issues_text = '; '.join(quality_report.get('key_issues') or [])
                logger.warning(
                    f'[QUALITY GATE] verdict={quality_report.get("verdict")} '
                    f'score={quality_report.get("score")} — auto-regenerating. '
                    f'Suggestion: {suggestion[:150]}'
                )

                # Augment الـ prompt بالـ correction
                correction_block = (
                    f'CRITICAL FIX REQUIRED — previous attempt failed quality check. '
                    f'Issues found: {issues_text[:250]}. '
                    f'You MUST FIX: {suggestion}. '
                )
                augmented_prompt = (
                    correction_block
                    + (ideogram_prompt if chosen_engine == 'ideogram' else mega_prompt)
                )[:2700]

                try:
                    retry_img = generate_design_image(
                        prompt=augmented_prompt,
                        size=size,
                        negative_prompt=negative,
                        category=presentation_category,
                        has_text_content=has_text_content,
                        has_arabic=has_arabic_in_text,
                        force_engine=chosen_engine,
                        block_schnell_fallback=True,
                    )
                    if retry_img.get('success') and retry_img.get('url'):
                        # Apply overlay على الصورة الجديدة لو مش Ideogram
                        new_url = retry_img['url']
                        if active_engine != 'ideogram' and text_overlay_info:
                            try:
                                from .text_overlay import overlay_text_on_image_url
                                retry_ov = overlay_text_on_image_url(
                                    image_url=new_url,
                                    text=text_overlay_info['text'],
                                    position=text_overlay_info.get('position', 'center'),
                                    color=text_overlay_info.get('color', '#000000'),
                                    font_size_ratio=float(text_overlay_info.get('font_ratio', 0.08)),
                                )
                                if retry_ov.get('success'):
                                    new_url = retry_ov['url']
                                    if new_url and new_url.startswith('/'):
                                        new_url = request.build_absolute_uri(new_url)
                            except Exception:
                                pass
                        image_url = new_url
                        img = retry_img  # update so response shows the retry's model
                        active_engine = retry_img.get('engine', active_engine)
                        quality_regenerated = True

                        # Re-verify the new image (cheap & gives final score)
                        try:
                            requalified = verify_design_quality(
                                image_url=image_url,
                                raw_idea=raw_idea,
                                category=presentation_category,
                                subtype=(mega.get('subtype') or body.get('subtype') or '').strip() or None,
                                expected_text=expected_text or None,
                            )
                            if requalified.get('success'):
                                # نخلي الـ original report محفوظ كـ history لكن
                                # نعرض النتيجة الجديدة كـ final
                                quality_report = {
                                    **requalified,
                                    'auto_regenerated': True,
                                    'original_verdict': quality_report.get('verdict'),
                                    'original_score': quality_report.get('score'),
                                }
                        except Exception as e:
                            logger.warning(f'[QUALITY GATE] re-verify failed: {e}')
                    else:
                        logger.warning(
                            f'[QUALITY GATE] retry generation failed: '
                            f'{retry_img.get("error")} — keeping original'
                        )
                except Exception as e:
                    logger.warning(f'[QUALITY GATE] retry exception (non-fatal): {e}')
        except Exception as e:
            logger.warning(f'[QUALITY GATE] verification exception (non-fatal): {e}')

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
            # 🔍 Quality Gate fields
            presentation_category=(presentation_category or '')[:20],
            detected_subtype=((mega.get('subtype') or body.get('subtype') or '') if isinstance(mega, dict) else '')[:20],
            quality_score=(quality_report or {}).get('score') if quality_report else None,
            quality_verdict=((quality_report or {}).get('verdict') or '')[:20] if quality_report else '',
            quality_issues=(quality_report or {}).get('key_issues') or [],
            auto_regenerated=quality_regenerated,
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

    # 💾 Auto-save to portfolio for customer audience → unified marketplace loop
    # يولّد design_code فوراً عشان الـ refinement chat يقدر يفتح ويعدل التصميم
    # من غير ما المستخدم يحتاج يدوس thumbs-up الأول.
    # 🚫 NO DEDUP — كل generation = CustomerDesign جديد. لو دوّبلنا بالـ
    # image_url match هنرجع للمستخدم الصورة القديمة (cache من قبل ترقية
    # _MEGA_SYSTEM) → بيكسر الـ test loop.
    design_code = None
    design_id = None
    if audience == 'customer' and customer and image_url:
        try:
            from clients.models import CustomerDesign
            valid_sizes = dict(CustomerDesign.SIZE_PRESETS).keys()
            size_preset = size if size in valid_sizes else 'auto'
            title = (raw_idea or 'تصميم AI')[:60]
            sel_lines = '\n'.join(f'• {k}: {v}' for k, v in (clean_selections or {}).items())
            desc = (
                f'المجال: {domain}\n'
                f'الفكرة: {raw_idea}\n\n'
                f'الاختيارات:\n{sel_lines}'
            )[:2000]
            design = CustomerDesign.objects.create(
                customer_id=customer.id,
                is_free_trial=True,
                title=title,
                description=desc,
                category='other',
                raw_input=(raw_idea or '')[:2000],
                engineered_prompt=(mega_prompt or '')[:2000],
                negative_prompt=(negative or '')[:1000],
                image_url=image_url[:600],
                model_used=(img.get('model') or 'flux')[:50],
                size_preset=size_preset,
            )
            design_code = str(design.design_code)
            design_id = design.id
            # سجّل أول رسالتين في الـ chat history (user idea + assistant image)
            try:
                from clients.models import DesignChatMessage
                DesignChatMessage.objects.create(
                    design=design, role='user', content=raw_idea[:500], image_url=''
                )
                DesignChatMessage.objects.create(
                    design=design, role='assistant',
                    content=f'تم توليد التصميم: {title}',
                    image_url=image_url[:600],
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning(f'[DESIGN GENERATE] auto-save to portfolio failed (non-fatal): {e}')

    # 🧵 Structured product/print metadata — category-driven labels
    text_overlay_info = mega.get('text_overlay') if isinstance(mega, dict) else None
    dims = mega.get('print_dimensions_cm') if isinstance(mega, dict) else None
    if isinstance(dims, dict) and dims.get('width') and dims.get('height'):
        dims_label = f"{dims['width']} × {dims['height']} cm"
    else:
        dims_label = size

    # Per-category material + print-tech labels (visible in sidebar)
    CATEGORY_META = {
        'apparel':   ('100% Combed Cotton — Jersey Knit', 'DTG / Screen-Print (integrated ink absorption)'),
        'document':  ('Coated 150-300 gsm paper', 'Digital offset (CMYK)'),
        'footwear':  ('Leather / Mesh upper', 'Product photography'),
        'accessory': ('Per-product material', 'Sublimation / DTG / Vinyl (per surface)'),
        'logo':      ('Vector artwork', 'Brand asset (transparent PNG / SVG)'),
        'signage':   ('Flex / Vinyl banner', 'Solvent / Eco-solvent print'),
        'packaging': ('350 gsm Cardstock', 'Offset + UV varnish'),
        'interior':  ('Architectural visualization', 'Render (not a physical print)'),
        'vehicle':   ('Cast vinyl wrap', 'Solvent print + laminate'),
        'other':     (None, None),
    }
    material, print_tech = CATEGORY_META.get(presentation_category, (None, None))

    structured_meta = {
        'category': presentation_category,
        'material': material,
        # backwards-compat: keep 'fabric' key for the apparel sidebar template
        'fabric': material if presentation_category == 'apparel' else None,
        'print_tech': print_tech,
        'placement': (mega.get('print_placement') if isinstance(mega, dict) else None) or (
            'front' if presentation_category == 'apparel' else None
        ),
        'suggested_dimensions': dims_label,
        'print_dimensions_cm': dims if isinstance(dims, dict) else None,
        'text_overlay': bool(text_overlay_info),
        'overlay_applied': overlay_applied,
        'engine': active_engine,                                       # 'ideogram' | 'flux'
        'overlay_skipped_reason': overlay_skipped_reason or None,
        'text_rendered_in_image': active_engine == 'ideogram' and has_text_content,
        # 🔍 Quality Gate metadata (visible in sidebar)
        'quality_score': (quality_report or {}).get('score') if quality_report else None,
        'quality_verdict': (quality_report or {}).get('verdict') if quality_report else None,
        'quality_issues': (quality_report or {}).get('key_issues') if quality_report else None,
        'auto_regenerated': quality_regenerated,
        # 🎨 Brand profile metadata
        'brand_applied': (mega.get('brand_applied') or {}).get('applied', False),
        'brand_name': (mega.get('brand_applied') or {}).get('brand_name', ''),
        'brand_logo_url': brand_logo_url,
    }

    # 🎨 Bump brand profile usage counter (non-fatal)
    if customer is not None and (mega.get('brand_applied') or {}).get('applied'):
        try:
            bp = getattr(customer, 'brand_profile', None)
            if bp and bp.is_active:
                from django.db.models import F
                type(bp).objects.filter(pk=bp.pk).update(
                    designs_with_brand=F('designs_with_brand') + 1,
                )
        except Exception:
            pass

    return JsonResponse({
        'success': True,
        'log_id': log_id,
        'design_code': design_code,
        'design_id': design_id,
        'audience': audience,
        'domain': domain,
        'presentation_category': presentation_category,
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
        'engine': active_engine,
        'engine_fallback_from': img.get('fallback_from'),
        'quality_report': quality_report,
        'auto_regenerated': quality_regenerated,
        'structured_meta': structured_meta,
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

    # ── Gather data — defensive: unified schema may store text_overlay as
    # either a flat string OR a nested {text, position, color} dict. كل
    # extraction بـ try/except عشان أي شكل غير متوقع ميـ crash-ش الـ PDF.
    text = ''
    text_color = '#000000'
    text_position = 'center'

    def _safe_str(val) -> str:
        """يـ extract نص نظيف من أي قيمة. dict → field 'text'. list → join. غير كده → str()."""
        if val is None:
            return ''
        if isinstance(val, str):
            return val.strip()
        if isinstance(val, dict):
            return str(val.get('text') or val.get('content') or val.get('value') or '').strip()
        if isinstance(val, (list, tuple)):
            return ', '.join(_safe_str(x) for x in val if x).strip()
        return str(val).strip()

    selections = log.selections if isinstance(log.selections, dict) else {}
    try:
        for k, v in selections.items():
            kl = (k or '').lower()
            # text content (دعم النص الجديد + الـ keys القديمة)
            if any(t in kl for t in ('text_on_design', 'text_overlay', 'text', 'النص', 'كتابة')) and not text:
                extracted = _safe_str(v)
                if extracted:
                    text = extracted
            # color — يدعم string مباشر أو nested {color: '#xxx'}
            if 'color' in kl:
                if isinstance(v, str) and v.startswith('#'):
                    text_color = v
                elif isinstance(v, dict):
                    c = v.get('color') or v.get('hex')
                    if isinstance(c, str) and c.startswith('#'):
                        text_color = c
            # placement / position — unified schema بيستخدم print_placement
            if any(t in kl for t in ('placement', 'position', 'مكان', 'موضع')):
                p = _safe_str(v).lower()
                if p in ('front', 'back', 'chest', 'center', 'top', 'bottom'):
                    text_position = p
    except Exception as e:
        logger.warning(f'[PDF] selections extraction error (non-fatal): {e}')

    # print_placement من الـ mega prompt result (لو متخزن في selections مباشرة)
    placement_direct = selections.get('print_placement') if selections else None
    if isinstance(placement_direct, str) and placement_direct.lower() in ('front', 'back'):
        text_position = placement_direct.lower()

    # Category — نـ derive من detected_domain
    cat = (log.detected_domain or '').lower().strip()
    if not cat or cat == 'universal_ai_design':
        cat = 'other'

    # Customer info — كله behind try/except
    customer_name = '—'
    customer_phone = '—'
    if log.customer_id:
        try:
            from clients.models import MarketplaceCustomer
            cust = MarketplaceCustomer.objects.filter(id=log.customer_id).first()
            if cust:
                customer_name = (getattr(cust, 'full_name', None) or getattr(cust, 'phone', None) or '—')
                customer_phone = getattr(cust, 'phone', None) or '—'
        except Exception as e:
            logger.warning(f'[PDF] customer lookup failed (non-fatal): {e}')

    # Design code — نـ link لـ CustomerDesign لو موجود. guard ضد image_url=None
    design_code = f'AIL-{log.id}'
    if log.customer_id and log.image_url:
        try:
            cd = CustomerDesign.objects.filter(
                customer_id=log.customer_id, image_url=log.image_url,
            ).first()
            if cd:
                design_code = str(cd.design_code)
        except Exception as e:
            logger.warning(f'[PDF] design_code lookup failed (non-fatal): {e}')

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


# ─────────────────────────────────────────────────────────────────────────────
# 📄 Print-Ready Spec PDF — via CustomerDesign.design_code (gallery cards path)
# ─────────────────────────────────────────────────────────────────────────────
@login_required
def design_print_spec_pdf_by_code(request, design_code):
    """نسخة من design_print_spec_pdf بتـ accept UUID design_code بدل log_id.

    هي اللي بيـ link لها الـ buttons في gallery cards (لما الـ user يكون شايف
    تصاميم محفوظة، مفيش معاه log_id مباشرة). بـ resolve الـ log من
    CustomerDesign.image_url match، وبيقع على CustomerDesign data كـ fallback.
    """
    from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
    from clients.models import AIPromptLearningLog, CustomerDesign, MarketplaceCustomer
    from .print_spec_pdf import build_print_spec_pdf

    cd = CustomerDesign.objects.filter(design_code=design_code).first()
    if not cd:
        logger.warning(f'[PDF] CustomerDesign with code={design_code} not found')
        return HttpResponseNotFound(
            '<!DOCTYPE html><html dir="rtl"><head><meta charset="utf-8">'
            '<title>التصميم غير موجود</title>'
            '<style>body{font-family:sans-serif;text-align:center;padding:40px;color:#475569;}</style>'
            '</head><body>'
            f'<h2>⚠️ التصميم رقم {design_code} غير موجود</h2>'
            '<p>الـ design code مش متعرف عليه — يمكن اتحذف أو الـ link غلط.</p>'
            '<p><a href="/marketplace/design-store/my-designs/">← رجوع لتصاميمي</a></p>'
            '</body></html>'
        )

    # Access check — العميل لازم يكون مالك التصميم أو superuser
    customer_id_in_session = request.session.get('marketplace_customer_id')
    is_owner = (
        request.user.is_superuser
        or (cd.customer_id and customer_id_in_session == cd.customer_id)
    )
    if not is_owner:
        logger.warning(f'[PDF] access denied for design_code={design_code}, user={request.user.id}')
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

    # نحاول نلاقي الـ log المرتبط (نفس customer + image_url)
    log = AIPromptLearningLog.objects.filter(
        customer_id=cd.customer_id,
        image_url=cd.image_url,
    ).first()

    # Gather text + color من log أو من CustomerDesign
    text = ''
    text_color = '#000000'
    text_position = 'center'
    if log and isinstance(log.selections, dict):
        for k, v in log.selections.items():
            kl = (k or '').lower()
            if any(t in kl for t in ('text_on_design', 'text', 'النص', 'كتابة')) and v and not text:
                text = str(v)
            if 'color' in kl and isinstance(v, str) and v.startswith('#'):
                text_color = v
    if not text:
        text = (cd.title or cd.raw_input or '')[:200]

    cat = (log.detected_domain if log else cd.category) or 'other'

    customer_name = '—'
    customer_phone = '—'
    cust = MarketplaceCustomer.objects.filter(id=cd.customer_id).first() if cd.customer_id else None
    if cust:
        customer_name = cust.full_name or cust.phone or '—'
        customer_phone = cust.phone or '—'

    try:
        pdf_bytes = build_print_spec_pdf(
            design_code=str(cd.design_code),
            image_url=cd.image_url,
            text=text,
            text_color=text_color,
            text_position=text_position,
            category=cat,
            customer_name=customer_name,
            customer_phone=customer_phone,
            quantity=1,
            notes='',
            raw_idea=(log.raw_input if log else cd.raw_input) or '',
        )
    except ImportError as e:
        logger.exception(f'[PDF] reportlab import failed for design_code={design_code}: {e}')
        return HttpResponse(
            '<h2>🔧 خدمة الـ PDF مش مفعّلة على السيرفر</h2>'
            f'<p>Technical: {e}</p>',
            content_type='text/html', status=503,
        )
    except Exception as e:
        logger.exception(f'[PDF] generation failed for design_code={design_code}: {e}')
        return HttpResponse(
            '<h2>⚠️ خطأ في توليد ملف الـ PDF</h2>'
            f'<p>Technical: {type(e).__name__}: {str(e)[:200]}</p>'
            '<p><a href="/marketplace/design-store/my-designs/">← رجوع لتصاميمي</a></p>',
            content_type='text/html', status=500,
        )

    resp = HttpResponse(pdf_bytes, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="print-spec-{cd.design_code}.pdf"'
    return resp
