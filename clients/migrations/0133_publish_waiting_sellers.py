"""🏪 بائعين السوق بقوا بيظهروا على طول (من غير موافقة صاحب المنصة).

+ 🐛 slug المتجر كان بيتعمل بحروف عربي ومسار صفحة المتجر بيقبل إنجليزي بس (404) ⇒ بيتصلّح هنا.

المتاجر والمنتجات اللي اتسجّلت قبل كده وكانت مستنية موافقة ⇒ تظهر دلوقتي.
(ماكانش فيه زرار «إخفاء» لمتجر بائع قبل كده غير من الأدمن، فأي متجر بائع مخفي هنا = كان مستني موافقة.)
"""
from django.db import migrations


def publish(apps, schema_editor):
    from django.db import connection
    if getattr(connection, 'schema_name', 'public') != 'public':
        return
    CatalogStore = apps.get_model('clients', 'CatalogStore')
    CatalogItem = apps.get_model('clients', 'CatalogItem')
    CatalogStore.objects.filter(seller__isnull=False, is_active=False).update(is_active=True)
    CatalogItem.objects.filter(store__seller__isnull=False, pending_review=True).update(
        is_active=True, pending_review=False)
    import re
    import uuid
    for store in CatalogStore.objects.filter(seller__isnull=False):
        if re.fullmatch(r'[-a-zA-Z0-9_]+', store.slug or ''):
            continue
        slug = f'{store.sector}-{uuid.uuid4().hex[:5]}'
        while CatalogStore.objects.filter(slug=slug).exists():
            slug = f'{store.sector}-{uuid.uuid4().hex[:5]}'
        CatalogStore.objects.filter(pk=store.pk).update(slug=slug)


class Migration(migrations.Migration):
    dependencies = [('clients', '0132_catalog_item_admin_hidden')]
    operations = [migrations.RunPython(publish, migrations.RunPython.noop)]
