from django.db import migrations, models


class Migration(migrations.Migration):
    """🎨 AI Image Studio — background replacement for product photos.

    Adds three fields to Product (mirrored onto HistoricalProduct so
    simple_history stays consistent):
      • image_original_backup — the pre-processing image, enabling a one-click
        revert after an AI background swap.
      • image_ai_bg_applied   — flag marking the current image as AI-processed.
      • image_ai_bg_preset    — key of the applied background preset.
    """

    dependencies = [
        ('inventory', '0034_wix_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='image_original_backup',
            field=models.ImageField(blank=True, null=True, upload_to='products/originals/', verbose_name='النسخة الأصلية للصورة (قبل معالجة AI)'),
        ),
        migrations.AddField(
            model_name='product',
            name='image_ai_bg_applied',
            field=models.BooleanField(default=False, verbose_name='تمت معالجة الخلفية بالذكاء الاصطناعي'),
        ),
        migrations.AddField(
            model_name='product',
            name='image_ai_bg_preset',
            field=models.CharField(blank=True, default='', max_length=40, verbose_name='خلفية AI المطبّقة'),
        ),
        migrations.AddField(
            model_name='historicalproduct',
            name='image_original_backup',
            field=models.TextField(blank=True, max_length=100, null=True, verbose_name='النسخة الأصلية للصورة (قبل معالجة AI)'),
        ),
        migrations.AddField(
            model_name='historicalproduct',
            name='image_ai_bg_applied',
            field=models.BooleanField(default=False, verbose_name='تمت معالجة الخلفية بالذكاء الاصطناعي'),
        ),
        migrations.AddField(
            model_name='historicalproduct',
            name='image_ai_bg_preset',
            field=models.CharField(blank=True, default='', max_length=40, verbose_name='خلفية AI المطبّقة'),
        ),
    ]
