from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('social_ads', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='socialpost',
            name='source',
            field=models.CharField(
                choices=[
                    ('autopilot', 'طيار آلي'),
                    ('manual', 'يدوي'),
                    ('imported', 'مستورد من الصفحة'),
                ],
                default='autopilot', max_length=12,
            ),
        ),
    ]
