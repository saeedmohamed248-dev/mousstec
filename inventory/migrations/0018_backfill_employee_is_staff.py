"""
Backfill: every existing employee should be marked is_staff=True so they can
access /secure-portal/. Going forward, CustomUserAdmin.save_model sets this
automatically for new users; this migration handles users created before
that change. Runs once per tenant schema.
"""
from django.db import migrations, connection


def set_employees_as_staff(apps, schema_editor):
    # Skip the public schema — its users are platform admins, not tenant employees.
    if getattr(connection, 'schema_name', 'public') == 'public':
        return

    User = apps.get_model('auth', 'User')
    EmployeeProfile = apps.get_model('inventory', 'EmployeeProfile')

    employee_user_ids = EmployeeProfile.objects.values_list('user_id', flat=True)
    User.objects.filter(id__in=employee_user_ids, is_staff=False).update(is_staff=True)


def noop_reverse(apps, schema_editor):
    # Reversing would lock real employees out — don't.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0017_b2b_listing_request_and_sale_return_link'),
    ]

    operations = [
        migrations.RunPython(set_employees_as_staff, noop_reverse),
    ]
