"""📣 Feed كتالوج ميتا للمطابع: أعمدة ميتا، والمنتج من غير سعر/صورة مايدخلش."""
import csv
import io
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient, TestCase, override_settings

from clients.catalog_seed import seed_catalog
from clients.models import CatalogItem, CatalogStore
from clients.tests.test_printing_customer_flow import _TenantDomainMixin
from clients.views.meta_feed_views import FIELDS

PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4'
       b'\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
URL = '/feeds/meta/printing.csv'


@override_settings(ALLOWED_HOSTS=['*'])
class MetaPrintingFeedTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)

    def _rows(self):
        r = HttpClient().get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        return list(csv.DictReader(io.StringIO(r.content.decode('utf-8-sig'))))

    def test_only_priced_printing_items_with_image(self):
        printing = CatalogItem.objects.filter(store__sector='printing')
        with_img, no_img = printing[0], printing[1]
        with_img.image = SimpleUploadedFile('card.png', PNG, content_type='image/png')
        with_img.price_from = Decimal('250')
        with_img.save()
        CatalogItem.objects.filter(pk=no_img.pk).update(image='', price_from=Decimal('99'))
        auto = CatalogItem.objects.filter(store__sector='automotive').first()
        auto.image = SimpleUploadedFile('p.png', PNG, content_type='image/png')
        auto.price_from = Decimal('10')
        auto.save()

        cache.clear()
        rows = self._rows()
        ids = [r['id'] for r in rows]
        self.assertEqual(ids, [f'cat-{with_img.item_code}'])
        row = rows[0]
        self.assertEqual(tuple(row.keys()), FIELDS)
        self.assertEqual(row['price'], '250.00 EGP')
        self.assertEqual(row['availability'], 'in stock')
        self.assertEqual(row['title'], with_img.title)
        self.assertTrue(row['link'].startswith('http') and str(with_img.item_code) in row['link'])
        self.assertTrue(row['image_link'].startswith('http'))

    def test_empty_feed_still_valid_csv(self):
        CatalogItem.objects.update(price_from=None)
        cache.clear()
        self.assertEqual(self._rows(), [])
