from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("print_market", "0002_reviews_disputes_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="marketorder",
            name="refund_done_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="اترجّع للعميل في"),
        ),
    ]
