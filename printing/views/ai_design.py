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



# AI design generation, watermark, WhatsApp send, studio status.

from .utils import *  # noqa: F401, F403



@csrf_exempt
@login_required
@require_POST
def ai_generate_design(request):
    """
    🎨 Tenant-side AI design generation — migrated to Smart Router (Phase N.6).

    Pipeline (unified with marketplace customer side):
        compose_mega_prompt  → builds cinematic prompt + extracts category
        generate_design_image → Smart Router (FLUX for photo, Ideogram for text)
        composite_logo       → PIL paste of Client.logo (FLUX outputs only)
        verify_design_quality → vision-based gate (optional)
        _apply_watermark_to_url → legacy diagonal watermark (preserved)

    Same POST body + same JSON response shape as the legacy OpenAI version —
    the frontend is untouched. New fields appended to the response are
    additive only (engine_used, brand_applied, logo_composited, quality_score).

    Gated by subscription + AI quota (unchanged).
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'ai_generation')
    if not allowed:
        return JsonResponse({'success': False, 'error': error}, status=403)

    # ── Inputs ──────────────────────────────────────────────────
    prompt = request.POST.get('prompt', '').strip()
    size = request.POST.get('size', '1024x1024')
    quality = request.POST.get('quality', 'standard')
    raw_input_text = request.POST.get('raw_input', prompt[:300])
    negative_prompt = request.POST.get('negative_prompt', '')
    design_category = request.POST.get('design_category', 'other')
    add_watermark = request.POST.get('add_watermark') == 'true'
    # 🆕 M1 — frontend sets pre_engineered=1 لو الـ prompt جاي من ai_prompt_engineer
    # عشان نـ skip الـ compose_mega_prompt LLM call ونوفر ~$0.002/توليد.
    pre_engineered = request.POST.get('pre_engineered') == '1'
    # one-shot logo upload (overrides the persistent tenant.logo for this request)
    logo_file = request.FILES.get('logo')

    if not prompt:
        return JsonResponse({'success': False, 'error': 'يرجى كتابة وصف التصميم المطلوب.'}, status=400)

    # 🛡️ Defensive size whitelist — Smart Router handles dimension parsing
    valid_sizes = ['1024x1024', '1024x1536', '1536x1024', '1024x1792', '1792x1024', 'auto']
    if size not in valid_sizes:
        size = '1024x1024'
    if size == 'auto':
        size = '1024x1024'

    # ── Build tenant brand_context (Phase N.6 minimal: logo + name + industry) ──
    brand_context = _tenant_brand_context(tenant, request_logo_file=logo_file)

    # ── Stage A: compose mega prompt (LLM-orchestrated) ─────────
    try:
        from erp_core.ai.design_engine import compose_mega_prompt
        mega = compose_mega_prompt(
            raw_idea=prompt,
            domain=design_category if design_category != 'other' else '',
            selections={},
            brand_context=brand_context,
            presentation_category=design_category if design_category != 'other' else None,
            already_engineered=pre_engineered,
        )
    except Exception as e:
        logger.exception(f'[AI STUDIO] mega compose crashed for {getattr(tenant, "name", "?")}')
        return JsonResponse({
            'success': False,
            'error': f'تعذرت صياغة البرومبت: {str(e)[:200]}',
        }, status=500)

    if not mega.get('success'):
        return JsonResponse({
            'success': False,
            'error': f'مقدرناش نصيغ البرومبت — جرب تعدل الوصف. (الخطأ: {mega.get("error", "")})',
        }, status=502)

    mega_prompt = mega['mega_prompt']
    final_negative = (negative_prompt + ' ' + mega.get('negative_prompt', '')).strip()[:1200]
    final_size = mega.get('recommended_size', size)
    presentation_category = mega.get('presentation_category')
    text_overlay = mega.get('text_overlay')
    has_text = bool(text_overlay and text_overlay.get('text'))

    # ── Stage B: generate image via Smart Router ────────────────
    try:
        from erp_core.ai.printing_copilot import generate_design_image
        img = generate_design_image(
            prompt=mega_prompt,
            size=final_size,
            negative_prompt=final_negative,
            category=presentation_category,
            has_text_content=has_text,
        )
    except Exception as e:
        logger.exception(f'[AI STUDIO] image generation crashed for {getattr(tenant, "name", "?")}')
        return JsonResponse({
            'success': False,
            'error': f'تعذر توليد الصورة: {str(e)[:200]}',
        }, status=500)

    if not img.get('success') or not img.get('url'):
        err_code = img.get('error', 'unknown')
        err_detail = (img.get('detail') or '')[:200]
        logger.warning(f'[AI STUDIO] generation failed: {err_code} — {err_detail}')
        # Map known error codes to friendly Arabic messages
        friendly = {
            'together_key_missing':  'مفتاح Together AI غير مُعد في النظام. تواصل مع مسؤول المنصة.',
            'ideogram_key_missing':  'مفتاح Ideogram غير مُعد. النظام هيستخدم FLUX بديل.',
        }.get(err_code, f'محرك التوليد رجع خطأ ({err_code}). برجاء المحاولة مرة أخرى.')
        return JsonResponse({'success': False, 'error': friendly}, status=502)

    image_url = img['url']
    used_engine = img.get('engine', 'flux')
    used_model = img.get('model') or used_engine

    # ── Stage C: composite brand logo (FLUX only) ───────────────
    # Ideogram already "draws" brand identity into the design itself — pasting
    # on top would corrupt the renderer's intent. FLUX outputs get the real
    # logo composited via PIL (same pipeline as marketplace).
    logo_source = None
    if logo_file:
        logo_source = logo_file
    elif tenant and getattr(tenant, 'logo', None) and tenant.logo:
        logo_source = tenant.logo

    logo_composited = False
    if logo_source and used_engine != 'ideogram':
        try:
            from erp_core.ai.logo_overlay import composite_logo_on_image_url
            comp = composite_logo_on_image_url(
                image_url=image_url,
                logo_source=logo_source,
                category=presentation_category or '',
                text_overlay_position=(
                    (text_overlay or {}).get('position') if has_text else None
                ),
            )
            if comp.get('success'):
                new_url = comp['url']
                if new_url and new_url.startswith('/'):
                    new_url = request.build_absolute_uri(new_url)
                image_url = new_url
                logo_composited = True
            elif not comp.get('skipped'):
                logger.warning(
                    f'[AI STUDIO] logo composite failed (non-fatal): {comp.get("error")}'
                )
        except Exception as e:
            logger.warning(f'[AI STUDIO] logo composite exception (non-fatal): {e}')

    # ── Stage D: optional quality gate (vision verification) ────
    quality_score = None
    if bool(getattr(settings, 'DESIGN_QUALITY_GATE_ENABLED', True)):
        try:
            from erp_core.ai.design_engine import verify_design_quality
            qr = verify_design_quality(
                image_url=image_url,
                raw_idea=prompt,
                category=presentation_category,
                expected_text=(text_overlay or {}).get('text') if has_text else None,
            )
            if qr.get('success'):
                quality_score = qr.get('score')
        except Exception as e:
            logger.warning(f'[AI STUDIO] quality gate failed (non-fatal): {e}')

    # ── Stage E: legacy watermark (preserved verbatim) ──────────
    watermarked_url = ''
    if add_watermark:
        try:
            watermarked_url = _apply_watermark_to_url(
                image_url, tenant.name if tenant else 'Mousstec', tenant, request,
            )
        except Exception as e:
            logger.warning(f'[AI STUDIO] Watermark failed: {e}')

    # ── Stage F: deduct quota + log session (preserved) ─────────
    from clients.models import AILimitTracker, AIStudioSession
    AILimitTracker.deduct(tenant, 'ai_generation', metadata={
        'prompt': mega_prompt[:200],
        'size': final_size,
        'quality': quality,
        'model': used_model,
        'engine': used_engine,
        'category': presentation_category,
        'logo_used': bool(logo_source),
        'logo_composited': logo_composited,
        'watermarked': bool(watermarked_url),
        'quality_score': quality_score,
        'brand_applied': (mega.get('brand_applied') or {}).get('applied', False),
        'user': request.user.username,
    })

    session = AIStudioSession.objects.create(
        tenant=tenant,
        user=request.user,
        raw_input=raw_input_text[:2000],
        engineered_prompt=mega_prompt[:5000],
        negative_prompt=final_negative[:1000],
        design_category=design_category[:50],
        logo_used=bool(logo_source),
        image_url=image_url,
        image_size=final_size,
        image_quality=quality,
        model_used=(used_model or '')[:50],
        watermarked=bool(watermarked_url),
        watermarked_image_url=watermarked_url,
    )
    if logo_file:
        logo_file.seek(0)
        session.logo_image = logo_file
        session.save(update_fields=['logo_image'])

    logger.info(
        f'🤖 [AI STUDIO]: {getattr(tenant, "name", "?")} — engine={used_engine} '
        f'category={presentation_category} logo_composited={logo_composited} '
        f'quality={quality_score} by {request.user.username} (session #{session.pk})'
    )

    return JsonResponse({
        'success': True,
        'image_url': watermarked_url or image_url,
        'original_url': image_url,
        'watermarked_url': watermarked_url,
        'revised_prompt': mega_prompt,
        'model_used': used_model,
        # 🆕 Additive response fields (Phase N.6) — frontend ignores unknowns
        'engine_used': used_engine,
        'presentation_category': presentation_category,
        'brand_applied': (mega.get('brand_applied') or {}).get('applied', False),
        'logo_composited': logo_composited,
        'quality_score': quality_score,
        'session_id': session.pk,
    })


@csrf_exempt
@login_required
@require_POST
def ai_smart_watermark(request):
    """
    Apply smart watermark to an uploaded image.
    Uses PIL (Pillow) — no external API needed, but gated by AI subscription.
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'smart_watermark')
    if not allowed:
        return JsonResponse({'success': False, 'error': error}, status=403)

    image_file = request.FILES.get('image')
    watermark_text = request.POST.get('watermark_text', tenant.name if tenant else 'Mousstec')
    opacity = int(request.POST.get('opacity', 40))

    if not image_file:
        return JsonResponse({'success': False, 'error': 'يرجى رفع صورة.'}, status=400)

    try:
        from PIL import Image, ImageDraw, ImageFont
        import io

        img = Image.open(image_file).convert('RGBA')
        txt_layer = Image.new('RGBA', img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)

        # Dynamic font size based on image width
        font_size = max(int(img.width / 15), 24)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

        # Get text bounding box
        bbox = draw.textbbox((0, 0), watermark_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Diagonal watermark pattern
        import math
        for y in range(0, img.height, text_h * 4):
            for x in range(-img.width, img.width * 2, text_w + 100):
                draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, opacity))

        watermarked = Image.alpha_composite(img, txt_layer).convert('RGB')

        # Save to buffer
        buffer = io.BytesIO()
        watermarked.save(buffer, format='JPEG', quality=92)
        buffer.seek(0)

        img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

        # Deduct quota
        from clients.models import AILimitTracker
        AILimitTracker.deduct(tenant, 'smart_watermark', metadata={
            'watermark_text': watermark_text,
            'user': request.user.username,
        })

        logger.info(f"🏷️ [WATERMARK]: {tenant.name} — Applied by {request.user.username}")

        return JsonResponse({
            'success': True,
            'image_base64': f'data:image/jpeg;base64,{img_base64}',
        })

    except ImportError:
        return JsonResponse({'success': False, 'error': 'مكتبة Pillow غير مثبتة.'}, status=500)
    except Exception as e:
        logger.error(f"🔴 [WATERMARK ERROR]: {tenant.name} — {e}")
        return JsonResponse({'success': False, 'error': 'حدث خطأ أثناء معالجة الصورة.'}, status=500)


@csrf_exempt
@login_required
@require_POST
def ai_send_whatsapp(request):
    """
    Generate a WhatsApp send link for a design image.
    Uses wa.me deep link (no API needed). Deducts from whatsapp quota.
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'whatsapp_send')
    if not allowed:
        return JsonResponse({'success': False, 'error': error}, status=403)

    phone = request.POST.get('phone', '').strip()
    image_url = request.POST.get('image_url', '').strip()
    message = request.POST.get('message', '').strip()

    if not phone:
        return JsonResponse({'success': False, 'error': 'يرجى إدخال رقم واتساب العميل.'}, status=400)

    # Normalize phone number
    import re
    phone_clean = re.sub(r'[^\d+]', '', phone)
    if phone_clean.startswith('0'):
        phone_clean = '2' + phone_clean  # Egypt country code
    if not phone_clean.startswith('+'):
        phone_clean = '+' + phone_clean

    # Build WhatsApp message
    if not message:
        company_name = tenant.name if tenant else 'الاستوديو'
        message = f'مرحباً، تصميمك جاهز من {company_name}!'
    if image_url:
        message += f'\n\nالتصميم: {image_url}'

    # URL encode
    from urllib.parse import quote
    wa_url = f'https://wa.me/{phone_clean.lstrip("+")}?text={quote(message)}'

    # Deduct quota
    from clients.models import AILimitTracker
    AILimitTracker.deduct(tenant, 'whatsapp_send', metadata={
        'phone': phone_clean,
        'user': request.user.username,
    })

    logger.info(f"📱 [WHATSAPP]: {tenant.name} — Sent by {request.user.username} to {phone_clean}")

    return JsonResponse({
        'success': True,
        'whatsapp_url': wa_url,
    })


@login_required
def ai_studio_status(request):
    """Return AI Studio subscription status and remaining quota for current tenant.

    🎁 يجمع رصيد الباقة المدفوعة + رصيد الهدايا من السوبر أدمن.
    """
    tenant = _get_tenant()
    if not tenant:
        return JsonResponse({'active': False, 'reason': 'no_tenant'})

    from clients.models import TenantSubscription, AILimitTracker

    # 🎁 احسب الرصيد المهدى من السوبر أدمن (مستقل عن الباقة)
    bonus_designs = AILimitTracker._get_bonus_remaining(tenant, 'ai_generation')
    bonus_whatsapp = AILimitTracker._get_bonus_remaining(tenant, 'whatsapp_send')
    bonus_watermarks = AILimitTracker._get_bonus_remaining(tenant, 'smart_watermark')

    try:
        sub = tenant.subscription
        has_paid_addon = bool(sub.is_active and sub.ai_addon)
    except TenantSubscription.DoesNotExist:
        sub = None
        has_paid_addon = False

    # لو مفيش باقة ولا هدية → نقول للعميل
    if not has_paid_addon and bonus_designs == 0 and bonus_whatsapp == 0 and bonus_watermarks == 0:
        return JsonResponse({'active': False, 'reason': 'no_ai_addon'})

    # ✅ في رصيد (إما باقة أو هدية أو الاتنين)
    ai_used = AILimitTracker.get_monthly_usage(tenant, 'ai_generation')
    wm_used = AILimitTracker.get_monthly_usage(tenant, 'smart_watermark')

    paid_ai_limit = sub.ai_addon.ai_generations_limit if has_paid_addon else 0
    paid_wm_limit = sub.ai_addon.whatsapp_messages_limit if has_paid_addon else 0
    paid_ai_remaining = max(0, paid_ai_limit - ai_used)
    paid_wm_remaining = max(0, paid_wm_limit - wm_used)

    return JsonResponse({
        'active': True,
        'addon_name': sub.ai_addon.name if has_paid_addon else '🎁 هدية الإدارة',
        # الإجمالي = باقة + هدية
        'ai_limit': paid_ai_limit + bonus_designs,
        'ai_used': ai_used,
        'ai_remaining': paid_ai_remaining + bonus_designs,
        'wm_limit': paid_wm_limit + bonus_watermarks,
        'wm_used': wm_used,
        'wm_remaining': paid_wm_remaining + bonus_watermarks,
        # تفاصيل الهدية للعرض في الواجهة
        'bonus': {
            'designs': bonus_designs,
            'whatsapp': bonus_whatsapp,
            'watermarks': bonus_watermarks,
            'has_gift': (bonus_designs + bonus_whatsapp + bonus_watermarks) > 0,
        },
    })
