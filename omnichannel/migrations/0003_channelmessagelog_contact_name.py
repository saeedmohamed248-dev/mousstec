from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omnichannel', '0002_subscription_lifecycle'),
    ]

    operations = [
        migrations.AddField(
            model_name='channelmessagelog',
            name='contact_name',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
    ]
