"""
🎨 AI Image Studio — استوديو الخلفيات الذكي لصور المخزون
=====================================================================
أداة تلقائية: أي صورة قطعة اترفعت في المخزون يقدر المستخدم يدوس زرار واحد
ويغيّر خلفيتها بالكامل (استوديو أبيض / معرض فخم / كربون … إلخ) بالذكاء
الاصطناعي — مع الحفاظ الكامل على شكل القطعة نفسها.

المعمارية:
  • بنعيد استخدام محرك التعديل الموجود أصلاً (FLUX.1-Kontext عبر Together AI)
    من erp_core.ai.printing_copilot — مفيش تكرار لكود توليد الصور.
  • Storage-agnostic: بنبني data-URI من الصورة الأصلية (بعد تصغيرها) عشان
    الخدمة تشتغل سواء الميديا على S3 أو على الـ local filesystem.
  • preview-then-apply: التوليد بيحفظ نسخة معاينة أولاً، والتطبيق منفصل عشان
    المستخدم يشوف النتيجة قبل ما يستبدل صورة القطعة (مع نسخة احتياطية للأصل).

الدوال العامة:
  • list_presets()                         → قائمة الخلفيات الجاهزة (للـ UI)
  • generate_preview(product, preset, ...) → يولّد معاينة ويحفظها مؤقتاً
  • apply_preview(product, preview_path)   → يطبّق المعاينة كصورة القطعة
  • revert(product)                        → يرجّع الصورة الأصلية
"""
from __future__ import annotations

import base64
import logging
import uuid
from io import BytesIO
from typing import Any, Optional

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

logger = logging.getLogger('mouss_tec_core')

# مجلد المعاينات المؤقتة داخل الـ default_storage. أي تطبيق (apply) لازم يتأكد
# إن الـ path اللي جاي منه العميل تحت المجلد ده — حماية من path injection.
PREVIEW_DIR = 'products/ai_previews/'
ORIGINAL_BACKUP_DIR = 'products/originals/'

# أقصى بُعد للصورة المصدر قبل ما نبعتها للمحرك — بيقلل حجم الـ payload
# وبيماشي مقاس المخرجات (1024²) من غير ما نضيّع تفاصيل القطعة.
_MAX_SOURCE_DIM = 1024
_DOWNLOAD_TIMEOUT = 45
_GEMINI_TIMEOUT = 60

# 🆓 محرك Gemini للصور (Nano Banana) — مجاني على مفتاح Google AI Studio الحالي.
# ده المحرك الافتراضي؛ لو فشل بنرجع لـ FLUX.1-Kontext (Together) لو مفتاحه موجود.
# قائمة موديلات مرتّبة بالأفضلية — نجرّبها بالترتيب لأن التسميات بتتغيّر.
_GEMINI_IMAGE_URL = (
    'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
)
_GEMINI_IMAGE_MODELS = (
    'gemini-2.5-flash-image',
    'gemini-2.5-flash-image-preview',
    'gemini-2.0-flash-preview-image-generation',
)


def _gemini_api_key() -> str:
    return str(
        getattr(settings, 'GEMINI_API_KEY', '')
        or getattr(settings, 'AI_VISION_API_KEY', '')
        or ''
    ).strip()


def _gemini_image_models() -> tuple[str, ...]:
    override = str(getattr(settings, 'GEMINI_IMAGE_MODEL', '') or '').strip()
    if override:
        return (override,) + tuple(m for m in _GEMINI_IMAGE_MODELS if m != override)
    return _GEMINI_IMAGE_MODELS


# =====================================================================
# 🎨 الخلفيات الجاهزة (Presets)
# =====================================================================
# كل preset فيه:
#   • label       — الاسم اللي بيظهر للمستخدم (عربي)
#   • icon        — Font Awesome class للـ UI
#   • instruction — تعليمات التعديل بالإنجليزي لموديل Kontext.
#
# 🔑 كل التعليمات مبنية على مبدأ واحد: "سيب القطعة زي ما هي بالظبط، غيّر
# الخلفية بس". ده أهم شرط في كتالوج قطع الغيار — أي تشويه للقطعة نفسها
# يخلّي الصورة عديمة الفائدة تجارياً.
_KEEP_SUBJECT = (
    "Keep the automotive part in the foreground exactly as-is — do not change "
    "its shape, color, text, labels, scratches or any detail. Only replace the "
    "background. Keep the part sharply in focus, centered and well-lit, "
    "professional e-commerce product photography, high detail, realistic shadows."
)

BACKGROUND_PRESETS: dict[str, dict[str, str]] = {
    'studio_white': {
        'label': 'استوديو أبيض نظيف',
        'icon': 'fa-square',
        'instruction': (
            "Replace the background with a pure seamless white studio backdrop "
            "(#ffffff), soft even lighting and a subtle natural contact shadow "
            "under the part. " + _KEEP_SUBJECT
        ),
    },
    'studio_gray': {
        'label': 'تدرّج رمادي احترافي',
        'icon': 'fa-circle-half-stroke',
        'instruction': (
            "Replace the background with a smooth neutral light-gray studio "
            "gradient (top lighter, bottom darker), soft diffused lighting and a "
            "clean reflective floor. " + _KEEP_SUBJECT
        ),
    },
    'showroom': {
        'label': 'معرض سيارات فاخر',
        'icon': 'fa-warehouse',
        'instruction': (
            "Place the part on a glossy dark showroom floor with an out-of-focus "
            "luxury car showroom in the background, cinematic ambient lighting and "
            "elegant reflections. " + _KEEP_SUBJECT
        ),
    },
    'carbon': {
        'label': 'كربون / تِك داكن',
        'icon': 'fa-microchip',
        'instruction': (
            "Replace the background with a dark carbon-fiber texture and subtle "
            "blue rim lighting for a high-tech premium performance look. "
            + _KEEP_SUBJECT
        ),
    },
    'workshop': {
        'label': 'ورشة احترافية',
        'icon': 'fa-screwdriver-wrench',
        'instruction': (
            "Place the part on a clean professional auto-workshop workbench with a "
            "softly blurred garage background, warm realistic lighting. "
            + _KEEP_SUBJECT
        ),
    },
    'gradient_brand': {
        'label': 'تدرّج أزرق تجاري',
        'icon': 'fa-droplet',
        'instruction': (
            "Replace the background with a smooth modern blue-to-navy gradient, "
            "clean marketing style with a soft glow behind the part. "
            + _KEEP_SUBJECT
        ),
    },
}

DEFAULT_PRESET = 'studio_white'


def list_presets() -> list[dict[str, str]]:
    """قائمة الـ presets جاهزة للـ template/JSON."""
    return [
        {'key': k, 'label': v['label'], 'icon': v['icon']}
        for k, v in BACKGROUND_PRESETS.items()
    ]


def build_instruction(preset_key: str, custom_prompt: str = '') -> Optional[str]:
    """يبني تعليمات التعديل النهائية.

    • preset معروف → تعليماته الجاهزة.
    • custom_prompt (بأي لغة) → بنغلّفه بشرط الحفاظ على القطعة.
    يرجّع None لو مفيش أي مدخل صالح.
    """
    custom_prompt = (custom_prompt or '').strip()
    if custom_prompt:
        return (
            f"Replace the background of this automotive part photo: {custom_prompt}. "
            + _KEEP_SUBJECT
        )
    preset = BACKGROUND_PRESETS.get(preset_key)
    if preset:
        return preset['instruction']
    return None


# =====================================================================
# 🖼️ أدوات الصور المشتركة
# =====================================================================
def _load_pillow():
    """استيراد Pillow بشكل كسول — dependency موجودة أصلاً (pillow في requirements)."""
    from PIL import Image  # noqa: WPS433 (local import: keeps module import cheap)
    return Image


def _read_field_bytes(image_field) -> Optional[bytes]:
    """يقرأ بايتات ImageField بأمان (يشتغل مع local + S3)."""
    if not image_field:
        return None
    try:
        image_field.open('rb')
        try:
            return image_field.read()
        finally:
            image_field.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[IMAGE STUDIO] failed reading source image: %s', exc)
        return None


def _downscaled_jpeg(raw: bytes) -> Optional[bytes]:
    """يصغّر الصورة لـ <=1024px ويرجّع بايتات JPEG (مصدر موحّد لكل المحركات)."""
    Image = _load_pillow()
    try:
        img = Image.open(BytesIO(raw))
        img = img.convert('RGB')
        img.thumbnail((_MAX_SOURCE_DIM, _MAX_SOURCE_DIM), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format='JPEG', quality=90)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[IMAGE STUDIO] failed encoding source image: %s', exc)
        return None


def _to_source_data_uri(jpeg_bytes: bytes) -> str:
    """يحوّل بايتات JPEG لـ data-URI (تستخدمه FLUX.1-Kontext)."""
    b64 = base64.b64encode(jpeg_bytes).decode('ascii')
    return f'data:image/jpeg;base64,{b64}'


# =====================================================================
# 🆓 محرك Gemini للصور (Nano Banana) — image editing عبر Google AI Studio
# =====================================================================
def _gen_via_gemini(jpeg_bytes: bytes, edit_instruction: str) -> dict[str, Any]:
    """يعدّل الصورة بـ Gemini (استبدال الخلفية) بمفتاح Google AI Studio المجاني.

    بيرجّع نفس شكل ناتج FLUX.1-Kontext:
      {success, b64_json|url, engine, model, cost_estimate_egp}
    """
    key = _gemini_api_key()
    if not key:
        return {'success': False, 'error': 'gemini_key_missing'}
    if not jpeg_bytes or not edit_instruction:
        return {'success': False, 'error': 'missing_input'}

    b64 = base64.b64encode(jpeg_bytes).decode('ascii')
    payload = {
        'contents': [{
            'role': 'user',
            'parts': [
                {'text': edit_instruction[:1800]},
                {'inline_data': {'mime_type': 'image/jpeg', 'data': b64}},
            ],
        }],
        # موديلات توليد الصور في Gemini بتطلب IMAGE ضمن الـ modalities.
        'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']},
    }
    headers = {'Content-Type': 'application/json'}

    last_error = 'gemini_failed'
    for model in _gemini_image_models():
        url = _GEMINI_IMAGE_URL.format(model=model, key=key)
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=_GEMINI_TIMEOUT)
        except requests.Timeout:
            last_error = 'gemini_timeout'
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning('[IMAGE STUDIO] gemini request failed: %s', exc)
            last_error = 'gemini_request_error'
            continue

        if resp.status_code in (400, 403, 404):
            # الموديل مش متاح للحساب/المفتاح — جرّب اللي بعده.
            last_error = f'gemini_{model}_{resp.status_code}'
            continue
        if resp.status_code != 200:
            last_error = f'gemini_http_{resp.status_code}'
            continue

        out_b64 = _extract_gemini_image(resp.json())
        if out_b64:
            return {
                'success': True,
                'b64_json': out_b64,
                'url': None,
                'engine': 'gemini',
                'model': model,
                'cost_estimate_egp': 0.0,   # ضمن الـ free tier
            }
        last_error = 'gemini_no_image'

    return {'success': False, 'error': last_error}


def _extract_gemini_image(data: dict) -> Optional[str]:
    """يستخرج أول جزء صورة (base64) من رد Gemini (v1beta REST → inlineData)."""
    try:
        for cand in data.get('candidates', []) or []:
            for part in (cand.get('content', {}) or {}).get('parts', []) or []:
                inline = part.get('inlineData') or part.get('inline_data')
                if inline and inline.get('data'):
                    return inline['data']
    except Exception as exc:  # noqa: BLE001
        logger.warning('[IMAGE STUDIO] gemini parse failed: %s', exc)
    return None


def _result_to_bytes(result: dict[str, Any]) -> Optional[bytes]:
    """يحوّل ناتج محرك التوليد (url أو b64_json) لبايتات."""
    b64 = result.get('b64_json')
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[IMAGE STUDIO] bad b64 result: %s', exc)
            return None
    url = result.get('url')
    if url:
        try:
            resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                return resp.content
            logger.warning('[IMAGE STUDIO] result download HTTP %s', resp.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[IMAGE STUDIO] result download failed: %s', exc)
    return None


# =====================================================================
# 🔀 اختيار المحرك: Gemini (مجاني) أولاً ثم FLUX.1-Kontext كـ fallback
# =====================================================================
def _run_engines(jpeg_bytes: bytes, instruction: str, *, use_pro: bool = False) -> dict[str, Any]:
    """يشغّل محركات تعديل الصورة بالترتيب ويرجّع أول نجاح.

    الترتيب الافتراضي: Gemini (مجاني بمفتاحك الحالي) → Together FLUX.1-Kontext.
    قابل للعكس عبر settings.IMAGE_STUDIO_ENGINE = 'together'.
    """
    order = str(getattr(settings, 'IMAGE_STUDIO_ENGINE', 'gemini') or 'gemini').strip().lower()
    engines = ['together', 'gemini'] if order == 'together' else ['gemini', 'together']

    last = {'success': False, 'error': 'no_engine_configured'}
    for name in engines:
        if name == 'gemini':
            if not _gemini_api_key():
                continue
            res = _gen_via_gemini(jpeg_bytes, instruction)
        else:  # together / kontext
            if not str(getattr(settings, 'TOGETHER_API_KEY', '') or '').strip():
                continue
            # 🔁 إعادة استخدام محرك التعديل الموجود (FLUX.1-Kontext) — image-to-image.
            from erp_core.ai.printing_copilot import _gen_via_flux_kontext
            res = _gen_via_flux_kontext(
                image_url=_to_source_data_uri(jpeg_bytes),
                edit_instruction=instruction,
                size='1024x1024',
                use_pro=use_pro,
            )
        if res.get('success'):
            return res
        logger.warning('[IMAGE STUDIO] engine %s failed: %s', name, res.get('error'))
        last = res
    return last


# =====================================================================
# 🚀 العمليات العامة
# =====================================================================
def generate_preview(
    product,
    preset_key: str = DEFAULT_PRESET,
    custom_prompt: str = '',
    *,
    use_pro: bool = False,
) -> dict[str, Any]:
    """يولّد معاينة بخلفية جديدة ويحفظها مؤقتاً — من غير ما يلمس صورة القطعة.

    Returns dict:
      نجاح: {ok: True, preview_url, preview_path, preset, engine, cost_estimate_egp}
      فشل : {ok: False, error, detail?}
    """
    if not getattr(product, 'image', None):
        return {'ok': False, 'error': 'no_image',
                'detail': 'القطعة ليس لها صورة أصلية للمعالجة.'}

    instruction = build_instruction(preset_key, custom_prompt)
    if not instruction:
        return {'ok': False, 'error': 'no_instruction',
                'detail': 'اختر خلفية جاهزة أو اكتب وصفاً للخلفية المطلوبة.'}

    raw = _read_field_bytes(product.image)
    if not raw:
        return {'ok': False, 'error': 'source_unreadable',
                'detail': 'تعذّر قراءة صورة القطعة الأصلية.'}

    jpeg = _downscaled_jpeg(raw)
    if not jpeg:
        return {'ok': False, 'error': 'source_encode_failed',
                'detail': 'تعذّر تجهيز الصورة الأصلية للمعالجة.'}

    result = _run_engines(jpeg, instruction, use_pro=use_pro)
    if not result.get('success'):
        logger.warning('[IMAGE STUDIO] all engines failed: %s', result.get('error'))
        return {'ok': False, 'error': result.get('error', 'generation_failed'),
                'detail': 'فشل توليد الخلفية الجديدة. تأكد من إعداد مفتاح Gemini '
                          '(GEMINI_API_KEY) أو Together (TOGETHER_API_KEY) وحاول مرة أخرى.'}

    out_bytes = _result_to_bytes(result)
    if not out_bytes:
        return {'ok': False, 'error': 'result_unreadable',
                'detail': 'تم التوليد لكن تعذّر جلب الصورة الناتجة.'}

    preview_path = f'{PREVIEW_DIR}{product.pk or "new"}_{uuid.uuid4().hex}.png'
    saved_path = default_storage.save(preview_path, ContentFile(out_bytes))

    return {
        'ok': True,
        'preview_path': saved_path,
        'preview_url': default_storage.url(saved_path),
        'preset': preset_key if not custom_prompt else 'custom',
        'engine': result.get('engine', 'kontext'),
        'cost_estimate_egp': result.get('cost_estimate_egp'),
    }


def apply_preview(product, preview_path: str, preset_key: str = '') -> dict[str, Any]:
    """يطبّق معاينة سبق توليدها كصورة رسمية للقطعة، مع نسخة احتياطية للأصل.

    يتحقق أن preview_path تحت PREVIEW_DIR (حماية) وأن الملف موجود فعلاً.
    """
    if (not preview_path or not preview_path.startswith(PREVIEW_DIR)
            or '..' in preview_path):
        return {'ok': False, 'error': 'invalid_preview_path',
                'detail': 'مسار المعاينة غير صالح.'}
    if not default_storage.exists(preview_path):
        return {'ok': False, 'error': 'preview_missing',
                'detail': 'انتهت صلاحية المعاينة. أعد التوليد.'}

    preview_bytes = None
    try:
        with default_storage.open(preview_path, 'rb') as fh:
            preview_bytes = fh.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning('[IMAGE STUDIO] preview read failed: %s', exc)
    if not preview_bytes:
        return {'ok': False, 'error': 'preview_unreadable',
                'detail': 'تعذّر قراءة صورة المعاينة.'}

    # نسخة احتياطية للأصل — مرة واحدة فقط (أول معالجة). لو اتعمل apply تاني
    # على قطعة معالجة قبل كده، بنحافظ على الأصل الحقيقي الأول.
    if product.image and not product.image_original_backup:
        orig_bytes = _read_field_bytes(product.image)
        if orig_bytes:
            ext = (product.image.name.rsplit('.', 1)[-1] or 'jpg')[:4]
            backup_name = f'{product.pk}_{uuid.uuid4().hex}.{ext}'
            product.image_original_backup.save(
                backup_name, ContentFile(orig_bytes), save=False)

    new_name = f'{product.pk}_bg_{uuid.uuid4().hex}.png'
    product.image.save(new_name, ContentFile(preview_bytes), save=False)
    product.image_ai_bg_applied = True
    product.image_ai_bg_preset = (preset_key or '')[:40]
    product.save(update_fields=[
        'image', 'image_original_backup', 'image_ai_bg_applied',
        'image_ai_bg_preset',
    ])

    # تنظيف ملف المعاينة بعد التطبيق (مش محتاجينه تاني).
    try:
        default_storage.delete(preview_path)
    except Exception:  # noqa: BLE001
        pass

    return {'ok': True, 'image_url': product.image.url}


def revert(product) -> dict[str, Any]:
    """يرجّع الصورة الأصلية المحفوظة قبل معالجة الـ AI."""
    if not product.image_original_backup:
        return {'ok': False, 'error': 'no_backup',
                'detail': 'لا توجد نسخة أصلية محفوظة لهذه القطعة.'}

    orig_bytes = _read_field_bytes(product.image_original_backup)
    if not orig_bytes:
        return {'ok': False, 'error': 'backup_unreadable',
                'detail': 'تعذّر قراءة النسخة الأصلية.'}

    ext = (product.image_original_backup.name.rsplit('.', 1)[-1] or 'jpg')[:4]
    restored_name = f'{product.pk}_restored_{uuid.uuid4().hex}.{ext}'
    product.image.save(restored_name, ContentFile(orig_bytes), save=False)
    product.image_ai_bg_applied = False
    product.image_ai_bg_preset = ''
    product.save(update_fields=[
        'image', 'image_ai_bg_applied', 'image_ai_bg_preset',
    ])
    return {'ok': True, 'image_url': product.image.url}
