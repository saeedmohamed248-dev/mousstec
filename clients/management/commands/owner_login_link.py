# 👑 يولّد رابط دخول مباشر للسوبر أدمن (بدون باسورد) — لتفادي مشاكل الـ
# autofill/القفل. الرابط صالح 10 دقائق ويسجّل دخول مستخدم is_superuser فقط.
#
#   docker compose exec web python manage.py owner_login_link
#   docker compose exec web python manage.py owner_login_link --email you@example.com
import os
import time

from django.core import signing
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django_tenants.utils import get_public_schema_name, schema_context


class Command(BaseCommand):
    help = 'يولّد رابط دخول مباشر للسوبر أدمن (بدون باسورد، صالح 10 دقائق)'

    def add_arguments(self, parser):
        parser.add_argument('--email', default='sa3eeedmohamed@hotmail.com')

    def handle(self, *args, **o):
        with schema_context(get_public_schema_name()):
            u = User.objects.filter(email__iexact=o['email'], is_superuser=True).first()
            if not u:
                self.stderr.write(self.style.ERROR(
                    f"لا يوجد سوبر أدمن بالإيميل {o['email']}. اعمله الأول."))
                return
            token = signing.dumps(
                {'user_id': u.id, 'created': int(time.time())},
                salt='owner-auto-login',
            )
        base = os.getenv('BASE_DOMAIN', 'mousstec.com')
        url = f"https://{base}/account/owner-login/?token={token}"
        self.stdout.write(self.style.SUCCESS("\n👑 رابط دخول السوبر أدمن (صالح 10 دقائق) — افتحه في المتصفح:\n"))
        self.stdout.write(url + "\n")
