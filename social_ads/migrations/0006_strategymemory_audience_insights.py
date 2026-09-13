from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("social_ads", "0005_socialpost_ab_testing"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategymemory",
            name="audience_insights",
            field=models.JSONField(
                blank=True, default=dict, verbose_name="رؤى الجمهور من التعليقات"
            ),
        ),
    ]
