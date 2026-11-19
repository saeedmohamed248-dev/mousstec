"""
📸 إضافة قطعة سريعة — رفع أكتر من صورة + تنظيف الخلفية من نفس الصفحة
=====================================================================
🐛 [FIX]: على الموبايل كل اختيار صور كان بيمسح اللي قبله (اترفعت صورة واحدة بس)،
ولينك «نظّف الخلفية» كان بينقل لاستوديو الصور قبل ما الفورم يتحفظ فالبيانات تضيع.
"""
import tempfile
from io import BytesIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import Branch, Product, ProductImage

from .base import ERPTenantTestCase
from .factories import make_branch


def _jpeg(name, color):
    from PIL import Image
    buf = BytesIO()
    Image.new('RGB', (40, 40), color).save(buf, format='JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), ALLOWED_HOSTS=['*'])
class QuickProductImagesTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        # TransactionTestCase: الفرع بيفضل بين الاختبارات — منعيدش إنشاؤه (حد الفروع في الباقة)
        self.branch = Branch.objects.first() or make_branch()
        self.boss, _ = User.objects.get_or_create(username='qp_boss', defaults=dict(is_staff=True, is_superuser=True))
        self.c = Client()
        self.c.force_login(self.boss)

    def test_multiple_images_all_saved_first_is_primary(self):
        r = self.c.post(reverse('inventory:quick_product_create'), {
            'part_number': 'QP-IMG', 'name': 'فانوس', 'retail_price': '150',
            'images': [_jpeg('a.jpg', (200, 0, 0)), _jpeg('b.jpg', (0, 200, 0)), _jpeg('c.jpg', (0, 0, 200))],
        }, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        product = Product.objects.get(part_number='QP-IMG')
        imgs = list(ProductImage.objects.filter(product=product).order_by('sort_order'))
        self.assertEqual(len(imgs), 3)
        self.assertEqual([i.is_primary for i in imgs], [True, False, False])
        self.assertEqual(product.image.name, imgs[0].image.name)

    def test_page_accumulates_images_and_cleans_bg_inline(self):
        r = self.c.get(reverse('inventory:quick_product'), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        # الـ input مالوش name — الصور بتتجمّع في JS وتتضاف للـ FormData
        self.assertIn('id="img-picker"', html)
        self.assertNotIn('name="images"', html)
        self.assertIn("fd.append('images'", html)
        # تنظيف الخلفية اختيار في نفس الصفحة مش لينك بينقل لصفحة تانية
        self.assertIn('id="clean-bg"', html)
        self.assertNotIn('بعد الحفظ من استوديو الصور', html)  # اللينك القديم (الاستوديو لسه في القايمة الجانبية)
