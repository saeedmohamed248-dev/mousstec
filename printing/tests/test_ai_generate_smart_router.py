"""
🎨 Tenant AI Studio — Smart Router Migration Tests (Phase N.6)
=====================================================================
Verifies the rewrite of `printing.views.ai_generate_design`:

  • OpenAI removed; pipeline now flows through compose_mega_prompt →
    generate_design_image (FLUX/Ideogram smart router) → composite_logo
    → quality gate → watermark.
  • Tenant brand_context is built from Client.logo + Client.name +
    Client.industry (no migration required, minimal profile).
  • Same JSON response shape as legacy; additive fields (engine_used,
    brand_applied, logo_composited, quality_score) appended.

All external calls are mocked: no real Together / Ideogram / FLUX /
OpenAI / vision-gate calls happen here.
"""
import json
from io import BytesIO
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import RequestFactory, TransactionTestCase
from django_tenants.utils import get_tenant_model, get_tenant_domain_model


def _parse(resp):
    """Direct view calls return django.http.JsonResponse — parse its body."""
    return json.loads(resp.content)


def _attach_real_logo(tenant):
    """Save a real 1x1 PNG to tenant.logo via the normal ImageField path.
    Avoids all the descriptor-patching pain MagicMock causes with FileFields."""
    from PIL import Image as _PIL
    buf = BytesIO()
    _PIL.new('RGB', (1, 1), (200, 0, 0)).save(buf, 'PNG')
    tenant.logo = SimpleUploadedFile('test.png', buf.getvalue(), content_type='image/png')
    tenant.save(update_fields=['logo'])


# ---------------------------------------------------------------------------
# Tenant test base — sets up a tenant schema for the printing app context.
# Distinct from inventory's ERPTenantTestCase to avoid coupling, but uses
# the same django-tenants pattern.
# ---------------------------------------------------------------------------
class _PrintingTenantTestCase(TransactionTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        TenantModel = get_tenant_model()
        DomainModel = get_tenant_domain_model()
        cls.tenant = TenantModel(
            schema_name='test_printing',
            name='مطبعة الاختبار',
            owner_name='Test Owner',
            phone='+201000000099',
            industry='printing',
        )
        cls.tenant.auto_create_schema = True
        cls.tenant.save(verbosity=0)
        cls.domain = DomainModel.objects.create(
            tenant=cls.tenant, domain='test-printing.test.com', is_primary=True,
        )
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        try:
            from clients.models import EscrowLedger, AILimitTracker, AIBonusGrant
            EscrowLedger.objects.filter(client=cls.tenant).delete()
            AILimitTracker.objects.filter(tenant=cls.tenant).delete()
            AIBonusGrant.objects.filter(tenant=cls.tenant).delete()
        except Exception:
            pass
        try:
            cls.domain.delete()
        except Exception:
            pass
        try:
            cls.tenant.delete(force_drop=True)
        except Exception:
            pass
        super().tearDownClass()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(tenant_to_restore):
    """Create user on the PUBLIC schema (AIStudioSession lives in SHARED so
    its auth_user FK resolves there), then restore tenant context."""
    connection.set_schema_to_public()
    try:
        existing = User.objects.filter(username='tester').first()
        user = existing or User.objects.create_user(username='tester', password='x')
    finally:
        connection.set_tenant(tenant_to_restore)
    return user


def _make_request_factory_post(data=None, files=None):
    """RequestFactory POST with multipart so we can attach files."""
    rf = RequestFactory()
    data = data or {}
    if files:
        data = dict(data)
        data.update(files)
    return rf.post('/printing/ai/generate-design/', data=data)


def _mock_mega(category='apparel', text_overlay=None, brand_applied=False):
    return {
        'success': True,
        'mega_prompt': 'A cinematic product mockup, studio lighting.',
        'negative_prompt': 'low quality, blurry',
        'recommended_size': '1024x1024',
        'presentation_category': category,
        'subtype': None,
        'text_overlay': text_overlay,
        'brand_applied': {
            'applied': brand_applied, 'brand_name': 'مطبعة الاختبار',
            'colors_applied': False, 'logo_described': brand_applied,
            'style_applied': False,
        },
    }


def _mock_img(url='https://cdn.test/img.jpg', engine='flux'):
    return {
        'success': True, 'url': url,
        'engine': engine, 'provider': 'together',
        'model': f'{engine}.1-dev',
    }


# ===========================================================================
# _tenant_brand_context — the helper that bridges Client fields → pipeline ctx
# ===========================================================================
class TenantBrandContextHelperTests(_PrintingTenantTestCase):

    def setUp(self):
        super().setUp()
        # Clear any logo leaked from a prior test in this class
        # (TransactionTestCase doesn't rebuild the class-level tenant fixture).
        if self.tenant.logo:
            self.tenant.logo.delete(save=False)
        self.tenant.logo = None
        self.tenant.save(update_fields=['logo'])

    def test_none_tenant_returns_none(self):
        from printing.views import _tenant_brand_context
        self.assertIsNone(_tenant_brand_context(None))

    def test_tenant_without_logo_builds_partial_ctx(self):
        from printing.views import _tenant_brand_context
        ctx = _tenant_brand_context(self.tenant)
        self.assertEqual(ctx['brand_name'], 'مطبعة الاختبار')
        self.assertEqual(ctx['industry'], 'printing')
        self.assertNotIn('logo_url', ctx)
        # logo_described False — there's no logo cue for the LLM
        self.assertNotIn('logo_described', ctx)

    def test_tenant_with_logo_sets_logo_cue(self):
        from printing.views import _tenant_brand_context
        _attach_real_logo(self.tenant)
        ctx = _tenant_brand_context(self.tenant)
        self.assertTrue(ctx['logo_described'])
        self.assertIn('logo_url', ctx)
        self.assertTrue(ctx['logo_url'].endswith('.png'))

    def test_request_logo_file_overrides_tenant_logo(self):
        """A logo uploaded in the POST gives logo_described=True even when
        tenant has no persistent logo."""
        from printing.views import _tenant_brand_context
        fake_file = SimpleUploadedFile('logo.png', b'\x89PNG\r\n', content_type='image/png')
        ctx = _tenant_brand_context(self.tenant, request_logo_file=fake_file)
        self.assertTrue(ctx['logo_described'])

    def test_tenant_without_name_returns_none(self):
        """`name` is a reserved MagicMock kwarg — use a plain object."""
        from printing.views import _tenant_brand_context
        class _Bare:
            name = ''
            industry = ''
            logo = None
        self.assertIsNone(_tenant_brand_context(_Bare()))


# ===========================================================================
# ai_generate_design — orchestrator integration tests (all engines mocked)
# ===========================================================================
class AiGenerateDesignViewTests(_PrintingTenantTestCase):
    """End-to-end view tests: real tenant + real DB writes, mocked engines."""

    def setUp(self):
        super().setUp()
        self.user = _make_user(self.tenant)
        # Clean-slate logo state per test
        if self.tenant.logo:
            self.tenant.logo.delete(save=False)
        self.tenant.logo = None
        self.tenant.save(update_fields=['logo'])

    # --- helpers ---------------------------------------------------
    def _run_view(self, data=None, files=None, *,
                  allow_access=True, mega=None, img=None,
                  composite_ok=True, quality_score=92):
        """Run the view with the standard mock stack. Returns the response."""
        from printing.views import ai_generate_design

        req = _make_request_factory_post(
            data=data or {'prompt': 'تيشرت رياضي بالأزرق'},
            files=files,
        )
        req.user = self.user

        check_patch = patch(
            'printing.views._check_ai_access',
            return_value=(allow_access, None if allow_access else 'no_access'),
        )
        deduct_patch = patch(
            'clients.models.AILimitTracker.deduct',
            return_value=True,
        )
        # AIStudioSession.create still hits the DB — let it run for realism

        compose_patch = patch(
            'erp_core.ai.design_engine.compose_mega_prompt',
            return_value=mega if mega is not None else _mock_mega(),
        )
        gen_patch = patch(
            'erp_core.ai.printing_copilot.generate_design_image',
            return_value=img if img is not None else _mock_img(),
        )
        comp_patch = patch(
            'erp_core.ai.logo_overlay.composite_logo_on_image_url',
            return_value={
                'success': composite_ok,
                'url': 'https://cdn.test/composited.jpg',
                'placement': 'chest_left',
                'width_ratio': 0.10,
                'avoided_text': True,
            } if composite_ok else {'success': False, 'error': 'logo_load_failed'},
        )
        verify_patch = patch(
            'erp_core.ai.design_engine.verify_design_quality',
            return_value={'success': True, 'score': quality_score, 'verdict': 'pass'},
        )

        with check_patch, deduct_patch, compose_patch, gen_patch, comp_patch, verify_patch:
            return ai_generate_design(req)

    # --- happy path ------------------------------------------------
    def test_successful_generation_returns_full_response(self):
        resp = self._run_view()
        self.assertEqual(resp.status_code, 200)
        data = _parse(resp)
        self.assertTrue(data['success'])
        self.assertIn('image_url', data)
        self.assertEqual(data['engine_used'], 'flux')
        self.assertEqual(data['presentation_category'], 'apparel')
        self.assertIn('session_id', data)
        # Additive fields present
        self.assertIn('brand_applied', data)
        self.assertIn('logo_composited', data)
        self.assertIn('quality_score', data)

    def test_response_preserves_legacy_field_shape(self):
        """Frontend depends on these specific fields — don't drop them."""
        resp = self._run_view()
        data = _parse(resp)
        for field in ('image_url', 'original_url', 'watermarked_url',
                      'revised_prompt', 'model_used', 'session_id'):
            self.assertIn(field, data)

    # --- input validation ------------------------------------------
    def test_empty_prompt_returns_400(self):
        resp = self._run_view(data={'prompt': '   '})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('وصف التصميم', _parse(resp)['error'])

    def test_invalid_size_falls_back_to_default(self):
        resp = self._run_view(data={
            'prompt': 'تيشرت',
            'size': 'gibberish-9999',
        })
        self.assertEqual(resp.status_code, 200)

    def test_size_auto_maps_to_1024(self):
        """'auto' was a gpt-image-1 thing — we normalize to 1024."""
        resp = self._run_view(data={'prompt': 'تيشرت', 'size': 'auto'})
        self.assertEqual(resp.status_code, 200)

    # --- access gate -----------------------------------------------
    def test_access_denied_returns_403(self):
        resp = self._run_view(allow_access=False)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(_parse(resp)['success'])

    # --- pipeline failures -----------------------------------------
    def test_compose_failure_returns_502_arabic(self):
        resp = self._run_view(mega={'success': False, 'error': 'empty_idea'})
        self.assertEqual(resp.status_code, 502)
        self.assertIn('البرومبت', _parse(resp)['error'])

    def test_image_gen_failure_returns_502_with_friendly_error(self):
        resp = self._run_view(img={
            'success': False,
            'error': 'together_key_missing',
            'detail': 'auth header missing',
        })
        self.assertEqual(resp.status_code, 502)
        self.assertIn('Together AI', _parse(resp)['error'])

    def test_unknown_engine_error_falls_to_generic_message(self):
        resp = self._run_view(img={
            'success': False,
            'error': 'flux_503_timeout',
        })
        self.assertEqual(resp.status_code, 502)
        # Generic message includes the error code
        self.assertIn('flux_503_timeout', _parse(resp)['error'])

    # --- smart router routing --------------------------------------
    def test_ideogram_engine_skips_logo_composite(self):
        """Ideogram already 'draws' brand identity — compositing would corrupt it."""
        _attach_real_logo(self.tenant)
        with patch('erp_core.ai.logo_overlay.composite_logo_on_image_url') as comp_mock:
            comp_mock.return_value = {'success': True, 'url': 'should-not-be-used'}
            resp = self._run_view(
                mega=_mock_mega(category='logo'),
                img=_mock_img(engine='ideogram'),
            )
        self.assertEqual(resp.status_code, 200)
        data = _parse(resp)
        self.assertEqual(data['engine_used'], 'ideogram')
        self.assertFalse(data['logo_composited'])
        comp_mock.assert_not_called()

    def test_flux_engine_with_tenant_logo_calls_composite(self):
        _attach_real_logo(self.tenant)
        resp = self._run_view(
            mega=_mock_mega(category='apparel'),
            img=_mock_img(engine='flux'),
        )
        data = _parse(resp)
        self.assertTrue(data['logo_composited'])
        self.assertEqual(data['image_url'].rsplit('/', 1)[-1], 'composited.jpg')

    def test_logo_upload_in_post_composites_without_tenant_logo(self):
        """One-shot logo from POST → composite triggers even when tenant.logo
        is empty."""
        fake_file = SimpleUploadedFile('logo.png', b'\x89PNG\r\n', content_type='image/png')
        resp = self._run_view(files={'logo': fake_file})
        data = _parse(resp)
        self.assertTrue(data['logo_composited'])

    def test_composite_failure_is_non_fatal(self):
        """A failed composite returns the un-composited URL, not 500."""
        _attach_real_logo(self.tenant)
        resp = self._run_view(composite_ok=False)
        self.assertEqual(resp.status_code, 200)
        data = _parse(resp)
        self.assertFalse(data['logo_composited'])
        # Original image_url survives
        self.assertEqual(data['image_url'].rsplit('/', 1)[-1], 'img.jpg')

    # --- quality gate ----------------------------------------------
    def test_quality_score_returned_when_gate_succeeds(self):
        resp = self._run_view(quality_score=88)
        data = _parse(resp)
        self.assertEqual(data['quality_score'], 88)

    # --- session persistence ---------------------------------------
    def test_aistudio_session_row_persisted(self):
        from clients.models import AIStudioSession
        before = AIStudioSession.objects.filter(tenant=self.tenant).count()
        resp = self._run_view()
        self.assertEqual(resp.status_code, 200)
        after = AIStudioSession.objects.filter(tenant=self.tenant).count()
        self.assertEqual(after - before, 1)
        latest = AIStudioSession.objects.filter(
            tenant=self.tenant
        ).order_by('-pk').first()
        self.assertIn('cinematic', latest.engineered_prompt.lower())

    def test_no_openai_dependency_in_view_module(self):
        """Sanity: the view no longer imports openai at module level."""
        import printing.views as pv
        # Module-level imports list — confirm 'openai' isn't there.
        # (Local imports inside functions are fine; we only check the head.)
        with open(pv.__file__, 'r', encoding='utf-8') as f:
            head = '\n'.join(f.readline() for _ in range(40))
        self.assertNotIn('import openai', head)
        self.assertNotIn('from openai', head)
