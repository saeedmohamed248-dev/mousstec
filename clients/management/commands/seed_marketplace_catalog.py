"""🌱 زرع/تحديث كتالوج السوق (متاجر Mouss Tec) — آمن تشغيله أكتر من مرة."""
from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context

from clients.catalog_seed import seed_catalog


class Command(BaseCommand):
    help = 'زرع متاجر ومنتجات كتالوج السوق (طباعة + قطع غيار)'

    def handle(self, *args, **options):
        from clients.models import CatalogItem, CatalogStore
        with schema_context('public'):
            stores, new_items = seed_catalog(CatalogStore, CatalogItem)
        self.stdout.write(f'المتاجر: {stores} · منتجات جديدة: {new_items}')
