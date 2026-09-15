# 🏷️ تصنيف القطع تلقائياً: يملأ part_category للمنتجات من اسم القطعة بنفس منطق
#    الكلمات المفتاحية اللي موقع FixIt بيستخدمه (util.js → CATEGORY_RULES)، عشان
#    القطع الموجودة قبل ما نضيف التصنيفات تتوزّع على فئاتها الصح على المتجر.
#
# الاستخدام (مع django-tenants):
#   python manage.py tenant_command classify_part_categories --schema=<schema> [--all] [--dry-run] [--sync]
#
#   --all      : يعيد تصنيف حتى المنتجات اللي ليها تصنيف بالفعل (الافتراضي: الفاضية بس)
#   --dry-run  : يعرض الأعداد من غير حفظ
#   --sync     : بعد التصنيف، يرفع كل المنتجات للموقع (زي fixit_sync_all)
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import Product

# 🗂️ نفس قواعد موقع FixIt بالظبط — الأخص أولاً (مثال "شجرة مياه" تروح تبريد قبل
#    عفشة). كل قاعدة: (مفتاح تصنيف موس تك، [كلمات مفتاحية]).
CATEGORY_RULES = [
    ('brakes', ['تيل', 'فرامل', 'دسك', 'ديسك', 'هوب', 'قماش', 'كاليبر']),
    ('cooling', ['كولر', 'ريداتير', 'رادياتير', 'راديتير', 'انتركولر', 'مروحه',
                 'قربه', 'قرب', 'كوع', 'ثرموست', 'خرطوم', 'تكييف', 'فريون',
                 'مكثف', 'كباس', 'مياه', 'دباب']),
    ('fuel', ['بنزين', 'رشاش', 'انجكتور', 'بخاخ', 'خزان وقود', 'مضخه وقود', 'هاي برشر']),
    ('suspension', ['مساعد', 'مقص', 'بيضه', 'تيش', 'ميزان', 'جلب', 'قاعده', 'قواعد',
                    'طنابير', 'طنبور', 'شداد', 'بارات', 'بليه', 'بلي', 'كرسي',
                    'رمان', 'عفشه', 'بطاح', 'كرداني', 'شجره', 'صره', 'طبه هوك']),
    ('electrical', ['دينامو', 'مارش', 'حساس', 'فيشه', 'فيش', 'بوجيه', 'كويل',
                    'بطاريه', 'كلاكس', 'بوق', 'ريلاي', 'فحمات', 'ماطور', 'علامه',
                    'اسطبات', 'لمبه', 'نور', 'مساحات', 'كنترول', 'موبينه']),
    ('engine', ['جوان', 'تيربو', 'فالف', 'بستم', 'شنبر', 'سلندر', 'كاتينه', 'صمام',
                'كامات', 'كرتير', 'صباب', 'بلف', 'دبريا', 'ديبريا', 'كلتش', 'فولان',
                'سايكلون', 'مجمع هواء', 'تقسيم', 'فلانشه', 'كردان']),
    ('filters', ['فلتر', 'اويل', 'زيت', 'سير', 'شمعات', 'صيانه']),
    ('body', ['غطاء', 'غطا', 'زجاج', 'مرايا', 'مرايه', 'شكمان', 'اكصدام', 'صدام',
              'صداد', 'رفرف', 'كبوت', 'باب', 'شمعه', 'مقبض', 'مسمار', 'مصفحه',
              'دواسه', 'طبلون', 'جنط', 'كاوتش', 'فانوس', 'مساحه']),
]


def _norm_ar(s):
    """توحيد الحروف عشان المطابقة تعدّي على اختلاف الإملاء (أ/إ/آ→ا، ة→ه، ى→ي)."""
    return (str(s or '')
            .replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
            .replace('ة', 'ه').replace('ى', 'ي'))


_NORM_RULES = [(cat, [_norm_ar(k) for k in kws]) for cat, kws in CATEGORY_RULES]


def infer_category(name):
    """يرجّع مفتاح تصنيف موس تك من اسم القطعة، أو '' لو مفيش تطابق."""
    n = _norm_ar(name)
    for cat, kws in _NORM_RULES:
        for kw in kws:
            if kw in n:
                return cat
    return ''


class Command(BaseCommand):
    help = 'تصنيف القطع تلقائياً (part_category) من اسم القطعة بنفس منطق موقع FixIt'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true',
                            help='يعيد تصنيف حتى المنتجات اللي ليها تصنيف (الافتراضي: الفاضية بس)')
        parser.add_argument('--dry-run', action='store_true',
                            help='يعرض الأعداد من غير حفظ')
        parser.add_argument('--sync', action='store_true',
                            help='بعد التصنيف، يرفع كل المنتجات للموقع')

    def handle(self, *args, **options):
        do_all = options.get('all')
        dry = options.get('dry_run')
        do_sync = options.get('sync')

        qs = Product.objects.all()
        if not do_all:
            qs = qs.filter(part_category='')

        total = qs.count()
        changed = 0
        matched_by_cat = {}
        unmatched = 0
        # نجيب الخيارات الصالحة عشان مانحطش قيمة مش معرّفة في الموديل
        valid = {c[0] for c in Product.PART_CATEGORY_CHOICES}

        self.stdout.write(f'🔎 فحص {total} منتج{"" if do_all else " (تصنيفهم فاضي)"}...')

        to_update = []
        for p in qs.iterator():
            cat = infer_category(p.name)
            if cat and cat in valid and cat != (p.part_category or ''):
                p.part_category = cat
                to_update.append(p)
                changed += 1
                matched_by_cat[cat] = matched_by_cat.get(cat, 0) + 1
            elif not cat:
                unmatched += 1

        if not dry and to_update:
            with transaction.atomic():
                # حفظ حقل واحد بس عشان السرعة ومنلمسش باقي الحقول
                Product.objects.bulk_update(to_update, ['part_category'], batch_size=200)

        # تقرير مقروء
        label = {c[0]: str(c[1]) for c in Product.PART_CATEGORY_CHOICES}
        self.stdout.write(self.style.SUCCESS(
            f'{"[تجريبي] " if dry else ""}✅ اتصنّف {changed} منتج، من غير تطابق {unmatched}'))
        for cat, n in sorted(matched_by_cat.items(), key=lambda x: -x[1]):
            self.stdout.write(f'   • {label.get(cat, cat)}: {n}')

        if do_sync and not dry:
            from inventory.services import fixit_sync
            if not fixit_sync.is_enabled():
                self.stdout.write(self.style.WARNING(
                    '⚠️ المزامنة مش مفعّلة لهذا الفرع — اتخطّينا خطوة الرفع. '
                    'شغّل fixit_sync_all يدوياً بعد التأكد من الإعداد.'))
            else:
                self.stdout.write('🔄 رفع المنتجات للموقع...')
                res = fixit_sync.push_all_products(stdout=self.stdout)
                self.stdout.write(self.style.SUCCESS(
                    f'✅ اترفع {res["items"] if isinstance(res, dict) else res} منتج للموقع'))
        elif do_sync and dry:
            self.stdout.write('ℹ️ --dry-run مفعّل، فمافيش رفع للموقع.')
