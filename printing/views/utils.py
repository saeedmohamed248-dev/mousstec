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



# Shared helpers: tenant resolution, AI quota check, watermark, brand context.



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
