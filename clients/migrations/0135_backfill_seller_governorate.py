"""🧭 البائعين اللي اتسجّلوا قبل بروفايل البائع: المحافظة بتتخمّن من «المدينة» اللي كتبوها."""
from django.db import migrations


def backfill(apps, schema_editor):
    from django.db import connection
    if getattr(connection, 'schema_name', 'public') != 'public':
        return
    from clients.services.price_board import guess_governorate
    MarketSeller = apps.get_model('clients', 'MarketSeller')
    for s in MarketSeller.objects.filter(governorate='').select_related('store'):
        gov = guess_governorate(s.store.city)
        if gov:
            MarketSeller.objects.filter(pk=s.pk).update(governorate=gov)


class Migration(migrations.Migration):
    dependencies = [('clients', '0134_seller_profile')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
