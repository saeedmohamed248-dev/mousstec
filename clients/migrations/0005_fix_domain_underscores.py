from django.db import migrations


def fix_domain_underscores(apps, schema_editor):
    """Replace underscores with hyphens in all tenant domain names.
    DNS hostnames do not allow underscores — this is a one-time data fix."""
    Domain = apps.get_model('clients', 'Domain')
    to_fix = Domain.objects.filter(domain__contains='_')
    for domain_obj in to_fix:
        fixed = domain_obj.domain.replace('_', '-')
        Domain.objects.filter(pk=domain_obj.pk).update(domain=fixed)


def reverse_fix(apps, schema_editor):
    pass  # intentionally irreversible


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0004_alter_globalb2bmarketplace_options_and_more'),
    ]

    operations = [
        migrations.RunPython(fix_domain_underscores, reverse_fix),
    ]
