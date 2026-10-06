"""🖼️ توحيد صور الرفع — أي صورة من الآيفون أو الأندرويد تشتغل وتتعرض.

المشكلة:
  • الآيفون بيصوّر بصيغة **HEIC/HEIF** — المتصفح مبيعرضهاش أصلاً في <img>،
    ومكتبة Pillow الافتراضية مبتقراهاش، فاستوديو الصور بيفشل والصورة متبانش.
  • صور الموبايل بتخزّن اتجاه الدوران في **EXIF** — لو حوّلناها بدون احترام
    الـ EXIF بتطلع مقلوبة/على جنب.
  • صور الموبايل ضخمة (4000px+) — بتاكل مساحة وبطء تحميل.

الحل: `normalize_image_to_jpeg` بيفتح أي صيغة (HEIC/WEBP/PNG/JPEG...)، يظبط
الدوران من EXIF، يصغّر الأبعاد الضخمة، ويحوّل لـ JPEG — صيغة كل المتصفحات
بتعرضها وكل أدوات المعالجة بتفهمها. بيتنادى وقت حفظ صورة المنتج فبيغطّي
كل مسارات الرفع من نقطة واحدة.

pillow-heif بيتسجّل مع Pillow عند الإقلاع (inventory/apps.py) فأي
`Image.open` في المشروع كله بيقرا HEIC تلقائياً.
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

from django.core.files.base import ContentFile

logger = logging.getLogger('mouss_tec_core')

# أقصى بُعد للصورة المحفوظة — صور الموبايل بتوصل 4000px+ بلا داعي.
_MAX_DIM = 2000
_JPEG_QUALITY = 88


def register_heif_opener() -> bool:
    """يسجّل قارئ HEIC/HEIF مع Pillow (يتنادى مرة عند الإقلاع). آمن لو المكتبة
    مش متثبّتة بعد — بيرجّع False بدل ما يكسر الإقلاع."""
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning('[IMAGE] pillow-heif not available (HEIC غير مدعوم): %s', exc)
        return False


def normalize_image_to_jpeg(django_file, *, max_dim: int = _MAX_DIM) -> Optional[ContentFile]:
    """يحوّل ملف صورة مرفوع لـ JPEG منضبط الاتجاه ومعقول الحجم.

    يرجّع `ContentFile` جاهز للحفظ (باسم .jpg)، أو `None` لو الملف مش صورة
    صالحة أو حصل أي خطأ — والـ caller ساعتها يسيب الملف الأصلي زي ما هو.
    """
    if not django_file:
        return None
    try:
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001 — Pillow مفروض متثبّت، بس ما نكسرش الحفظ
        return None

    try:
        # نقرا البايتات من غير ما نستهلك ملف الرفع نهائياً
        try:
            django_file.seek(0)
        except Exception:  # noqa: BLE001
            pass
        raw = django_file.read()
        try:
            django_file.seek(0)
        except Exception:  # noqa: BLE001
            pass
        if not raw:
            return None

        img = Image.open(BytesIO(raw))
        # 🔄 نظبّط الدوران من EXIF قبل أي تحويل (صور الموبايل بتطلع على جنب من غيره)
        img = ImageOps.exif_transpose(img)
        # الشفافية (PNG/HEIC) بتبقى أسود في JPEG — نركّبها على أبيض
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            bg = Image.new('RGBA', img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img).convert('RGB')
        else:
            img = img.convert('RGB')

        if max_dim and (img.width > max_dim or img.height > max_dim):
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)

        out = BytesIO()
        img.save(out, format='JPEG', quality=_JPEG_QUALITY, optimize=True)
        # اسم جديد بامتداد .jpg (المتصفح والطباعة بيعتمدوا على الامتداد أحياناً)
        base = getattr(django_file, 'name', 'image') or 'image'
        base = base.rsplit('/', 1)[-1].rsplit('.', 1)[0][:40] or 'image'
        return ContentFile(out.getvalue(), name=f'{base}.jpg')
    except Exception as exc:  # noqa: BLE001
        logger.warning('[IMAGE] normalize failed (%s) — نسيب الملف الأصلي', exc)
        return None
