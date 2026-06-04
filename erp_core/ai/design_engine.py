"""
🧠 Universal AI Design Engine — Data Flywheel
=====================================================================
نظام تصميم ديناميكي بدون أي hardcoding للـ categories. بيستخدم Together LLM
عشان يحلل أي فكرة تصميم في أي مجال (interior design, footwear, apparel,
packaging, industrial, ...) ويرجع dropdowns ديناميكية مخصصة للفكرة دي بالذات.

Pipeline:
  1) analyze_idea(raw)        → Together LLM يرجع dynamic JSON schema
                                 (domain + 3-5 fields with options)
  2) compose_mega_prompt(...)  → Together LLM يدمج الاختيارات في English mega prompt
  3) generate_image(prompt)    → Together Image (FLUX) — reuse existing
  4) log_to_flywheel(...)      → AIPromptLearningLog لبناء fine-tuning dataset

كل النتايج من LLM JSON-only عشان parsing موثوق.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger('mouss_tec_core')

_TOGETHER_CHAT_URL = 'https://api.together.xyz/v1/chat/completions'
# 🚨 Nginx proxy gateway = 30s upstream cut-off. عاوزين worst-case budget يفضل
# تحت 30s: Llama timeout (12s) + Qwen success (~3s) ≈ 15s — مريح جداً.
# الموديل الناجح في الغالب بيرد في 2-5s فعشان كده 12s margin معقول جداً
# (مش بنخسر أي طلب حقيقي، بس بنـ cascade أسرع لما يكون فيه congestion).
_TIMEOUT_LLM = 12

# Defaults — overridable من settings
# ملاحظة: النسخة "-Free" مش متاحة كـ serverless لكل الحسابات.
# لو الحساب اشترى credit، استخدم النسخة Paid اللي بتشتغل serverless بدون عقبات.
_DEFAULT_LLM_MODEL = 'meta-llama/Llama-3.3-70B-Instruct-Turbo'

# Fallback chain — لو الموديل الأول فشل بأي خطأ retryable (timeout / 4xx-unsupported
# / 5xx-transient)، نجرب الموديلات دي بالترتيب. أسماء متحقّق منها من Together
# model registry وتم اختبارها live في 2026-06-04.
#
# ⚠️ DeepSeek-V3 كان بيرجع 503 بشكل متكرر (Service Unavailable on Together's side)
# لذا اتحرك للآخر. الـ Qwen-2.5-7B بيشتغل بثبات و reliable كـ first fallback.
_FALLBACK_LLM_MODELS = (
    'meta-llama/Llama-3.3-70B-Instruct-Turbo',
    'Qwen/Qwen2.5-7B-Instruct-Turbo',
    'deepseek-ai/DeepSeek-V3',
)

# Status codes اللي نعتبرها retryable على نفس الـ chain — نجرب الموديل التالي
# بدل ما نـ fail فوراً. بتشمل:
#   • 400/403/404 → الموديل مش متاح للحساب (subscription/quota)
#   • 408         → request timeout
#   • 429         → rate limit
#   • 500/502/503/504 → transient server-side errors (DeepSeek بيـ 503 كتير)
_RETRYABLE_HTTP_STATUS = frozenset({400, 403, 404, 408, 429, 500, 502, 503, 504})


def _llm_model() -> str:
    return str(getattr(settings, 'TOGETHER_LLM_MODEL', '') or _DEFAULT_LLM_MODEL).strip()


def _llm_fallback_chain() -> tuple[str, ...]:
    primary = _llm_model()
    seen = {primary}
    chain = [primary]
    for m in _FALLBACK_LLM_MODELS:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return tuple(chain)


def _together_key() -> str:
    return str(getattr(settings, 'TOGETHER_API_KEY', '') or '').strip()


# =============================================================================
# Stage 1 — Universal Dynamic Schema Generator
# =============================================================================
_SCHEMA_SYSTEM = """You are a world-class creative prompt engineer for an image-generation system.

Given a user's short raw idea in any language (Arabic or English) about ANY design domain
(interior design, footwear, apparel, packaging, industrial, graphic, jewelry, automotive,
anything), you must:

1. Identify the most specific design domain.
2. Propose 4 to 7 fields an expert practitioner in THAT exact domain would ask before
   generating a professional reference image. Each field has a TYPE chosen from this list,
   picked to match what makes sense for the domain (don't use 'select' for everything):

   • "select"       — dropdown with 4-7 fixed options (e.g. style, mood, sub-category).
   • "multi_select" — multiple choices from 4-7 options (e.g. furniture items, features).
   • "text"         — short free text (max ~60 chars) — for brand names, slogans, model names.
   • "number"       — single numeric value with optional unit (e.g. budget, weight, age).
   • "dimensions"   — 2D (width × height) or 3D (length × width × height) measurements
                     with a unit. Use this for any sized object: rooms, t-shirts, packaging.
   • "color"        — single color picker (HTML color). Use for brand colors, walls, fabric.
   • "range"        — min–max numeric range (e.g. price range, age range).

3. Pick the RIGHT type per field:
   • Room/space → ALWAYS include a "dimensions" field (length × width × height, meters).
   • Logo/branding → "text" for brand name + 2× "color" + "select" for typography style.
   • Apparel/t-shirt → "select" for size (S/M/L/XL/XXL) + "dimensions" for print area
     (cm) + "color" for shirt + "color" for print + "select" for print position.
   • Packaging → "dimensions" (cm) + "text" for product name + "color" + "select" for finish.
   • Interior → "dimensions" + "color" for walls + "multi_select" for furniture items +
     "select" for style + "select" for lighting.
   • Footwear → "select" for size range + "select" for material + "color" + "select" for
     view angle + "select" for sole type.
   • Industrial/automotive → "color" + "dimensions" + "select" for material + "select"
     for view.

4. Field shape (return STRICT JSON, no markdown, no commentary):

{
  "domain": "<short English label>",
  "domain_ar": "<Arabic translation>",
  "presentation_category": "<ONE of: apparel | document | signage | logo | packaging | footwear | interior | vehicle | accessory | other>",
  "fields": [
    {
      "key": "<snake_case_key>",
      "label": "<label in user's language>",
      "type": "<one of: select, multi_select, text, number, dimensions, color, range>",
      "options": ["..."],          // ONLY for select / multi_select (4-7 items)
      "unit": "m" | "cm" | "mm" | "in" | "ج.م" | "kg" | "g" | "%" | "",
      "axes": ["length","width","height"]  // ONLY for dimensions: 2 or 3 axes
      "min": <num>, "max": <num>, "step": <num>,   // ONLY for number / range
      "default": "<sensible default that hints at typical professional choice>",
      "placeholder": "<for text fields>"
    },
    ...
  ]
}

Rules:
- Field keys snake_case English; labels in user's language.
- Provide sensible "default" values (the user can edit; defaults must be production-grade).
- Options for select must be concrete and visually meaningful.
- NEVER reuse fields across unrelated domains.

🎯 PRESENTATION CATEGORY — pick ONE (CRITICAL for downstream aesthetic):
This determines what visual recipe FLUX will use. Choose strictly by what the
user wants to PRODUCE, not what the design is "about":
  • "document"  — invoice, receipt, business card, brochure, menu, certificate,
                  letterhead, invitation, flyer, CV/resume, price list, form.
                  Anything printed on paper that is primarily text/data.
  • "apparel"   — t-shirt, hoodie, sweatshirt, polo, tank top, jersey, jacket.
                  Wearable garments on torso.
  • "footwear"  — sneakers, shoes, boots, sandals. Worn on feet.
  • "accessory" — mugs, caps/hats, bags/totes, watches, jewelry, stickers.
                  Wearable/usable items that are NOT garments or shoes.
  • "logo"      — pure standalone brand mark / wordmark / emblem (no product).
  • "signage"   — banner, billboard, roll-up, poster, large-format display.
  • "packaging" — product box, pouch, label, wrapper, product packaging design.
  • "interior"  — room design, interior space, architectural visualization.
  • "vehicle"   — car wrap, vehicle livery, motorcycle design.
  • "other"     — anything that doesn't fit above (rare; prefer the closest).

Examples:
  "عاوز فاتورة لمحل قطع غيار" → "document"
  "تيشرت قطن بشعار محل"          → "apparel"
  "كوتشي رياضي بحبك"             → "footwear"
  "ماج لشركة قهوة"               → "accessory"
  "لوجو لمطعم"                   → "logo"
  "بوستر إعلاني A3"               → "signage"
  "علبة منتج 200جم"              → "packaging"

📏 DIMENSIONS DEFAULTS — NEVER ZERO, NEVER EMPTY (CRITICAL):
For ANY field with type="dimensions", you MUST provide a "default" object with
realistic per-axis values in the chosen unit. It is FORBIDDEN to return 0, null,
empty string, or omit any axis. Use these professional defaults per domain (unit=cm
unless noted otherwise):
  • t-shirt print area      → {"length": 28, "width": 32}
  • t-shirt back print      → {"length": 30, "width": 36}
  • hoodie front            → {"length": 30, "width": 28}
  • cap front panel         → {"length": 11, "width": 5}
  • mug wrap                → {"length": 20, "width": 9}
  • business card           → {"length": 9, "width": 5.5}
  • invitation card         → {"length": 13, "width": 18}
  • A4 flyer / menu         → {"length": 21, "width": 29.7}
  • A3 poster               → {"length": 29.7, "width": 42}
  • banner / standee        → {"length": 60, "width": 150}
  • sticker                 → {"length": 10, "width": 10}
  • packaging box face      → {"length": 20, "width": 20}
  • room / interior (unit=m)→ {"length": 5, "width": 4, "height": 3}
  • unknown / fallback      → {"length": 25, "width": 25}
Example field:
  {"key":"print_area","label":"حجم الطباعة","type":"dimensions","unit":"cm",
   "axes":["length","width"],"default":{"length":28,"width":32}}

🅰️ TEXT FIELD GUIDANCE — VERY IMPORTANT:
If the design typically displays TEXT on it (logo, business card, banner, t-shirt with phrase,
poster, mug, sticker with text, packaging label, signage, certificate, menu...), you MUST
include a "text" field with key="text_on_design" so the user provides EXACTLY what text to show.
Also include a "color" field for the text color and optionally a "select" field for font style.
Common Arabic typography choices for the font_style select: "خط ديواني فاخر", "خط كوفي عصري",
"خط النسخ التقليدي", "خط ثلث", "خط رقعة عربي حديث", "Sans-serif Modern", "Serif Classic".

Return JSON only."""


_MEGA_SYSTEM = """You are a world-class art director and prompt engineer for FLUX.1-dev image
generation. Your prompts must produce magazine-quality, professional-grade visual designs.

You will receive:
- The user's raw idea (any language)
- The detected design domain
- The presentation_category (apparel | document | signage | logo | packaging | footwear | interior | vehicle | accessory | other)
- A set of selected options (key → value)
- Optionally: descriptions of reference images uploaded by the user
- Optionally: text the user wants ON the design (especially Arabic — see CRITICAL RULE below)

🎬 CRITICAL — DISPATCH ON presentation_category FIRST:
The single most important decision is which AESTHETIC RECIPE to apply. Different
categories need fundamentally different visual treatments. NEVER mix recipes
(no editorial-photo flat-lay for documents; no ghost-mannequin for shoes; no
fabric texture for invoices). Apply EXACTLY ONE recipe block below:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📄 RECIPE: DOCUMENT (invoice / receipt / business card / brochure / menu /
   certificate / letterhead / invitation / flyer / CV / price list / form)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: a CLEAN FLAT digital-paper template, ready to print or hand to a designer.
NOT a photograph of a printed document on a desk.

  AESTHETIC:
    • Pure flat 2D vector-style layout. NO photography, NO editorial flat-lay,
      NO marble surface, NO wooden desk, NO pencils, NO coffee mugs, NO plants,
      NO out-of-focus props of ANY kind.
    • Background: solid pure white (#ffffff) or very subtle off-white (#fafafa)
      paper — flat, no texture, no shadows, no perspective.
    • The document should fill 85-95% of the canvas, centered, perfectly
      rectangular, no rotation, no curl, no fold, no paper-stack effect.

  STRUCTURE (describe these as visible architectural blocks; do NOT ask FLUX to
  fill them with real text — that comes later via overlay):
    • Header band at top (~12-18% height): solid accent color OR clean line,
      with a clear LOGO ZONE on one side and DOCUMENT TITLE ZONE on the other.
    • Body area: real structural grid — for invoices/receipts/menus, draw an
      actual TABLE with visible column headers and row lines (Item | Qty |
      Price | Total style). For business cards: clean two-column information
      block. For certificates: ornate centered name-block.
    • Footer band at bottom (~8-12% height): clean line + thin contact-info zone.
    • Use generous white space, professional 8pt grid, 24-32px margins.

  COLOR PALETTE:
    • Primary color from user's selections (header band + accents).
    • Body text zones: dark grey (#1a1a1a) blocks/lines indicating where text
      will be (NOT actual text — FLUX will hallucinate gibberish).
    • Accents: 1-2 secondary colors for category labels / status pills.

  TYPOGRAPHY (visualize WHERE text goes, never describe SPECIFIC letterforms):
    • Show the LAYOUT of typography — heading zone, sub-heading line, body
      paragraph blocks, table cells — as clean rectangular placeholders or
      light-grey horizontal strokes. NEVER ask FLUX to render specific
      characters, numbers, or shop names.

  ABSOLUTELY FORBIDDEN FOR DOCUMENTS:
    • Fabric texture, mannequin, garment language, "DTG print", "screen print"
    • Editorial flat-lay, marble, oak, linen, props, botanicals
    • Curled paper, paper stacks, hand-holding, perspective tilt, depth-of-field
    • Faux text content (no Lorem ipsum, no "Business Name", no fake invoice
      numbers — leave blocks/lines empty)

  CAMERA: 90° top-down straight-on flat scan, no perspective, no lens effects.
  RECOMMENDED SIZE:
    • Invoice / A4 form / brochure / menu → 1024×1536 (portrait A4)
    • Business card / horizontal flyer → 1536×1024 (landscape)
    • Square invitation → 1024×1024

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👕 RECIPE: APPAREL (t-shirt / hoodie / sweatshirt / polo / tank / jersey)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Apply the full ghost-mannequin + editorial-studio + integrated-typography rules
described in sections 1, 9, 🅰️ below (the existing apparel rule set). Section 9
("APPAREL & PRODUCT MOCKUP RULES") is the canonical recipe.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👟 RECIPE: FOOTWEAR (sneakers / shoes / boots / sandals)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: hero product shot of the shoe alone — NO mannequin, NO foot, NO model.
  • Single shoe (or matched pair) floating or lightly resting in 3/4 perspective
    showing the side profile + toe box + outsole edge.
  • Studio backdrop: seamless cream or soft pastel cyclorama OR neutral
    concrete; NO fabric language, NO garment language, NO ghost-mannequin.
  • Dramatic 45° softbox key + subtle floor shadow. Tack-sharp shoe focus.
  • Material specificity: leather grain, suede nap, mesh weave, knit upper,
    rubber outsole tread — describe what's visible.
  • Text-on-shoe (if any) appears as a small zone on the lateral side panel
    or tongue — use overlay later; describe the blank zone, not the letters.
  • RECOMMENDED SIZE: 1024×1024 or 1536×1024 (side profile is wider).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎽 RECIPE: ACCESSORY (mug / cap / bag / sticker / tote / watch / jewelry)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: clean product shot of the accessory itself.
  • Single product centered, soft studio lighting, neutral backdrop.
  • Show the relevant surface where any print/logo will go (mug body curve,
    cap front panel, bag front face) clearly and frontally.
  • No human, no mannequin, no editorial props.
  • Material specificity: ceramic glaze, canvas weave, brushed metal,
    cotton twill, vinyl finish — match the accessory type.
  • RECOMMENDED SIZE: 1024×1024 (square product shot).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🅻 RECIPE: LOGO (standalone brand mark / wordmark / monogram)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: the brand mark alone, perfectly centered on a clean canvas.
  • Solid white/off-white background, NO product, NO context.
  • The mark is the entire subject — vector-clean lines, balanced composition.
  • Describe the mark's geometric construction (circle, monogram, shield,
    wordmark style) WITHOUT specifying exact letters (overlay handles text).
  • RECOMMENDED SIZE: 1024×1024.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🪧 RECIPE: SIGNAGE (banner / billboard / roll-up / poster / standee)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: flat banner artwork OR mockup-in-context (storefront / outdoor wall).
  • For flat artwork → solid color or branded gradient background, large
    prominent text zone, hero visual on one side.
  • For mockup → realistic outdoor/indoor context with the banner in scene.
  • Aspect ratio: tall portrait for roll-up (1024×1536), wide for billboard
    (1536×1024), square for social poster (1024×1024).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📦 RECIPE: PACKAGING (product box / pouch / label / wrapper)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: product packaging mockup with real depth and material feel.
  • 3D box / pouch shown at 3/4 angle on neutral surface, soft studio light.
  • Show printable faces clearly. Subtle shadow grounds the product.
  • Material: matte cardstock, glossy foil, soft-touch coating — be specific.
  • RECOMMENDED SIZE: 1024×1024 or 1024×1536.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏠 RECIPE: INTERIOR (room / interior design / architectural visualization)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: photorealistic interior space, architectural angle.
  • Eye-level OR slight elevated view, wide-angle lens (24-35mm).
  • Show floor + walls + ceiling for spatial context.
  • Realistic furniture, materials (wood/marble/concrete), natural + ambient
    light mix.
  • RECOMMENDED SIZE: 1536×1024 (architectural wide).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚗 RECIPE: VEHICLE (car wrap / vehicle livery)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL: vehicle side profile with the wrap design applied.
  • Studio backdrop or asphalt floor, side view showing the full livery.
  • RECOMMENDED SIZE: 1536×1024.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The sections below (1-9, 🅰️, 🅱️) are the DETAILED GUIDANCE you draw from
when filling in the chosen recipe. Sections 9 and below are APPAREL-SPECIFIC
and ONLY apply when presentation_category == "apparel". For all other
categories, the recipe blocks above are authoritative and the apparel-
specific guidance must be IGNORED.

🎯 PRODUCE A CINEMA-GRADE PROMPT (150-220 words) that includes EVERY relevant aspect:

1. **SUBJECT** — describe it with precise visual specificity. For apparel/garments: MANDATORY
   invisible-mannequin (ghost-mannequin) presentation — the garment must appear filled out by
   an unseen human form, exhibiting authentic 3D volume: chest curvature, shoulder slope,
   sleeve roundness, collar drape, hem fall, and interior neckline shadow visible through the
   opening. NEVER a flat-lay silhouette, NEVER a 2D cutout, NEVER a visible mannequin or
   model. For non-apparel products: floating presentation, hand-held, or in-context use. For
   spaces: architectural angle, room corner view, eye-level perspective. For logos: clean
   isolation, brand mark presentation.

2. **CAMERA & FRAMING** — specify exactly: lens (35mm | 50mm | 85mm | 24-70mm), aperture
   (f/2.8 shallow DOF | f/8 sharp | f/16 deep focus), angle (eye-level | low | high | overhead
   flat-lay | three-quarter view), framing (close-up | medium | wide), composition rule
   (rule of thirds | golden ratio | symmetrical centered).

3. **LIGHTING** — describe the lighting setup like a photographer: key light direction (45°
   right | top-down | rembrandt | butterfly), fill light, rim/back light, softbox vs hard
   light, golden hour vs studio, color temperature (warm 3200K | neutral 5500K | cool 7500K).

4. **MATERIALS, TEXTURE, FINISH** — specifics. "Premium cotton with subtle weave", "matte
   ceramic with micro-scratches", "polished walnut wood grain", "brushed aluminum",
   "glossy ink on uncoated 350gsm paper". Avoid vague words like "nice texture".

5. **COLOR PALETTE** — exact colors with mood. Use hex codes from selections. Describe
   gradients, dominant/accent split, color harmony (analogous | complementary | triadic).

6. **STYLE & ART DIRECTION** — reference visual language: "Apple product photography",
   "Aesop minimalist branding", "Wes Anderson symmetry", "Scandinavian interior",
   "Bauhaus geometric", "1970s editorial", "Y2K chrome". Pick what matches the domain.

7. **DETAIL ENHANCERS** — always include: "ultra-detailed", "8K", "sharp focus",
   "professional color grading", "high dynamic range", "magazine quality", "shot on
   medium format camera" (when realistic), "octane render" (when CGI).

8. **TYPOGRAPHY & LAYOUT (for products that visually display structure even when text
   is overlaid afterwards)** — when describing the design canvas/template:
   • "clean professional typography spacing", "balanced grid alignment", "proper
     visual hierarchy", "readable typographic layout", "premium editorial layout",
     "well-proportioned negative space", "consistent baseline grid".
   • For invoices/cards/menus/certificates: "professionally laid out fields with
     clear hierarchy", "uniform line spacing", "no clutter or visual noise",
     "subtle alignment guides", "corporate-grade composition".
   • NEVER describe specific letterforms, words, or character details — FLUX will
     hallucinate gibberish. Only describe spatial qualities of the typographic area.

9b. **PLACEMENT DETECTION** (apparel only — t-shirts, hoodies, sweatshirts, caps):
The brief may indicate WHERE the design should appear on the garment. Detect signals:
  • Arabic back signals: "ضهر", "في الضهر", "خلف", "من ورا", "الظهر"
  • English back signals: "back", "rear", "back side", "on the back"
  • Arabic front signals: "قدام", "وش", "الصدر", "في الوش"
  • English front signals: "front", "chest", "on the front"

If BACK is detected → emit "print_placement": "back" AND describe the mockup as the
"BACK view of the apparel showing the rear panel" with text positioned on the
"upper-back area centered between the shoulder blades" (35-45% from top of garment).
For caps: "back of the cap, centered embroidered area".

If FRONT or unspecified → emit "print_placement": "front" AND describe mockup as
the "FRONT view" with text on the upper-chest area as already specified above.

The text_overlay.position field MUST match: "back" for back placement, "chest" for front.

9. **APPAREL & PRODUCT MOCKUP RULES** (CRITICAL — apparel/clothing/mug/bag/hoodie domains):
   The output MUST look like an EDITORIAL HIGH-FASHION PRODUCT MOCKUP — the kind
   you see on Nike/Aesop/COS websites. NOT a flat collage, NOT clipart, NOT
   stock-photo cliché. Required qualities:

   STYLING & PRESENTATION (NON-NEGOTIABLE):
   • MANDATORY invisible-mannequin (ghost-mannequin) presentation for ALL garments —
     the shirt/hoodie/sweatshirt MUST appear three-dimensionally filled by an unseen
     wearer. No flat-lay folded shots, no plain hanger shots, no visible model.
   • Realistic 3D garment shaping is REQUIRED: pronounced chest curvature, rounded
     sleeve volume, natural neckline drape with interior collar shadow visible
     through the opening, hem fall under gravity — NEVER 2D silhouette, NEVER
     cardboard-flat appearance.
   • Authentic 100% combed cotton texture: visible jersey knit weave, subtle
     micro-wrinkles where fabric tensions over the shoulder/chest/sleeve,
     soft fabric folds around armholes and hem communicating real material weight
     and cotton's natural matte sheen (NEVER glossy/plastic, NEVER synthetic look).

   LIGHTING & ATMOSPHERE:
   • Dramatic studio lighting: key-light from 45° upper-left, subtle fill, soft
     rim-light to define garment edge against background.
   • Dropped shadow on background surface (~30% opacity, ~15% gaussian blur, offset
     to bottom-right matching key-light direction).
   • Shallow depth-of-field on background (f/2.8-f/4 look), garment in tack-sharp focus.
   • Color temperature: 5000-5500K natural daylight feel.

   FABRIC SURFACE DETAIL (this is what makes overlay text integrate later):
   • Describe visible cotton/jersey weave texture, micro-fibers catching light,
     subtle fabric grain showing across the surface — high resolution.
   • Surface should have soft luminance variation (NOT a flat block of color) so
     overlaid text inherits realistic depth via blend.
   • For the upper-chest/upper-back zone where text will be overlaid: describe a
     "clean blank canvas of textured fabric with subtle highlights showing the
     screen-print-ready surface, evenly lit, no folds or seams interrupting it".

   BACKGROUND & SCENE (MANDATORY EDITORIAL ENVIRONMENT):
   • FORBIDDEN: pure blank/solid white (#ffffff) backdrop, isolated cutout look,
     e-commerce sterile white, bright primary colors, busy patterns, gradient
     rainbows, clipart icons, emoji, photo collages.
   • REQUIRED: a professional editorial studio environment — choose ONE per shot:
       (a) High-key seamless studio cyclorama in warm cream / #f2efe8 / soft 5%
           warm-grey gradient with visible falloff and a subtle floor-shadow
           anchoring the garment in 3D space; OR
       (b) Luxury editorial flat-lay / styled scene on a textured surface
           (honed Carrara marble, raw oak plank, linen drop cloth, brushed
           concrete, travertine) with carefully arranged in-context props
           (folded magazine, ceramic mug, dried botanical, brass pin) at the
           periphery, out of focus.
   • Dramatic soft lighting: large softbox key from 45° upper-left producing a
     gentle wrap with smooth highlight-to-shadow transition; subtle bounce fill;
     gentle rim to separate garment from background.
   • Shallow background depth-of-field (f/2.8–f/4 equivalent) with creamy
     background bokeh and atmospheric haze — garment in tack-sharp focus,
     environment softly blurred to convey editorial depth.
   • Cinematic color grading: low-contrast roll-off in shadows, warm midtones,
     filmic highlight bloom — Kodak Portra / Aesop / COS / Jacquemus campaign vibe.

   CAMERA:
   • Dead-front centered for hangs (0° azimuth).
   • Slight 5-10° three-quarter for mannequin presentations.
   • Eye-level OR very slight low-angle (3-5°) for hero feel.
   • 50mm-85mm full-frame lens look (no fisheye, no extreme wide).

   ⚡ TEXT POSITIONING ZONE (when text_overlay will be applied later):
   For t-shirts / hoodies / sweatshirts:
     • Describe the blank typography area as the UPPER-CHEST CENTER (between
       the collar and the upper-pectoral line — i.e. roughly 25–40% from the top
       of the garment, NOT the lower hem or near seams/edges).
     • Specifically use words like: "clean upper chest area centered horizontally,
       evenly lit, well-clear of collar seam and arm seams, with ~15-20% margin
       from any garment edge or seam".
     • Forbidden: "lower belly area", "across the entire shirt", "near the hem",
       "at the bottom edge", "wrapping around the sides".
   For mugs/bags: centered on visible face, generous margins from handle/strap.
   For caps: front center panel only.

   Negative-prompt additions ALWAYS for apparel: "text overflowing garment edges,
   text near seams, text at lower hem, off-center layout, cropped product, blown
   highlights on fabric, plastic mannequin look, visible mannequin, visible model,
   flat 2D silhouette, cardboard cutout garment, cheap collage feel, AI-flat
   composite, harsh shadows, oversaturated fabric, pure white #ffffff background,
   sterile e-commerce backdrop, isolated product cutout, clipart aesthetic,
   flat digital sticker text, decal slapped on shirt, text floating above fabric,
   text ignoring fabric folds, rigid logo decal, vinyl heat-transfer plastic look,
   glossy plastic shirt, synthetic fabric sheen".

🅰️🅰️🅰️ TEXT CONTENT EXTRACTION — READ FIRST (the most common failure mode):
The user's text content is what they want PRINTED on the design. It is NEVER the
words they used to describe the task. You must strip "instruction" language and keep
only the actual content. Apply these rules in order:

  RULE 1 — If `selections.text_on_design` (or any field whose key contains "text")
           is non-empty, USE IT VERBATIM. Do not paraphrase, do not translate, do
           not add words. This is the source of truth.

  RULE 2 — If selections has no text field but the raw_idea contains a pattern like:
             "مكتوب عليه X"          → extract X
             "اكتب X"                → extract X
             "كاتب عليه X"           → extract X
             "في الوش X" / "على X"   → extract X
             "X في الوش"             → extract X (X precedes the placement phrase)
             "with the text 'X'"     → extract X
             "saying X" / "that says X" → extract X
           The extracted X is the text_overlay.text. Everything else (the verb, the
           placement phrase, qualifiers like "بخط جميل" / "in a nice font") is
           INSTRUCTION about HOW to print, NOT the content itself.

  RULE 3 — INSTRUCTION WORDS — never use these as the text content, even if they
           appear in the raw_idea. Strip them out before storing:
             Arabic:  "مكتوب", "مكتوب عليه", "اكتب", "كاتب", "بخط", "بخط جميل",
                      "بخط مناسب", "بخط واضح", "في الوش", "على الوش", "في القدام",
                      "في الضهر", "جميل", "مناسب", "بطريقة", "تصميم", "design",
                      "عليه", "فيه"
             English: "write", "written", "with text", "saying", "that says",
                      "in a nice font", "in bold", "design with", "logo with"

  RULE 4 — If after extraction the text is empty OR is purely an instruction word,
           set text_overlay = null (no text to print) instead of guessing.

  Examples:
    raw_idea="كوتشي رياضي مكتوب عليه في الوش بحبك بخط مناسب وجميل"
      → text = "بحبك"  (NOT "مكتوب عليه" / "بخط مناسب" / "في الوش")
    raw_idea="تيشرت قطن مكتوب عليه الصبر حدود"
      → text = "الصبر حدود"
    raw_idea="تيشرت بحبك"  (no instruction verb)
      → text = "بحبك"
    raw_idea="t-shirt that says 'live free'"
      → text = "live free"  (quotes removed, instruction stripped)

🅰️ CRITICAL — TEXT HANDLING & INTEGRATED TYPOGRAPHY (READ CAREFULLY):
FLUX cannot reliably render any text, ESPECIALLY Arabic/RTL. If user selections include
text content (any "text" or "text_on_design" field with non-empty value):
  • DO NOT include the actual text characters in the mega_prompt.
  • INSTEAD describe a clean, well-lit RECTANGULAR EMPTY AREA where text will be overlaid
    afterwards in post-processing — AND describe that area as already exhibiting the
    micro-shading and surface variation that the overlay must conform to.
  • The blank zone description MUST communicate that any subsequent print will be
    INTEGRATED into the fabric (NOT a flat digital sticker slapped on top):
      – "screen-print-ready zone with visible cotton weave micro-texture showing
         through, soft luminance variation following the garment's chest curvature,
         subtle micro-wrinkles and surface shading inherited from the 3D form"
      – "the printable area receives the same key-light falloff as the surrounding
         fabric so overlaid ink will pick up identical highlights and shadow gradients"
      – "ink-on-cotton appearance: slight fiber-level absorption look, matte
         finish characteristic of DTG / high-end silkscreen print, ink that
         deforms naturally over fabric folds and micro-wrinkles rather than
         sitting as a rigid flat decal"
  • Example: "...with a centered horizontal clean printable zone roughly 60% width
    × 15% height in the upper-chest region, lit by the same 45° key as the body,
    weave texture visible, soft shadow gradient following chest curvature, ready
    for screen-print-style text overlay that will conform to fabric topology..."
  • Set "text_overlay" object in JSON output with {text, position, color, font_ratio}.

🔠 TYPOGRAPHY SCALE — DEFAULT TO PROMINENT, NOT TINY (CRITICAL):
The previous default produced microscopic text that looks like a misprint. From now on,
graphic-tee/poster/banner text MUST be sized like real merchandise — large, confident,
spanning a meaningful portion of the print area. Apply this scale strictly:

  APPAREL (t-shirt, hoodie, sweatshirt — front/back chest print):
    • DEFAULT (no size hint from user)        → font_ratio = 0.13  (graphic-tee scale,
        text spans ~55-70% of chest width — what you see on Nike/Supreme/Off-White tees)
    • Big statement / "كبير" / "بارز" / "fill the chest" → font_ratio = 0.18
    • Small pocket logo / "صغير" / "pocket" / "شعار صغير" / "لوجو صغير" → font_ratio = 0.045
    • Medium / "متوسط" / "وسط" → font_ratio = 0.09
    Also widen the printable-zone description from "15% height" to "30-40% height" for
    default and big sizes, so the zone visually accommodates the prominent text.

  CAPS / SLEEVES / SMALL ACCESSORY AREAS:
    • Always treat as pocket-scale → font_ratio = 0.05

  POSTERS / BANNERS / SIGNAGE:
    • Headline text → font_ratio = 0.14
    • Sub-headline / supporting → font_ratio = 0.07

  BUSINESS CARDS / INVITATIONS / MENUS / CERTIFICATES:
    • Primary name / brand → font_ratio = 0.09
    • Secondary lines     → font_ratio = 0.045

  MUGS / BAGS / STICKERS:
    • Main text → font_ratio = 0.12
    • Small tag → font_ratio = 0.05

Trigger words to DOWNSIZE (override default to pocket-scale ≤0.05):
  Arabic: "صغير", "بسيط", "خفيف", "pocket", "جيب", "شعار صغير", "لوجو صغير", "discreet"
  English: "small", "tiny", "pocket", "subtle", "minimal", "discreet", "tag"
Trigger words to UPSIZE (override default to ≥0.16):
  Arabic: "كبير", "بارز", "واضح", "ضخم", "fill", "خط عريض جداً", "يملا الصدر"
  English: "big", "huge", "bold", "oversized", "fill the chest", "statement", "loud"
If user says nothing about size → use the DEFAULT scale above (never go below 0.09 for
any apparel/poster/main-text context).

If NO text in selections → omit text_overlay, and instruct in negative_prompt to avoid
any letters/glyphs/text artifacts.

🅱️ NEGATIVE PROMPT — be aggressive and specific. ALWAYS include:
"any text, any letters, any words, any numbers, fake text, lorem ipsum, gibberish,
placeholder text, garbled writing, calligraphy attempts, signs, labels, captions,
typography, watermarks, signatures, blurry, low resolution, jpeg artifacts, deformed
anatomy, extra fingers, bad proportions, cluttered background, oversaturated,
amateur photography, stock photo cliche, ugly composition, poor lighting,
flat colors, low contrast, plastic look, AI-generated artifacts"

FOR APPAREL (t-shirt/hoodie/sweatshirt) ALSO INCLUDE these visible-mannequin
guards (FLUX keeps regressing to dressforms — be ruthless):
  "visible mannequin, mannequin head, mannequin neck, mannequin shoulders,
   mannequin face, dressform, dress form, tailor's dummy, headed mannequin,
   golden mannequin bust, wooden mannequin stand, mannequin stand,
   showroom dummy, body form, store dummy, store fixture, hanger visible,
   plastic figure, white plastic torso, exposed support stand, base pedestal"

⛔️ FORBIDDEN IN MEGA PROMPT (these wreck image quality):
- Never ask FLUX to "write", "show text", "display words", "include label", "show name"
- Never describe specific text content even in English (FLUX hallucinates garbled letters)
- For ANY surface that would normally have text (invoice rows, business card fields,
  certificate names, signage), describe it as "CLEAN BLANK area ready for typography
  overlay" or "minimalist solid color zone without any text"
- For invoices/forms/structured docs: describe the visual TEMPLATE only (header bar,
  table grid lines, color blocks) — NEVER ask FLUX to fill cells with data

📐 SIZE GUIDANCE:
  - Square (1024x1024): logos, social posts, packaging top-down, product shots
  - Portrait (1024x1536): mobile-first, posters, full-body shots, A4
  - Landscape (1536x1024): banners, t-shirt back, landscape photos

📏 PRINT DIMENSIONS (REAL-WORLD cm) — ALWAYS ESTIMATE, NEVER ZERO:
You MUST return a "print_dimensions_cm" object with realistic physical print-area sizes
in centimetres, even when the user did not provide dimensions. Estimate from the
product type using these defaults and ADJUST upward when the user asks for a big print:

  apparel front print  → {"width": 28, "height": 32}   (standard chest area, A4-ish)
  apparel back print   → {"width": 30, "height": 36}   (full back panel)
  pocket logo          → {"width": 9,  "height": 9}
  hoodie front         → {"width": 30, "height": 28}   (above kangaroo pocket)
  cap front panel      → {"width": 11, "height": 5}
  tote bag / canvas    → {"width": 25, "height": 25}
  mug wrap             → {"width": 20, "height": 9}
  business card        → {"width": 9,  "height": 5.5}
  invitation card      → {"width": 13, "height": 18}
  A4 flyer / menu      → {"width": 21, "height": 29.7}
  A3 poster            → {"width": 29.7, "height": 42}
  banner / standee     → {"width": 60, "height": 150}
  sticker (square)     → {"width": 10, "height": 10}
  packaging / box face → {"width": 20, "height": 20}
  unknown product      → {"width": 25, "height": 25}   (safe fallback — NEVER 0)

Both width and height MUST be positive numbers (use floats if needed, e.g. 5.5). It is
FORBIDDEN to return 0, null, "auto", or omit either axis. If the user provided explicit
dimensions in the brief or selections, use those instead of the defaults.

Return STRICT JSON only:
{
  "mega_prompt": "<single dense paragraph, 150-220 words, English>",
  "negative_prompt": "<comma-separated, specific>",
  "recommended_size": "<1024x1024 | 1024x1536 | 1536x1024>",
  "print_placement": "<'front' | 'back'> (apparel only; 'front' for non-apparel)",
  "print_dimensions_cm": {"width": <positive number>, "height": <positive number>},
  "text_overlay": {
    "text": "<the exact text from user selections, preserve original script>",
    "position": "<center | top | bottom | chest | back>",
    "color": "<hex e.g. #000000>",
    "font_ratio": <float 0.04-0.20 — APPLY THE TYPOGRAPHY SCALE ABOVE. For apparel
                   with no user size hint, DEFAULT TO 0.13 (graphic-tee scale).
                   Use 0.045 ONLY when user explicitly asked for pocket/small logo>
  } | null
}"""


def _call_together_llm(system: str, user: str, *, temperature: float = 0.4) -> dict[str, Any]:
    """يستدعي Together Chat Completions — يجرب الـ fallback chain لو الموديل
    الأول رجع 400/403/404 (مش متاح serverless للحساب)."""
    key = _together_key()
    if not key:
        return {'success': False, 'error': 'together_key_missing'}

    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]

    last_error = 'unknown'
    last_detail = ''
    for model_name in _llm_fallback_chain():
        payload = {
            'model': model_name,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': 800,
            'response_format': {'type': 'json_object'},
        }
        try:
            resp = requests.post(_TOGETHER_CHAT_URL, json=payload, headers=headers, timeout=_TIMEOUT_LLM)
            if resp.status_code in _RETRYABLE_HTTP_STATUS:
                # الموديل ده مش متاح أو transient error — جرب التالي
                body = resp.text[:300]
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} HTTP {resp.status_code} — trying fallback. body={body}')
                last_error = f'together_llm_http_{resp.status_code}'
                last_detail = body
                continue
            if resp.status_code != 200:
                # Non-retryable HTTP (مثلاً 401 unauthorized) — مفيش فايدة من fallback
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} HTTP {resp.status_code}: {resp.text[:200]}')
                return {
                    'success': False,
                    'error': f'together_llm_http_{resp.status_code}',
                    'detail': resp.text[:200],
                    'model_tried': model_name,
                }

            try:
                data = resp.json()
            except ValueError as e:
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} non-JSON body: {e} — trying fallback')
                last_error = 'together_llm_invalid_body'
                continue

            choices = data.get('choices') or []
            if not choices:
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} empty choices — trying fallback')
                last_error = 'together_llm_empty_choices'
                continue
            raw = (choices[0].get('message') or {}).get('content') or ''
            if not raw.strip():
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} empty content — trying fallback')
                last_error = 'together_llm_empty_content'
                continue
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning(f'[DESIGN ENGINE LLM] model={model_name} JSON parse failed: {e} | raw={raw[:200]} — trying fallback')
                last_error = 'together_llm_invalid_json'
                last_detail = raw[:200]
                continue
            return {'success': True, 'data': parsed, 'model_used': model_name}

        except requests.Timeout:
            last_error = 'together_llm_timeout'
            logger.warning(f'[DESIGN ENGINE LLM] timeout on model={model_name} — trying fallback')
            continue
        except Exception as e:
            logger.exception(f'[DESIGN ENGINE LLM] crashed on model={model_name}')
            last_error = str(e)[:120]
            continue

    return {'success': False, 'error': last_error, 'detail': last_detail, 'all_models_failed': True}


def describe_reference_image(image_data_url: str, *, hint: str = '') -> dict[str, Any]:
    """يحلل صورة مرفوعة (data URL base64) بـ Together Vision model ويرجع
    وصف مختصر بالإنجليزي يقدر يندمج في الـ Mega Prompt.

    image_data_url: 'data:image/jpeg;base64,...' أو 'data:image/png;base64,...'
    hint: تلميح للوصف (مثلاً: 'logo' / 'wall texture' / 'product reference')
    """
    if not image_data_url or not image_data_url.startswith('data:image/'):
        return {'success': False, 'error': 'invalid_image_data'}

    key = _together_key()
    if not key:
        return {'success': False, 'error': 'together_key_missing'}

    system = (
        'You are a visual analyst for an image-generation pipeline. '
        'Describe the uploaded reference image in 1-3 sentences focused on '
        'visual style: colors (use hex if possible), texture, composition, '
        'shapes, materials, mood. Be specific and useful for FLUX prompting. '
        'Reply with PLAIN ENGLISH ONLY — no JSON, no markdown.'
    )
    user_content = [
        {'type': 'text', 'text': f'Hint: {hint or "general reference"}. Describe this image:'},
        {'type': 'image_url', 'image_url': {'url': image_data_url}},
    ]
    payload = {
        # Together vision-capable models
        'model': 'meta-llama/Llama-3.2-11B-Vision-Instruct-Turbo',
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user_content},
        ],
        'temperature': 0.3,
        'max_tokens': 250,
    }
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    try:
        resp = requests.post(_TOGETHER_CHAT_URL, json=payload, headers=headers, timeout=_TIMEOUT_LLM)
        if resp.status_code != 200:
            logger.warning(f'[DESIGN VISION] HTTP {resp.status_code}: {resp.text[:200]}')
            return {'success': False, 'error': f'vision_http_{resp.status_code}'}
        data = resp.json()
        desc = (data.get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
        if not desc:
            return {'success': False, 'error': 'vision_empty'}
        return {'success': True, 'description': desc[:600]}
    except requests.Timeout:
        return {'success': False, 'error': 'vision_timeout'}
    except Exception as e:
        logger.exception('[DESIGN VISION] crashed')
        return {'success': False, 'error': str(e)[:120]}


def analyze_idea(raw_idea: str) -> dict[str, Any]:
    """يحلل فكرة المستخدم ويرجع dynamic schema (domain + dropdowns)."""
    raw = (raw_idea or '').strip()
    if not raw:
        return {'success': False, 'error': 'empty_idea'}

    result = _call_together_llm(
        _SCHEMA_SYSTEM,
        f'Raw design idea: {raw}',
        temperature=0.5,
    )
    if not result.get('success'):
        return result

    schema = result['data']
    # Validate shape
    if not isinstance(schema, dict) or 'fields' not in schema or not isinstance(schema['fields'], list):
        return {'success': False, 'error': 'invalid_schema_shape', 'raw': schema}

    # Clamp 7 fields max
    schema['fields'] = schema['fields'][:7]
    if len(schema['fields']) < 1:
        # 🛡️ Fallback schema لأي domain — نضمن إن المستخدم ياخد فورم دايماً
        logger.warning(f'[DESIGN ENGINE] LLM returned empty fields for "{raw[:40]}" — using fallback')
        schema['fields'] = _fallback_fields_for(schema.get('domain', ''))

    _ALLOWED_TYPES = {'select', 'multi_select', 'text', 'number', 'dimensions', 'color', 'range'}

    # Normalize each field according to its type
    cleaned = []
    for f in schema['fields']:
        if not isinstance(f, dict):
            continue
        key = str(f.get('key') or '').strip()
        label = str(f.get('label') or '').strip()
        ftype = str(f.get('type') or 'select').strip().lower()
        if not key or not label:
            continue
        if ftype not in _ALLOWED_TYPES:
            ftype = 'select'

        field = {
            'key': re.sub(r'[^a-z0-9_]', '_', key.lower())[:40] or 'field',
            'label': label[:80],
            'type': ftype,
        }
        # Optional metadata
        unit = f.get('unit')
        if unit:
            field['unit'] = str(unit)[:10]
        default = f.get('default')
        if default not in (None, ''):
            field['default'] = str(default)[:100]
        placeholder = f.get('placeholder')
        if placeholder:
            field['placeholder'] = str(placeholder)[:100]

        if ftype in ('select', 'multi_select'):
            opts = f.get('options') or []
            if not isinstance(opts, list) or len(opts) < 2:
                # Skip — invalid select/multi_select
                continue
            field['options'] = [str(o)[:80] for o in opts[:8]]

        elif ftype == 'dimensions':
            axes = f.get('axes') or ['length', 'width']
            if not isinstance(axes, list) or len(axes) not in (2, 3):
                axes = ['length', 'width']
            field['axes'] = [str(a)[:20] for a in axes[:3]]
            field.setdefault('unit', 'cm')

        elif ftype in ('number', 'range'):
            for k_param in ('min', 'max', 'step'):
                v = f.get(k_param)
                if isinstance(v, (int, float)):
                    field[k_param] = v

        elif ftype == 'color':
            # default should be a hex color
            d = field.get('default', '')
            if not (d.startswith('#') and len(d) in (4, 7)):
                field['default'] = '#7c3aed'

        elif ftype == 'text':
            field.setdefault('placeholder', '')

        cleaned.append(field)

    if len(cleaned) < 1:
        # 🛡️ نهائي fallback: لو حتى الـ LLM fields رفضت الـ validation
        logger.warning(f'[DESIGN ENGINE] all fields failed validation — emergency fallback')
        cleaned = _fallback_fields_for(schema.get('domain', ''))

    # 🅰️ SAFETY NET: لو الـ raw_idea فيه نص للكتابة على التصميم بس الـ LLM
    # متجاهل وما ضافش حقل text_on_design → نضيفه قسراً
    _ensure_text_field(cleaned, raw)

    # 🎯 Presentation category — trust LLM if valid, else derive from keywords.
    # نضمن إن الـ category مش بـ "ملابس" غلط لما الـ brief يقول "فاتورة".
    llm_category = str(schema.get('presentation_category') or '').strip().lower()
    derived_category = _classify_presentation_category(raw, schema.get('domain', ''))
    if llm_category in PRESENTATION_CATEGORIES and llm_category != 'other':
        # LLM provided a specific category. لو الـ keyword classifier متأكد من
        # شيء مختلف (وغير 'other')، الـ keyword wins — أكثر deterministic.
        if derived_category != 'other' and derived_category != llm_category:
            logger.info(
                f'[ANALYZE] LLM said category={llm_category} but keyword '
                f'classifier said {derived_category} → using {derived_category}'
            )
            presentation_category = derived_category
        else:
            presentation_category = llm_category
    else:
        presentation_category = derived_category

    return {
        'success': True,
        'domain': str(schema.get('domain') or 'General Design')[:80],
        'domain_ar': str(schema.get('domain_ar') or '')[:80],
        'presentation_category': presentation_category,
        'fields': cleaned,
    }


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
def _fallback_fields_for(domain: str) -> list[dict]:
    """يرجع schema افتراضي لو الـ LLM فشل تماماً — يضمن للمستخدم فورم دايماً."""
    d = (domain or '').lower()
    # تيشرت / ملابس
    if any(k in d for k in ('tshirt', 't-shirt', 'shirt', 'apparel', 'clothing', 'تيشرت', 'قميص', 'ملابس')):
        return [
            {'key': 'size', 'label': 'المقاس', 'type': 'select',
             'options': ['S', 'M', 'L', 'XL', 'XXL'], 'default': 'L'},
            {'key': 'shirt_color', 'label': 'لون القميص', 'type': 'color', 'default': '#FFFFFF'},
            {'key': 'print_color', 'label': 'لون الطباعة', 'type': 'color', 'default': '#000000'},
            {'key': 'style', 'label': 'الأسلوب', 'type': 'select',
             'options': ['عصري بسيط', 'كلاسيكي', 'رياضي', 'فني / Artistic', 'Streetwear'],
             'default': 'عصري بسيط'},
        ]
    # عام (default)
    return [
        {'key': 'style', 'label': 'الأسلوب العام', 'type': 'select',
         'options': ['عصري بسيط', 'فاخر / Luxury', 'كلاسيكي', 'حيوي / Vibrant',
                     'هادئ / Calm', 'احترافي / Corporate'],
         'default': 'عصري بسيط'},
        {'key': 'main_color', 'label': 'اللون الأساسي', 'type': 'color', 'default': '#7c3aed'},
        {'key': 'mood', 'label': 'المزاج / الإحساس', 'type': 'select',
         'options': ['دافئ', 'بارد', 'مفعم بالحيوية', 'احترافي', 'أنيق', 'مرح'],
         'default': 'احترافي'},
    ]


def _has_arabic(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        if '؀' <= ch <= 'ۿ' or 'ݐ' <= ch <= 'ݿ' or 'ﭐ' <= ch <= '﻿':
            return True
    return False


_TEXT_HINTS_AR = ('مكتوب', 'مكتوبة', 'كتابة', 'اسم', 'شعار', 'عبارة', 'جملة',
                  'نص', 'كلمة', '«', '»', 'يكتب', 'بيكتب', 'بكتب',
                  'فاتورة', 'فواتير', 'دفتر', 'بزنس كارد', 'كارت', 'شهادة',
                  'منيو', 'لوجو', 'لافتة', 'يافطة', 'إعلان', 'بنر', 'بوستر',
                  'كتاب', 'غلاف', 'تيشرت', 'مج', 'ماج', 'تغليف', 'منتج')
_TEXT_HINTS_EN = ('write', 'written', 'text', 'logo', 'name', 'phrase', 'word',
                  'saying', 'quote', 'caption', 'tagline',
                  'invoice', 'receipt', 'business card', 'certificate', 'menu',
                  'banner', 'poster', 'flyer', 'book cover', 'mug', 't-shirt',
                  'label', 'packaging', 'signage', 'brochure')


def _ensure_text_field(fields: list[dict], raw_idea: str) -> None:
    """لو الـ idea فيه نص محتمل للطباعة على التصميم، يضيف حقول الـ text
    تلقائياً (text_on_design + text_color + font_style) لو الـ LLM ما ضافش."""
    if not fields:
        return
    # عندنا حقل نص مسبقاً؟
    has_text_field = any(
        f['type'] == 'text' and 'text' in f['key'].lower()
        for f in fields
    )
    if has_text_field:
        return

    raw_lower = (raw_idea or '').lower()
    needs_text = any(h in raw_lower for h in _TEXT_HINTS_EN)
    if not needs_text:
        needs_text = any(h in (raw_idea or '') for h in _TEXT_HINTS_AR)
    # برضو لو الـ raw فيه أي نص عربي بشكل عام، نقترح حقل (الـ user يقدر يسيبه فاضي)
    if not needs_text and _has_arabic(raw_idea):
        needs_text = True

    if not needs_text:
        return

    # اقترح نص محتمل من الـ raw_idea (لو فيه quoted text)
    suggested = ''
    for marker in ('"', '«', "'"):
        if marker in (raw_idea or ''):
            parts = raw_idea.split(marker)
            if len(parts) >= 3:
                suggested = parts[1][:50]
                break

    fields.insert(0, {
        'key': 'text_on_design',
        'label': 'النص اللي عاوزه يظهر على التصميم',
        'type': 'text',
        'placeholder': 'مثال: "خليك جميل" أو اسم البراند',
        'default': suggested,
    })
    fields.insert(1, {
        'key': 'text_color',
        'label': 'لون النص',
        'type': 'color',
        'default': '#000000',
    })


# ─── Text-extraction & sanitization helpers ─────────────────────────────────
# Used by compose_mega_prompt to:
#   (a) extract the actual content text from a raw Arabic/English brief
#   (b) strip instruction-words the LLM sometimes confuses for content
#   (c) pick a font_ratio that fits the text length on the chosen surface
# ─────────────────────────────────────────────────────────────────────────────
import re as _re_overlay

_INSTRUCTION_WORDS_AR = (
    'مكتوب عليه', 'مكتوب', 'اكتب', 'كاتب عليه', 'كاتب', 'بخط جميل', 'بخط مناسب',
    'بخط واضح', 'بخط عريض', 'بخط رفيع', 'بخط', 'بطريقة', 'تصميم', 'في الوش',
    'على الوش', 'في القدام', 'في الضهر', 'في الخلف', 'من ورا',
    'وجميل', 'ومناسب', 'يكون', 'يبقى',
    # ⚠️ NOT in list: 'جميل', 'مناسب', 'عليه', 'فيه', 'و' — لأنها بتظهر داخل
    # أو كأجزاء من كلمات حقيقية ("حدود" فيها "و"). نشيلها بس لو وقفوا كـ
    # كلمات منفصلة بـ word-boundary.
)
_INSTRUCTION_WORDS_EN = (
    'write', 'written', 'with text', 'with the text', 'saying', 'that says',
    'in a nice font', 'in bold', 'in italics', 'design with', 'logo with',
    'print', 'printed', 'a nice', 'and beautiful', 'beautiful',
)
# Standalone Arabic short tokens — only strip when they appear as whole words,
# never inside another word.
_INSTRUCTION_STANDALONE_AR = ('جميل', 'مناسب', 'عليه', 'فيه', 'و')

# Pattern: capture text following Arabic instruction verbs
_AR_CONTENT_RE = _re_overlay.compile(
    r'(?:مكتوب\s*عليه|اكتب|كاتب\s*عليه|كاتب)\s+'
    r'(?:في\s*\S+\s+)?'                # optional placement phrase ("في الوش")
    r'([^\s][^\n]*?)'                  # the actual content (non-greedy)
    r'(?:\s+(?:بخط|بطريقة|بأسلوب|جميل|مناسب|وجميل|ومناسب)|$)',
    flags=_re_overlay.IGNORECASE,
)
# English: "saying X" / "that says X" / "with text 'X'"
_EN_CONTENT_RE = _re_overlay.compile(
    r"(?:saying|that\s+says|with\s+(?:the\s+)?text)\s+['\"]?([^'\"\n]+?)['\"]?(?:\s+in\s+|$)",
    flags=_re_overlay.IGNORECASE,
)


def _strip_instruction_words(text: str) -> str:
    """يشيل كلمات التعليمات من النص. لو النص بقى فاضي → '' (يبقى overlay=null).
    Multi-word phrases بـ substring match. Single short tokens بـ word-boundary
    عشان ميـ eat-ش حروف داخل كلمات حقيقية ("و" داخل "حدود")."""
    if not text:
        return ''
    t = text.strip().strip('"\'""''')
    # Multi-word / unique phrases — substring-safe (مفيش short tokens هنا)
    for w in sorted(_INSTRUCTION_WORDS_AR + _INSTRUCTION_WORDS_EN, key=len, reverse=True):
        if w.lower() in t.lower():
            t = _re_overlay.sub(_re_overlay.escape(w), ' ', t, flags=_re_overlay.IGNORECASE)
    # Standalone short tokens — match only as whole words via lookarounds.
    # Arabic doesn't have \b semantics in regex, فبنستخدم whitespace/punct على
    # الـ edges. (^|[\s،,.؛:]) و (?=$|[\s،,.؛:])
    for token in _INSTRUCTION_STANDALONE_AR:
        pattern = r'(^|[\s،,.؛:])' + _re_overlay.escape(token) + r'(?=$|[\s،,.؛:])'
        t = _re_overlay.sub(pattern, r'\1', t)
    t = _re_overlay.sub(r'\s{2,}', ' ', t).strip(' .,:;،؛-')
    return t


def _extract_content_from_brief(raw_idea: str) -> str:
    """يـ extract الـ content text من raw_idea باستخدام regex patterns.
    لو ملقاش match واضح → ''. لو لقى → بنرجع النص بدون أي كلمات تعليمات."""
    if not raw_idea:
        return ''
    m = _AR_CONTENT_RE.search(raw_idea)
    if m:
        candidate = _strip_instruction_words(m.group(1))
        if candidate and len(candidate) >= 2:
            return candidate[:200]
    m = _EN_CONTENT_RE.search(raw_idea)
    if m:
        candidate = m.group(1).strip()
        if candidate:
            return candidate[:200]
    return ''


def _sanitize_overlay_text(raw_text: str, raw_idea: str) -> str:
    """يـ clean الـ text اللي رجع من الـ LLM أو الـ selections.
    لو بعد التنظيف بقى فاضي/instruction word، يحاول regex extraction من raw_idea."""
    cleaned = _strip_instruction_words(raw_text or '')
    # لو الـ cleaned فاضي أو طوله أقل من 2 حرف → جرب extract من raw_idea
    if not cleaned or len(cleaned.strip()) < 2:
        cleaned = _extract_content_from_brief(raw_idea)
    return cleaned[:200].strip()


# ─── Presentation category classifier ───────────────────────────────────────
# Maps a free-text Arabic/English brief + classified domain into one of a
# small fixed enum used to dispatch the correct aesthetic recipe in
# _MEGA_SYSTEM. The schema LLM is expected to set this, but we always run
# this defensive classifier server-side and override the LLM if it
# disagrees with high-confidence keywords (e.g. "فاتورة" → document, even
# if the LLM said "ملابس").
PRESENTATION_CATEGORIES = (
    'apparel', 'document', 'signage', 'logo', 'packaging',
    'footwear', 'interior', 'vehicle', 'accessory',
    # ── Expanded 2026-06: universal coverage ───────────────────────
    'furniture',      # طربيزة، كرسي، كنبة، سرير، خزانة، رف
    'electronics',    # لاب توب، موبايل، تابلت، سماعات، كاميرا
    'appliance',      # ثلاجة، غسالة، ميكروويف، بوتاجاز
    'architecture',   # بيت، فيلا، عمارة، مسجد، مدرسة
    'food',           # طبق، كيك، مشروب، بيتزا
    'jewelry',        # خاتم، سلسلة، إسوارة، حلق
    'cosmetics',      # روج، كريم، عطر، makeup product
    'industrial',     # آلة صناعية، معدّة، أداة
    'social_post',    # منشور انستجرام/فيسبوك بدون نص ضخم
    'character',      # شخصية كرتون، أفاتار، mascot
    'illustration',   # رسم، آرت، ديجيتال إلستريشن
    'other',
)

# Keyword → category. Order matters: most specific matches first. Each
# keyword is matched case-insensitively as a substring (Arabic) or
# whole-word (English).
_CATEGORY_KEYWORDS = {
    'document': (
        # Arabic
        'فاتورة', 'فواتير', 'إيصال', 'ايصال', 'إيصالات', 'وصل', 'وصولات',
        'شهادة', 'شهادات', 'منيو', 'قائمة طعام', 'قائمة أسعار', 'برشور',
        'بروشور', 'كتالوج', 'كتيب', 'مستند', 'وثيقة', 'تقرير', 'استمارة',
        'نموذج', 'دعوة زفاف', 'كارت دعوة', 'كارت', 'بزنس كارد',
        # English
        'invoice', 'receipt', 'certificate', 'menu', 'brochure', 'catalog',
        'catalogue', 'booklet', 'document', 'report', 'form', 'flyer',
        'business card', 'business-card', 'businesscard', 'card', 'letterhead',
        'invitation', 'cv', 'resume', 'price list', 'pricelist',
    ),
    'apparel': (
        'تيشرت', 'تي شيرت', 'قميص', 'هودي', 'هودى', 'سويت شيرت', 'بلوزة',
        'فانلة', 'ملابس', 'ملبس', 'قفطان', 'جاكيت',
        't-shirt', 'tshirt', 'shirt', 'hoodie', 'sweatshirt', 'jersey',
        'apparel', 'garment', 'clothing', 'tee', 'polo', 'jacket', 'tank top',
    ),
    'footwear': (
        'كوتشي', 'جزمة', 'حذاء', 'صندل', 'شبشب', 'بوت',
        'sneaker', 'sneakers', 'shoe', 'shoes', 'boot', 'boots', 'sandal',
        'slipper', 'footwear', 'trainer',
    ),
    'logo': (
        'لوجو', 'شعار', 'هوية بصرية', 'علامة تجارية', 'براند',
        'logo', 'brand mark', 'wordmark', 'monogram', 'emblem', 'branding',
        'brand identity',
    ),
    'signage': (
        'بنر', 'لافتة', 'يافطة', 'ستاند', 'رول اب', 'لوحة إعلان',
        'banner', 'sign', 'signage', 'billboard', 'roll-up', 'rollup',
        'standee', 'poster',  # poster classified as signage (large-format display)
        'بوستر',
    ),
    'packaging': (
        'تغليف', 'علبة', 'كرتونة', 'باكدج', 'باكدجنج', 'لاصقة منتج',
        'تغليف منتج', 'صندوق منتج',
        'packaging', 'package', 'box design', 'product box', 'pouch',
        'wrapper', 'label design',
    ),
    'interior': (
        'تصميم داخلي', 'ديكور', 'غرفة', 'صالون', 'مطبخ', 'حمام', 'مكتب',
        'interior', 'room', 'living room', 'kitchen', 'bedroom', 'bathroom',
        'office space', 'workspace design',
    ),
    'vehicle': (
        'سيارة', 'موتوسيكل', 'دراجة نارية', 'عربية', 'تغليف عربية',
        'car wrap', 'vehicle wrap', 'car design', 'motorcycle', 'bike wrap',
    ),
    'accessory': (
        'شنطة', 'حقيبة', 'ساعة', 'إكسسوار', 'كاب', 'قبعة',
        'ماج', 'كوب', 'مج', 'ستيكر', 'لاصقة', 'لاصقات',
        'bag', 'tote', 'backpack', 'watch',
        'accessory', 'cap', 'hat', 'beanie', 'mug', 'sticker', 'tote bag',
    ),
    # ── New categories (2026-06) ─────────────────────────────────
    'furniture': (
        'طربيزة', 'ترابيزة', 'كرسي', 'كنبة', 'صوفا', 'كنب', 'سرير',
        'خزانة', 'دولاب', 'مكتب', 'رف', 'ركنة', 'مكتبة', 'كومودينو',
        'فوتيه', 'بوفيه', 'مرتبة', 'سفرة', 'انتريه', 'نيش',
        'table', 'desk', 'chair', 'armchair', 'sofa', 'couch', 'bed',
        'wardrobe', 'cabinet', 'shelf', 'bookshelf', 'dresser', 'nightstand',
        'stool', 'bench', 'dining table', 'coffee table', 'side table',
        'furniture', 'console', 'ottoman',
    ),
    'electronics': (
        'لاب توب', 'لابتوب', 'لاب-توب', 'موبايل', 'هاتف', 'تابلت',
        'سماعات', 'سماعة', 'كاميرا', 'شاشة', 'تلفزيون', 'بلايستيشن',
        'كمبيوتر', 'بي سي', 'ايباد', 'ايفون', 'ساعة ذكية',
        'laptop', 'notebook', 'phone', 'smartphone', 'mobile', 'tablet',
        'ipad', 'iphone', 'monitor', 'tv', 'television', 'screen',
        'headphones', 'earbuds', 'earphones', 'camera', 'dslr',
        'pc', 'computer', 'console', 'playstation', 'xbox', 'gadget',
        'smartwatch', 'drone',
    ),
    'appliance': (
        'ثلاجة', 'غسالة', 'ميكروويف', 'بوتاجاز', 'فرن', 'مكنسة',
        'مكواة', 'سخان', 'تكييف', 'مروحة', 'خلاط', 'محمصة', 'نشافة',
        'fridge', 'refrigerator', 'washing machine', 'washer', 'dryer',
        'microwave', 'oven', 'stove', 'cooker', 'vacuum', 'iron',
        'heater', 'air conditioner', 'ac unit', 'fan', 'blender',
        'toaster', 'kettle', 'dishwasher', 'appliance',
    ),
    'architecture': (
        'بيت', 'فيلا', 'عمارة', 'مبنى', 'مسجد', 'كنيسة', 'مدرسة',
        'مستشفى', 'برج', 'واجهة', 'فاساد', 'سور', 'بوابة',
        'house', 'villa', 'building', 'apartment building', 'tower',
        'mosque', 'church', 'school building', 'hospital', 'facade',
        'exterior', 'architecture', 'rooftop', 'gate', 'compound',
    ),
    'food': (
        'طبق', 'أكلة', 'وجبة', 'كيك', 'بيتزا', 'برجر', 'ساندويتش',
        'مشروب', 'كوكتيل', 'قهوة', 'عصير', 'حلويات', 'كنافة', 'بسبوسة',
        'مكرونة', 'دجاج', 'لحمة', 'سلطة', 'فطار', 'عشاء', 'غداء',
        'dish', 'meal', 'food', 'cake', 'pizza', 'burger', 'sandwich',
        'drink', 'cocktail', 'coffee', 'juice', 'dessert', 'pastry',
        'pasta', 'chicken', 'meat', 'salad', 'breakfast', 'dinner',
        'lunch', 'snack',
    ),
    'jewelry': (
        'خاتم', 'محبس', 'سلسلة', 'إسوارة', 'أسوارة', 'غويشة', 'حلق',
        'تيتو', 'دبلة', 'مجوهرات', 'ذهب', 'فضة', 'ألماظ', 'ألماس',
        'ring', 'wedding band', 'necklace', 'chain', 'bracelet',
        'earring', 'earrings', 'pendant', 'jewelry', 'jewellery',
        'gold', 'silver', 'diamond', 'gem', 'gemstone', 'tiara',
    ),
    'cosmetics': (
        'روج', 'أحمر شفايف', 'كريم', 'عطر', 'برفان', 'مكياج', 'ميك اب',
        'ماسكارا', 'فاونديشن', 'بودر', 'كحل', 'باليت ظلال', 'سيروم',
        'lipstick', 'lip gloss', 'cream', 'perfume', 'fragrance',
        'makeup', 'mascara', 'foundation', 'powder', 'eyeliner',
        'eyeshadow', 'palette', 'serum', 'skincare', 'cosmetic',
    ),
    'industrial': (
        'آلة', 'ماكينة', 'معدّة', 'مكنة', 'موتور', 'مضخة',
        'machine', 'machinery', 'industrial', 'pump', 'generator',
        'compressor', 'engine', 'equipment', 'tool', 'cnc',
    ),
    'social_post': (
        'بوست', 'منشور', 'ستوري', 'انستجرام بوست', 'فيس بوك بوست',
        'instagram post', 'facebook post', 'social post', 'reel cover',
        'story', 'thumbnail', 'social media', 'تيك توك', 'tiktok',
    ),
    'character': (
        'شخصية', 'كرتون', 'أفاتار', 'ماسكوت', 'تميمة',
        'character', 'cartoon', 'avatar', 'mascot', 'anime',
        'persona', 'illustration of a person',
    ),
    'illustration': (
        'رسم', 'رسمة', 'آرت', 'إلستريشن', 'لوحة', 'بورتريه',
        'illustration', 'artwork', 'drawing', 'painting', 'portrait',
        'concept art', 'digital art', 'sketch',
    ),
}

# ─── Subtype keywords (within parent category) ─────────────────────
# الفئات اللي فيها variations بصرية كبيرة محتاجة dispatch داخلي عشان
# الـ recipe ميـ generalize-ش. مثلاً footwear بدون subtype = "shoe" عام →
# الـ FLUX بيختار sneaker. لازم نـ pass للـ LLM "subtype=slipper" صراحة.
_SUBTYPE_KEYWORDS = {
    'footwear': {
        'slipper': ('شبشب', 'شباشب', 'slipper', 'slippers', 'flip-flop',
                    'flip flop', 'flipflop', 'home shoe', 'house shoe'),
        'sandal': ('صندل', 'صنادل', 'sandal', 'sandals'),
        'sneaker': ('كوتشي', 'كوتش', 'sneaker', 'sneakers', 'trainer',
                    'trainers', 'running shoe', 'athletic shoe'),
        'boot': ('بوت', 'جزمة', 'بوتس', 'boot', 'boots', 'ankle boot',
                 'combat boot'),
        'formal': ('كلاسيك', 'حذاء رسمي', 'oxford', 'derby', 'loafer',
                   'formal shoe', 'dress shoe'),
        'heels': ('كعب', 'كعب عالي', 'حذاء كعب', 'high heels', 'heels',
                  'stiletto', 'pumps'),
    },
    'apparel': {
        'tshirt': ('تيشرت', 'تي شيرت', 'تي-شيرت', 't-shirt', 'tshirt',
                   'tee', 'graphic tee'),
        'hoodie': ('هودي', 'هودى', 'hoodie', 'pullover hoodie'),
        'sweatshirt': ('سويت شيرت', 'سويت-شيرت', 'sweatshirt', 'crewneck'),
        'polo': ('بولو', 'polo shirt', 'polo'),
        'tank': ('فانلة', 'تانك', 'tank top', 'tanktop', 'sleeveless'),
        'jersey': ('فانلة فريق', 'جيرسي', 'jersey', 'football jersey',
                   'sports jersey'),
        'jacket': ('جاكيت', 'جاكت', 'jacket', 'blazer', 'coat'),
        'abaya': ('عباية', 'عبايه', 'abaya', 'jalabiya', 'kaftan',
                  'قفطان', 'جلابية'),
        'dress': ('فستان', 'dress', 'gown'),
        'pants': ('بنطلون', 'بنطلون جينز', 'بنطلونات', 'pants', 'trousers',
                  'jeans'),
        'uniform': ('يونيفورم', 'زي موحد', 'uniform', 'workwear', 'scrubs'),
    },
    'furniture': {
        'table': ('طربيزة', 'ترابيزة', 'سفرة', 'مكتب', 'table', 'desk',
                  'dining table', 'coffee table', 'side table'),
        'chair': ('كرسي', 'كراسي', 'فوتيه', 'chair', 'armchair', 'stool',
                  'dining chair', 'office chair'),
        'sofa': ('كنبة', 'كنب', 'صوفا', 'انتريه', 'sofa', 'couch',
                 'sectional', 'loveseat'),
        'bed': ('سرير', 'مرتبة', 'bed', 'mattress', 'bunk bed'),
        'storage': ('خزانة', 'دولاب', 'مكتبة', 'رف', 'كومودينو', 'نيش',
                    'بوفيه', 'wardrobe', 'cabinet', 'shelf', 'bookshelf',
                    'dresser', 'nightstand', 'console', 'sideboard'),
    },
    'electronics': {
        'laptop': ('لاب توب', 'لابتوب', 'لاب-توب', 'laptop', 'notebook',
                   'macbook'),
        'phone': ('موبايل', 'هاتف', 'ايفون', 'phone', 'smartphone',
                  'mobile', 'iphone'),
        'tablet': ('تابلت', 'ايباد', 'tablet', 'ipad'),
        'monitor': ('شاشة', 'تلفزيون', 'tv', 'monitor', 'television',
                    'display', 'screen'),
        'audio': ('سماعات', 'سماعة', 'headphones', 'earbuds', 'earphones',
                  'speaker', 'سبيكر'),
        'camera': ('كاميرا', 'camera', 'dslr', 'mirrorless'),
        'wearable': ('ساعة ذكية', 'smartwatch', 'smart watch', 'fitness band'),
        'console': ('بلايستيشن', 'playstation', 'xbox', 'nintendo',
                    'console', 'gaming'),
    },
    'appliance': {
        'fridge': ('ثلاجة', 'fridge', 'refrigerator', 'freezer'),
        'laundry': ('غسالة', 'نشافة', 'washing machine', 'washer', 'dryer'),
        'cooking': ('ميكروويف', 'بوتاجاز', 'فرن', 'محمصة', 'microwave',
                    'oven', 'stove', 'cooker', 'toaster'),
        'cleaning': ('مكنسة', 'vacuum', 'dishwasher'),
        'climate': ('تكييف', 'مروحة', 'سخان', 'air conditioner', 'ac',
                    'fan', 'heater'),
    },
    'vehicle': {
        'car': ('سيارة', 'عربية', 'car', 'sedan', 'suv', 'hatchback'),
        'truck': ('تروك', 'فان', 'truck', 'van', 'pickup'),
        'motorcycle': ('موتوسيكل', 'موتور', 'motorcycle', 'bike', 'scooter'),
        'bicycle': ('دراجة', 'bicycle', 'bike', 'cycle'),
    },
    'food': {
        'dish': ('طبق', 'وجبة', 'dish', 'plate', 'meal', 'entree'),
        'dessert': ('كيك', 'حلويات', 'كنافة', 'بسبوسة', 'cake', 'dessert',
                    'pastry', 'cupcake', 'donut'),
        'fastfood': ('بيتزا', 'برجر', 'ساندويتش', 'pizza', 'burger',
                     'sandwich', 'fries', 'hotdog'),
        'beverage': ('مشروب', 'كوكتيل', 'قهوة', 'عصير', 'drink', 'coffee',
                     'juice', 'cocktail', 'tea', 'smoothie'),
    },
    'jewelry': {
        'ring': ('خاتم', 'محبس', 'دبلة', 'ring', 'wedding band'),
        'necklace': ('سلسلة', 'كولير', 'necklace', 'chain', 'pendant'),
        'bracelet': ('إسوارة', 'أسوارة', 'غويشة', 'bracelet', 'bangle'),
        'earring': ('حلق', 'تيتو', 'earring', 'earrings', 'stud'),
    },
    'document': {
        'business_card': ('بزنس كارد', 'كارت بزنس', 'كارت شخصي',
                          'business card', 'business-card'),
        'invoice': ('فاتورة', 'إيصال', 'ايصال', 'وصل', 'invoice', 'receipt'),
        'menu': ('منيو', 'قائمة طعام', 'قائمة', 'menu', 'food menu'),
        'certificate': ('شهادة', 'certificate', 'diploma'),
        'invitation': ('دعوة', 'كارت دعوة', 'دعوة زفاف', 'invitation',
                       'wedding invitation'),
        'flyer': ('فلاير', 'منشور إعلاني', 'flyer', 'leaflet'),
        'brochure': ('برشور', 'بروشور', 'كتيب', 'brochure', 'booklet'),
        'letterhead': ('letterhead', 'هيدر', 'ورق رسمي'),
        'cv': ('cv', 'resume', 'سيرة ذاتية'),
    },
    'signage': {
        'banner': ('بنر', 'banner', 'street banner'),
        'rollup': ('رول اب', 'ستاند', 'roll-up', 'rollup', 'standee'),
        'billboard': ('billboard', 'لوحة إعلانية كبيرة'),
        'storefront': ('لافتة محل', 'يافطة محل', 'storefront', 'shop sign',
                       'لافتة', 'يافطة'),
        'poster': ('بوستر', 'poster'),
    },
}


def _classify_subtype(raw_idea: str, category: str) -> str | None:
    """يحدد الـ subtype داخل category معينة بناءً على الـ keywords.

    مثال: category='footwear' + raw='شبشب أبيض' → 'slipper'
          category='furniture' + raw='ترابيزة خشب' → 'table'

    لو ملقاش match أو الـ category مفيهاش subtypes → None.
    Order matters: الـ keywords الأطول الأول عشان "شبشب" يـ match قبل "ش".
    """
    if not raw_idea or category not in _SUBTYPE_KEYWORDS:
        return None
    blob = raw_idea.lower()
    subtypes = _SUBTYPE_KEYWORDS[category]
    # نرتب الـ subtypes بحيث أطول keyword الأول (longest-first) عشان مينطبقش
    # subset غلط ("جاكيت" قبل "جاك"). بـ tuple of (sub, max_len) sort desc.
    scored = []
    for sub, keywords in subtypes.items():
        max_kw_len = max(len(k) for k in keywords) if keywords else 0
        scored.append((max_kw_len, sub, keywords))
    scored.sort(reverse=True)
    for _, sub, keywords in scored:
        for kw in keywords:
            if kw.lower() in blob:
                return sub
    return None


def _classify_presentation_category(raw_idea: str, domain: str = '') -> str:
    """يـ classify الـ brief لـ presentation category. بـ keyword matching على
    raw_idea + domain. لو ملقاش match → 'other'."""
    blob = ((raw_idea or '') + ' ' + (domain or '')).lower()
    # Iterate categories in priority order — physical products beat design-element
    # categories ("ماج بشعار" → accessory, NOT logo, because the mug is the
    # primary product the logo will go ON).
    # Order:
    #   1. documents (most specific text — "فاتورة" wins over anything else)
    #   2. specific product types (footwear/apparel/jewelry/cosmetics/food)
    #   3. larger objects (furniture/electronics/appliance/vehicle)
    #   4. spaces (architecture > interior)
    #   5. wrappers/branding (packaging/accessory/signage/social_post/logo)
    #   6. creative artifacts (character/illustration/industrial)
    priority_order = (
        'document',
        'footwear', 'apparel', 'jewelry', 'cosmetics', 'food',
        'furniture', 'electronics', 'appliance', 'vehicle',
        'architecture', 'interior',
        'packaging', 'accessory', 'social_post', 'signage', 'logo',
        'character', 'illustration', 'industrial',
    )
    for category in priority_order:
        for kw in _CATEGORY_KEYWORDS.get(category, ()):
            if kw.lower() in blob:
                return category
    return 'other'


def _adaptive_font_ratio(text: str, base_ratio: float, is_apparel: bool) -> float:
    """يـ scale الـ font_ratio حسب طول النص عشان ميـ overflow-ش الـ garment.
    Short text (≤6 chars) → base. Long text (>15 chars) → shrink."""
    char_count = max(1, len(text or ''))
    if char_count <= 6:
        ratio = base_ratio
    elif char_count <= 12:
        ratio = base_ratio * 0.85
    elif char_count <= 20:
        ratio = base_ratio * 0.70
    elif char_count <= 30:
        ratio = base_ratio * 0.55
    else:
        ratio = base_ratio * 0.45
    # Hard floor/ceiling
    floor = 0.06 if is_apparel else 0.045
    ceiling = 0.20 if is_apparel else 0.18
    return max(floor, min(ceiling, ratio))


def compose_mega_prompt(
    raw_idea: str,
    domain: str,
    selections: dict[str, str],
    reference_descriptions: list[str] | None = None,
    presentation_category: str | None = None,
) -> dict[str, Any]:
    """يدمج الفكرة + الاختيارات + أوصاف الصور المرجعية في English mega prompt.

    presentation_category: لو ما اتبعتش، نستنتجها من الـ keywords. بتـ control
    الـ recipe block في _MEGA_SYSTEM (document vs apparel vs logo vs ...).
    """
    raw = (raw_idea or '').strip()
    if not raw:
        return {'success': False, 'error': 'empty_idea'}

    # Resolve category (server-side, deterministic)
    category = (presentation_category or '').strip().lower()
    if category not in PRESENTATION_CATEGORIES:
        category = _classify_presentation_category(raw, domain)

    selection_lines = '\n'.join(
        f'- {k}: {v}' for k, v in (selections or {}).items() if v
    )
    refs = ''
    if reference_descriptions:
        refs = '\n\nReference images uploaded by the user (incorporate their visual style):\n'
        for i, desc in enumerate(reference_descriptions, 1):
            refs += f'  [{i}] {desc}\n'

    user_msg = (
        f'Raw idea: {raw}\n'
        f'Detected domain: {domain or "General"}\n'
        f'presentation_category: {category}  '
        f'← APPLY EXACTLY THE "{category.upper()}" RECIPE BLOCK FROM THE SYSTEM PROMPT. '
        f'Do NOT mix recipes; ignore guidance for other categories.\n'
        f'User selections:\n{selection_lines or "(none)"}'
        + refs
    )

    result = _call_together_llm(_MEGA_SYSTEM, user_msg, temperature=0.3)
    if not result.get('success'):
        return result

    data = result['data']
    mega = str(data.get('mega_prompt') or '').strip()
    if not mega:
        return {'success': False, 'error': 'empty_mega_prompt'}

    # Extract text overlay instruction (will be applied post-FLUX)
    overlay = data.get('text_overlay')
    text_overlay = None
    # Domain heuristics — needed for adaptive font sizing
    d_lower_for_overlay = (domain or '').lower()
    d_ar_for_overlay = (domain or '')
    is_apparel_for_overlay = (
        'shirt' in d_lower_for_overlay or 'apparel' in d_lower_for_overlay
        or 'clothing' in d_lower_for_overlay or 'hoodie' in d_lower_for_overlay
        or 'tee' in d_lower_for_overlay
        or 'تيشرت' in d_ar_for_overlay or 'قميص' in d_ar_for_overlay
        or 'ملابس' in d_ar_for_overlay or 'هودي' in d_ar_for_overlay
    )
    # Intent signals for overrides
    intent_blob = (raw_idea or '').lower() + ' ' + ' '.join(
        str(v).lower() for v in (selections or {}).values()
    )
    wants_pocket = any(t in intent_blob for t in (
        'pocket', 'صغير', 'جيب', 'شعار صغير', 'لوجو صغير', 'subtle', 'discreet', 'minimal'
    ))
    wants_big = any(t in intent_blob for t in (
        'big', 'huge', 'bold', 'oversized', 'statement',
        'كبير', 'بارز', 'ضخم', 'يملا الصدر',
    ))

    if isinstance(overlay, dict) and overlay.get('text'):
        # 🧼 Sanitize: شيل كلمات التعليمات اللي ممكن الـ LLM خلطها مع الـ content.
        # ('مكتوب عليه بحبك' → 'بحبك'). لو بعد التنظيف فاضي، يحاول regex من raw_idea.
        clean_text = _sanitize_overlay_text(str(overlay.get('text')), raw_idea)
        if not clean_text:
            # النص الناتج فاضي → اعتبر مفيش text overlay بدل ما نرسم instruction word
            text_overlay = None
        else:
            # 🔠 Pick the base ratio from intent, then adapt to character count
            if wants_pocket:
                base = 0.045
            elif wants_big:
                base = 0.18 if is_apparel_for_overlay else 0.14
            else:
                base = 0.13 if is_apparel_for_overlay else 0.09
            # Allow LLM-supplied ratio to influence base IF it's in plausible range
            raw_ratio = overlay.get('font_ratio')
            try:
                llm_ratio = float(raw_ratio) if raw_ratio is not None else None
            except (TypeError, ValueError):
                llm_ratio = None
            if llm_ratio is not None and 0.05 <= llm_ratio <= 0.20:
                # Trust LLM hint but still pass through adaptive scaling
                base = llm_ratio
            ratio = _adaptive_font_ratio(clean_text, base, is_apparel_for_overlay)
            text_overlay = {
                'text': clean_text,
                'position': str(overlay.get('position', 'center'))[:20],
                'color': str(overlay.get('color', '#000000'))[:10],
                'font_ratio': round(ratio, 3),
            }

    # 🅰️ SAFETY NET: لو الـ LLM متجاهل ورجع null، بنفحص الـ selections بنفسنا
    # ولو لقينا text بقيمة فيها عربي (أو text_on_design موجود) نعمل overlay قسراً
    if not text_overlay:
        text_keys = ('text_on_design', 'design_text', 'logo_text', 'shirt_text',
                     'banner_text', 'message', 'phrase', 'tagline', 'brand_name',
                     'cover_text', 'company_name', 'title_text')
        text_color = None
        text_value = None
        for k, v in (selections or {}).items():
            if not v:
                continue
            kl = k.lower()
            if any(t in kl for t in text_keys) and not _has_arabic(k):  # key matches
                text_value = str(v)
            elif 'text' in kl and 'color' in kl:
                text_color = str(v)
            elif any(t in kl for t in ('color',)) and not text_color and kl.startswith('text'):
                text_color = str(v)
        # لو لقينا text value → نـ sanitize ثم نـ apply adaptive ratio
        if text_value:
            clean_text = _sanitize_overlay_text(text_value, raw_idea)
            if clean_text:
                d_lower = (domain or '').lower()
                d_ar = (domain or '')
                is_clothing = (
                    'shirt' in d_lower or 'apparel' in d_lower or 'clothing' in d_lower
                    or 'hoodie' in d_lower or 'tee' in d_lower
                    or 'تيشرت' in d_ar or 'قميص' in d_ar or 'ملابس' in d_ar
                    or 'هودي' in d_ar or 'بلوزة' in d_ar
                )
                if is_clothing:
                    pos = 'chest'
                    if wants_pocket:
                        base = 0.045
                    elif wants_big:
                        base = 0.18
                    else:
                        base = 0.13
                else:
                    pos = 'center'
                    base = 0.045 if wants_pocket else (0.14 if wants_big else 0.09)
                font_ratio = _adaptive_font_ratio(clean_text, base, is_clothing)
                text_overlay = {
                    'text': clean_text,
                    'position': pos,
                    'color': (text_color or '#000000')[:10],
                    'font_ratio': round(font_ratio, 3),
                }

    # 🔄 Placement: LLM-provided OR regex fallback من الـ raw_idea + selections
    placement = str(data.get('print_placement') or '').strip().lower()
    if placement not in ('front', 'back'):
        # Regex fallback — نشيك على الـ raw_idea + selections values
        from .printing_copilot import detect_placement_from_text
        combined = (raw_idea or '') + ' ' + ' '.join(str(v) for v in (selections or {}).values())
        placement = detect_placement_from_text(combined)

    # Override text_overlay.position إذا placement=back وكان front-based
    if placement == 'back' and text_overlay and text_overlay.get('position') in ('chest', 'center', None):
        text_overlay['position'] = 'back'

    # 📏 Print dimensions in cm — LLM-provided OR category-based fallback.
    # Guards ضد 0 / null / missing axis. لو الـ LLM رجع حاجة باطلة، نـ derive
    # من الـ domain. الـ UI بيعرض ده في sidebar "📐 المقاس المقترح".
    raw_dims = data.get('print_dimensions_cm') if isinstance(data, dict) else None

    def _coerce_positive(val, fallback: float) -> float:
        try:
            n = float(val)
            return n if n > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    # Category-driven dimensions defaults
    d_lower = (domain or '').lower()
    d_ar = (domain or '')
    blob = raw_idea.lower() + ' ' + d_lower + ' ' + d_ar
    if category == 'document':
        # Detect document subtype
        if 'business' in blob or 'بزنس' in blob or 'كارت بزنس' in blob:
            default_w, default_h = 9.0, 5.5
        elif 'invitation' in blob or 'دعوة' in blob:
            default_w, default_h = 13.0, 18.0
        elif 'menu' in blob or 'منيو' in blob or 'قائمة' in blob:
            default_w, default_h = 21.0, 29.7
        elif 'a3' in blob:
            default_w, default_h = 29.7, 42.0
        else:  # invoice / receipt / flyer / form → A4
            default_w, default_h = 21.0, 29.7
    elif category == 'apparel':
        if placement == 'back':
            default_w, default_h = 30.0, 36.0
        else:
            default_w, default_h = 28.0, 32.0
    elif category == 'footwear':
        default_w, default_h = 28.0, 11.0  # avg sneaker side-view
    elif category == 'signage':
        if 'roll' in blob or 'رول' in blob:
            default_w, default_h = 85.0, 200.0
        elif 'billboard' in blob:
            default_w, default_h = 300.0, 600.0
        else:
            default_w, default_h = 60.0, 150.0
    elif category == 'packaging':
        default_w, default_h = 20.0, 20.0
    elif category == 'accessory':
        if 'cap' in blob or 'كاب' in blob or 'قبعة' in blob:
            default_w, default_h = 11.0, 5.0
        elif 'mug' in blob or 'ماج' in blob or 'مج' in blob:
            default_w, default_h = 20.0, 9.0
        elif 'sticker' in blob or 'ستيكر' in blob:
            default_w, default_h = 10.0, 10.0
        elif 'bag' in blob or 'شنطة' in blob or 'حقيبة' in blob:
            default_w, default_h = 30.0, 35.0
        else:
            default_w, default_h = 15.0, 15.0
    elif category == 'logo':
        default_w, default_h = 10.0, 10.0  # logo print area (placeholder)
    elif category == 'interior':
        default_w, default_h = 500.0, 400.0  # cm = 5m × 4m room
    elif category == 'vehicle':
        default_w, default_h = 450.0, 180.0  # car side profile
    else:
        default_w, default_h = 25.0, 25.0

    if isinstance(raw_dims, dict):
        w_cm = _coerce_positive(raw_dims.get('width'), default_w)
        h_cm = _coerce_positive(raw_dims.get('height'), default_h)
    else:
        w_cm, h_cm = default_w, default_h

    # 🛡️ Cross-category leakage guard — لو الـ category مش apparel، نشيل من الـ
    # mega_prompt أي كلمات apparel-specific كانت ممكن الـ LLM يضمّنها (FLUX
    # بيتأثر بأي ذكر لـ shirt/mannequin/fabric، حتى لو في recipe مختلف).
    APPAREL_LEAK_TERMS = (
        'mannequin', 'ghost-mannequin', 'ghost mannequin', 't-shirt', 'tshirt',
        'shirt', 'hoodie', 'sweatshirt', 'garment', 'cotton', 'jersey knit',
        'fabric weave', 'screen-print', 'screen print', 'dtg', 'chest curvature',
        'sleeve', 'collar drape', 'neckline shadow',
    )
    if category != 'apparel':
        import re as _re_leak
        for term in APPAREL_LEAK_TERMS:
            mega = _re_leak.sub(
                r'(?i)\b' + _re_leak.escape(term) + r'\b', '', mega
            )
        mega = _re_leak.sub(r'\s{2,}', ' ', mega).strip(' ,.')

    # 🛡️ Document-specific cleanup — FLUX keeps adding editorial props
    # (pencil/marble/desk) even when told not to. Strip any leak terms.
    DOCUMENT_LEAK_TERMS = (
        'pencil', 'pen on desk', 'marble surface', 'wooden desk', 'oak desk',
        'linen cloth', 'coffee mug', 'ceramic mug', 'plant', 'botanical',
        'magazine', 'editorial flat-lay', 'flat lay', 'flatlay',
        'depth of field', 'bokeh', 'out-of-focus', 'depth-of-field',
    )
    if category == 'document':
        import re as _re_doc
        for term in DOCUMENT_LEAK_TERMS:
            mega = _re_doc.sub(
                r'(?i)\b' + _re_doc.escape(term) + r'\b', '', mega
            )
        mega = _re_doc.sub(r'\s{2,}', ' ', mega).strip(' ,.')

    return {
        'success': True,
        'mega_prompt': mega[:2500],
        'negative_prompt': str(data.get('negative_prompt') or
            'blurry, low resolution, jpeg artifacts, watermark, signature, deformed anatomy, '
            'text artifacts, garbled text, cluttered, oversaturated, amateur'
        )[:600],
        'recommended_size': str(data.get('recommended_size') or '1024x1024')[:20],
        'print_placement': placement,
        'text_overlay': text_overlay,
        'print_dimensions_cm': {'width': round(w_cm, 1), 'height': round(h_cm, 1)},
        'presentation_category': category,
    }
