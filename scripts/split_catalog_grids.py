"""✂️ تقسيم صور Gemini المجمّعة (كل صورة ٢×٢ = ٤ منتجات) لصور منفصلة بأسماء الكتالوج.

    python scripts/split_catalog_grids.py ~/Downloads/part1 --start G001
    python scripts/split_catalog_grids.py ~/Downloads/part1 --start G001 --dry-run

* الخريطة (أنهي كود في أنهي ربع) من docs/catalog_image_prompts_per_store_missing.txt.
* لو اسم الملف فيه G012 ⇒ بياخده. غير كده الصور بتترتّب بالاسم وبتبدأ من --start.
* الناتج WEBP في clients/catalog_photos (الموجود مابيتكتبش عليه إلا بـ --overwrite)،
  وبعدها: python manage.py import_catalog_images --bundled
"""
import argparse
import os
import re
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS = os.path.join(ROOT, 'docs', 'catalog_image_prompts_per_store_missing.txt')
OUT = os.path.join(ROOT, 'clients', 'catalog_photos')
POS = ('top-left', 'top-right', 'bottom-left', 'bottom-right')
EXT = ('.png', '.jpg', '.jpeg', '.webp')


def load_map(path=PROMPTS):
    """{رقم الجريد: {الربع: الكود}} من ملف البرومبتات."""
    grids, cur = {}, None
    with open(path, encoding='utf-8') as fh:
        lines = fh.readlines()
    for line in lines:
        m = re.match(r'GRID G(\d{3}):', line)
        if m:
            cur = grids.setdefault(int(m.group(1)), {})
            continue
        m = re.match(r'\s+(top-left|top-right|bottom-left|bottom-right) \[(p\d{3}(?:-\d+)?)\]', line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
    return grids


def trim_gap(img):
    """بيشيل الفاصل الأبيض حوالين الربع (لو موجود) عشان الصورة ماتطلعش ببرواز."""
    gray = img.convert('L')
    inv = gray.point(lambda v: 255 if v < 235 else 0)
    box = inv.getbbox()
    if not box:
        return img
    w, h = img.size
    # 🛡️ مانقصّش أكتر من ٦٪ من أي ناحية — الخلفية الفاتحة نفسها مش فاصل
    l, t, r, b = box
    lim_w, lim_h = int(w * .06), int(h * .06)
    return img.crop((min(l, lim_w), min(t, lim_h), max(r, w - lim_w), max(b, h - lim_h)))


def split(img):
    w, h = img.size
    hw, hh = w // 2, h // 2
    boxes = ((0, 0, hw, hh), (hw, 0, w, hh), (0, hh, hw, h), (hw, hh, w, h))
    return {p: trim_gap(img.crop(b)) for p, b in zip(POS, boxes)}


def main(argv=None):
    ap = argparse.ArgumentParser(description='تقسيم صور Gemini المجمّعة ٢×٢')
    ap.add_argument('folder')
    ap.add_argument('--start', default='G001', help='رقم أول جريد في الفولدر (مثلاً G017)')
    ap.add_argument('--out', default=OUT)
    ap.add_argument('--overwrite', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)

    grids = load_map()
    files = sorted(f for f in os.listdir(a.folder) if f.lower().endswith(EXT))
    if not files:
        sys.exit('مفيش صور في الفولدر')
    nxt = int(re.sub(r'\D', '', a.start) or 1)
    done = 0
    for f in files:
        m = re.search(r'G(\d{3})', f, re.IGNORECASE)
        g = int(m.group(1)) if m else nxt
        nxt = g + 1
        cells = grids.get(g)
        if not cells:
            print(f'⚠️ {f}: G{g:03d} مش في ملف البرومبتات — اتعدّى')
            continue
        img = Image.open(os.path.join(a.folder, f)).convert('RGB')
        for pos, part in split(img).items():
            code = cells.get(pos)
            if not code:
                continue
            dest = os.path.join(a.out, f'{code}.webp')
            if os.path.exists(dest) and not a.overwrite:
                print(f'  ⏭️ {code} موجود')
                continue
            print(f'  {f} [{pos}] ⇒ {code}.webp')
            if not a.dry_run:
                part.save(dest, 'WEBP', quality=85, method=6)
            done += 1
    print(f'✅ {done} صورة' + (' (تجربة بس)' if a.dry_run else f' في {a.out}'))
    return done


if __name__ == '__main__':
    main()
