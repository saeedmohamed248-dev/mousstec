"""
🎨 بوت إزالة الخلفية المحلي — Mouss Tec (بدون أي خدمة خارجية)
=====================================================================
يشيل خلفية صورة القطعة محلياً بالكامل على السيرفر (موديل U²-Net عبر
onnxruntime) ثم يركّب القطعة على خلفية نظيفة (أبيض / تدرّج رمادي / تدرّج
أزرق / داكن …) مع ظل ناعم تحت القطعة.

مفيش أي مفتاح API ولا تكلفة لكل صورة — كله يشتغل على الجهاز:
  • الموديل بيتنزّل مرة واحدة وقت بناء صورة الدوكر (شوف Dockerfile) لمسار
    IMAGE_STUDIO_U2NET_PATH، فأول طلب مايستناش تنزيل.
  • المعالجة CPU-only (مايحتاجش كارت شاشة).

المعمارية:
  • ملف مستقل عن image_studio.py: ده «المحرّك»، وimage_studio.py هو
    الـ orchestrator اللي بيحفظ المعاينة ويطبّقها.
  • تحميل الجلسة (session) كسول ومرة واحدة لكل عملية (process) — تقيل يتعمل
    مرة، وبعدها كل توليد سريع.

التطبيع (normalization) مطابق للوصفة المعروفة لموديل U²-Net عشان نضمن نفس
جودة القص المجرّبة (resize→LANCZOS، قسمة على أعلى قيمة بكسل، mean/std لكل
قناة، ثم CHW).
"""
from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Optional

logger = logging.getLogger('mouss_tec_core')

# مقاس الكادر النهائي للصورة الناتجة (مربّع — متّسق في الكتالوج والـ POS).
_OUTPUT_SIZE = 1024
# نسبة ملء القطعة للكادر (باقي المساحة هامش نظيف حوالين القطعة).
_SUBJECT_RATIO = 0.82
# أقصى بُعد نشتغل عليه من الصورة المصدر (أداء/ذاكرة) قبل توليد الماسك.
_MAX_WORK_DIM = 2000
# مقاس مدخل موديل U²-Net.
_MODEL_INPUT = (320, 320)

# تطبيع القنوات (نفس قيم U²-Net الأصلية).
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

# جلسة onnxruntime محمّلة مرة واحدة (تقيلة التحميل).
_SESSION = None


def _model_path() -> str:
    """مسار ملف الموديل — من settings أو متغيّر البيئة أو الافتراضي بتاع الدوكر."""
    try:
        from django.conf import settings
        p = getattr(settings, 'IMAGE_STUDIO_U2NET_PATH', '')
        if p:
            return p
    except Exception:  # noqa: BLE001 — نشتغل حتى خارج Django
        pass
    return os.environ.get('IMAGE_STUDIO_U2NET_PATH', '/app/.models/u2net.onnx')


def is_available() -> bool:
    """هل المحرّك جاهز؟ (المكتبات متثبّتة + ملف الموديل موجود)."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return os.path.exists(_model_path())


def _get_session():
    """يحمّل جلسة onnxruntime مرة واحدة (CPU)."""
    global _SESSION
    if _SESSION is None:
        import onnxruntime as ort
        path = _model_path()
        if not os.path.exists(path):
            raise FileNotFoundError(f'U2Net model not found at {path}')
        _SESSION = ort.InferenceSession(
            path, providers=['CPUExecutionProvider'])
    return _SESSION


# =====================================================================
# 🧠 توليد ماسك القطعة (U²-Net)
# =====================================================================
def _predict_mask(session, pil_rgb):
    """يرجّع ماسك رمادي (L) بنفس مقاس الصورة المصدر — أبيض = القطعة."""
    import numpy as np
    from PIL import Image

    im = pil_rgb.convert('RGB').resize(_MODEL_INPUT, Image.LANCZOS)
    arr = np.array(im).astype(np.float32)
    arr = arr / max(float(arr.max()), 1e-6)  # normalize بأعلى قيمة بكسل
    tmp = np.zeros((arr.shape[0], arr.shape[1], 3), dtype=np.float32)
    for c in range(3):
        tmp[:, :, c] = (arr[:, :, c] - _MEAN[c]) / _STD[c]
    tmp = tmp.transpose((2, 0, 1))[np.newaxis, :, :, :].astype(np.float32)

    inp_name = session.get_inputs()[0].name
    pred = session.run(None, {inp_name: tmp})[0][0, 0, :, :]

    mi, ma = float(pred.min()), float(pred.max())
    pred = (pred - mi) / (ma - mi) if (ma - mi) > 1e-6 else np.zeros_like(pred)
    mask = Image.fromarray((pred * 255).astype('uint8'), mode='L')
    return mask.resize(pil_rgb.size, Image.LANCZOS)


# =====================================================================
# 🖼️ الخلفيات النظيفة (تُرسَم محلياً — مفيش توليد)
# =====================================================================
# كل preset إمّا ('solid', color) أو ('vgrad', top_color, bottom_color).
_BACKGROUNDS: dict[str, tuple] = {
    'studio_white':   ('solid', (255, 255, 255)),
    'studio_gray':    ('vgrad', (245, 247, 250), (206, 212, 220)),
    'gradient_brand': ('vgrad', (37, 99, 235), (15, 23, 60)),
    'carbon':         ('vgrad', (32, 34, 38), (12, 13, 15)),
    'showroom':       ('vgrad', (44, 48, 56), (16, 18, 22)),
    'workshop':       ('vgrad', (238, 232, 222), (206, 196, 180)),
}
_DEFAULT_BG = ('solid', (255, 255, 255))


def _render_background(size, preset_key):
    """يبني خلفية RGBA حسب الـ preset (لون ثابت أو تدرّج رأسي)."""
    from PIL import Image

    spec = _BACKGROUNDS.get(preset_key, _DEFAULT_BG)
    if spec[0] == 'solid':
        return Image.new('RGBA', size, spec[1] + (255,))

    # تدرّج رأسي: نبني عمود 1×h بسرعة ثم نمدّه على العرض.
    _, top, bottom = spec
    w, h = size
    col = Image.new('RGB', (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
    return col.resize(size).convert('RGBA')


# =====================================================================
# 🚀 العملية الرئيسية
# =====================================================================
def process(raw: bytes, preset_key: str = 'studio_white') -> Optional[bytes]:
    """يشيل الخلفية ويركّب القطعة على خلفية الـ preset مع ظل ناعم.

    يرجّع بايتات PNG للصورة الناتجة، أو None لو فشلت المعالجة.
    """
    from PIL import Image, ImageFilter

    session = _get_session()

    src = Image.open(BytesIO(raw)).convert('RGB')
    src.thumbnail((_MAX_WORK_DIM, _MAX_WORK_DIM), Image.LANCZOS)

    mask = _predict_mask(session, src)
    cut = src.convert('RGBA')
    cut.putalpha(mask)

    bbox = cut.getbbox()
    if bbox:
        cut = cut.crop(bbox)

    canvas = (_OUTPUT_SIZE, _OUTPUT_SIZE)
    max_dim = int(_OUTPUT_SIZE * _SUBJECT_RATIO)
    cut.thumbnail((max_dim, max_dim), Image.LANCZOS)

    bg = _render_background(canvas, preset_key)
    cw, ch = cut.size
    ox = (canvas[0] - cw) // 2
    oy = (canvas[1] - ch) // 2

    # ظل تلامس ناعم تحت القطعة — يدّي إحساس واقعي على الخلفيات الفاتحة.
    try:
        shadow_alpha = Image.new('L', canvas, 0)
        shadow_alpha.paste(cut.split()[3], (ox, oy + int(ch * 0.05)))
        shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(20))
        shadow_alpha = shadow_alpha.point(lambda p: int(p * 0.45))
        black = Image.new('RGBA', canvas, (0, 0, 0, 255))
        transparent = Image.new('RGBA', canvas, (0, 0, 0, 0))
        shadow_layer = Image.composite(black, transparent, shadow_alpha)
        bg = Image.alpha_composite(bg, shadow_layer)
    except Exception as exc:  # noqa: BLE001 — الظل تجميلي؛ نكمّل من غيره لو فشل
        logger.debug('[BG REMOVAL] shadow skipped: %s', exc)

    bg.paste(cut, (ox, oy), cut)

    out = BytesIO()
    bg.convert('RGB').save(out, format='PNG')
    return out.getvalue()
