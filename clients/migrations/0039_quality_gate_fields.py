"""🔍 Quality Gate fields for AIPromptLearningLog.

Adds Vision-based verification metadata so we can:
  • Track which generations failed quality checks
  • Build a learning corpus of (brief → bad output → correction) tuples
  • Filter the admin Data Flywheel by verdict and category
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0038_diagnosticsaddon_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='aipromptlearninglog',
            name='quality_score',
            field=models.IntegerField(
                blank=True, db_index=True, null=True,
                verbose_name='درجة الجودة (1-10) من Vision LLM',
            ),
        ),
        migrations.AddField(
            model_name='aipromptlearninglog',
            name='quality_verdict',
            field=models.CharField(
                blank=True, db_index=True, max_length=20,
                help_text='excellent | acceptable | needs_regen | critical_fail',
                verbose_name='حكم الجودة',
            ),
        ),
        migrations.AddField(
            model_name='aipromptlearninglog',
            name='quality_issues',
            field=models.JSONField(
                blank=True, default=list,
                verbose_name='المشاكل المكتشفة',
            ),
        ),
        migrations.AddField(
            model_name='aipromptlearninglog',
            name='auto_regenerated',
            field=models.BooleanField(
                db_index=True, default=False,
                verbose_name='هل اتعاد توليده تلقائياً بسبب فشل Quality Gate؟',
            ),
        ),
        migrations.AddField(
            model_name='aipromptlearninglog',
            name='presentation_category',
            field=models.CharField(
                blank=True, db_index=True, max_length=20,
                help_text='apparel | document | footwear | furniture | ...',
                verbose_name='فئة العرض',
            ),
        ),
        migrations.AddField(
            model_name='aipromptlearninglog',
            name='detected_subtype',
            field=models.CharField(
                blank=True, db_index=True, max_length=20,
                help_text='slipper | sneaker | table | laptop | ...',
                verbose_name='الـ subtype داخل الفئة',
            ),
        ),
        migrations.AddIndex(
            model_name='aipromptlearninglog',
            index=models.Index(
                fields=['presentation_category', 'detected_subtype'],
                name='clients_aip_pres_subtype_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='aipromptlearninglog',
            index=models.Index(
                fields=['quality_verdict', '-created_at'],
                name='clients_aip_qverdict_idx',
            ),
        ),
    ]
