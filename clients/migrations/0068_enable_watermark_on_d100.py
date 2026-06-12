"""Enable custom watermark feature on the d100 designer pack (599 EGP)."""
from django.db import migrations


def enable(apps, schema_editor):
    DesignPkg = apps.get_model('clients', 'DesignPackage')
    DesignPkg.objects.filter(slug='d100').update(allows_watermark=True)


def disable(apps, schema_editor):
    DesignPkg = apps.get_model('clients', 'DesignPackage')
    DesignPkg.objects.filter(slug='d100').update(allows_watermark=False)


class Migration(migrations.Migration):
    dependencies = [
        ('clients', '0067_add_tenant_topup_purchase_type'),
    ]
    operations = [migrations.RunPython(enable, disable)]
