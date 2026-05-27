"""
🤖 Printing AI Studio Views
==============================
AI-powered design generation and smart watermark for printing tenants.
Gated by TenantSubscription + AILimitTracker.
"""
import logging
import base64
import json
from io import BytesIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db import connection

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

    if not sub.ai_addon:
        return False, 'لم يتم تفعيل حزمة AI Studio على اشتراكك. تواصل مع الإدارة لإضافة حزمة AI.'

    if not AILimitTracker.can_use(tenant, action_type):
        return False, 'تم استنفاد حصتك الشهرية من هذه الخدمة. يتم تجديد الحصة في بداية كل شهر.'

    return True, None


@login_required
@require_POST
def ai_generate_design(request):
    """
    Generate an AI design using OpenAI DALL-E API.
    Gated by subscription + AI quota.
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'ai_generation')
    if not allowed:
        return JsonResponse({'success': False, 'error': error}, status=403)

    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        return JsonResponse({
            'success': False,
            'error': 'مفتاح OpenAI API غير مُعد في النظام. تواصل مع مسؤول المنصة.'
        }, status=500)

    prompt = request.POST.get('prompt', '').strip()
    size = request.POST.get('size', '1024x1024')
    quality = request.POST.get('quality', 'standard')

    if not prompt:
        return JsonResponse({'success': False, 'error': 'يرجى كتابة وصف التصميم المطلوب.'}, status=400)

    # Validate size
    valid_sizes = ['1024x1024', '1024x1792', '1792x1024']
    if size not in valid_sizes:
        size = '1024x1024'

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)

        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size=size,
            quality=quality,
            n=1,
        )

        image_url = response.data[0].url
        revised_prompt = response.data[0].revised_prompt

        # Deduct quota
        from clients.models import AILimitTracker
        AILimitTracker.deduct(tenant, 'ai_generation', metadata={
            'prompt': prompt[:200],
            'size': size,
            'quality': quality,
            'user': request.user.username,
        })

        logger.info(f"🤖 [AI STUDIO]: {tenant.name} — Generated design by {request.user.username}")

        return JsonResponse({
            'success': True,
            'image_url': image_url,
            'revised_prompt': revised_prompt,
        })

    except openai.RateLimitError:
        return JsonResponse({'success': False, 'error': 'تم تجاوز حدود OpenAI API. حاول مرة أخرى بعد دقيقة.'}, status=429)
    except openai.APIError as e:
        logger.error(f"🔴 [AI STUDIO ERROR]: {tenant.name} — {e}")
        return JsonResponse({'success': False, 'error': f'خطأ في OpenAI API: {str(e)}'}, status=500)
    except ImportError:
        return JsonResponse({'success': False, 'error': 'مكتبة openai غير مثبتة على السيرفر.'}, status=500)
    except Exception as e:
        logger.error(f"🔴 [AI STUDIO ERROR]: {tenant.name} — {e}")
        return JsonResponse({'success': False, 'error': 'حدث خطأ غير متوقع. حاول مرة أخرى.'}, status=500)


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
    """Return AI Studio subscription status and remaining quota for current tenant."""
    tenant = _get_tenant()
    if not tenant:
        return JsonResponse({'active': False, 'reason': 'no_tenant'})

    from clients.models import TenantSubscription, AILimitTracker

    try:
        sub = tenant.subscription
    except TenantSubscription.DoesNotExist:
        return JsonResponse({'active': False, 'reason': 'no_subscription'})

    if not sub.is_active or not sub.ai_addon:
        return JsonResponse({'active': False, 'reason': 'no_ai_addon'})

    ai_used = AILimitTracker.get_monthly_usage(tenant, 'ai_generation')
    wm_used = AILimitTracker.get_monthly_usage(tenant, 'smart_watermark')

    return JsonResponse({
        'active': True,
        'addon_name': sub.ai_addon.name,
        'ai_limit': sub.ai_addon.ai_generations_limit,
        'ai_used': ai_used,
        'ai_remaining': max(0, sub.ai_addon.ai_generations_limit - ai_used),
        'wm_limit': sub.ai_addon.whatsapp_messages_limit,
        'wm_used': wm_used,
        'wm_remaining': max(0, sub.ai_addon.whatsapp_messages_limit - wm_used),
    })
