"""📥 رفع صور منتجات الكتالوج مرة واحدة — من ZIP أو فولدر، بأسماء الملفات اللي في docs/catalog_image_prompts.md.

    python manage.py import_catalog_images صور.zip --dry-run
    python manage.py import_catalog_images صور.zip

* كل ملف اسمه بيبدأ بالكود (p001-… / a017-…) ⇒ بيتحط على كل منتجات النوع ده في كل المتاجر.
* الامتداد مش مهم (png/jpg/webp)، والصورة بتتصغّر لو أكبر من ١٦٠٠ بكسل وتتحفظ WEBP خفيفة.
* مابيلمسش منتج صاحب المنصة رفع له صورة بإيده من الأدمن (إلا بـ --overwrite).
"""
import io
import os
import re
import uuid
import zipfile

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.webp')
MAX_DIM = 1600


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
        parser.add_argument('path', help='ملف ZIP أو فولدر فيه الصور')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--overwrite', action='store_true', help='حتى المنتجات اللي ليها صورة')

    def handle(self, *args, **opts):
        from clients.management.commands.generate_catalog_images import image_key, image_slots
        from clients.models import CatalogItem
        codes = {name.split('-', 1)[0]: key for name, key, *_ in image_slots()}   # p001 ⇒ المفتاح
        with schema_context('public'):
            by_key = {}
            for it in CatalogItem.objects.select_related('store'):
                by_key.setdefault(image_key(it), []).append(it)
            done = items = 0
            unknown, seen = [], set()
            for name, data in _iter_files(opts['path']):
                m = re.match(r'^([pa]\d{3})', name.lower())
                key = codes.get(m.group(1)) if m else None
                if not key:
                    unknown.append(name)
                    continue
                seen.add(m.group(1))
                targets = [i for i in by_key.get(key, []) if opts['overwrite'] or not i.image]
                self.stdout.write(f'{"·" if opts["dry_run"] else "✓"} {name} ⇒ {len(targets)} منتج')
                if opts['dry_run'] or not targets:
                    continue
                body, ext = _optimize(data)
                saved = default_storage.save(f'catalog/photos/{m.group(1)}-{uuid.uuid4().hex[:8]}{ext}',
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
            if missing:
                self.stdout.write(f'ℹ️ لسه مالهاش صورة ({len(missing)}): {", ".join(missing[:30])}')
