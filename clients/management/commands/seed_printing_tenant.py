"""
حقن البيانات الأساسية لمستأجر طباعة موجود (فرع + خزينة + خامات).
الاستخدام:
    python manage.py seed_printing_tenant <schema_name>
مثال:
    python manage.py seed_printing_tenant flez
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django_tenants.utils import schema_context


class Command(BaseCommand):
    help = 'Seed printing data for an existing tenant'

    def add_arguments(self, parser):
        parser.add_argument('schema_name', type=str)

    def handle(self, *args, **options):
        schema = options['schema_name']

        with schema_context(schema):
            with transaction.atomic():
                from printing.models import PrintBranch, PrintTreasury, PrintMaterial

                branch, created = PrintBranch.objects.get_or_create(
                    name="الفرع الرئيسي",
                    defaults={'address': "المقر الرئيسي", 'is_active': True}
                )
                if created:
                    self.stdout.write(self.style.SUCCESS(f'Created branch: الفرع الرئيسي'))

                treasury, created = PrintTreasury.objects.get_or_create(
                    name="الخزينة النقدية (الرئيسية)",
                    branch=branch,
                    defaults={'balance': 0.00, 'is_active': True}
                )
                if created:
                    self.stdout.write(self.style.SUCCESS(f'Created treasury: الخزينة النقدية'))

                materials = [
                    ('ورق A4 (80 جم)', 'paper', 'رزمة', 180, 5),
                    ('ورق A3 (130 جم لامع)', 'paper', 'رزمة', 450, 3),
                    ('ورق كوشيه A4 (200 جم)', 'paper', 'رزمة', 550, 3),
                    ('حبر أسود (Toner)', 'ink', 'قطعة', 350, 2),
                    ('حبر ألوان CMYK (مجموعة)', 'ink', 'طقم', 1200, 1),
                    ('فينيل لاصق أبيض (متر)', 'vinyl', 'متر', 25, 20),
                    ('بنر فليكس (280 جم)', 'banner', 'متر', 15, 30),
                    ('لفة تغليف حراري (A4)', 'other', 'لفة', 120, 3),
                ]

                for name, cat, unit, cost, min_stock in materials:
                    obj, created = PrintMaterial.objects.get_or_create(
                        name=name,
                        defaults={
                            'category': cat, 'unit': unit,
                            'quantity': 0, 'cost_per_unit': cost,
                            'min_stock_level': min_stock, 'branch': branch
                        }
                    )
                    if created:
                        self.stdout.write(f'  + {name}')

        self.stdout.write(self.style.SUCCESS(f'\nSeeding complete for schema "{schema}"'))
