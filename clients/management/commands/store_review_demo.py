# 📱 حسابات تجربة لمراجعين Google Play و App Store (إجباري عند آبل).
#
# بيعمل شركتين جاهزين ببيانات حقيقية الشكل — واحدة ورشة وواحدة مطبعة — عشان المراجع
# يدخل الابلكيشن ويشوف كل الشاشات. idempotent: تشغيله تاني بيحدّث كلمة السر ويمدّ الفترة بس.
#
#   python manage.py store_review_demo --password "Review2026!"
#   ⇒ كود الشركة: appreview      (ورشة)   مستخدم: reviewer
#   ⇒ كود الشركة: appreviewprint (مطبعة)  مستخدم: reviewer
#
# (على السيرفر: docker compose exec web python manage.py store_review_demo --password ...)
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context

AUTO_SUB = 'appreview'
PRINT_SUB = 'appreviewprint'
REVIEWER = 'reviewer'
DEMO_DAYS = 365   # المراجعة ممكن تتكرر مع كل تحديث — الحساب مايخلصش


class Command(BaseCommand):
    help = "شركتين تجربة (ورشة + مطبعة) لمراجعين متاجر الموبايل"

    def add_arguments(self, parser):
        parser.add_argument('--base-domain', default=getattr(settings, 'BASE_DOMAIN', 'mousstec.com'))
        parser.add_argument('--password', required=True, help='كلمة سر حساب المراجع في الشركتين')

    def handle(self, *args, **o):
        base, pw = o['base_domain'], o['password']
        # 🚗 الورشة: نفس بيانات seed_demo (أصناف بأرصدة افتتاحية + عملاء + موظفين)
        call_command('seed_demo', base_domain=base, sub=AUTO_SUB, password=pw,
                     stdout=self.stdout, stderr=self.stderr)
        self._finish(AUTO_SUB, 'Mouss Tec — Demo Workshop', pw, 'automotive')

        # 🎨 المطبعة
        call_command('bootstrap_tenant', base_domain=base, sub=PRINT_SUB, name='Mouss Tec — Demo Print Shop',
                     username='admin', password=pw, industry='printing',
                     stdout=self.stdout, stderr=self.stderr)
        self._finish(PRINT_SUB, 'Mouss Tec — Demo Print Shop', pw, 'printing')
        with schema_context(PRINT_SUB):
            if self._print_seeded():
                self.stdout.write("ℹ️ بيانات المطبعة التجريبية موجودة بالفعل")
            else:
                with transaction.atomic():
                    self._print_data()
                self.stdout.write(self.style.SUCCESS("✅ اتضافت بيانات المطبعة التجريبية"))

        self.stdout.write(self.style.SUCCESS(
            "\n🎉 جاهز — اكتبهم في Review notes في المتجرين:\n"
            f"   ورشة:  كود الشركة {AUTO_SUB}   / المستخدم {REVIEWER}\n"
            f"   مطبعة: كود الشركة {PRINT_SUB} / المستخدم {REVIEWER}\n"
            "   (كلمة السر اللي كتبتها)"))

    def _finish(self, sub, name, password, industry):
        """الاسم والقطاع + مدّ الفترة سنة + من غير قفل تأكيد الإيميل + حساب reviewer مالك."""
        from clients.models import Client
        Client.objects.filter(schema_name=sub).update(
            name=name, industry=industry,
            business_type='print_and_design' if industry == 'printing' else 'service_center',
            trial_ends_at=timezone.localdate() + timedelta(days=DEMO_DAYS),
            email_verify_deadline=None, business_type_confirmed=True)
        with schema_context(sub):
            u = User.objects.filter(username=REVIEWER).first()
            if u is None:
                u = User.objects.create_superuser(REVIEWER, f'{REVIEWER}@{sub}.demo', password,
                                                  first_name='App Reviewer')
            else:
                u.set_password(password)
                u.is_staff = u.is_superuser = u.is_active = True
                u.save()
            from inventory.models import EmployeeProfile
            EmployeeProfile.objects.update_or_create(user=u, defaults={'role': 'admin'})

    @staticmethod
    def _print_seeded():
        from printing.models import PrintCustomer
        return PrintCustomer.objects.filter(name__startswith='DEMO ').exists()

    @staticmethod
    def _print_data():
        """طلبات في كل مرحلة إنتاج + بروفة مستنية العميل + خامة ناقصة + عرض سعر."""
        from django.core.files.base import ContentFile
        from printing.models import (
            MachineProfile, PriceQuotation, PrintBranch, PrintCustomer, PrintJob, PrintMaterial,
            PrintOrder, PrintProof, PrintTreasury, QuotationLine,
        )
        branch, _ = PrintBranch.objects.get_or_create(name='الفرع الرئيسي', defaults={'address': 'القاهرة'})
        PrintTreasury.objects.get_or_create(name='الخزينة الرئيسية', defaults={'kind': 'cash'})
        machine = MachineProfile.objects.create(name='Konica C3070', machine_type='digital', branch=branch)
        PrintMaterial.objects.create(name='ورق كوشيه 300 جم', category='paper', unit='فرخ',
                                     branch=branch, quantity=Decimal('3'), min_stock=Decimal('50'))
        PrintMaterial.objects.create(name='فينيل لامع', category='vinyl', unit='متر',
                                     branch=branch, quantity=Decimal('120'), min_stock=Decimal('20'))

        nada = PrintCustomer.objects.create(name='DEMO شركة الندى للدعاية', phone='01011112222',
                                            company='الندى')
        noor = PrintCustomer.objects.create(name='DEMO مطعم النور', phone='01033334444')
        due = timezone.now() + timedelta(days=2)

        def order(customer, status, jobs):
            o = PrintOrder.objects.create(customer=customer, status=status, branch=branch, date_due=due)
            made = []
            for desc, qty, price, stage, waived in jobs:
                made.append(PrintJob.objects.create(
                    order=o, description=desc, quantity=qty, unit_price=Decimal(price),
                    machine=machine, proof_waived=waived, stage=stage))
            return o, made

        order(nada, 'confirmed', [('1000 كارت بزنس', 1, '650', 'design', True)])
        _o, (box,) = order(noor, 'confirmed', [('علب برجر مطبوعة', 500, '4.5', 'design', False)])
        # بروفة مستنية موافقة العميل (صورة PNG صغيرة)
        png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00'
               b'\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d'
               b'\x00\x00\x00\x00IEND\xaeB`\x82')
        proof = PrintProof(job=box, notes='بروفة أولى')
        proof.file.save('demo-proof.png', ContentFile(png), save=False)
        proof.save()
        order(nada, 'in_progress', [('رول أب 85×200', 2, '850', 'printing', True),
                                    ('فلاير A5', 2000, '0.9', 'finishing', True)])
        order(noor, 'draft', [('منيو A3 مقوى', 30, '45', 'design', True)])

        q = PriceQuotation.objects.create(customer=nada, title='حملة افتتاح فرع جديد', status='sent')
        QuotationLine.objects.create(quotation=q, description='بانر 3×1 متر', quantity=Decimal('4'),
                                     unit_price=Decimal('600'), line_total=Decimal('2400'))
        QuotationLine.objects.create(quotation=q, description='ستيكر لوجو', quantity=Decimal('500'),
                                     unit_price=Decimal('2'), line_total=Decimal('1000'))
        q.recalc_totals()
