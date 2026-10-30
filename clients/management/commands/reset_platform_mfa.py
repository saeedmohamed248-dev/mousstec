"""يقفل التحقق بخطوتين لحساب منصة ضاع منه الموبايل وأكواد الاسترجاع.

الاستخدام (من السيرفر بس — مفيش زرار له في الموقع عن قصد):
    python manage.py reset_platform_mfa owner@mousstec.com

بعدها صاحب الحساب يدخل بكلمة السر ويفعّل التحقق من جديد من /account/mfa/.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, schema_context


class Command(BaseCommand):
    help = "يقفل التحقق بخطوتين لحساب على الدومين الرئيسي (استرجاع حساب المنصة)"

    def add_arguments(self, parser):
        parser.add_argument('username', help='اسم المستخدم أو الإيميل على الدومين الرئيسي')

    def handle(self, *args, username, **options):
        from clients.models import PlatformEvent, PlatformUserMFA
        with schema_context(get_public_schema_name()):
            User = get_user_model()
            user = (User.objects.filter(username=username).first()
                    or User.objects.filter(email__iexact=username).first())
            if user is None:
                raise CommandError(f"مفيش مستخدم «{username}» على الدومين الرئيسي.")
            n = PlatformUserMFA.objects.filter(user=user).update(
                is_enabled=False, backup_codes=[], enabled_at=None)
            PlatformEvent.objects.create(
                event_type='other', tenant_schema='public', user_name=user.username,
                description=f"🔐 إعادة ضبط التحقق بخطوتين لحساب المنصة «{user.username}» من السيرفر",
            )
        self.stdout.write(self.style.SUCCESS(
            f"✅ التحقق بخطوتين اتقفل لـ {user.username}" if n else f"ℹ️ {user.username} ماكانش مفعّل التحقق بخطوتين"))
