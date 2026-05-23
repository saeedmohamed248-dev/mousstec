"""
أداة إصلاح كلمة مرور أدمن المستأجر.
الاستخدام:
    python manage.py fix_tenant_password <schema_name> <email> <new_password>

مثال:
    python manage.py fix_tenant_password flez sa3eeedmohamed@icloud.com MyNewPass123
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django_tenants.utils import schema_context


class Command(BaseCommand):
    help = 'Reset admin password for a specific tenant'

    def add_arguments(self, parser):
        parser.add_argument('schema_name', type=str, help='اسم الـ schema الخاص بالمستأجر')
        parser.add_argument('email', type=str, help='البريد الإلكتروني (username) للأدمن')
        parser.add_argument('new_password', type=str, help='كلمة المرور الجديدة')

    def handle(self, *args, **options):
        schema = options['schema_name']
        email = options['email']
        new_pass = options['new_password']
        User = get_user_model()

        with schema_context(schema):
            try:
                user = User.objects.get(username=email)
                user.set_password(new_pass)
                user.is_staff = True
                user.is_superuser = True
                user.save()
                self.stdout.write(self.style.SUCCESS(
                    f'Password reset for "{email}" in schema "{schema}"'
                ))
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR(
                    f'User "{email}" not found in schema "{schema}"'
                ))
                # List existing users
                users = User.objects.all().values_list('username', flat=True)
                self.stdout.write(f'Available users: {list(users)}')
