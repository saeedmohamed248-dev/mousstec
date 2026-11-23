"""🧱 هيكل الكود — عشان أي مبرمج يلاقي الحاجة في مكانها، ومافيش ملف يرجع «عملاق».

1. الحزم اللي اتقسمت (ملف ضخم ⇒ موديول لكل مجال) لازم:
   * كل موديول فيها يتعمله import لوحده من غير أخطاء استيراد دائري.
   * أي استيراد نسبي (`from .x import` / `from ..models import`) يشاور على موديول موجود —
     الاستيراد النسبي جوه ملف اتنقل لباكدج بيتحسب من الباكدج الجديدة (`.models` بقت
     `printing.admin.models` اللي مش موجودة) والغلطة دي بتظهر 500 وقت التشغيل بس.
   * أي `from <الحزمة> import X` في أي حتة في المشروع يلاقي X (الأسماء القديمة لسه متصدّرة).
2. مافيش ملف بايثون جديد يعدّي LIMIT سطر. الملفات الكبيرة القديمة متسجّلة بحجمها
   الحالي في LEGACY_GIANTS: مسموح تصغر (وساعتها نزّل الرقم) بس ماتكبرش —
   الكود الجديد يتحط في موديول جديد جنبها.
"""
import ast
import importlib
import importlib.util
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
LIMIT = 1200

SPLIT_PACKAGES = (
    'printing.models',
    'printing.admin',
    'clients.views.saas_admin_views',
    'clients.views.admin_views',
    'robot.views',
    'robot.services',
    'hr.views',
    'inventory.models',
    'inventory.admin',
    'inventory.views_lightning',
    'clients.models',
)

# الملفات اللي كانت أكبر من LIMIT قبل القاعدة دي — الرقم = الحد الأقصى المسموح.
# صغّرت ملف منهم؟ نزّل رقمه (أو شيله لو بقى تحت LIMIT).
LEGACY_GIANTS = {
    'clients/admin.py': 1430,
    'clients/models/marketplace_b2b.py': 1365,
    'clients/models/tenancy.py': 1655,
    'clients/views/auth_views.py': 1597,
    'clients/views/design_store_views/generate.py': 1398,
    'clients/views/marketplace_core_views.py': 1363,
    'clients/views/subscription_views.py': 1228,
    'erp_core/ai/design_engine/__init__.py': 2971,
    'erp_core/ai/design_views.py': 1271,
    'erp_core/settings.py': 1390,  # إعدادات بتكبر طبيعي مع كل خدمة (آخر زيادة: قالب واتساب التواصل)
    'inventory/admin/dashboard.py': 1338,
    'inventory/services/accounting_service.py': 1234,
    'inventory/views/reports.py': 1377,
    'robot/brain/loop.py': 1334,
}

_SKIP_DIRS = {'.git', 'node_modules', 'venv', '.venv', 'staticfiles', 'migrations', 'tests', '__pycache__'}


def _project_py_files(include_tests=False):
    skip = _SKIP_DIRS - ({'tests'} if include_tests else set())
    for path in ROOT.rglob('*.py'):
        rel = path.relative_to(ROOT)
        if skip.intersection(rel.parts[:-1]):
            continue
        if not include_tests and rel.name.startswith('test_'):
            continue
        yield rel


def _package_dir(dotted):
    return Path(importlib.import_module(dotted).__file__).parent


def _module_of(rel):
    parts = list(rel.with_suffix('').parts)
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


class SplitPackagesTests(SimpleTestCase):

    def test_every_submodule_imports(self):
        for pkg in SPLIT_PACKAGES:
            for path in sorted(_package_dir(pkg).glob('*.py')):
                if path.stem != '__init__':
                    with self.subTest(module=f'{pkg}.{path.stem}'):
                        importlib.import_module(f'{pkg}.{path.stem}')

    def test_every_relative_import_resolves(self):
        for pkg in SPLIT_PACKAGES:
            for path in _package_dir(pkg).glob('*.py'):
                # الموديول جوه الحزمة (أو __init__ بتاعها): النقطة الواحدة = الحزمة نفسها
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    if isinstance(node, ast.ImportFrom) and node.level:
                        target = importlib.util.resolve_name('.' * node.level + (node.module or ''), pkg)
                        with self.subTest(file=f'{pkg}/{path.name}:{node.lineno}'):
                            self.assertIsNotNone(importlib.util.find_spec(target), target)

    def test_names_imported_from_split_packages_still_exist(self):
        """أي `from printing.admin import X` (أو نسبي) في أي ملف في المشروع لازم يلاقي X."""
        packages = {pkg: importlib.import_module(pkg) for pkg in SPLIT_PACKAGES}
        missing = []
        for rel in _project_py_files(include_tests=True):
            try:
                tree = ast.parse((ROOT / rel).read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            here = _module_of(rel)
            anchor = here if rel.name == '__init__.py' else here.rpartition('.')[0]
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                target = (importlib.util.resolve_name('.' * node.level + (node.module or ''), anchor)
                          if node.level else node.module)
                mod = packages.get(target)
                if mod is None:
                    continue
                for alias in node.names:
                    if alias.name != '*' and not hasattr(mod, alias.name):
                        try:  # `from pkg import submodule`
                            importlib.import_module(f'{target}.{alias.name}')
                        except ImportError:
                            missing.append(f'{rel}:{node.lineno} {target}.{alias.name}')
        self.assertEqual(missing, [])

    def test_superadmin_urls_still_resolve(self):
        import erp_core.urls as urls
        from clients.views import saas_admin_views
        names = set()
        for line in Path(urls.__file__).read_text(encoding='utf-8').splitlines():
            if 'saas_admin_views.' in line and 'import' not in line:
                names.add(line.split('saas_admin_views.')[1].split(',')[0].split(')')[0].strip())
        self.assertTrue(names)
        self.assertEqual(sorted(n for n in names if not hasattr(saas_admin_views, n)), [])


# حزم ليها SEAMS: أسماء الاختبارات بتعمل لها mock.patch.object(<الحزمة>, "X")، فجوه الحزمة
# لازم تتنادى `<alias>.X` (عن طريق الحزمة) — نداء `X(...)` مباشر مش هيشوف الـ patch.
SEAM_PACKAGES = {'robot.services': 'services', 'robot.views': 'views'}


class SeamTests(SimpleTestCase):

    def test_seams_are_only_used_through_the_package(self):
        bad = []
        for pkg in SEAM_PACKAGES:
            seams = set(importlib.import_module(pkg).SEAMS)
            for path in _package_dir(pkg).glob('*.py'):
                if path.stem == '__init__':
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    if isinstance(node, ast.Name) and node.id in seams and isinstance(node.ctx, ast.Load):
                        bad.append(f'{pkg}/{path.name}:{node.lineno} {node.id}')
        self.assertEqual(bad, [], 'نادِ الاسم ده كـ <alias>.X عشان الـ patch يوصل له')

    def test_every_patched_name_is_a_declared_seam(self):
        """`mock.patch.object(services, "X")` في الاختبارات ⇒ X لازم يكون في services.SEAMS."""
        missing = []
        for rel in _project_py_files(include_tests=True):
            if 'tests' not in rel.parts and not rel.name.startswith('test_'):
                continue
            for node in ast.walk(ast.parse((ROOT / rel).read_text(encoding='utf-8'))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'object' and len(node.args) >= 2):
                    continue
                target, name = node.args[0], node.args[1]
                if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                    continue
                alias = target.attr if isinstance(target, ast.Attribute) else getattr(target, 'id', None)
                for pkg, pkg_alias in SEAM_PACKAGES.items():
                    if alias == pkg_alias and rel.parts[0] == pkg.split('.')[0]:
                        if name.value not in importlib.import_module(pkg).SEAMS:
                            missing.append(f'{rel}:{node.lineno} {pkg}.{name.value}')
        self.assertEqual(missing, [], 'ضيف الاسم لـ SEAMS في __init__ الحزمة ونادِه كـ <alias>.X')


class FileSizeRatchetTests(SimpleTestCase):

    def test_no_new_giant_modules(self):
        too_big = {}
        for rel in _project_py_files():
            key = rel.as_posix()
            lines = len((ROOT / rel).read_text(encoding='utf-8').splitlines())
            allowed = LEGACY_GIANTS.get(key, LIMIT)
            if lines > allowed:
                too_big[key] = (lines, allowed)
        self.assertEqual(
            too_big, {},
            'ملفات عدّت الحد (السطور، المسموح) — قسّمها لموديولات حسب المجال '
            '(شوف printing/models/ أو clients/views/saas_admin_views/ كمثال).')

    def test_legacy_list_has_no_stale_entries(self):
        """لو ملف قديم اتقسم/اتشال أو صغر تحت الحد، شيله من LEGACY_GIANTS عشان القاعدة تفضل مشدودة."""
        stale = []
        for key in LEGACY_GIANTS:
            path = ROOT / key
            if not path.exists() or len(path.read_text(encoding='utf-8').splitlines()) <= LIMIT:
                stale.append(key)
        self.assertEqual(stale, [])
