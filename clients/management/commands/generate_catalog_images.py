"""🖼️ صور منتجات الكتالوج بالذكاء الاصطناعي — صورة فوتوغرافية لكل نوع منتج.

* صورة واحدة لكل نوع (مثلاً «بوجيهات إيريديوم» ليها صورة واحدة لكل الماركات) ⇒ ~١٢٥ صورة مش ٧٦٥.
* بيستخدم نفس محرك صور المنصة (FLUX/Ideogram عبر printing_copilot)، ولو مش متاح ⇒ Gemini بمفتاح المنصة.
* مابيلمسش أي منتج صاحب المنصة رفع له صورة بنفسه (إلا بـ --overwrite)، وتقدر توقفه وتكمّله بعدين.

    python manage.py generate_catalog_images --dry-run        # يوريك العدد والتكلفة والبرومبتات
    python manage.py generate_catalog_images                  # يولّد ويحفظ
    python manage.py generate_catalog_images --sector printing --limit 10
"""
import base64
import logging
import re
import time
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context

logger = logging.getLogger('mouss_tec_core')

SIZE = '1024x768'
NEGATIVE = ('text, letters, words, logo, watermark, brand name, label text, low quality, blurry, '
            'distorted, deformed, duplicated objects, cartoon, illustration, 3d render, jpeg artifacts')

STYLE = {
    'printing': ('professional e-commerce product photography, photorealistic, studio lighting, '
                 'soft natural shadow, clean light warm-grey seamless background, sharp focus, high detail, '
                 'premium print shop catalog, 4k'),
    'automotive': ('professional automotive spare part product photography, photorealistic, studio lighting, '
                   'clean white seamless background, sharp focus, high detail, new genuine part, 4k'),
}


def image_key(item):
    """مفتاح «نوع المنتج»: المنتجات اللي ليها نفس النوع بتاخد نفس الصورة (الماركة مابتفرقش في الصورة)."""
    title_en = item.title_en or item.title
    if item.store.sector == 'automotive':
        title_en = re.sub(r'\s+-\s+.*$', '', title_en)                  # «Battery - BMW» ⇒ «Battery»
        title_en = re.sub(r'^\S+(\s/\s\S+)? tyres in original sizes$', 'Tyres', title_en)
    return f'{item.store.sector}:{title_en.strip().lower()}'


def prompt_for(item):
    title_en = re.sub(r'\s+-\s+.*$', '', item.title_en or item.title)
    if item.store.sector == 'automotive' and 'tyres' in title_en.lower():
        title_en = 'Car tyres'
    desc = re.sub(r'\s+', ' ', (item.description_en or ''))
    if item.store.sector == 'automotive':
        desc = desc.split(' — ')[0]        # من غير أسامي الموديلات — وإلا بيرسم عربية بدل القطعة
    desc = desc[:220]
    return (f'{title_en}. {desc} Show the product itself, attractive composition, '
            f'no text and no logos on the product. {STYLE[item.store.sector]}')


def _generate(prompt):
    """bytes أو None — FLUX/Ideogram الأول، وبعدين Gemini."""
    try:
        from erp_core.ai.printing_copilot import generate_design_image
        img = generate_design_image(prompt=prompt[:1800], size=SIZE, negative_prompt=NEGATIVE,
                                    category='product_photo', quality_tier='hd')
        if img and img.get('success'):
            if img.get('b64_json'):
                return base64.b64decode(img['b64_json'])
            url = img.get('url') or ''
            if url.startswith('http'):
                import requests
                r = requests.get(url, timeout=60)
                if r.status_code < 400 and r.content:
                    return r.content
        logger.info('[CATALOG IMG] FLUX unavailable: %s', (img or {}).get('error'))
    except Exception as exc:  # noqa: BLE001
        logger.info('[CATALOG IMG] FLUX error: %s', exc)
    return _gemini(prompt)


def _gemini(prompt):
    import requests
    key = str(getattr(settings, 'GEMINI_API_KEY', '') or '').strip()
    if not key:
        return None
    for model in ('gemini-2.5-flash-image', 'gemini-2.0-flash-preview-image-generation'):
        try:
            r = requests.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                params={'key': key}, timeout=90,
                json={'contents': [{'parts': [{'text': 'Generate a photo (landscape 4:3). ' + prompt}]}],
                      'generationConfig': {'responseModalities': ['TEXT', 'IMAGE']}})
            if r.status_code >= 400:
                continue
            for p in r.json()['candidates'][0]['content']['parts']:
                inline = p.get('inlineData') or p.get('inline_data')
                if inline and inline.get('data'):
                    return base64.b64decode(inline['data'])
        except Exception:  # noqa: BLE001
            continue
    return None


class Command(BaseCommand):
    help = 'توليد صور فوتوغرافية بالذكاء الاصطناعي لمنتجات كتالوج السوق'

    def add_arguments(self, parser):
        parser.add_argument('--sector', choices=['printing', 'automotive'])
        parser.add_argument('--limit', type=int, default=0, help='أقصى عدد صور تتولّد المرة دي')
        parser.add_argument('--dry-run', action='store_true', help='يعرض العدد والتكلفة والبرومبتات من غير توليد')
        parser.add_argument('--overwrite', action='store_true', help='يولّد حتى للمنتجات اللي ليها صورة')
        parser.add_argument('--sleep', type=float, default=1.5, help='ثواني بين كل صورة')

    def handle(self, *args, **opts):
        from clients.models import CatalogItem
        with schema_context('public'):
            qs = CatalogItem.objects.select_related('store').filter(is_active=True, store__is_active=True)
            if opts['sector']:
                qs = qs.filter(store__sector=opts['sector'])
            groups = {}
            for it in qs.order_by('store__sector', 'id'):
                groups.setdefault(image_key(it), []).append(it)
            # نوع كل منتجاته ليها صور (اترفعت أو اتولّدت قبل كده) ⇒ نعدّيه
            todo = [(k, items) for k, items in groups.items()
                    if opts['overwrite'] or any(not i.image for i in items)]
            if opts['limit']:
                todo = todo[:opts['limit']]
            cost = len(todo) * float(getattr(settings, 'AI_COST_USD_PER_IMAGE', 0.04))
            self.stdout.write(f'أنواع منتجات محتاجة صورة: {len(todo)} (من {len(groups)}) · '
                              f'منتجات: {sum(len(i) for _, i in todo)} · تكلفة تقريبية: ${cost:.2f}')
            if opts['dry_run']:
                for k, items in todo[:200]:
                    self.stdout.write(f'- {k} ({len(items)})\n  {prompt_for(items[0])[:180]}')
                return

            done = failed = 0
            for k, items in todo:
                data = _generate(prompt_for(items[0]))
                if not data:
                    failed += 1
                    self.stderr.write(f'✗ {k}')
                    continue
                name = default_storage.save(f'catalog/ai/{uuid.uuid4().hex}.png', ContentFile(data))
                for it in items:
                    if opts['overwrite'] or not it.image:
                        it.image.name = name                      # نفس الملف لكل منتجات النوع
                        it.save(update_fields=['image'])
                done += 1
                self.stdout.write(f'✓ {k} ⇒ {len(items)} منتج')
                time.sleep(opts['sleep'])
            self.stdout.write(f'خلص: {done} صورة · فشل: {failed}')
