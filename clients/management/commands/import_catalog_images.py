"""📥 رفع صور منتجات الكتالوج مرة واحدة — من ZIP أو فولدر، بأسماء الملفات اللي في docs/catalog_image_prompts.md.

    python manage.py import_catalog_images صور.zip --dry-run
    python manage.py import_catalog_images صور.zip
    python manage.py import_catalog_images --bundled      # الصور اللي جوه المشروع (clients/catalog_photos)

* كل ملف اسمه بيبدأ بالكود (p001-… / a017-…) ⇒ بيتحط على كل منتجات النوع ده في كل المتاجر.
* الامتداد مش مهم (png/jpg/webp)، والصورة بتتصغّر لو أكبر من ١٦٠٠ بكسل وتتحفظ WEBP خفيفة.
* مابيلمسش منتج صاحب المنصة رفع له صورة بإيده من الأدمن (إلا بـ --overwrite).
* --bundled بيتشغّل لوحده في خطوة النشر (deploy/entrypoint.migrate.sh): المنتجات اللي ليها صورة بتتعدّى،
  فالصور الجديدة بس هي اللي بتترفع.
* 🛍️ سوق حقيقي: لنفس الكود ممكن أكتر من نسخة (p109.webp، p109-2.webp، p109-3.webp…) ⇒ كل متجر بياخد نسخة
  ثابتة حسب الـ slug بتاعه، فنفس المنتج مايبانش بنفس الصورة في كل المتاجر. الصورة اللي اتحطت أوتوماتيك
  بتتبدّل للنسخة الصح، واللي صاحب المنصة رفعها بإيده ماتتلمسش.
"""
import io
import os
import re
import uuid
import zipfile
import zlib

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.webp')
MAX_DIM = 1600
# p109.webp ⇒ (p109، 1) · p109-2.webp ⇒ (p109، 2) · p082-laser-cut.png ⇒ (p082، 1)
NAME_RE = re.compile(r'^([pa]\d{3})(?:-(\d{1,2}))?(?=[-._]|$)')
# الصور اللي الأمر ده حطها: catalog/photos/p109-xxxx.webp (نسخة ١) أو catalog/photos/p109v2-xxxx.webp
AUTO_RE = re.compile(r'^catalog/photos/([pa]\d{3})(?:v(\d{1,2}))?-')


def assign_variants(store_slugs, code, n):
    """{slug: رقم النسخة ١..n} — ثابت مع كل نشر.

    🐛 [FIX]: الأول كل متجر كان بياخد نسخة بالـ hash ⇒ متجرين ممكن ياخدوا نفس الصورة حتى لو النسخ كفاية.
    دلوقتي المتاجر بتترتّب (ترتيب ثابت ومخلوط لكل كود) وبتاخد النسخ بالدور ⇒ لو النسخ ≥ عدد المتاجر،
    كل متجر ليه صورة لوحده ومفيش ولا صورة متكررة في شركة تانية.
    """
    order = sorted(set(store_slugs), key=lambda s: (zlib.crc32(f'{s}:{code}'.encode()), s))
    return {slug: i % n + 1 for i, slug in enumerate(order)} if n > 0 else {}


def auto_variant(image_name, code):
    """لو الصورة دي الأمر ده اللي حاطها للكود ده ⇒ رقم نسختها، وإلا None (صاحب المنصة رفعها بإيده)."""
    m = AUTO_RE.match(image_name or '')
    if not m or m.group(1) != code:
        return None
    return int(m.group(2) or 1)


def _iter_files(path):
    """[(اسم الملف، bytes)] من ZIP أو فولدر."""
    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for f in files:
                if f.lower().endswith(IMAGE_EXT):
                    with open(os.path.join(root, f), 'rb') as fh:
                        yield f, fh.read()
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                name = os.path.basename(info.filename)
                if not info.is_dir() and name.lower().endswith(IMAGE_EXT) and not name.startswith('._'):
                    yield name, z.read(info)
    else:
        raise CommandError(f'مش ZIP ولا فولدر: {path}')


def _optimize(data):
    """WEBP خفيفة بحد أقصى MAX_DIM — ولو Pillow فشل بنرجع الأصل."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img = img.convert('RGB')
        img.thumbnail((MAX_DIM, MAX_DIM))
        out = io.BytesIO()
        img.save(out, 'WEBP', quality=86, method=6)
        return out.getvalue(), '.webp'
    except Exception:  # noqa: BLE001
        return data, '.png'


class Command(BaseCommand):
    help = 'رفع صور منتجات الكتالوج من ZIP/فولدر بأسماء ملفات البرومبتات'

    def add_arguments(self, parser):
        parser.add_argument('path', nargs='?', help='ملف ZIP أو فولدر فيه الصور')
        parser.add_argument('--bundled', action='store_true', help='الصور اللي جوه المشروع (clients/catalog_photos)')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--overwrite', action='store_true', help='حتى المنتجات اللي ليها صورة')

    def handle(self, *args, **opts):
        from clients.management.commands.generate_catalog_images import image_key, image_slots
        from clients.models import CatalogItem
        if opts['bundled']:
            opts['path'] = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'catalog_photos')
        if not opts['path']:
            raise CommandError('حدّد ملف ZIP أو فولدر، أو استخدم --bundled')
        codes = {name.split('-', 1)[0]: key for name, key, *_ in image_slots()}   # p001 ⇒ المفتاح
        with schema_context('public'):
            by_key = {}
            for it in CatalogItem.objects.select_related('store'):
                by_key.setdefault(image_key(it), []).append(it)
            done = items = 0
            unknown, seen = [], set()
            files = {}   # الكود ⇒ {رقم النسخة: bytes}
            short = {}   # الكود ⇒ كام نسخة ناقصة عشان كل متجر ياخد صورة لوحده
            for name, data in _iter_files(opts['path']):
                m = NAME_RE.match(name.lower())
                key = codes.get(m.group(1)) if m else None
                if not key:
                    unknown.append(name)
                    continue
                files.setdefault(m.group(1), {})[int(m.group(2) or 1)] = data
            for code in sorted(files):
                seen.add(code)
                versions = files[code]
                order = sorted(versions)                 # النسخ بالترتيب ⇒ ١..n من غير فراغات
                plan = {}                                # رقم النسخة ⇒ المنتجات اللي هتاخدها
                items_of = by_key.get(codes[code], [])
                slot = assign_variants([i.store.slug for i in items_of], code, len(order))
                stores = len(slot)
                if stores > len(order):
                    short[code] = stores - len(order)
                for it in items_of:
                    want = slot[it.store.slug]
                    have = auto_variant(it.image.name, code) if it.image else None
                    # 🐛 [FIX]: الأول كانت كل المتاجر بتاخد نفس الصورة؛ دلوقتي الصورة الأوتوماتيك بتتبدّل
                    # لنسخة المتجر، واللي اترفعت بالإيد (have=None) ماتتلمسش إلا بـ --overwrite.
                    if opts['overwrite'] or not it.image or (have is not None and have != want):
                        plan.setdefault(want, []).append(it)
                n_targets = sum(len(v) for v in plan.values())
                self.stdout.write(f'{"·" if opts["dry_run"] else "✓"} {code} ({len(order)} نسخة) ⇒ {n_targets} منتج')
                if opts['dry_run']:
                    continue
                for idx, targets in sorted(plan.items()):
                    body, ext = _optimize(versions[order[idx - 1]])
                    tag = code if idx == 1 else f'{code}v{idx}'
                    saved = default_storage.save(f'catalog/photos/{tag}-{uuid.uuid4().hex[:8]}{ext}',
                                                 ContentFile(body))
                    for it in targets:
                        it.image.name = saved
                        it.save(update_fields=['image'])
                    done += 1
                    items += len(targets)
            missing = sorted(set(codes) - seen)
            self.stdout.write(f'صور: {done} · منتجات اتحدّثت: {items}')
            if unknown:
                self.stdout.write(f'⚠️ ملفات اسمها مش معروف ({len(unknown)}): {", ".join(unknown[:15])}')
            if short:
                top = sorted(short.items(), key=lambda kv: -kv[1])
                self.stdout.write(f'🛍️ صور متكررة بين المتاجر — محتاجين {sum(short.values())} نسخة كمان '
                                  f'عشان كل متجر ياخد صورة لوحده: '
                                  + ', '.join(f'{c}×{n}' for c, n in top[:30]))
            if missing:
                self.stdout.write(f'ℹ️ لسه مالهاش صورة ({len(missing)}): {", ".join(missing[:30])}')
