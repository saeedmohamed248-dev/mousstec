"""
🎨 Brand Memory — Customer Brand Profile endpoints (Phase 5).

One flexible endpoint (GET/POST/DELETE) for the full profile + a small
endpoint for deleting a single logo slot + the editor page.

Extracted from ``_legacy.py`` as part of the incremental view-module split.
The package facade (``clients/views/__init__.py``) keeps the legacy import
names alive so ``erp_core/urls.py`` continues to work unchanged.
"""
from __future__ import annotations

import logging
import re as _re_hex

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from ._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')

_HEX_RE = _re_hex.compile(r'^#[0-9A-Fa-f]{3,8}$')


def _hex_or(default, value):
    v = (value or '').strip()
    return v if _HEX_RE.match(v) else default


@csrf_exempt
def brand_profile_view(request):
    """🎨 GET/POST endpoint لـ Customer Brand Profile.

    GET: يرجع الـ profile الحالي للعميل (أو null لو مفيش).
    POST: ينشئ أو يحدّث الـ profile (multipart لتدعم logo upload).
    DELETE: يمسح الـ profile كاملاً.
    """
    from clients.models import CustomerBrandProfile
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    # ── GET ──────────────────────────────────────────────────────
    if request.method == 'GET':
        try:
            bp = customer.brand_profile
        except CustomerBrandProfile.DoesNotExist:
            return JsonResponse({"profile": None}, status=200)

        return JsonResponse({
            "profile": {
                "brand_name": bp.brand_name,
                "brand_name_en": bp.brand_name_en,
                "tagline": bp.tagline,
                "primary_color": bp.primary_color,
                "secondary_color": bp.secondary_color,
                "accent_color": bp.accent_color,
                "logo_url": bp.logo_image.url if bp.has_logo else None,
                "logo_alt_url": bp.logo_alt_image.url if (bp.logo_alt_image and bp.logo_alt_image.name) else None,
                "industry": bp.industry,
                "industry_display": bp.get_industry_display(),
                "aesthetic": bp.aesthetic,
                "aesthetic_display": bp.get_aesthetic_display(),
                "tone": bp.tone,
                "tone_display": bp.get_tone_display(),
                "arabic_font": bp.arabic_font,
                "arabic_font_display": bp.get_arabic_font_display(),
                "english_font": bp.english_font,
                "english_font_display": bp.get_english_font_display(),
                "style_notes": bp.style_notes,
                "is_active": bp.is_active,
                "auto_inject_logo": bp.auto_inject_logo,
                "auto_inject_colors": bp.auto_inject_colors,
                "designs_with_brand": bp.designs_with_brand,
            },
            "choices": {
                "industry": list(CustomerBrandProfile.INDUSTRY_CHOICES),
                "aesthetic": list(CustomerBrandProfile.AESTHETIC_CHOICES),
                "tone": list(CustomerBrandProfile.TONE_CHOICES),
                "font_style": list(CustomerBrandProfile.FONT_STYLE_CHOICES),
            },
        })

    # ── POST: create or update ───────────────────────────────────
    if request.method == 'POST':
        # 🛡️ Rate limit — 10 updates per minute
        rate_key = f'brand_profile_save:{customer.pk}'
        rate_count = cache.get(rate_key, 0)
        if rate_count >= 10:
            return JsonResponse({"error": "حفظ كتير في وقت قصير — استنى دقيقة"}, status=429)
        cache.set(rate_key, rate_count + 1, 60)

        brand_name = request.POST.get('brand_name', '').strip()
        if not brand_name or len(brand_name) < 2:
            return JsonResponse({"error": "اسم البراند مطلوب (حرفين على الأقل)"}, status=400)

        # ── Choices validation (strict — reject unknown values) ───
        IND_VALUES = {k for k, _ in CustomerBrandProfile.INDUSTRY_CHOICES}
        AES_VALUES = {k for k, _ in CustomerBrandProfile.AESTHETIC_CHOICES}
        TONE_VALUES = {k for k, _ in CustomerBrandProfile.TONE_CHOICES}
        FONT_VALUES = {k for k, _ in CustomerBrandProfile.FONT_STYLE_CHOICES}

        industry = request.POST.get('industry', 'other').strip()
        if industry not in IND_VALUES:
            industry = 'other'
        aesthetic = request.POST.get('aesthetic', 'modern_minimal').strip()
        if aesthetic not in AES_VALUES:
            aesthetic = 'modern_minimal'
        tone = request.POST.get('tone', 'warm').strip()
        if tone not in TONE_VALUES:
            tone = 'warm'
        arabic_font = request.POST.get('arabic_font', 'arabic_modern').strip()
        if arabic_font not in FONT_VALUES:
            arabic_font = 'arabic_modern'
        english_font = request.POST.get('english_font', 'modern_sans').strip()
        if english_font not in FONT_VALUES:
            english_font = 'modern_sans'

        bp, created = CustomerBrandProfile.objects.get_or_create(
            customer=customer,
            defaults={'brand_name': brand_name[:120]},
        )
        bp.brand_name = brand_name[:120]
        bp.brand_name_en = request.POST.get('brand_name_en', '').strip()[:120]
        bp.tagline = request.POST.get('tagline', '').strip()[:200]
        bp.primary_color = _hex_or('#7c3aed', request.POST.get('primary_color'))
        bp.secondary_color = _hex_or('#1e293b', request.POST.get('secondary_color'))
        bp.accent_color = _hex_or('', request.POST.get('accent_color'))
        bp.industry = industry
        bp.aesthetic = aesthetic
        bp.tone = tone
        bp.arabic_font = arabic_font
        bp.english_font = english_font
        bp.style_notes = request.POST.get('style_notes', '').strip()[:500]
        bp.is_active = request.POST.get('is_active', '1') in ('1', 'true', 'on')
        bp.auto_inject_logo = request.POST.get('auto_inject_logo', '1') in ('1', 'true', 'on')
        bp.auto_inject_colors = request.POST.get('auto_inject_colors', '1') in ('1', 'true', 'on')

        # ── Logo uploads (validated) ─────────────────────────────
        logo_file = request.FILES.get('logo')
        if logo_file:
            if logo_file.size > 5 * 1024 * 1024:
                return JsonResponse({"error": "حجم اللوجو أكبر من 5MB"}, status=400)
            ct = (logo_file.content_type or '').lower()
            if ct not in ('image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'):
                return JsonResponse({"error": "صيغة اللوجو لازم تكون PNG/JPG/WEBP/SVG"}, status=400)
            bp.logo_image = logo_file

        logo_alt = request.FILES.get('logo_alt')
        if logo_alt:
            if logo_alt.size > 5 * 1024 * 1024:
                return JsonResponse({"error": "حجم اللوجو البديل أكبر من 5MB"}, status=400)
            bp.logo_alt_image = logo_alt

        bp.save()

        return JsonResponse({
            "status": "success",
            "created": created,
            "brand_name": bp.brand_name,
            "logo_url": bp.logo_image.url if bp.has_logo else None,
            "message": "✅ تم حفظ ملف البراند! هيتطبق تلقائياً في كل تصميم جاي.",
        })

    # ── DELETE: wipe the entire profile ─────────────────────────
    if request.method == 'DELETE':
        try:
            customer.brand_profile.delete()
            return JsonResponse({"status": "deleted"})
        except CustomerBrandProfile.DoesNotExist:
            return JsonResponse({"status": "no_profile"})

    return JsonResponse({"error": "Method not allowed"}, status=405)


@csrf_exempt
def brand_profile_delete_logo(request, slot):
    """يحذف لوجو معيّن (primary أو alt) من ملف البراند بدون مسح البقية."""
    from clients.models import CustomerBrandProfile
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)
    if slot not in ('primary', 'alt'):
        return JsonResponse({"error": "slot لازم يكون primary أو alt"}, status=400)
    try:
        bp = customer.brand_profile
    except CustomerBrandProfile.DoesNotExist:
        return JsonResponse({"error": "مفيش ملف براند محفوظ"}, status=404)
    if slot == 'primary':
        bp.logo_image.delete(save=False)
        bp.logo_image = None
    else:
        bp.logo_alt_image.delete(save=False)
        bp.logo_alt_image = None
    bp.save(update_fields=['logo_image' if slot == 'primary' else 'logo_alt_image'])
    return JsonResponse({"status": "success", "slot": slot})


def brand_profile_page(request):
    """🎨 صفحة تحرير ملف البراند — تستهلك JSON API الموجود."""
    customer = _marketplace_auth(request)
    if not customer:
        # /marketplace/login/ is a JSON-only POST API. The actual login UI
        # lives as a modal inside the sector pages, reached via /marketplace/
        # (choose_sector.html). Send unauthenticated users there.
        return redirect(f"/marketplace/?next={request.path}")
    return render(request, 'clients/marketplace/brand_profile.html', {
        'customer': customer,
    })
