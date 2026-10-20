"""📦 inventory/views_lightning اتقسم من ملف واحد (~٧٠٠٠ سطر) لموديول لكل مجال.

اللي لازم يفضل صح بعد التقسيم:
* كل موديول بيتعمله import من غير أخطاء، وكل الأسماء القديمة (`views_lightning.X`
  اللي urls.py وباقي الكود بيستخدمها) لسه موجودة.
* أي `from .x import` جوه دالة بيشاور على موديول موجود — استيراد نسبي جوه دالة
  اتنقلت لباكدج بيتحسب من الباكدج الجديدة (كده `/system/products/` وقعت 500 أثناء
  التقسيم: `inventory.views_lightning.services` مش موجود).
"""
import ast
import importlib
import importlib.util
from pathlib import Path

from django.test import SimpleTestCase

import inventory.views_lightning as vl

PKG_DIR = Path(vl.__file__).parent


class ViewsLightningPackageTests(SimpleTestCase):

    def modules(self):
        return sorted(p.stem for p in PKG_DIR.glob('*.py') if p.stem != '__init__')

    def test_every_module_imports(self):
        for name in self.modules():
            importlib.import_module(f'inventory.views_lightning.{name}')

    def test_every_relative_import_resolves(self):
        for path in PKG_DIR.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom) and node.level:
                    target = importlib.util.resolve_name('.' * node.level + (node.module or ''),
                                                         'inventory.views_lightning')
                    self.assertIsNotNone(importlib.util.find_spec(target),
                                         f'{path.name}:{node.lineno} → {target}')

    def test_url_views_still_exported_from_the_package(self):
        from inventory import urls
        names = {line.split('views_lightning.')[1].split(',')[0].split(')')[0]
                 for line in Path(urls.__file__).read_text(encoding='utf-8').splitlines()
                 if 'views_lightning.' in line}
        self.assertTrue(names)
        missing = sorted(n for n in names if not hasattr(vl, n))
        self.assertEqual(missing, [])

    def test_no_module_grows_back_into_a_giant(self):
        sizes = {p.name: len(p.read_text(encoding='utf-8').splitlines()) for p in PKG_DIR.glob('*.py')}
        self.assertLess(max(sizes.values()), 1200, sizes)
