from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0029_eta_invoice_submission'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleinvoice',
            name='signature_image',
            field=models.ImageField(blank=True, null=True, upload_to='invoice_signatures/%Y/%m/', verbose_name='توقيع العميل الرقمي'),
        ),
        migrations.AddField(
            model_name='saleinvoice',
            name='signed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='وقت التوقيع'),
        ),
    ]
