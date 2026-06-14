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



# Product type autocomplete + report.

from .utils import *  # noqa: F401, F403



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
