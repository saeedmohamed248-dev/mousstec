# 🚀 تهيئة أول مرة: إنشاء الفرع العام (public) + أول فرع + مدير.
# idempotent — تشغيله تاني بأمان.
#
# مثال:
#   python manage.py bootstrap_tenant --sub demo --password "MyPass123"
#   (مع Docker: docker compose exec web python manage.py bootstrap_tenant ...)
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, schema_context

from clients.models import Client, Domain


class Command(BaseCommand):
    help = "تهيئة أول مرة: الفرع العام + أول فرع + حساب مدير (idempotent)"

    def add_arguments(self, parser):
        parser.add_argument('--base-domain', default=getattr(settings, 'BASE_DOMAIN', 'mousstec.com'))
        parser.add_argument('--sub', default='demo', help="اسم سَبدومين الفرع الأول (demo → demo.<domain>)")
        parser.add_argument('--name', default='الفرع الرئيسي')
        parser.add_argument('--owner', default='Admin')
        parser.add_argument('--phone', default='01000000000')
        parser.add_argument('--username', default='admin')
        parser.add_argument('--email', default='')
        parser.add_argument('--password', required=True)

    def handle(self, *args, **o):
        base = o['base_domain'].strip().lower().rstrip('.')
        sub = o['sub'].strip().lower()
        schema = sub.replace('-', '_')
        pub_name = get_public_schema_name()

        # 1) الفرع العام (public) — مصدر صفحة الهبوط والدومين الأساسي
        public = Client.objects.filter(schema_name=pub_name).first()
        if not public:
            public = Client(schema_name=pub_name, name='Mouss Tec Platform',
                            owner_name=o['owner'], phone=o['phone'])
            public.auto_create_schema = False   # سكيمة public موجودة سلفاً
            public.save()
            self.stdout.write(self.style.SUCCESS("✅ اتعمل الفرع العام (public)"))
        else:
            self.stdout.write("ℹ️ الفرع العام (public) موجود بالفعل")

        # الدومين الأساسي → الفرع العام
        _, created = Domain.objects.get_or_create(
            domain=base, defaults={'tenant': public, 'is_primary': True})
        self.stdout.write(self.style.SUCCESS(f"✅ الدومين الأساسي: {base}")
                          if created else f"ℹ️ الدومين {base} موجود")

        # 2) أول فرع حقيقي (يعمل سكيمته ويطبّق مهاجراته تلقائياً)
        branch = Client.objects.filter(schema_name=schema).first()
        if not branch:
            self.stdout.write(f"🔄 إنشاء الفرع '{schema}' وتطبيق مهاجراته (ممكن ياخد دقيقة)...")
            branch = Client(schema_name=schema, name=o['name'],
                            owner_name=o['owner'], phone=o['phone'],
                            industry='automotive', status='trial')
            branch.save()
            self.stdout.write(self.style.SUCCESS(f"✅ اتعمل الفرع: {schema}"))
        else:
            self.stdout.write(f"ℹ️ الفرع {schema} موجود بالفعل")

        # دومين الفرع
        branch_domain = f"{sub}.{base}"
        _, created = Domain.objects.get_or_create(
            domain=branch_domain, defaults={'tenant': branch, 'is_primary': True})
        self.stdout.write(self.style.SUCCESS(f"✅ دومين الفرع: {branch_domain}")
                          if created else f"ℹ️ الدومين {branch_domain} موجود")

        # 3) حساب المدير داخل سكيمة الفرع
        email = o['email'] or f"{o['username']}@{base}"
        with schema_context(schema):
            u = User.objects.filter(username=o['username']).first()
            if u:
                u.set_password(o['password'])
                u.is_staff = u.is_superuser = True
                u.save()
                self.stdout.write("ℹ️ المدير موجود — تم تحديث كلمة السر")
            else:
                User.objects.create_superuser(o['username'], email, o['password'])
                self.stdout.write(self.style.SUCCESS(f"✅ اتعمل المدير: {o['username']}"))

        self.stdout.write(self.style.SUCCESS(
            f"\n🎉 خلص! افتح: https://{branch_domain}/  ودخول بـ ({o['username']})"))
        self.stdout.write(f"   والصفحة الرئيسية: https://{base}/")
