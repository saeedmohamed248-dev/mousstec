from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clients", "0119_b2b_purchase_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicerequest",
            name="accepted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="وقت قبول العرض"),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="rating_reminded_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="اتفكّر يقيّم في"),
        ),
    ]
