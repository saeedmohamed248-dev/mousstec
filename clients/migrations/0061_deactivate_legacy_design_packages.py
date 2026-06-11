from django.db import migrations


LEGACY_SLUGS = [
    'cust_2', 'cust_4', 'cust_8',
    'des_15', 'des_25', 'des_50', 'des_100',
    'starter', 'pro', 'business', 'studio', 'single',
]


def delete_legacy(apps, schema_editor):
    DesignPackage = apps.get_model('clients', 'DesignPackage')
    qs = DesignPackage.objects.filter(slug__in=LEGACY_SLUGS)
    for pkg in qs:
        if pkg.purchases.exists():
            pkg.is_active = False
            pkg.save(update_fields=['is_active'])
        else:
            pkg.delete()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0060_manualpaymentreceipt'),
    ]

    operations = [
        migrations.RunPython(delete_legacy, noop),
    ]
