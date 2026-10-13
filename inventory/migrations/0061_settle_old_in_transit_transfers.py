# 🚚 التحويلات اللي كانت «في الطريق» بالمنطق القديم كانت دخلت فرع الوصول خلاص
#   (المخزون اتنقل من الفرعين). المنطق الجديد بيدخّلها فرع الوصول عند «تم الاستلام»
#   بس — فبنعلّم القديمة «تم الاستلام» (من غير ما نحرّك مخزون) عشان ماتتضافش مرتين.
from django.db import migrations


def settle(apps, schema_editor):
    StockTransfer = apps.get_model('inventory', 'StockTransfer')
    StockTransfer.objects.filter(status='in_transit').update(status='completed')


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0060_purchase_item_manual_extra"),
    ]

    operations = [
        migrations.RunPython(settle, migrations.RunPython.noop),
    ]
