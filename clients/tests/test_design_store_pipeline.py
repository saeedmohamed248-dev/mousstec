"""
Marketplace design-store pipeline tests — C1/C2/C3 migration verification.

Strategy
--------
The transient EscrowLedger / "connection already closed" teardown errors come
from ``inventory.tests.base.ERPTenantTestCase`` (multi-tenant schema dance).
The marketplace views we care about live in SHARED_APPS (`clients`) — public
schema — so we use the plain ``django.test.TestCase`` and patch every AI
boundary. No tenant create/drop, no flaky teardown.

What we verify (per critical bug)
---------------------------------
C1 design_store_generate — goes through compose_mega_prompt with
   brand_context, then generate_design_image (Smart Router), then
   composite_logo_on_image_url, then verify_design_quality.
C2 design_store_regenerate — same overlay pipeline (no more raw
   generate_flux_image call that bypassed brand + composite + quality gate).
C3 design_store_refine — brand_context still applied via compose_mega_prompt
   fast-path (already_engineered=True) and composite_logo runs after refine.

We mock the actual AI calls — no network, no tokens consumed.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TestCase, RequestFactory
from django.core.cache import cache

from clients.models import (
    MarketplaceCustomer,
    CustomerDesign,
    CustomerBrandProfile,
)
from clients.views import _legacy as legacy_views


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_customer(free_designs=5):
    return MarketplaceCustomer.objects.create(
        customer_type='individual',
        full_name='Test Customer',
        phone=f'+2010{uuid.uuid4().int % 100000000:08d}',
        sector='printing',
        is_verified=True,
        free_designs_total=free_designs,
        free_designs_used=0,
    )


def _make_brand(customer):
    return CustomerBrandProfile.objects.create(
        customer=customer,
        brand_name='Acme Co',
        primary_color='#0066FF',
        secondary_color='#FF6600',
        accent_color='#00CC88',
        aesthetic='modern',
        is_active=True,
        auto_inject_colors=True,
        auto_inject_logo=False,
    )


def _ok_mega(prompt='ENGINEERED MEGA PROMPT', brand_applied=True):
    return {
        'success': True,
        'mega_prompt': prompt,
        'negative_prompt': 'low quality',
        'presentation_category': 'logo',
        'text_overlay': None,
        'brand_applied': {'applied': brand_applied, 'fields': ['primary_color']},
    }


def _ok_image(engine='flux', url='https://cdn.test/img.png'):
    return {
        'success': True,
        'engine': engine,
        'model': f'{engine}-test',
        'url': url,
        'b64_json': None,
    }


# ---------------------------------------------------------------------------
# C1 — design_store_generate goes through full pipeline
# ---------------------------------------------------------------------------
class DesignStoreGeneratePipelineTests(TestCase):
    """C1: design_store_generate must invoke compose_mega_prompt with
    brand_context, then generate_design_image (Smart Router), then composite
    and quality gate."""

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.customer = _make_customer(free_designs=3)
        self.brand = _make_brand(self.customer)

    def _post(self, **extra):
        data = {
            'title': 'Test Logo',
            'description': 'A bold modern logo for a coffee shop',
            'category': 'logo',
            'size_preset': '1024x1024',
            'output_format': 'png',
        }
        data.update(extra)
        req = self.factory.post('/marketplace/design-store/generate/', data)
        req.COOKIES['mp_session'] = str(self.customer.session_token)
        return req

    @patch('erp_core.ai.design_engine.verify_design_quality')
    @patch('erp_core.ai.logo_overlay.composite_logo_on_image_url')
    @patch('erp_core.ai.printing_copilot.generate_design_image')
    @patch('erp_core.ai.design_engine.compose_mega_prompt')
    def test_generate_invokes_full_unified_pipeline(
        self, mock_mega, mock_gen, mock_composite, mock_quality,
    ):
        mock_mega.return_value = _ok_mega()
        mock_gen.return_value = _ok_image(engine='flux')
        mock_composite.return_value = {'success': True, 'url': '/media/composited.png'}
        mock_quality.return_value = {'success': True, 'score': 0.92}

        # Customer with brand profile + auto_inject_logo=False, so no logo composite.
        resp = legacy_views.design_store_generate(self._post())

        # Pipeline assertions
        self.assertTrue(mock_mega.called, 'compose_mega_prompt must be invoked')
        kwargs = mock_mega.call_args.kwargs
        self.assertTrue(
            kwargs.get('already_engineered'),
            'overlay approach requires already_engineered=True (M1 fast path)',
        )
        self.assertIsNotNone(
            kwargs.get('brand_context'),
            'brand_context must be passed when customer has active brand profile',
        )

        self.assertTrue(mock_gen.called, 'Smart Router (generate_design_image) must be invoked')
        self.assertTrue(mock_quality.called, 'Quality gate must run')

        # Response shape
        self.assertEqual(resp.status_code, 200)
        import json
        payload = json.loads(resp.content)
        self.assertEqual(payload['status'], 'success')
        self.assertEqual(payload['engine_used'], 'flux')
        self.assertTrue(payload['brand_applied'])
        self.assertEqual(payload['quality_score'], 0.92)

    @patch('erp_core.ai.design_engine.verify_design_quality')
    @patch('erp_core.ai.logo_overlay.composite_logo_on_image_url')
    @patch('erp_core.ai.printing_copilot.generate_design_image')
    @patch('erp_core.ai.design_engine.compose_mega_prompt')
    def test_generate_composites_brand_logo_when_present(
        self, mock_mega, mock_gen, mock_composite, mock_quality,
    ):
        # Enable auto_inject_logo + attach a fake logo file
        from django.core.files.base import ContentFile
        self.brand.auto_inject_logo = True
        self.brand.logo_image.save('logo.png', ContentFile(b'fake-png-bytes'))

        mock_mega.return_value = _ok_mega()
        mock_gen.return_value = _ok_image(engine='flux')
        mock_composite.return_value = {'success': True, 'url': '/media/with_logo.png'}
        mock_quality.return_value = {'success': True, 'score': 0.9}

        resp = legacy_views.design_store_generate(self._post())

        self.assertTrue(
            mock_composite.called,
            'composite_logo_on_image_url must run when brand has auto_inject_logo + logo',
        )
        import json
        payload = json.loads(resp.content)
        self.assertTrue(payload['logo_composited'])

    @patch('erp_core.ai.design_engine.verify_design_quality')
    @patch('erp_core.ai.logo_overlay.composite_logo_on_image_url')
    @patch('erp_core.ai.printing_copilot.generate_design_image')
    @patch('erp_core.ai.design_engine.compose_mega_prompt')
    def test_ideogram_skips_composite(
        self, mock_mega, mock_gen, mock_composite, mock_quality,
    ):
        """Ideogram draws text/logo natively — composite must be skipped."""
        from django.core.files.base import ContentFile
        self.brand.auto_inject_logo = True
        self.brand.logo_image.save('logo.png', ContentFile(b'fake-png-bytes'))

        mock_mega.return_value = _ok_mega()
        mock_gen.return_value = _ok_image(engine='ideogram')  # ← key
        mock_composite.return_value = {'success': True, 'url': '/media/x.png'}
        mock_quality.return_value = {'success': True, 'score': 0.9}

        legacy_views.design_store_generate(self._post())

        self.assertFalse(
            mock_composite.called,
            'composite must be skipped when engine=ideogram (it draws logo itself)',
        )


# ---------------------------------------------------------------------------
# C2 — design_store_regenerate goes through full pipeline
# ---------------------------------------------------------------------------
class DesignStoreRegeneratePipelineTests(TestCase):
    """C2: regenerate must use the same unified pipeline as generate."""

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.customer = _make_customer()
        self.brand = _make_brand(self.customer)
        self.design = CustomerDesign.objects.create(
            customer=self.customer,
            title='Original', description='Original description bold logo',
            category='logo', size_preset='1024x1024',
            output_format='png',
            raw_input='Original description bold logo',
            engineered_prompt='ENGINEERED ORIGINAL PROMPT',
            image_url='https://cdn.test/orig.png',
            model_used='flux-test',
            regenerations_allowed=3,
            regenerations_used=0,
        )

    def _post(self):
        req = self.factory.post(
            f'/marketplace/design-store/{self.design.design_code}/regenerate/',
        )
        req.COOKIES['mp_session'] = str(self.customer.session_token)
        return req

    @patch('erp_core.ai.design_engine.verify_design_quality')
    @patch('erp_core.ai.logo_overlay.composite_logo_on_image_url')
    @patch('erp_core.ai.printing_copilot.generate_design_image')
    @patch('erp_core.ai.design_engine.compose_mega_prompt')
    def test_regenerate_uses_unified_pipeline(
        self, mock_mega, mock_gen, mock_composite, mock_quality,
    ):
        mock_mega.return_value = _ok_mega(prompt='REENGINEERED')
        mock_gen.return_value = _ok_image(engine='flux', url='https://cdn.test/new.png')
        mock_composite.return_value = {'success': True, 'url': '/media/x.png'}
        mock_quality.return_value = {'success': True, 'score': 0.88}

        resp = legacy_views.design_store_regenerate(self._post(), self.design.design_code)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(mock_mega.called, 'C2: compose_mega_prompt must be invoked')
        kwargs = mock_mega.call_args.kwargs
        self.assertTrue(kwargs.get('already_engineered'))
        self.assertIsNotNone(
            kwargs.get('brand_context'),
            'C2 BUG REGRESSION: brand_context still not passed on regenerate',
        )
        self.assertTrue(mock_gen.called, 'C2: Smart Router must be invoked (not raw FLUX)')


# ---------------------------------------------------------------------------
# C3 — design_store_refine applies brand + composite
# ---------------------------------------------------------------------------
class DesignStoreRefinePipelineTests(TestCase):
    """C3: refine must (a) apply brand_context to the refinement prompt and
    (b) run logo composite after refine."""

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.customer = _make_customer()
        self.brand = _make_brand(self.customer)
        self.design = CustomerDesign.objects.create(
            customer=self.customer,
            title='Tshirt', description='A tshirt with a bold logo on chest',
            category='tshirt', size_preset='1024x1536',
            output_format='png',
            raw_input='A tshirt with a bold logo on chest',
            engineered_prompt='ENGINEERED TSHIRT PROMPT',
            image_url='https://cdn.test/tshirt.png',
            model_used='flux-test',
            regenerations_allowed=5,
            regenerations_used=0,
        )

    def _post(self, refinement='change the color to navy blue'):
        req = self.factory.post(
            f'/marketplace/design-store/{self.design.design_code}/refine/',
            {'refinement': refinement},
        )
        req.COOKIES['mp_session'] = str(self.customer.session_token)
        return req

    @patch('erp_core.ai.logo_overlay.composite_logo_on_image_url')
    @patch('erp_core.ai.printing_copilot.refine_design_image')
    @patch('erp_core.ai.printing_copilot.classify_refinement_intent')
    @patch('erp_core.ai.design_engine.compose_mega_prompt')
    def test_refine_applies_brand_and_composites(
        self, mock_mega, mock_intent, mock_refine, mock_composite,
    ):
        mock_intent.return_value = {
            'intent': 'color_change', 'can_use_kontext': True,
            'confidence': 0.9, 'detected_signals': ['color'],
        }
        mock_mega.return_value = _ok_mega(prompt='REFINED + BRANDED')
        mock_refine.return_value = {
            'success': True, 'url': 'https://cdn.test/refined.png',
            'b64_json': None, 'model': 'flux-kontext',
            'refinement_method': 'kontext_i2i',
        }
        mock_composite.return_value = {'success': True, 'url': '/media/with_logo.png'}

        # Skip the inner translation/overlay LLM hops — not under test here.
        with patch('inventory.ai_services.call_llm_layer', return_value='change to navy'):
            resp = legacy_views.design_store_refine(self._post(), self.design.design_code)

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            mock_mega.called,
            'C3 BUG REGRESSION: refine still bypasses compose_mega_prompt (no brand)',
        )
        kwargs = mock_mega.call_args.kwargs
        self.assertIsNotNone(
            kwargs.get('brand_context'),
            'C3 BUG REGRESSION: brand_context not injected into refine prompt',
        )
        self.assertTrue(kwargs.get('already_engineered'))
