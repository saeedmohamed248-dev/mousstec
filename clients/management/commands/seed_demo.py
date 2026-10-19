# 🌱 شركة تجريبية جاهزة ببيانات — للمطوّر الجديد ولاختبارات المتصفح (e2e/).
# idempotent — تشغيله تاني بيحدّث كلمات السر بس ومابيكرّرش البيانات.
#
# مثال (محلي، الدومين localhost):
#   python manage.py seed_demo --password Demo12345
#   → http://demo.localhost:8000/login/   (admin / cashier / tech)
#
# البيانات بتدخل بنفس طرق السيستم (رأس مال على الخزنة ورصيد افتتاحي للأصناف)
# عشان الدفاتر تفضل متطابقة (audit_books) من أول لحظة.
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django_tenants.utils import schema_context

DEMO_PRODUCTS = (
    # (رقم القطعة، الاسم، سعر البيع، التكلفة، الكمية)
    ('DEMO-OIL-5W30', 'زيت محرك 5W30 (٤ لتر)', '1250.00', '900.00', 40),
    ('DEMO-FLT-OIL', 'فلتر زيت', '180.00', '110.00', 60),
    ('DEMO-FLT-AIR', 'فلتر هواء', '320.00', '210.00', 25),
    ('DEMO-PAD-FR', 'تيل فرامل أمامي', '1450.00', '1000.00', 12),
    ('DEMO-PLUG', 'بوجيه إيريديوم', '420.00', '300.00', 48),
    ('DEMO-BATT-70', 'بطارية ٧٠ أمبير', '4200.00', '3500.00', 6),
)
DEMO_STAFF = (
    # (اسم المستخدم، الاسم، الدور)
    ('cashier', 'كاشير تجريبي', 'cashier'),
    ('tech', 'فني تجريبي', 'tech'),
    ('accountant', 'محاسب تجريبي', 'accountant'),
)


class Command(BaseCommand):
    help = "شركة تجريبية ببيانات (أصناف/عملاء/مورد/موظفين) للتطوير واختبارات المتصفح"

    def add_arguments(self, parser):
        parser.add_argument('--base-domain', default=getattr(settings, 'BASE_DOMAIN', 'localhost'))
        parser.add_argument('--sub', default='demo')
        parser.add_argument('--password', required=True, help='كلمة سر كل الحسابات التجريبية')

    def handle(self, *args, **o):
        sub = o['sub'].strip().lower()
        schema = sub.replace('-', '_')
        call_command('bootstrap_tenant', base_domain=o['base_domain'], sub=sub,
                     name='مركز موس تك التجريبي', username='admin',
                     password=o['password'], stdout=self.stdout, stderr=self.stderr)
        with schema_context(schema):
            self._staff(o['password'])
            if self._seeded():
                self.stdout.write("ℹ️ البيانات التجريبية موجودة بالفعل")
            else:
                with transaction.atomic():
                    self._data()
                self.stdout.write(self.style.SUCCESS("✅ اتضافت البيانات التجريبية"))
        self.stdout.write(self.style.SUCCESS(
            f"🎉 جاهز: http://{sub}.{o['base_domain']}:8000/login/ — "
            f"admin / {' / '.join(u for u, _, _ in DEMO_STAFF)}"))

    @staticmethod
    def _seeded():
        from inventory.models import Product
        return Product.objects.filter(part_number__startswith='DEMO-').exists()

    def _staff(self, password):
        from inventory.models import Branch, EmployeeProfile
        branch = Branch.objects.order_by('pk').first()
        for username, name, role in DEMO_STAFF:
            user = User.objects.filter(username=username).first()
            if user is None:
                user = User.objects.create_user(username, f'{username}@demo.test', password,
                                                first_name=name)
            else:
                user.set_password(password)
                user.save(update_fields=['password'])
            EmployeeProfile.objects.update_or_create(
                user=user, defaults={'role': role, 'branch': branch})

    @staticmethod
    def _data():
        from inventory.models import Branch, Customer, FinancialTransaction, Product, Treasury, Vendor
        from inventory.services.inventory_service import set_branch_stock

        branch = Branch.objects.order_by('pk').first()
        treasury = Treasury.objects.filter(branch=branch, is_active=True).order_by('pk').first()
        # رأس مال المالك — بيتقيّد على حساب رأس المال (زي شاشة الخزنة بالظبط)
        FinancialTransaction.objects.create(
            treasury=treasury, transaction_type='in', amount=Decimal('100000.00'),
            description='رأس مال (بيانات تجريبية)', equity_kind='capital')
        for part, name, price, cost, qty in DEMO_PRODUCTS:
            product = Product.objects.create(
                part_number=part, name=name, brand='Generic',
                retail_price=Decimal(price), purchase_price=Decimal(cost))
            set_branch_stock(product, branch, qty, unit_cost=Decimal(cost),
                             reference_type='DemoSeed', note='رصيد افتتاحي تجريبي', opening=True)
        Customer.objects.create(name='أحمد علي', phone='01001234567')
        Customer.objects.create(name='شركة النقل السريع', phone='01112345678')
        Vendor.objects.create(name='مورد قطع الغيار', phone='01223456789')
