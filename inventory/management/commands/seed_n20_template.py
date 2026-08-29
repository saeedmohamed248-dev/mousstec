# 🌱 زرع قالب فك جاهز لمحرك BMW N20 (Reverse BOM)
# الاستخدام:
#   python manage.py seed_n20_template
#   python manage.py seed_n20_template --force   # يعيد بناء البنود لو القالب موجود
# مع django-tenants:
#   python manage.py tenant_command seed_n20_template --schema=<schema_name>
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import DisassemblyTemplate, TemplateItem

TEMPLATE_NAME = 'تفكيك محرك N20 القياسي'
ENGINE_CODE = 'N20'

# القطع القياسية اللي بيتفكّك لها محرك N20 + وزن نسبي تقريبي من قيمة المحرك.
# مجموع النِّسَب = 100. الوزن بيتحوّل لسعر تقديري من قيمة الأب وقت التحميل.
# (part_name, weight_percentage, sku_prefix)
N20_PARTS = [
    ('شورت بلوك (Short Block)',            Decimal('35'), 'SHORTBLOCK'),
    ('رأس المحرك كامل (Cylinder Head)',     Decimal('20'), 'HEAD'),
    ('التوربو (Turbocharger)',              Decimal('15'), 'TURBO'),
    ('الدينامو (Alternator)',               Decimal('5'),  'ALT'),
    ('كمبروسر التكييف (AC Compressor)',     Decimal('5'),  'ACCOMP'),
    ('عمود الكامات (Camshafts)',            Decimal('5'),  'CAM'),
    ('طقم التايمنج والسلسلة (Timing Kit)',  Decimal('4'),  'TIMING'),
    ('طرمبة الزيت (Oil Pump)',              Decimal('3'),  'OILPUMP'),
    ('مجمع السحب (Intake Manifold)',        Decimal('3'),  'INTAKE'),
    ('الحساسات والبواجي (Sensors/Coils)',   Decimal('3'),  'SENSORS'),
    ('طرمبة المياه (Water Pump)',           Decimal('2'),  'WATERPUMP'),
]


class Command(BaseCommand):
    help = 'زرع قالب فك جاهز لمحرك N20 (idempotent — أعد التشغيل بأمان)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='يعيد بناء بنود القالب لو كان موجوداً بالفعل',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        template, created = DisassemblyTemplate.objects.get_or_create(
            name=TEMPLATE_NAME,
            defaults={
                'engine_code': ENGINE_CODE,
                'default_scrap_revenue': Decimal('0.00'),
                'is_active': True,
                'notes': 'قالب مزروع تلقائياً — عدّل الأوزان حسب السوق.',
            },
        )

        if created:
            self.stdout.write(self.style.SUCCESS(f'✅ اتعمل القالب: {TEMPLATE_NAME}'))
        elif options['force']:
            template.items.all().delete()
            self.stdout.write(self.style.WARNING('♻️ القالب موجود — جاري إعادة بناء البنود (--force)'))
        else:
            self.stdout.write(self.style.WARNING(
                f'⚠️ القالب "{TEMPLATE_NAME}" موجود بالفعل ({template.items.count()} بند). '
                'استخدم --force لإعادة بنائه.'))
            return

        for order, (name, weight, prefix) in enumerate(N20_PARTS, start=1):
            TemplateItem.objects.create(
                template=template,
                part_name=name,
                default_estimated_sales_price=Decimal('0.00'),
                weight_percentage=weight,
                sku_prefix=prefix,
                sort_order=order,
            )

        total = sum(w for _, w, _ in N20_PARTS)
        self.stdout.write(self.style.SUCCESS(
            f'✅ اتزرع {len(N20_PARTS)} بند لقالب N20 (مجموع الأوزان {total}%).'))
