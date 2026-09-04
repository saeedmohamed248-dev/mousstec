"""
🎨 AI Image Studio — tests
=====================================================================
يغطّي:
  • طبقة الخدمة (image_studio): بناء التعليمات، توليد المعاينة، التطبيق،
    الاسترجاع — مع محاكاة (mock) لمحرك FLUX.1-Kontext عشان مانضربش API حقيقي.
  • طبقة الـ views: بوابة الصلاحيات + مسارات generate/apply/revert.

المحرك الخارجي (_gen_via_flux_kontext) بيتعمله monkeypatch على مصدره في
erp_core.ai.printing_copilot عشان generate_preview بيستورده وقت التنفيذ.
"""
import base64
import json
import tempfile
from io import BytesIO

from django.core.files.base import ContentFile
from django.test import RequestFactory, override_settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.messages.middleware import MessageMiddleware

import erp_core.ai.printing_copilot as copilot
from inventory.models import Product
from inventory.services import image_studio as studio
from inventory.views.image_studio import (
    image_studio, image_studio_generate, image_studio_apply, image_studio_revert,
)

from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_product


def _png_bytes(color=(200, 30, 30), size=(64, 64)) -> bytes:
    """يولّد بايتات PNG صغيرة حقيقية عبر Pillow (متوفرة في requirements)."""
    from PIL import Image
    buf = BytesIO()
    Image.new('RGB', size, color).save(buf, format='PNG')
    return buf.getvalue()


def _fake_kontext_ok(*args, **kwargs):
    """بديل ناجح لمحرك التوليد — يرجّع صورة PNG كـ b64 (بدون شبكة)."""
    return {
        'success': True,
        'b64_json': base64.b64encode(_png_bytes(color=(10, 10, 60))).decode('ascii'),
        'url': None,
        'engine': 'kontext',
        'model': 'test-kontext',
        'cost_estimate_egp': 1.5,
    }


def _fake_kontext_fail(*args, **kwargs):
    return {'success': False, 'error': 'kontext_http_500'}


# Together as the active engine keeps these tests deterministic (Gemini needs a
# real key + network); the Gemini path is covered separately below.
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(),
                   IMAGE_STUDIO_ENGINE='together',
                   TOGETHER_API_KEY='test-key', GEMINI_API_KEY='')
class ImageStudioServiceTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.product = make_product(part_number='IMG-0001', name='مصباح أمامي')
        self.product.image.save('src.png', ContentFile(_png_bytes()), save=True)
        # patch the provider at its source module
        self._orig = copilot._gen_via_flux_kontext
        copilot._gen_via_flux_kontext = _fake_kontext_ok

    def tearDown(self):
        copilot._gen_via_flux_kontext = self._orig

    # ── build_instruction ────────────────────────────────────────────
    def test_presets_available(self):
        keys = {p['key'] for p in studio.list_presets()}
        self.assertIn('studio_white', keys)
        self.assertTrue(len(keys) >= 4)

    def test_build_instruction_preset(self):
        ins = studio.build_instruction('studio_white')
        self.assertIn('white', ins.lower())
        self.assertIn('background', ins.lower())

    def test_build_instruction_custom_wraps_keep_subject(self):
        ins = studio.build_instruction('', custom_prompt='خلفية سوداء')
        self.assertIn('خلفية سوداء', ins)
        self.assertIn('Only replace the background', ins)

    def test_build_instruction_empty_returns_none(self):
        self.assertIsNone(studio.build_instruction('nonexistent', ''))

    # ── generate_preview ─────────────────────────────────────────────
    def test_generate_preview_no_image(self):
        p = make_product(part_number='NO-IMG')
        res = studio.generate_preview(p, 'studio_white')
        self.assertFalse(res['ok'])
        self.assertEqual(res['error'], 'no_image')

    def test_generate_preview_ok_saves_under_preview_dir(self):
        res = studio.generate_preview(self.product, 'studio_white')
        self.assertTrue(res['ok'], res)
        self.assertTrue(res['preview_path'].startswith(studio.PREVIEW_DIR))
        from django.core.files.storage import default_storage
        self.assertTrue(default_storage.exists(res['preview_path']))

    def test_generate_preview_provider_failure(self):
        copilot._gen_via_flux_kontext = _fake_kontext_fail
        res = studio.generate_preview(self.product, 'studio_white')
        self.assertFalse(res['ok'])

    @override_settings(IMAGE_STUDIO_ENGINE='gemini',
                       GEMINI_API_KEY='test-gemini', TOGETHER_API_KEY='')
    def test_generate_preview_gemini_engine(self):
        """المحرك الافتراضي المجاني (Gemini) — نطبع الاستدعاء عبر _run_engines."""
        import base64 as _b64
        orig = studio._gen_via_gemini
        studio._gen_via_gemini = lambda jpeg, instr: {
            'success': True, 'engine': 'gemini', 'model': 'test',
            'b64_json': _b64.b64encode(_png_bytes(color=(0, 80, 0))).decode('ascii'),
            'url': None, 'cost_estimate_egp': 0.0,
        }
        try:
            res = studio.generate_preview(self.product, 'studio_white')
        finally:
            studio._gen_via_gemini = orig
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['engine'], 'gemini')

    # ── apply_preview ────────────────────────────────────────────────
    def test_apply_preview_backs_up_and_applies(self):
        gen = studio.generate_preview(self.product, 'studio_white')
        applied = studio.apply_preview(
            self.product, gen['preview_path'], preset_key='studio_white')
        self.assertTrue(applied['ok'], applied)
        self.product.refresh_from_db()
        self.assertTrue(self.product.image_ai_bg_applied)
        self.assertEqual(self.product.image_ai_bg_preset, 'studio_white')
        self.assertTrue(bool(self.product.image_original_backup))
        # preview file cleaned up after apply
        from django.core.files.storage import default_storage
        self.assertFalse(default_storage.exists(gen['preview_path']))

    def test_apply_preview_rejects_path_outside_preview_dir(self):
        res = studio.apply_preview(self.product, 'products/../../etc/passwd')
        self.assertFalse(res['ok'])
        self.assertEqual(res['error'], 'invalid_preview_path')

    def test_apply_preview_missing_file(self):
        res = studio.apply_preview(
            self.product, studio.PREVIEW_DIR + 'does_not_exist.png')
        self.assertFalse(res['ok'])
        self.assertEqual(res['error'], 'preview_missing')

    # ── revert ───────────────────────────────────────────────────────
    def test_revert_restores_original(self):
        gen = studio.generate_preview(self.product, 'studio_white')
        studio.apply_preview(self.product, gen['preview_path'], 'studio_white')
        self.product.refresh_from_db()
        res = studio.revert(self.product)
        self.assertTrue(res['ok'], res)
        self.product.refresh_from_db()
        self.assertFalse(self.product.image_ai_bg_applied)
        self.assertEqual(self.product.image_ai_bg_preset, '')

    def test_revert_without_backup(self):
        res = studio.revert(self.product)
        self.assertFalse(res['ok'])
        self.assertEqual(res['error'], 'no_backup')


def _wire(user, tenant, method='get', path='/', body=None):
    rf = RequestFactory()
    if method == 'post':
        req = rf.post(path, data=json.dumps(body or {}),
                      content_type='application/json')
    else:
        req = rf.get(path)
    SessionMiddleware(lambda r: None).process_request(req)
    req.session.save()
    AuthenticationMiddleware(lambda r: None).process_request(req)
    MessageMiddleware(lambda r: None).process_request(req)
    req.user = user
    req.tenant = tenant
    return req


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(),
                   IMAGE_STUDIO_ENGINE='together',
                   TOGETHER_API_KEY='test-key', GEMINI_API_KEY='')
class ImageStudioViewTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.product = make_product(part_number='V-IMG-1', name='رادياتير')
        self.product.image.save('v.png', ContentFile(_png_bytes()), save=True)
        self._orig = copilot._gen_via_flux_kontext
        copilot._gen_via_flux_kontext = _fake_kontext_ok

    def tearDown(self):
        copilot._gen_via_flux_kontext = self._orig

    # ── permission gate ──────────────────────────────────────────────
    def test_page_allowed_for_stock_role(self):
        user, _ = make_employee('u_stock', role='stock', branch=self.branch)
        r = image_studio(_wire(user, self.tenant, 'get', '/system/image-studio/'))
        self.assertEqual(r.status_code, 200)

    def test_page_forbidden_for_cashier(self):
        user, _ = make_employee('u_cash', role='cashier', branch=self.branch)
        r = image_studio(_wire(user, self.tenant, 'get', '/system/image-studio/'))
        self.assertEqual(r.status_code, 403)

    # ── generate → apply → revert happy path ─────────────────────────
    def test_generate_apply_revert_flow(self):
        user, _ = make_employee('u_admin', role='admin', branch=self.branch)

        gen = image_studio_generate(_wire(
            user, self.tenant, 'post', '/system/image-studio/generate/',
            {'product_id': self.product.id, 'preset': 'studio_white'}))
        self.assertEqual(gen.status_code, 200)
        payload = json.loads(gen.content)
        self.assertTrue(payload['preview_path'].startswith(studio.PREVIEW_DIR))

        applied = image_studio_apply(_wire(
            user, self.tenant, 'post', '/system/image-studio/apply/',
            {'product_id': self.product.id, 'preview_path': payload['preview_path'],
             'preset': 'studio_white'}))
        self.assertEqual(applied.status_code, 200)
        self.product.refresh_from_db()
        self.assertTrue(self.product.image_ai_bg_applied)

        reverted = image_studio_revert(_wire(
            user, self.tenant, 'post', '/system/image-studio/revert/',
            {'product_id': self.product.id}))
        self.assertEqual(reverted.status_code, 200)
        self.product.refresh_from_db()
        self.assertFalse(self.product.image_ai_bg_applied)

    def test_generate_unknown_product(self):
        user, _ = make_employee('u_admin2', role='admin', branch=self.branch)
        r = image_studio_generate(_wire(
            user, self.tenant, 'post', '/system/image-studio/generate/',
            {'product_id': 999999, 'preset': 'studio_white'}))
        self.assertEqual(r.status_code, 404)
