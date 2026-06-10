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


def _get_tenant():
    """Get current tenant from connection schema."""
    from clients.models import Client
    schema = connection.schema_name
    if schema == 'public':
        return None
    return Client.objects.filter(schema_name=schema).first()


def _check_ai_access(tenant, action_type='ai_generation'):
    """
    Check if tenant has active AI subscription and remaining quota.
    Returns (allowed: bool, error_message: str or None)

    🔧 Fix (post Phase N.6 smoke test): the previous version short-circuited
    on `not sub.ai_addon`, which blocked tenants who had a super-admin
    bonus grant but no paid addon — contradicting ai_studio_status which
    honors bonuses. Now we delegate to AILimitTracker.can_use() which
    correctly checks the bonus pool BEFORE the paid subscription.
    """
    if not tenant:
        return False, 'لا يمكن تحديد المستأجر.'

    from clients.models import TenantSubscription, AILimitTracker
    try:
        sub = tenant.subscription
    except TenantSubscription.DoesNotExist:
        return False, 'لا يوجد اشتراك مفعّل. تواصل مع الإدارة لتفعيل حزمة AI Studio.'

    if not sub.is_active:
        return False, 'اشتراكك غير مفعّل حالياً. تواصل مع الإدارة لتجديد الاشتراك.'

    if AILimitTracker.can_use(tenant, action_type):
        return True, None

    # Denied — distinguish the two distinct failure modes for a useful message:
    #   • no paid addon AND no bonus left → tell user to contact admin
    #   • paid addon exists but quota exhausted → tell user to wait for renewal
    bonus_remaining = AILimitTracker._get_bonus_remaining(tenant, action_type)
    if not sub.ai_addon and bonus_remaining == 0:
        return False, (
            'لم يتم تفعيل حزمة AI Studio على اشتراكك ولا توجد هدايا متاحة. '
            'تواصل مع الإدارة لإضافة حزمة AI أو منح هدية.'
        )
    return False, 'تم استنفاد حصتك الشهرية من هذه الخدمة. يتم تجديد الحصة في بداية كل شهر.'


def _apply_watermark_to_url(image_url, watermark_text, tenant, request):
    """
    Download image from URL, apply diagonal text watermark, save back to storage.
    Returns the absolute URL of the watermarked image.
    """
    import requests as _req
    import io as _io
    import uuid as _uuid
    from PIL import Image, ImageDraw, ImageFont
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    # Fetch the original image
    if image_url.startswith('http'):
        r = _req.get(image_url, timeout=30)
        img_bytes = r.content
    else:
        # Relative URL → resolve via storage
        rel_path = image_url.lstrip('/').replace('media/', '', 1)
        with default_storage.open(rel_path, 'rb') as f:
            img_bytes = f.read()

    img = Image.open(_io.BytesIO(img_bytes)).convert('RGBA')
    txt_layer = Image.new('RGBA', img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    font_size = max(int(img.width / 15), 28)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), watermark_text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    opacity = 55

    for y in range(0, img.height, text_h * 4):
        for x in range(-img.width, img.width * 2, text_w + 100):
            draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, opacity))

    watermarked = Image.alpha_composite(img, txt_layer).convert('RGB')
    buf = _io.BytesIO()
    watermarked.save(buf, format='JPEG', quality=90)
    buf.seek(0)

    schema = getattr(tenant, 'schema_name', 'public') if tenant else 'public'
    filename = f"ai_studio/{schema}/wm_{_uuid.uuid4().hex}.jpg"
    saved_path = default_storage.save(filename, ContentFile(buf.getvalue()))
    url = default_storage.url(saved_path)
    if url.startswith('/'):
        url = request.build_absolute_uri(url)
    return url


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


def _tenant_brand_context(tenant, *, request_logo_file=None):
    """🎨 يبني brand_context dict بسيط للـ tenant من الـ Client fields الموجودة.

    Phase N.6 — minimal brand profile: brand_name + industry + logo cue.
    The full symmetric model (TenantBrandProfile mirroring CustomerBrandProfile)
    is deferred to a future slice — this gets the tenant brand-aware
    immediately without a migration.

    Returns dict or None (no tenant → no brand context, classifier-side
    treats it as 'guest' generation).
    """
    if not tenant or not getattr(tenant, 'name', None):
        return None
    ctx = {
        'brand_name': tenant.name,
    }
    industry = getattr(tenant, 'industry', '') or ''
    if industry:
        ctx['industry'] = industry  # raw key — Smart Router doesn't care
    # Logo cue — instructs the LLM to leave a reserved area; the actual paste
    # happens in stage C via composite_logo_on_image_url.
    has_logo = False
    if request_logo_file:
        has_logo = True  # composite reads from request.FILES later
    elif getattr(tenant, 'logo', None) and tenant.logo:
        try:
            ctx['logo_url'] = tenant.logo.url
            has_logo = True
        except Exception:
            pass
    if has_logo:
        ctx['logo_described'] = True
    return ctx


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


# =====================================================================
# 🧠 Smart Business Copilot — متوصل بالداتابيز الفعلية
# =====================================================================

def _query_business_data(query, request=None):
    """
    يحلل سؤال المستخدم ويجيب من الداتابيز الفعلية.
    يرجع dict فيه: context (البيانات), intent (نوع السؤال)
    """
    from printing.models import (
        PrintOrder, PrintJob, PrintTransaction, PrintTreasury,
        PrintCustomer, PrintMaterial, Designer, DesignerWorkLog,
        MachineProfile, PrintBranch,
    )

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    q = query.lower()

    # ============ 1. مبيعات اليوم ============
    if any(k in q for k in ['بيع', 'مبيعات', 'ايراد', 'إيراد', 'دخل', 'بعنا', 'بيعنا', 'revenue', 'sales']):
        period = 'اليوم'
        date_filter = {'date__gte': today_start}
        if any(k in q for k in ['الشهر', 'شهر', 'شهري']):
            period = 'الشهر'
            date_filter = {'date__gte': month_start}
        elif any(k in q for k in ['امبارح', 'أمس', 'البارحه']):
            period = 'أمس'
            yesterday_start = today_start - timedelta(days=1)
            date_filter = {'date__gte': yesterday_start, 'date__lt': today_start}
        elif any(k in q for k in ['اسبوع', 'أسبوع', 'الاسبوع']):
            period = 'الأسبوع'
            date_filter = {'date__gte': today_start - timedelta(days=7)}

        income = PrintTransaction.objects.filter(
            transaction_type='in', **date_filter
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        order_count = PrintOrder.objects.filter(
            date_created__gte=date_filter.get('date__gte', today_start),
        ).count()

        return {
            'intent': 'sales',
            'context': f"إجمالي المبيعات/الإيرادات {period}: {income:,.2f} ج.م\nعدد الطلبات {period}: {order_count} طلب",
        }

    # ============ 2. مصاريف ============
    if any(k in q for k in ['مصاريف', 'مصروف', 'صرف', 'خرج', 'expense', 'مصروفات']):
        period = 'اليوم'
        date_filter = {'date__gte': today_start}
        if any(k in q for k in ['الشهر', 'شهر', 'شهري']):
            period = 'الشهر'
            date_filter = {'date__gte': month_start}
        elif any(k in q for k in ['اسبوع', 'أسبوع', 'الاسبوع']):
            period = 'الأسبوع'
            date_filter = {'date__gte': today_start - timedelta(days=7)}

        expenses = PrintTransaction.objects.filter(
            transaction_type='out', **date_filter
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        # تفاصيل أكبر 5 مصاريف
        top_expenses = PrintTransaction.objects.filter(
            transaction_type='out', **date_filter
        ).order_by('-amount')[:5]
        details = "\n".join(
            f"  • {tx.description or 'بدون وصف'}: {tx.amount:,.2f} ج.م"
            for tx in top_expenses
        )

        return {
            'intent': 'expenses',
            'context': f"إجمالي المصروفات {period}: {expenses:,.2f} ج.م\nأكبر المصروفات:\n{details}" if details else f"إجمالي المصروفات {period}: {expenses:,.2f} ج.م",
        }

    # ============ 3. فاتورة / طلب محدد ============
    order_match = re.search(r'(?:طلب|فاتور[ةه]|order|اوردر|أوردر)\s*(?:رقم|#|no)?\s*[#]?(\d+|PO-[\d-]+)', q, re.IGNORECASE)
    if not order_match:
        # Try standalone number with context
        order_match = re.search(r'(?:رقم|#)\s*(\d+)', q)
    if order_match:
        order_ref = order_match.group(1)
        # Try by number suffix or full order_number
        orders = PrintOrder.objects.filter(
            Q(order_number__icontains=order_ref) | Q(pk=int(order_ref) if order_ref.isdigit() else 0)
        )[:1]
        if orders:
            order = orders[0]
            jobs = order.jobs.all()
            total_cost = sum(j.actual_cost or j.calculated_cost for j in jobs)
            total_revenue = order.net_total
            profit = total_revenue - total_cost
            profit_status = "ربح ✅" if profit > 0 else ("خسارة ❌" if profit < 0 else "تعادل")

            jobs_detail = "\n".join(
                f"  • {j.description[:60]}: سعر {j.total_price:,.2f} — تكلفة {j.actual_cost or j.calculated_cost:,.2f}"
                for j in jobs
            )
            return {
                'intent': 'order_detail',
                'context': (
                    f"طلب #{order.order_number} — العميل: {order.customer.name}\n"
                    f"الحالة: {order.get_status_display()}\n"
                    f"الإجمالي: {order.total_amount:,.2f} ج.م | خصم: {order.discount:,.2f} | صافي: {total_revenue:,.2f}\n"
                    f"المدفوع: {order.paid_amount:,.2f} | المتبقي: {order.remaining:,.2f}\n"
                    f"التكلفة الفعلية: {total_cost:,.2f} ج.م\n"
                    f"الربح: {profit:,.2f} ج.م ({profit_status})\n"
                    f"المهام:\n{jobs_detail}" if jobs_detail else ""
                ),
            }
        return {'intent': 'order_not_found', 'context': f"لم أجد طلب برقم {order_ref}"}

    # ============ 4. أرباح ============
    if any(k in q for k in ['ربح', 'أرباح', 'ارباح', 'كسب', 'كسبنا', 'profit', 'صافي']):
        period = 'الشهر'
        date_filter = {'date_created__gte': month_start}
        if any(k in q for k in ['يوم', 'النهاردة', 'اليوم', 'today']):
            period = 'اليوم'
            date_filter = {'date_created__gte': today_start}

        income = PrintTransaction.objects.filter(
            transaction_type='in', date__gte=date_filter['date_created__gte']
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        expenses = PrintTransaction.objects.filter(
            transaction_type='out', date__gte=date_filter['date_created__gte']
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        profit = income - expenses

        # Also calculate from completed jobs
        completed_jobs = PrintJob.objects.filter(
            is_complete=True,
            completed_at__gte=date_filter['date_created__gte'],
        )
        job_profit = completed_jobs.aggregate(total=Sum('actual_profit'))['total'] or Decimal('0')

        return {
            'intent': 'profit',
            'context': (
                f"التقرير المالي — {period}:\n"
                f"  إجمالي الإيرادات: {income:,.2f} ج.م\n"
                f"  إجمالي المصروفات: {expenses:,.2f} ج.م\n"
                f"  صافي الربح (خزينة): {profit:,.2f} ج.م\n"
                f"  صافي ربح المهام المكتملة: {job_profit:,.2f} ج.م"
            ),
        }

    # ============ 5. خزينة / رصيد ============
    if any(k in q for k in ['خزينة', 'خزنة', 'رصيد', 'كاش', 'balance', 'treasury', 'فلوس']):
        treasuries = PrintTreasury.objects.filter(is_active=True)
        total = sum(t.balance for t in treasuries)
        details = "\n".join(f"  • {t.name}: {t.balance:,.2f} ج.م" for t in treasuries)
        return {
            'intent': 'treasury',
            'context': f"رصيد الخزائن:\n{details}\nالإجمالي: {total:,.2f} ج.م",
        }

    # ============ 6. عملاء ============
    if any(k in q for k in ['عميل', 'عملاء', 'customer', 'زبون', 'زباين']):
        # Search for specific customer
        name_match = re.search(r'(?:عميل|زبون)\s+(.+)', q)
        if name_match:
            name = name_match.group(1).strip()
            customers = PrintCustomer.objects.filter(name__icontains=name)[:5]
            if customers:
                details = "\n".join(
                    f"  • {c.name} | {c.phone or 'بدون رقم'} | {c.company or ''}"
                    for c in customers
                )
                return {'intent': 'customer_search', 'context': f"نتائج البحث:\n{details}"}
            return {'intent': 'customer_not_found', 'context': f"لم أجد عميل باسم '{name}'"}

        total_customers = PrintCustomer.objects.count()
        new_this_month = PrintCustomer.objects.filter(created_at__gte=month_start).count()
        return {
            'intent': 'customers',
            'context': f"إجمالي العملاء: {total_customers}\nعملاء جدد هذا الشهر: {new_this_month}",
        }

    # ============ 7. طلبات مفتوحة ============
    if any(k in q for k in ['طلب', 'طلبات', 'اوردر', 'order', 'شغل', 'مفتوح']):
        open_orders = PrintOrder.objects.filter(
            status__in=['draft', 'confirmed', 'in_progress']
        ).order_by('-date_created')[:10]
        if open_orders:
            details = "\n".join(
                f"  • #{o.order_number} — {o.customer.name} | {o.get_status_display()} | {o.net_total:,.2f} ج.م"
                for o in open_orders
            )
            return {
                'intent': 'open_orders',
                'context': f"الطلبات المفتوحة ({open_orders.count()}):\n{details}",
            }
        return {'intent': 'open_orders', 'context': "لا توجد طلبات مفتوحة حالياً 🎉"}

    # ============ 8. مخزون / خامات ============
    if any(k in q for k in ['مخزون', 'خامات', 'ورق', 'حبر', 'stock', 'inventory', 'خامه', 'material']):
        low_stock = PrintMaterial.objects.filter(quantity__lte=F('min_stock'))
        total_materials = PrintMaterial.objects.count()
        stock_value = PrintMaterial.objects.aggregate(
            val=Sum(F('quantity') * F('cost_per_unit'))
        )['val'] or 0

        if low_stock.exists():
            alerts = "\n".join(
                f"  ⚠️ {m.name}: {m.quantity} {m.unit} (الحد الأدنى: {m.min_stock})"
                for m in low_stock[:10]
            )
            return {
                'intent': 'stock',
                'context': f"إجمالي الخامات: {total_materials} | قيمة المخزون: {stock_value:,.2f} ج.م\n\nتنبيهات نقص:\n{alerts}",
            }
        return {
            'intent': 'stock',
            'context': f"إجمالي الخامات: {total_materials} | قيمة المخزون: {stock_value:,.2f} ج.م\nلا توجد تنبيهات نقص ✅",
        }

    # ============ 9. مصممين / أداء ============
    if any(k in q for k in ['مصمم', 'مصممين', 'designer', 'أداء', 'اداء', 'performance']):
        designers = Designer.objects.filter(is_active=True)
        results = []
        for d in designers:
            stats = d.get_month_stats()
            results.append(
                f"  • {d.user.get_full_name() or d.user.username}: "
                f"{stats['total_works'] or 0} عمل | "
                f"{stats['total_hours'] or 0} ساعة | "
                f"تقييم: {stats['avg_rating'] or '-'}/5"
            )
        if results:
            return {'intent': 'designers', 'context': f"أداء المصممين هذا الشهر:\n" + "\n".join(results)}
        return {'intent': 'designers', 'context': "لا يوجد مصممين مسجلين بعد."}

    # ============ 10. ماكينات ============
    if any(k in q for k in ['ماكينة', 'ماكينات', 'طابعة', 'machine', 'printer']):
        machines = MachineProfile.objects.filter(is_active=True)
        if machines:
            details = "\n".join(
                f"  • {m.name} ({m.get_machine_type_display()}) — تكلفة/ساعة: {m.hourly_operating_cost:,.2f} ج.م"
                for m in machines
            )
            return {'intent': 'machines', 'context': f"الماكينات النشطة ({machines.count()}):\n{details}"}
        return {'intent': 'machines', 'context': "لا توجد ماكينات مسجلة بعد."}

    # ============ 11. تصاميم / رصيد تصاميم AI ============
    if any(k in q for k in ['تصميم', 'تصاميم', 'design', 'باقي', 'رصيدي', 'كريدت']):
        try:
            from hr.models import AIDesignSubscription
            from printing.models import Designer
            # Check if user is a designer (request may be None)
            current_user = getattr(request, 'user', None) if request else None
            designer = Designer.objects.filter(user=current_user, is_active=True).first() if current_user else None
            if designer:
                # Get AI subscription
                sub = AIDesignSubscription.objects.filter(
                    designer__user=current_user, status='active'
                ).first()
                if sub:
                    remaining = (sub.ai_generations_limit - sub.ai_generations_used) if sub.ai_generations_limit > 0 else '∞'
                    return {
                        'intent': 'designs_balance',
                        'context': (
                            f"🎨 رصيد تصاميمك AI:\n"
                            f"  الباقة: {sub.get_plan_display()}\n"
                            f"  التصاميم المستخدمة: {sub.ai_generations_used}\n"
                            f"  المتبقي: {remaining}\n"
                            f"  الحالة: {sub.get_status_display()}\n"
                            f"  تنتهي: {sub.end_date or 'غير محدد'}"
                        ),
                    }
                return {
                    'intent': 'designs_balance',
                    'context': "ليس لديك اشتراك AI نشط حالياً. تواصل مع الإدارة لتفعيل باقة تصاميم AI.",
                }
            # Not a designer — show general design stats
            total_designs = DesignerWorkLog.objects.filter(
                date__gte=month_start
            ).count() if 'DesignerWorkLog' in dir() else 0
            return {
                'intent': 'designs_stats',
                'context': f"إجمالي أعمال التصميم هذا الشهر: {total_designs} عمل",
            }
        except Exception:
            return {'intent': 'designs_balance', 'context': "لم أتمكن من جلب بيانات التصاميم."}

    # ============ لم يتطابق — ارجع None ============
    return None


def _get_system_knowledge_printing():
    """بناء قاعدة معرفية شاملة عن سيستم المطبعة لـ Gemini"""
    return (
        "أنت Mouss Tec Copilot — المساعد الذكي الرسمي لنظام Mouss Tec لإدارة المطابع واستوديوهات التصميم.\n"
        "أنت عارف كل حاجة عن السيستم وبتساعد المستخدمين يفهموه ويستخدموه صح.\n\n"
        "## معرفتك بالسيستم:\n"
        "1. **طلبات الطباعة (PrintOrder)**: العميل بيعمل طلب → بيتضاف مهام طباعة (PrintJob) → كل مهمة ليها نوع بند (تيشرت/كارت/بنر/إلخ) وماكينة ومصمم وسعر وتكلفة\n"
        "2. **نوع البند (ProductType)**: أي حاجة المطبعة بتطبعها — تيشرت، كارت بزنس، بنر، ماج، فلاير، ستيكر. بيتسجل أوتوماتيك ويعمل autocomplete\n"
        "3. **الماكينات (MachineProfile)**: كل ماكينة ليها تكلفة تشغيل بالساعة (كهرباء + عمالة + أحبار CMYK). السيستم بيحسب التكلفة الفعلية لكل مهمة أوتوماتيك\n"
        "4. **المصممين (Designer)**: كل مصمم ليه ملف — بتتبع عدد أعماله الشهرية، ساعات العمل، تقييم العملاء (1-5 نجوم)، ونوع التنفيذ (يدوي/AI/AI+تعديل)\n"
        "5. **الخزينة (PrintTreasury)**: إيداع وسحب مع تتبع الرصيد. كل حركة مرتبطة بالطلب اللي اتعملت عليه\n"
        "6. **المخزون (PrintMaterial)**: خامات الطباعة (ورق/حبر/فينيل/بنر/لامينيشن). فيه تنبيه أوتوماتيك لما الكمية تقل عن الحد الأدنى\n"
        "7. **العملاء (PrintCustomer)**: اسم + تليفون + واتساب + شركة. بتقدر تبحث عن أي عميل بالاسم\n"
        "8. **ملفات المشاريع**: كل طلب يقدر يتضاف عليه 3 ملفات مشروع (PSD, AI, PDF)\n"
        "9. **AI Studio**: توليد تصاميم بالذكاء الاصطناعي (DALL-E) + علامة مائية ذكية + إرسال واتساب — محمي بنظام حصص شهرية\n"
        "10. **صلاحيات الموظفين (StaffPermission)**: الأدمن بيتحكم مين يشوف الخزينة/الأرباح/الملفات/AI Studio/المخزون/التقارير\n\n"
        "## طريقة حساب الربح:\n"
        "ربح المهمة = سعر البيع (unit_price × quantity × copies) - تكلفة التشغيل (ساعات الماكينة + أحبار)\n"
        "ربح الطلب = مجموع أرباح المهام\n"
        "الربح الشهري = إجمالي الإيرادات (إيداعات الخزينة) - إجمالي المصروفات (سحوبات الخزينة)\n\n"
        "## إزاي تعلّم المستخدم:\n"
        "- لو سأل سؤال مش واضح، اقترح عليه أسئلة محددة يقدر يسألها\n"
        "- لو سأل عن ميزة مش عارفها، اشرحله إزاي يوصلها في السيستم\n"
        "- لو سأل عن تقرير، اشرحله الأرقام ومعناها ونصيحتك\n"
        "- أجب بالعربي المصري، مختصر ومهني\n"
        "- لا تخترع أرقام — استخدم البيانات الفعلية فقط\n"
    )


def _get_live_context_printing():
    """جلب سياق حي شامل من داتابيز المطبعة"""
    from printing.models import (
        PrintOrder, PrintJob, PrintTransaction, PrintTreasury,
        PrintCustomer, PrintMaterial, Designer, MachineProfile,
    )

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # مبيعات ومصاريف
    income_today = PrintTransaction.objects.filter(transaction_type='in', date__gte=today_start).aggregate(t=Sum('amount'))['t'] or 0
    income_month = PrintTransaction.objects.filter(transaction_type='in', date__gte=month_start).aggregate(t=Sum('amount'))['t'] or 0
    expenses_today = PrintTransaction.objects.filter(transaction_type='out', date__gte=today_start).aggregate(t=Sum('amount'))['t'] or 0
    expenses_month = PrintTransaction.objects.filter(transaction_type='out', date__gte=month_start).aggregate(t=Sum('amount'))['t'] or 0

    # خزينة
    treasuries = PrintTreasury.objects.filter(is_active=True)
    treasury_info = ", ".join(f"{t.name}: {t.balance:,.2f}" for t in treasuries)
    total_balance = sum(t.balance for t in treasuries)

    # طلبات
    open_orders = PrintOrder.objects.filter(status__in=['draft', 'confirmed', 'in_progress']).count()
    today_orders = PrintOrder.objects.filter(date_created__gte=today_start).count()

    # عملاء
    total_customers = PrintCustomer.objects.count()
    recent_customers = PrintCustomer.objects.order_by('-created_at')[:5]
    customers_list = ", ".join(f"{c.name}" for c in recent_customers)

    # مخزون
    low_stock = PrintMaterial.objects.filter(quantity__lte=F('min_stock'))
    low_stock_items = ", ".join(f"{m.name} ({m.quantity} {m.unit})" for m in low_stock[:5])

    # مصممين
    designers = Designer.objects.filter(is_active=True)
    designers_info = []
    for d in designers:
        stats = d.get_month_stats()
        designers_info.append(f"{d.user.get_full_name() or d.user.username}: {stats['total_works'] or 0} عمل")

    return (
        f"## البيانات الحية الآن:\n"
        f"📅 التاريخ: {now.strftime('%Y-%m-%d %H:%M')}\n"
        f"💰 إيرادات اليوم: {income_today:,.2f} ج.م | إيرادات الشهر: {income_month:,.2f} ج.م\n"
        f"💸 مصروفات اليوم: {expenses_today:,.2f} ج.م | مصروفات الشهر: {expenses_month:,.2f} ج.م\n"
        f"📊 صافي ربح الشهر: {float(income_month) - float(expenses_month):,.2f} ج.م\n"
        f"🏦 الخزائن: {treasury_info} | الإجمالي: {total_balance:,.2f} ج.م\n"
        f"📋 طلبات مفتوحة: {open_orders} | طلبات اليوم: {today_orders}\n"
        f"👥 إجمالي العملاء: {total_customers} | آخر العملاء: {customers_list}\n"
        f"📦 تنبيهات مخزون: {low_stock_items or 'لا يوجد نقص ✅'}\n"
        f"🎨 المصممين: {', '.join(designers_info) or 'لا يوجد مصممين مسجلين'}\n"
    )


@login_required
def copilot_chat(request):
    """
    🧠 Smart Business Copilot — يرد على أسئلة من الداتابيز الفعلية.
    مجاني — لا يستهلك حصة AI ولا API خارجي (إلا لتنسيق الرد).
    """

    query = request.GET.get('query', '').strip()
    if not query:
        return JsonResponse({
            'status': 'success',
            'recommendations': 'أهلاً! اسألني عن أي حاجة — المبيعات، المصاريف، الأرباح، الطلبات، العملاء، المخزون، أو حتى إزاي تستخدم السيستم.'
        })

    # الخطوة 1: استعلم من الداتابيز للبيانات المحددة
    db_result = _query_business_data(query, request=request)
    db_context = db_result['context'] if db_result else ""

    # الخطوة 2: جلب سياق حي شامل + معرفة السيستم
    try:
        live_context = _get_live_context_printing()
    except Exception as e:
        logger.warning(f"[COPILOT] Live context failed: {e}")
        live_context = ""

    system_knowledge = _get_system_knowledge_printing()

    # الخطوة 3: Gemini للرد الذكي
    try:
        from inventory.ai_services import call_llm_layer
        if getattr(settings, 'ENABLE_AI_PREDICTIONS', False) and getattr(settings, 'AI_VISION_API_KEY', None):
            user_content = f"سؤال المستخدم: {query}"
            if db_context:
                user_content += f"\n\nنتيجة البحث في الداتابيز:\n{db_context}"
            user_content += f"\n\n{live_context}"

            messages = [
                {"role": "system", "content": system_knowledge},
                {"role": "user", "content": user_content},
            ]
            ai_response = call_llm_layer(messages, json_mode=False, max_retries=1)
            if ai_response:
                return JsonResponse({
                    'status': 'success',
                    'recommendations': ai_response.replace('\n', '<br>'),
                })
    except Exception as e:
        logger.warning(f"[COPILOT] Gemini failed: {e}")

    # Fallback: رجّع البيانات الخام
    if db_context:
        return JsonResponse({
            'status': 'success',
            'recommendations': db_context.replace('\n', '<br>'),
        })

    # Fallback ذكي — ردود محلية حسب نوع السؤال بدون Gemini
    q_lower = query.lower()

    # تحيات
    if any(k in q_lower for k in ['hi', 'hello', 'اهلا', 'أهلا', 'مرحبا', 'سلام', 'صباح', 'مساء', 'ازيك', 'إزيك']):
        return JsonResponse({
            'status': 'success',
            'recommendations': (
                'أهلاً بيك! 👋 أنا المستشار الذكي لمطبعتك.<br>'
                'أقدر أساعدك في:<br>'
                '📊 <b>بيانات حية:</b> اسألني "بيعنا كام؟" أو "مصاريفنا كام؟"<br>'
                '📋 <b>تفاصيل طلب:</b> "فاتورة رقم 5 كسبنا فيها ولا خسرنا؟"<br>'
                '💰 <b>الخزينة:</b> "رصيد الخزينة كام؟"<br>'
                '👤 <b>العملاء:</b> "عميل أحمد — بياناته إيه؟"<br>'
                '🎨 <b>المصممين:</b> "أداء المصممين" أو "مين أشطر مصمم؟"<br>'
                '📦 <b>المخزون:</b> "حالة المخزون" أو "إيه الخامات اللي قربت تخلص؟"<br>'
                '📖 <b>تعلّم:</b> "إزاي أعمل طلب؟" أو "عاوز أتعلم النظام"'
            ),
        })

    # طلبات التعلم
    if any(k in q_lower for k in ['اتعلم', 'أتعلم', 'تعلم', 'علمني', 'شرح', 'اشرح', 'ازاي', 'إزاي', 'كيف', 'طريقة']):
        # تعلم عام
        if any(k in q_lower for k in ['النظام', 'السيستم', 'البرنامج', 'كله', 'عموما']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '📖 <b>دليلك السريع لنظام المطبعة:</b><br><br>'
                    '1️⃣ <b>طلب جديد:</b> الطلبات → طلب جديد → اختر العميل → أضف المهام (تيشرت، كروت، بوستر...) → احفظ<br>'
                    '2️⃣ <b>المهام:</b> كل طلب فيه مهام — كل مهمة ليها نوع بند وسعر وتكلفة<br>'
                    '3️⃣ <b>المصممين:</b> سجّل أعمال كل مصمم يومياً + تقييم الشغل<br>'
                    '4️⃣ <b>الماكينات:</b> سجّل كل ماكينة + تكلفة CMYK → النظام يحسبلك الربح الحقيقي<br>'
                    '5️⃣ <b>الخامات:</b> أضف الخامات (ورق، حبر، خام تيشرت) + حد أدنى → تنبيه تلقائي لما يقرب يخلص<br>'
                    '6️⃣ <b>الخزينة:</b> كل تحصيل ومصروف يتسجل تلقائي → رصيدك لحظي<br>'
                    '7️⃣ <b>التقارير:</b> أرباح، مبيعات، أداء المصممين، تكلفة كل ماكينة<br><br>'
                    'اسألني عن أي نقطة بالتفصيل! 💡'
                ),
            })

        # طلب طباعة
        if any(k in q_lower for k in ['طلب', 'اوردر', 'أوردر', 'فاتور']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '📋 <b>إزاي تعمل طلب طباعة جديد:</b><br><br>'
                    '1. ادخل على "طلبات الطباعة" → "إضافة طلب جديد"<br>'
                    '2. اختر العميل (أو أضف عميل جديد)<br>'
                    '3. أضف المهام: كل مهمة = بند (مثلاً: تيشرت، كارت شخصي، بوستر)<br>'
                    '4. حدد الكمية والسعر — النظام يحسب الإجمالي تلقائي<br>'
                    '5. لو عاوز ترفع ملف المشروع (PSD/AI)، ارفعه من خانة "ملف المشروع"<br>'
                    '6. احفظ الطلب → ابدأ التنفيذ → غيّر الحالة لـ "قيد التنفيذ" → "مكتمل"<br><br>'
                    '💡 النظام بيحسبلك الربح لكل طلب تلقائي!'
                ),
            })

        # مصممين
        if any(k in q_lower for k in ['مصمم', 'ديزاين', 'تصميم', 'designer']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '🎨 <b>إدارة المصممين:</b><br><br>'
                    '1. ادخل "المصممين" → "إضافة مصمم" → اكتب اسمه وتخصصه<br>'
                    '2. سجّل أعماله يومياً من "سجل الأعمال" → اختر المصمم → أضف الشغل + ساعات العمل<br>'
                    '3. قيّم كل شغلة (ممتاز/جيد/مقبول)<br>'
                    '4. شوف الإحصائيات: أشطر مصمم، أكتر واحد شغّال، متوسط التقييمات<br><br>'
                    '💡 اسألني "أداء المصممين" وهقولك الإحصائيات الحية!'
                ),
            })

        # خامات / مخزون
        if any(k in q_lower for k in ['خام', 'مخزون', 'ورق', 'حبر', 'صنف', 'stock']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '📦 <b>إدارة الخامات والمخزون:</b><br><br>'
                    '1. ادخل "مخزون الخامات" → "إضافة خامة" → اسمها ونوعها وسعرها<br>'
                    '2. حدد "حد أدنى" — النظام يحذّرك تلقائي لما الكمية تنزل تحته<br>'
                    '3. سجّل الوارد والمنصرف → الرصيد يتحدث تلقائي<br>'
                    '4. تحويل بين الفروع: من خامة معينة → حدد الكمية → اختر الفرع<br><br>'
                    '💡 اسألني "حالة المخزون" وهقولك إيه اللي قرب يخلص!'
                ),
            })

        # ماكينات
        if any(k in q_lower for k in ['ماكين', 'طابع', 'printer', 'cmyk']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '🖨️ <b>إدارة الماكينات:</b><br><br>'
                    '1. ادخل "الماكينات" → "إضافة ماكينة" → اسمها ونوعها<br>'
                    '2. سجّل تكلفة CMYK لكل لون (Cyan, Magenta, Yellow, Black) → حاسبة التكلفة<br>'
                    '3. النظام يحسبلك تكلفة الطباعة الفعلية لكل مهمة<br>'
                    '4. تقرير ربحية كل ماكينة → تعرف أنهي ماكينة بتكسبك أكتر<br>'
                ),
            })

        # خزينة
        if any(k in q_lower for k in ['خزين', 'فلوس', 'كاش', 'treasury', 'دفع']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '💰 <b>الخزينة والمدفوعات:</b><br><br>'
                    '1. "الخزائن" → شوف رصيد كل خزينة لحظياً<br>'
                    '2. تحصيل من عميل: "المعاملات" → "تحصيل" → اختر العميل والمبلغ<br>'
                    '3. مصروف: "المعاملات" → "صرف" → اكتب الوصف والمبلغ<br>'
                    '4. تحويل بين خزائن: "تحويل" → من خزينة → إلى خزينة<br><br>'
                    '💡 اسألني "رصيد الخزينة كام؟" وهقولك!'
                ),
            })

        # عام
        return JsonResponse({
            'status': 'success',
            'recommendations': (
                '📖 أقدر أعلّمك أي حاجة في النظام! اسألني عن:<br>'
                '• "إزاي أعمل طلب طباعة؟"<br>'
                '• "إزاي أضيف مصمم؟"<br>'
                '• "إزاي أدير المخزون والخامات؟"<br>'
                '• "إزاي أشوف أرباحي؟"<br>'
                '• "إزاي أسجل ماكينة وأحسب تكلفتها؟"<br>'
                '• "إزاي أدير الخزينة؟"<br>'
                '• أو اسأل أي سؤال تاني وأنا هساعدك! 💡'
            ),
        })

    # Fallback نهائي — قائمة المساعدة
    return JsonResponse({
        'status': 'success',
        'recommendations': (
            'مش متأكد فهمت سؤالك 🤔 جرّب تسأل بشكل تاني، مثلاً:<br>'
            '📊 <b>بيانات:</b> "بيعنا كام؟" | "مصاريفنا كام؟" | "رصيد الخزينة؟"<br>'
            '📋 <b>طلبات:</b> "فاتورة رقم 5" | "آخر الطلبات"<br>'
            '👤 <b>عملاء:</b> "عميل أحمد" | "أكتر عميل بيشتري"<br>'
            '📖 <b>تعلّم:</b> "عاوز أتعلم النظام" | "إزاي أعمل طلب؟"<br>'
            '🎨 <b>مصممين:</b> "أداء المصممين" | "مين أشطر مصمم؟"'
        ),
    })


# =====================================================================
# 🏷️ Product Type Autocomplete API
# =====================================================================

@login_required
def product_type_autocomplete(request):
    """
    Autocomplete API لأنواع البنود.
    GET /printing/api/product-types/?q=تيش → [{"id": 1, "name": "تيشرت", "count": 45}]
    """
    q = request.GET.get('q', '').strip()
    from printing.models import ProductType
    qs = ProductType.objects.all()
    if q:
        qs = qs.filter(name__icontains=q)
    results = [
        {'id': pt.id, 'name': pt.name, 'count': pt.usage_count}
        for pt in qs[:15]
    ]
    return JsonResponse({'results': results})


# =====================================================================
# 📊 تقرير أكتر البنود شغالة
# =====================================================================

@login_required
def product_type_report(request):
    """
    تقرير أنواع البنود — أكتر بند شغال وعدد مرات الاستخدام.
    """
    from printing.models import ProductType
    types = ProductType.objects.filter(usage_count__gt=0).order_by('-usage_count')[:20]
    data = [
        {'name': pt.name, 'count': pt.usage_count}
        for pt in types
    ]
    return JsonResponse({'results': data})


# =====================================================================
# 🎨 AI Prompt Engineer Agent — FLUX/SDXL Pipeline
# =====================================================================
# SECTOR: Printing & Media ONLY (Persona B)
# ISOLATION: Completely decoupled from Copilot (Consultant) and Automotive
# PURPOSE: Pure Function — transforms casual Arabic/English design
#          descriptions into cinematic, commercial-grade image prompts
# =====================================================================

_PROMPT_ENGINEER_SYSTEM = """You are an elite Prompt Engineering Agent for a professional printing and media design studio.

## YOUR SOLE PURPOSE:
Transform raw, casual user descriptions (often in Arabic) into highly detailed, cinematic, commercial-quality English prompts optimized for advanced text-to-image models (DALL-E 3, FLUX.1, SDXL).

## CRITICAL DISTINCTION — DELIVERABLE vs CLIENT INDUSTRY:
You are a DESIGN STUDIO. You design things FOR clients in ANY industry.
- ✅ ACCEPT: "design a promotional pen for a car parts company" → this is a PEN DESIGN job
- ✅ ACCEPT: "logo for a restaurant" → this is a LOGO job, regardless of restaurant being food industry
- ✅ ACCEPT: "business card for a doctor" → this is a CARD design, not a medical service
- ✅ ACCEPT: "sticker for an auto shop" → this is a STICKER design
- ✅ ACCEPT: "t-shirt for a gym" → this is a T-SHIRT design
- ❌ REJECT only if the user wants an ACTUAL non-design service:
  - "fix my car engine"
  - "diagnose my vehicle"
  - "treat my illness"
  - "legal advice on contracts"

**The client's industry is IRRELEVANT — what matters is whether the OUTPUT is a printable/visual design asset.**
If the user describes ANY visual artifact (logo, card, poster, mockup, pen, mug, t-shirt, packaging, brochure, banner, sticker, social post, billboard, menu, invitation, sign, label, brand identity), ACCEPT and engineer the prompt — no matter what business the client is in.

## ENRICHMENT PIPELINE:
When transforming the user's raw intent, you MUST inject these expert parameters:

### 1. COMPOSITION & STYLE:
- Layout structure (minimalist, editorial, Swiss grid, asymmetric balance)
- Visual hierarchy (primary focal point, supporting elements)
- Style direction (photorealistic, flat design, 3D render, isometric, watercolor, retro)

### 2. LIGHTING & ATMOSPHERE:
- Lighting type (volumetric, studio softbox, golden hour, neon rim light, dramatic chiaroscuro)
- Mood/atmosphere (premium, corporate, playful, luxurious, bold)
- Color grading (cinematic teal-orange, monochromatic, vibrant CMYK, pastel)

### 3. TYPOGRAPHY (when text appears in the design):
- Specify exact text placement, font style cues (bold sans-serif, elegant serif, modern geometric)
- If the user provides brand/company name, INCLUDE IT in the prompt with clear typography
- Ensure text is clean, crisp, and print-ready

### 4. TECHNICAL QUALITY:
- Resolution cues: 8K, ultra-HD, sharp focus, hyper-detailed
- Print standards: pristine borders, bleed-safe, CMYK-optimized colors
- Material cues: glossy finish, matte texture, embossed, foil stamp effect, photorealistic product mockup

### 5. DESIGN CATEGORIES YOU EXCEL AT (output format):
- Business cards, letterheads, brand identity systems
- Posters, banners, roll-ups, billboards
- Social media posts, stories, covers
- Packaging, labels, product mockups (pens, mugs, bottles, boxes, etc.)
- Flyers, brochures, catalogs, menus
- T-shirt prints, mug designs, merchandise (any printed promotional item)
- Wedding invitations, event cards
- Stickers, vinyl wraps, vehicle wraps (branding visuals)
- Logos, icons, brand marks

### 6. CONTEXTUAL INDUSTRY HINTS:
When the client is in a specific industry, USE that context to make the design more relevant:
- Car parts company → mechanical, technical, masculine aesthetic, gear/wrench motifs OK
- Restaurant → appetizing colors, food photography aesthetic
- Tech startup → futuristic, clean, gradient-rich
- Healthcare → trust-blue, clean white, calming greens
Use the industry as CREATIVE FUEL, not as a rejection trigger.

## OUTPUT FORMAT:
You MUST respond with ONLY valid JSON. No prose, no markdown, no explanation.
{
  "status": "success",
  "original_intent": "<the raw user request restated in English>",
  "design_category": "<detected category: business_card|poster|social_media|packaging|flyer|banner|tshirt|invitation|sticker|brand_identity|menu|mockup|logo|merchandise|other>",
  "engineered_prompt": "<the final enriched, hyper-detailed English prompt — include brand name if provided>",
  "negative_prompt": "<elements to avoid: blurry, low quality, distorted text, artifacts, watermark, cropped>",
  "recommended_size": "<optimal image dimensions: 1024x1024|1024x1792|1792x1024>",
  "recommended_quality": "<standard|hd>"
}

ONLY return rejected if the user is asking for a NON-design service (medical advice, car repair, legal help, etc.):
{
  "status": "rejected",
  "reason": "This appears to be a service request, not a design/printing task. Please describe a visual design you need."
}
"""


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


# =====================================================================
# 💾 AI Studio History & Sessions
# =====================================================================

@login_required
def ai_studio_history(request):
    """عرض كل التصاميم اللي العميل عملها قبل كده."""
    from clients.models import AIStudioSession

    tenant = _get_tenant()
    if not tenant:
        return render(request, 'printing/ai_history.html', {'sessions': [], 'tenant': None})

    sessions = AIStudioSession.objects.filter(tenant=tenant).order_by('-created_at')[:100]

    return render(request, 'printing/ai_history.html', {
        'sessions': sessions,
        'tenant': tenant,
        'total': sessions.count(),
    })


@login_required
def ai_studio_history_api(request):
    """JSON list of past sessions for the AI Studio modal."""
    from clients.models import AIStudioSession

    tenant = _get_tenant()
    if not tenant:
        return JsonResponse({'sessions': []})

    qs = AIStudioSession.objects.filter(tenant=tenant).order_by('-created_at')[:50]
    sessions = [{
        'id': s.pk,
        'raw_input': s.raw_input[:120],
        'design_category': s.design_category,
        'image_url': s.watermarked_image_url or s.image_url,
        'logo_used': s.logo_used,
        'watermarked': s.watermarked,
        'is_favorite': s.is_favorite,
        'created_at': s.created_at.strftime('%Y-%m-%d %H:%M'),
        'model_used': s.model_used,
    } for s in qs]

    return JsonResponse({'sessions': sessions, 'count': len(sessions)})


@csrf_exempt
@login_required
@require_POST
def ai_session_toggle_favorite(request, session_id):
    """مفضّل / إلغاء مفضّل لجلسة."""
    from clients.models import AIStudioSession
    tenant = _get_tenant()
    session = get_object_or_404(AIStudioSession, pk=session_id, tenant=tenant)
    session.is_favorite = not session.is_favorite
    session.save(update_fields=['is_favorite'])
    return JsonResponse({'success': True, 'is_favorite': session.is_favorite})


@csrf_exempt
@login_required
@require_POST
def ai_session_delete(request, session_id):
    """حذف جلسة من السجل."""
    from clients.models import AIStudioSession
    tenant = _get_tenant()
    session = get_object_or_404(AIStudioSession, pk=session_id, tenant=tenant)
    session.delete()
    return JsonResponse({'success': True})


@login_required
def ai_attach_search(request):
    """🔍 بحث عن طلب طباعة لربطه بتصميم AI Studio."""
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'invoices': []})

    try:
        from printing.models import PrintOrder
    except ImportError:
        return JsonResponse({'invoices': [], 'error': 'PrintOrder model not found'})

    # ⚠️ Fixed: search through PrintOrder (which has the customer relation)
    qs = PrintOrder.objects.select_related('customer').filter(
        Q(customer__name__icontains=query) |
        Q(customer__phone__icontains=query) |
        Q(order_number__icontains=query)
    ).order_by('-date_created')[:15]

    invoices = []
    for order in qs:
        invoices.append({
            'id': order.pk,
            'code': order.order_number or f'PO-{order.pk}',
            'customer': (order.customer.name if order.customer else '—'),
            'date': order.date_created.strftime('%Y-%m-%d') if order.date_created else '',
            'total': str(order.total_amount or 0),
            'status': order.get_status_display(),
        })
    return JsonResponse({'invoices': invoices})


@csrf_exempt
@login_required
@require_POST
def ai_session_attach(request, session_id):
    """🔗 ربط جلسة AI Studio بطلب طباعة."""
    from clients.models import AIStudioSession
    tenant = _get_tenant()
    session = get_object_or_404(AIStudioSession, pk=session_id, tenant=tenant)

    invoice_id = request.POST.get('invoice_id')
    if not invoice_id:
        return JsonResponse({'success': False, 'error': 'invoice_id required'}, status=400)

    try:
        from printing.models import PrintOrder
        order = PrintOrder.objects.get(pk=invoice_id)
    except PrintOrder.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'الطلب غير موجود'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'خطأ: {e}'}, status=500)

    # Append to notes (PrintOrder has 'notes' field per migration 0001)
    attached = False
    if hasattr(order, 'notes'):
        order.notes = (order.notes or '') + f"\n\n🎨 تصميم AI Studio (جلسة #{session.pk}): {session.image_url}"
        order.save(update_fields=['notes'])
        attached = True

    logger.info(f"🔗 [AI ATTACH]: Session #{session.pk} attached to PrintOrder #{order.pk} by {request.user.username}")
    return JsonResponse({
        'success': True,
        'message': f'تم ربط التصميم بالطلب {order.order_number} بنجاح',
        'invoice_id': order.pk,
        'attached': attached,
    })



# =====================================================================
# 📒 8. كشف حساب العميل (Customer Statement)
# =====================================================================

@login_required
def customer_statement(request, customer_id):
    """كشف حساب شامل للعميل: كل الفواتير + المدفوعات + الرصيد الجاري.

    ✓ كل PrintOrder (الفواتير الصادرة للعميل) → debit
    ✓ كل PrintTransaction.transaction_type='in' المربوط بطلباته → credit
    ✓ running balance بعد كل حركة + إجماليات

    يدعم: ?from=YYYY-MM-DD&to=YYYY-MM-DD للفلترة
          ?print=1 للنسخة المخصصة للطباعة
    """
    from printing.models import PrintCustomer, PrintOrder, PrintTransaction

    customer = get_object_or_404(PrintCustomer, pk=customer_id)

    # فلترة بالتاريخ
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()

    orders_qs = PrintOrder.objects.filter(customer=customer)
    payments_qs = PrintTransaction.objects.filter(
        order__customer=customer, transaction_type='in',
    )

    if date_from:
        orders_qs = orders_qs.filter(date_created__date__gte=date_from)
        payments_qs = payments_qs.filter(date__date__gte=date_from)
    if date_to:
        orders_qs = orders_qs.filter(date_created__date__lte=date_to)
        payments_qs = payments_qs.filter(date__date__lte=date_to)

    # دمج الفواتير + المدفوعات في timeline واحد مرتب بالتاريخ
    events = []
    for o in orders_qs.select_related('branch'):
        events.append({
            'date': o.date_created,
            'type': 'invoice',
            'ref': o.order_number,
            'description': f'فاتورة #{o.order_number}' + (f' — {o.notes[:60]}' if o.notes else ''),
            'debit': o.net_total,   # عليه (مدين)
            'credit': Decimal('0'),
            'status': o.get_status_display(),
            'obj_id': o.pk,
        })
    for p in payments_qs.select_related('treasury', 'order'):
        events.append({
            'date': p.date,
            'type': 'payment',
            'ref': f'#{p.pk}',
            'description': p.description or f'دفعة على فاتورة #{p.order.order_number if p.order else ""}',
            'debit': Decimal('0'),
            'credit': p.amount,    # دفع (دائن)
            'status': p.treasury.name if p.treasury else '',
            'obj_id': p.pk,
        })

    events.sort(key=lambda e: e['date'])

    running = Decimal('0')
    for ev in events:
        running += ev['debit'] - ev['credit']
        ev['balance'] = running

    # إجماليات
    total_invoiced = sum((e['debit'] for e in events), Decimal('0'))
    total_paid = sum((e['credit'] for e in events), Decimal('0'))
    final_balance = total_invoiced - total_paid

    # كل الطلبات للملخص العلوي (بدون فلترة)
    all_orders = PrintOrder.objects.filter(customer=customer)
    summary = {
        'total_orders': all_orders.count(),
        'open_orders': all_orders.exclude(status__in=['delivered', 'cancelled']).count(),
        'delivered_orders': all_orders.filter(status='delivered').count(),
    }

    return render(request, 'printing/customer_statement.html', {
        'customer': customer,
        'events': events,
        'total_invoiced': total_invoiced,
        'total_paid': total_paid,
        'final_balance': final_balance,
        'summary': summary,
        'date_from': date_from,
        'date_to': date_to,
        'print_mode': request.GET.get('print') == '1',
    })


# =====================================================================
# 📊 Order Profit Detail — تحليل ربح/خسارة الطلب
# =====================================================================

@login_required
def order_profit_detail(request, order_id):
    """تحليل تكلفة وربحية طلب الطباعة على مستوى المهام.

    لكل PrintJob: machine_cost + ink_cost + designer_cost = full_cost
    على مستوى الطلب: revenue (net_total) − total_cost = gross_profit
    """
    from printing.models import PrintOrder, StaffPermission

    # صلاحية: staff أو can_view_profits
    if not request.user.is_staff:
        try:
            if not request.user.print_permissions.can_view_profits:
                from django.http import HttpResponseForbidden
                return HttpResponseForbidden("لا تملك صلاحية مشاهدة الأرباح.")
        except StaffPermission.DoesNotExist:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("لا تملك صلاحية مشاهدة الأرباح.")

    order = get_object_or_404(
        PrintOrder.objects.select_related('customer', 'branch'),
        pk=order_id,
    )

    job_rows = []
    sum_machine = sum_ink = sum_designer = sum_revenue = Decimal('0')
    for job in order.jobs.select_related('machine', 'designer', 'designer__user', 'product_type').all():
        mc = job.machine_cost
        ic = job.ink_cost
        dc = job.designer_cost
        fc = mc + ic + dc
        rev = job.total_price
        job_rows.append({
            'job': job,
            'machine_cost': mc,
            'ink_cost': ic,
            'designer_cost': dc,
            'full_cost': fc,
            'revenue': rev,
            'profit': rev - fc,
            'margin_percent': round((rev - fc) / max(rev, Decimal('0.01')) * Decimal('100'), 2) if rev > 0 else Decimal('0'),
        })
        sum_machine += mc
        sum_ink += ic
        sum_designer += dc
        sum_revenue += rev

    total_cost = sum_machine + sum_ink + sum_designer
    net_total = order.net_total
    gross_profit = net_total - total_cost
    margin = round(gross_profit / max(net_total, Decimal('0.01')) * Decimal('100'), 2) if net_total > 0 else Decimal('0')

    # نسب التكلفة
    cost_breakdown = []
    if total_cost > 0:
        for label, value, color in [
            ('تشغيل الماكينات', sum_machine, '#f59e0b'),
            ('الأحبار', sum_ink, '#06b6d4'),
            ('أجور المصممين', sum_designer, '#ec4899'),
        ]:
            cost_breakdown.append({
                'label': label,
                'value': value,
                'color': color,
                'percent': (value / total_cost * Decimal('100')) if total_cost else Decimal('0'),
            })

    return render(request, 'printing/order_profit_detail.html', {
        'order': order,
        'job_rows': job_rows,
        'sum_machine': sum_machine,
        'sum_ink': sum_ink,
        'sum_designer': sum_designer,
        'total_cost': total_cost,
        'net_total': net_total,
        'discount': order.discount,
        'gross_profit': gross_profit,
        'margin': margin,
        'is_profitable': gross_profit > 0,
        'cost_breakdown': cost_breakdown,
    })


# =====================================================================
# 💰 9. عروض الأسعار (Quotations)
# =====================================================================

from django.views.decorators.csrf import csrf_exempt as _csrf_exempt
from django.contrib.auth.decorators import login_required as _login_required


@_login_required
@_csrf_exempt
def quotation_create(request):
    """POST /printing/quotation/create/ — إنشاء عرض سعر سريع."""
    from printing.models import PriceQuotation, QuotationLine, PrintCustomer

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON غير صالح'}, status=400)

    title = (data.get('title') or '').strip()
    if not title or len(title) < 3:
        return JsonResponse({'error': 'العنوان مطلوب (3 حروف على الأقل)'}, status=400)

    customer_id = data.get('customer_id')
    customer_obj = None
    if customer_id:
        try:
            customer_obj = PrintCustomer.objects.get(pk=int(customer_id))
        except (PrintCustomer.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'error': 'العميل غير موجود'}, status=404)

    lines_data = data.get('lines', [])
    if not lines_data or not isinstance(lines_data, list):
        return JsonResponse({'error': 'أضف بنداً واحداً على الأقل'}, status=400)

    try:
        discount = Decimal(str(data.get('discount', '0') or '0'))
        tax_percent = Decimal(str(data.get('tax_percent', '0') or '0'))
    except (ValueError, ArithmeticError):
        return JsonResponse({'error': 'قيم رقمية غير صالحة'}, status=400)

    quote = PriceQuotation.objects.create(
        customer=customer_obj,
        customer_name=(data.get('customer_name') or '').strip(),
        customer_phone=(data.get('customer_phone') or '').strip(),
        customer_whatsapp=(data.get('customer_whatsapp') or '').strip(),
        title=title,
        notes=(data.get('notes') or '').strip(),
        discount=discount,
        tax_percent=tax_percent,
        created_by=request.user,
        status='draft',
    )

    # Insert lines
    for idx, ln in enumerate(lines_data):
        try:
            qty = Decimal(str(ln.get('quantity', '1')))
            price = Decimal(str(ln.get('unit_price', '0')))
        except (ValueError, ArithmeticError):
            continue
        desc = (ln.get('description') or '').strip()
        if not desc:
            continue
        QuotationLine.objects.create(
            quotation=quote, description=desc[:300],
            quantity=qty, unit_price=price, sort_order=idx,
        )

    quote.recalc_totals()
    quote.refresh_from_db()

    public_url = request.build_absolute_uri(f'/printing/quotation/view/{quote.share_token}/')
    return JsonResponse({
        'success': True,
        'message': 'تم إنشاء العرض بنجاح',
        'quote_id': quote.pk,
        'quote_number': quote.quote_number,
        'total': str(quote.total),
        'share_url': public_url,
        'whatsapp_url': f'https://wa.me/?text={request.build_absolute_uri(public_url)}',
    })


def quotation_public_view(request, share_token):
    """GET /printing/quotation/view/<uuid>/ — صفحة عمومية للعميل لمشاهدة العرض."""
    from printing.models import PriceQuotation

    quote = get_object_or_404(PriceQuotation, share_token=share_token)

    # Auto-mark as sent on first view (لو لسه draft)
    if quote.status == 'draft':
        quote.status = 'sent'
        quote.sent_at = timezone.now()
        quote.save(update_fields=['status', 'sent_at'])

    # Auto-expire
    if quote.is_expired and quote.status == 'sent':
        quote.status = 'expired'
        quote.save(update_fields=['status'])

    return render(request, 'printing/quotation_public.html', {
        'quote': quote,
        'lines': quote.lines.all().order_by('sort_order'),
    })


@_csrf_exempt
def quotation_respond(request, share_token):
    """POST /printing/quotation/view/<uuid>/respond/ — العميل يقبل أو يرفض."""
    from printing.models import PriceQuotation

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON غير صالح'}, status=400)

    action = data.get('action')
    if action not in ('accept', 'reject'):
        return JsonResponse({'error': 'إجراء غير صالح'}, status=400)

    quote = get_object_or_404(PriceQuotation, share_token=share_token)

    if quote.status not in ('sent', 'draft'):
        return JsonResponse({'error': 'لا يمكن الرد على هذا العرض الآن'}, status=400)

    if quote.is_expired:
        quote.status = 'expired'
        quote.save(update_fields=['status'])
        return JsonResponse({'error': 'هذا العرض منتهي الصلاحية'}, status=400)

    quote.status = 'accepted' if action == 'accept' else 'rejected'
    quote.responded_at = timezone.now()
    quote.save(update_fields=['status', 'responded_at'])

    return JsonResponse({
        'success': True,
        'message': 'شكراً! تم تسجيل قبولك للعرض. سيتواصل معك الفريق قريباً.' if action == 'accept'
                   else 'تم تسجيل رفضك للعرض. شكراً لوقتك.',
        'status': quote.status,
    })


@_login_required
@_csrf_exempt
def quotation_convert_to_order(request, quote_id):
    """POST — تحويل عرض مقبول إلى PrintOrder رسمي."""
    from printing.models import PriceQuotation, PrintOrder

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    quote = get_object_or_404(PriceQuotation, pk=quote_id)

    if quote.status != 'accepted':
        return JsonResponse({'error': 'العرض لازم يبقى مقبول الأول'}, status=400)

    if quote.converted_order_id:
        return JsonResponse({'error': 'هذا العرض تحوّل لطلب بالفعل',
                            'order_id': quote.converted_order_id}, status=400)

    if not quote.customer:
        return JsonResponse({'error': 'لازم تربط العرض بعميل مسجل أولاً'}, status=400)

    order = PrintOrder.objects.create(
        customer=quote.customer,
        total_amount=quote.total,
        notes=f'تم إنشاؤه من عرض السعر #{quote.quote_number}\n\n{quote.notes}',
        status='confirmed',
    )

    quote.status = 'converted'
    quote.converted_order = order
    quote.save(update_fields=['status', 'converted_order'])

    return JsonResponse({
        'success': True,
        'message': f'تم تحويل العرض إلى طلب #{order.order_number}',
        'order_id': order.pk,
        'order_url': f'/secure-portal/printing/printorder/{order.pk}/change/',
    })


# =====================================================================
# 📈 10. تقرير الأرباح والخسائر (P&L Report)
# =====================================================================

@_login_required
def profit_loss_report(request):
    """تقرير شهري للإيرادات vs المصروفات + صافي الربح.

    Query: ?year=2026&month=6 (افتراضي: الشهر الحالي)
    """
    from printing.models import PrintTransaction, PrintOrder
    from django.db.models import Sum
    from datetime import date as _date

    today = timezone.now().date()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        if month < 1 or month > 12: month = today.month
        if year < 2020 or year > 2100: year = today.year
    except (ValueError, TypeError):
        year, month = today.year, today.month

    # Range
    start = _date(year, month, 1)
    if month == 12:
        end = _date(year + 1, 1, 1)
    else:
        end = _date(year, month + 1, 1)

    # حركات الشهر
    in_txns = PrintTransaction.objects.filter(
        transaction_type='in', date__gte=start, date__lt=end,
    )
    out_txns = PrintTransaction.objects.filter(
        transaction_type='out', date__gte=start, date__lt=end,
    )

    total_income = in_txns.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    total_expense = out_txns.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    net_profit = total_income - total_expense

    # تصنيف المصروفات بسيط من الـ description (keyword matching)
    def categorize(desc):
        d = (desc or '').lower()
        if any(k in d for k in ('راتب', 'مرتب', 'سلف', 'بونص', 'salary', 'payroll')):
            return ('💼 رواتب وعمولات', '#8b5cf6')
        if any(k in d for k in ('كهرب', 'كهرباء', 'فاتورة كهرباء', 'electricity')):
            return ('💡 كهرباء ومرافق', '#f59e0b')
        if any(k in d for k in ('ايجار', 'إيجار', 'rent')):
            return ('🏢 إيجارات', '#06b6d4')
        if any(k in d for k in ('ورق', 'حبر', 'خام', 'paper', 'ink', 'material')):
            return ('📦 خامات', '#10b981')
        if any(k in d for k in ('صيان', 'تصليح', 'maintain', 'repair')):
            return ('🔧 صيانة', '#ef4444')
        if any(k in d for k in ('شحن', 'توصيل', 'مواصلات', 'delivery', 'transport')):
            return ('🚚 شحن ومواصلات', '#3b82f6')
        return ('📌 أخرى', '#64748b')

    expense_cats = {}
    for txn in out_txns.values('description', 'amount'):
        cat, color = categorize(txn['description'])
        if cat not in expense_cats:
            expense_cats[cat] = {'name': cat, 'color': color, 'total': Decimal('0'), 'count': 0}
        expense_cats[cat]['total'] += txn['amount']
        expense_cats[cat]['count'] += 1
    expense_cats_list = sorted(expense_cats.values(), key=lambda c: c['total'], reverse=True)
    for c in expense_cats_list:
        c['percent'] = (c['total'] / total_expense * 100) if total_expense else Decimal('0')

    # طلبات الشهر
    orders_this_month = PrintOrder.objects.filter(date_created__gte=start, date_created__lt=end)
    orders_count = orders_this_month.count()
    orders_total_amount = orders_this_month.aggregate(t=Sum('total_amount'))['t'] or Decimal('0')
    orders_paid = orders_this_month.aggregate(t=Sum('paid_amount'))['t'] or Decimal('0')
    orders_outstanding = orders_total_amount - orders_paid

    # 6-month trend (للرسم البياني)
    trend = []
    for offset in range(5, -1, -1):
        m_month = month - offset
        m_year = year
        while m_month < 1:
            m_month += 12; m_year -= 1
        m_start = _date(m_year, m_month, 1)
        if m_month == 12:
            m_end = _date(m_year + 1, 1, 1)
        else:
            m_end = _date(m_year, m_month + 1, 1)
        m_in = PrintTransaction.objects.filter(transaction_type='in', date__gte=m_start, date__lt=m_end).aggregate(t=Sum('amount'))['t'] or Decimal('0')
        m_out = PrintTransaction.objects.filter(transaction_type='out', date__gte=m_start, date__lt=m_end).aggregate(t=Sum('amount'))['t'] or Decimal('0')
        trend.append({
            'label': f'{m_year}/{m_month:02d}',
            'income': float(m_in),
            'expense': float(m_out),
            'net': float(m_in - m_out),
        })

    # محاسب: تليفون التحقق
    months_ar = ['يناير', 'فبراير', 'مارس', 'إبريل', 'مايو', 'يونيو',
                 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']

    return render(request, 'printing/profit_loss.html', {
        'year': year, 'month': month, 'month_name': months_ar[month - 1],
        'total_income': total_income,
        'total_expense': total_expense,
        'net_profit': net_profit,
        'is_profit': net_profit >= 0,
        'profit_margin': (net_profit / total_income * 100) if total_income else Decimal('0'),
        'expense_cats': expense_cats_list,
        'orders_count': orders_count,
        'orders_total_amount': orders_total_amount,
        'orders_paid': orders_paid,
        'orders_outstanding': orders_outstanding,
        'trend': trend,
        'in_count': in_txns.count(),
        'out_count': out_txns.count(),
    })
