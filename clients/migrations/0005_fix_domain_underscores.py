from django.db import migrations
from django.conf import settings


def fix_domain_records(apps, schema_editor):
    """Fix two issues in Domain records:
    1. Replace underscores with hyphens (invalid DNS hostnames)
    2. Replace localhost domains with the real BASE_DOMAIN for production
    """
    Domain = apps.get_model('clients', 'Domain')
    base_domain = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')

    for domain_obj in Domain.objects.all():
        original = domain_obj.domain
        fixed = original

        # Fix underscores → hyphens
        fixed = fixed.replace('_', '-')

        # Fix localhost domains → production domain
        if '.localhost' in fixed:
            slug = fixed.split('.localhost')[0]
            fixed = f"{slug}.{base_domain}"

        if fixed != original:
            Domain.objects.filter(pk=domain_obj.pk).update(domain=fixed)


def reverse_fix(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0004_alter_globalb2bmarketplace_options_and_more'),
    ]

    operations = [
        migrations.RunPython(fix_domain_records, reverse_fix),
    ]
