"""
Product image upload — a part created through the quick-product form must
persist its uploaded image, and the create path must stay intact when no
image is supplied.
"""
import io
import json
import uuid

from django.test import RequestFactory
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.core.files.uploadedfile import SimpleUploadedFile

from inventory.models import Product
from inventory.views_lightning import quick_product_create
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee


def _png_bytes():
    """A minimal valid 1x1 PNG that Pillow's ImageField validation accepts."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (1, 1), (200, 40, 40)).save(buf, format='PNG')
    return buf.getvalue()


def _wire(user, tenant, data):
    rf = RequestFactory()
    req = rf.post('/system/quick-product/create/', data)
    SessionMiddleware(lambda r: None).process_request(req)
    req.session.save()
    AuthenticationMiddleware(lambda r: None).process_request(req)
    req.user = user
    req.tenant = tenant
    return req


class ProductImageUploadTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = make_branch()
        self.admin_user, _ = make_employee('img_admin', role='admin', branch=self.branch)

    def test_create_with_image_persists_file(self):
        sku = f'IMG-{uuid.uuid4().hex[:6]}'
        upload = SimpleUploadedFile('part.png', _png_bytes(), content_type='image/png')
        req = _wire(self.admin_user, self.tenant, {
            'part_number': sku, 'name': 'قطعة بصورة',
            'retail_price': '150.00', 'image': upload,
        })
        resp = quick_product_create(req)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data.get('ok'))

        product = Product.objects.get(pk=data['product_id'])
        self.assertTrue(product.image)
        self.assertIn('products/', product.image.name)
        product.image.delete(save=False)  # don't leave the file on disk

    def test_create_without_image_still_succeeds(self):
        sku = f'NOIMG-{uuid.uuid4().hex[:6]}'
        req = _wire(self.admin_user, self.tenant, {
            'part_number': sku, 'name': 'قطعة بدون صورة',
            'retail_price': '90.00',
        })
        resp = quick_product_create(req)
        self.assertEqual(resp.status_code, 200)
        product = Product.objects.get(part_number=sku)
        self.assertFalse(product.image)

    def test_oversized_image_rejected(self):
        sku = f'BIG-{uuid.uuid4().hex[:6]}'
        big = SimpleUploadedFile('big.png', b'x' * (5 * 1024 * 1024 + 1), content_type='image/png')
        req = _wire(self.admin_user, self.tenant, {
            'part_number': sku, 'name': 'قطعة كبيرة',
            'retail_price': '10.00', 'image': big,
        })
        resp = quick_product_create(req)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Product.objects.filter(part_number=sku).exists())
