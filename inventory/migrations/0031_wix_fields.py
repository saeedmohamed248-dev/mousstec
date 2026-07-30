from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0030_saleinvoice_signature'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='wix_product_id',
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True, verbose_name='Wix Product ID'),
        ),
        migrations.AddField(
            model_name='saleinvoice',
            name='wix_order_id',
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True, verbose_name='Wix Order ID'),
        ),
    ]
