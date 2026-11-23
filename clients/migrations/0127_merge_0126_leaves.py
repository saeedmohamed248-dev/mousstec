# 🐛 [FIX]: PRين (#440 و #443) عملوا مهاجرة 0126 كل واحد لوحده ⇒ «Conflicting migrations»
#    و migrate_schemas كان هيقف وقت النشر. دي مهاجرة دمج بس (مفيش تغيير في الجداول).
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0126_catalogitem_price_from'),
        ('clients', '0126_seed_seasonal_printing_products'),
    ]

    operations = []
