# ✍️ حقول التوقيع الإلكتروني على فاتورة البيع — additive وآمن.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0027_alter_usermfa_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleinvoice',
            name='digital_signature',
            field=models.TextField(blank=True, default='', editable=False, verbose_name='التوقيع الإلكتروني'),
        ),
        migrations.AddField(
            model_name='saleinvoice',
            name='signature_captured_at',
            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name='وقت التوقيع'),
        ),
    ]
