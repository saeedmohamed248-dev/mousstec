"""📝 يكتب docs/catalog_image_prompts.md (+ .csv): برومبت لكل نوع منتج + اسم الملف اللي import_catalog_images بيفهمه."""
import csv

from django.conf import settings
from django.core.management.base import BaseCommand

from clients.management.commands.generate_catalog_images import image_slots, slot_prompt

MASTER = """I will give you a list of product photos to create for an online print-shop / car-parts catalog.
For EACH item in the list:
1. Generate ONE separate image, landscape 4:3 (1024x768 or larger), photorealistic product photography.
2. Follow the item's prompt exactly. Never put any text, letters, brand names, watermarks or logos in the image.
3. Save the image with EXACTLY the file name I give (keep the code at the start, e.g. p001-...png).
When you finish the whole list, put all the images in ONE ZIP file named like the batch (e.g. batch-01.zip) and give me the download link.
Do not skip any item. If you cannot finish all items, tell me exactly which file names are missing."""

BATCH = 10


class Command(BaseCommand):
    help = 'كتابة ملف برومبتات صور منتجات الكتالوج'

    def handle(self, *args, **opts):
        slots = image_slots()
        pr = [s for s in slots if s[4] == 'printing']
        au = [s for s in slots if s[4] == 'automotive']
        out = [
            '# 🖼️ برومبتات صور منتجات الكتالوج', '',
            f'الملف ده فيه **برومبت لكل نوع منتج** في سوق Mouss Tec ({len(slots)} صورة: '
            f'{len(pr)} طباعة وتصميم + {len(au)} قطع غيار). الصورة الواحدة بتتحط على كل منتجات النوع ده '
            'في كل المتاجر (مثلاً صورة «بوجيهات إيريديوم» لكل الماركات).', '',
            'كل صورة ليها **اسم ملف ثابت** — هو اللي بيربطها بالمنتجات لما تترفع، فلازم يفضل زي ما هو '
            '(المهم أول ٤ حروف: `p001` … `a047`).', '',
            '## إزاي تستخدمه', '',
            '1. افتح مولّد الصور اللي تحبه (ChatGPT أو Gemini أو غيره).',
            '2. الصق **البرومبت الرئيسي** الأول، وبعده **دفعة واحدة** (١٠ صور) — المولّدات بتتعب لو بعتّ كله مرة واحدة.',
            '3. اطلب منه الـ ZIP في الآخر ونزّله. كرّر لباقي الدفعات (كل دفعة في محادثة جديدة أحسن).',
            '4. لو المولّد مابيعرفش يعمل ZIP: نزّل الصور واحدة واحدة **وسمّيها بنفس الاسم**.',
            '5. ابعت الـ ZIPs، وهتترفع كلها مرة واحدة بالأمر:',
            '   `python manage.py import_catalog_images الصور.zip`', '',
            '> مش لازم تعمل كل الصور مرة واحدة — اللي ييجي يترفع، والباقي يفضل بالأيقونة لحد ما يكمل. '
            'وأي منتج إنت رافع له صورة بإيدك من الأدمن مابيتلمسش.', '',
            '## البرومبت الرئيسي (الصقه الأول في كل محادثة)', '', '```', MASTER, '```', '',
        ]
        n = 0
        for title, items in (('طباعة وتصميم', pr), ('قطع غيار', au)):
            for b in range(0, len(items), BATCH):
                n += 1
                out += [f'## دفعة {n:02d} — {title}', '', '```', f'Batch {n:02d} (ZIP name: batch-{n:02d}.zip):', '']
                for name, _k, t, scene, sector in items[b:b + BATCH]:
                    out += [f'File: {name}', 'Prompt: ' + slot_prompt(t, scene, sector), '']
                out += ['```', '']
        base = settings.BASE_DIR / 'docs'
        (base / 'catalog_image_prompts.md').write_text('\n'.join(out), encoding='utf-8')
        with open(base / 'catalog_image_prompts.csv', 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['file_name', 'sector', 'product', 'prompt'])
            for name, _k, t, scene, sector in slots:
                w.writerow([name, sector, t, slot_prompt(t, scene, sector)])
        self.stdout.write(f'اتكتب: {len(slots)} برومبت في {n} دفعة ⇒ docs/catalog_image_prompts.md')
