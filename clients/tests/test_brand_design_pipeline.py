"""
🎨 End-to-End Brand Profile → Smart Router → Design Pipeline Tests
===================================================================

Verifies that a customer with a saved CustomerBrandProfile successfully
threads their identity (name, colors, logo cue, aesthetic) through the
design pipeline and that the Smart Router (`pick_design_engine`) lands
on the right provider per category.

Layers covered:
  L1  apply_brand_profile()        — pure merge logic
  L2  pick_design_engine()         — Smart Router category routing
  L3  CustomerBrandProfile.as_brand_context() — model → dict bridge
  L4  compose_mega_prompt()        — full pipeline w/ mocked LLM call

No real Ideogram / FLUX / Together LLM calls happen here. We mock
`_call_together_llm` so the suite runs offline and deterministic.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from clients.models import MarketplaceCustomer, CustomerBrandProfile
from erp_core.ai.design_engine import apply_brand_profile, compose_mega_prompt
from erp_core.ai.printing_copilot import pick_design_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_customer(phone='+201000000001'):
    return MarketplaceCustomer.objects.create(
        customer_type='individual',
        full_name='Test Customer',
        phone=phone,
        sector='printing',
        is_verified=True,
    )


def _make_brand(customer, **overrides):
    defaults = dict(
        brand_name='موس تك',
        brand_name_en='Mouss Tec',
        tagline='تصميم بسرعة الضوء',
        primary_color='#7c3aed',
        secondary_color='#1e293b',
        accent_color='#ec4899',
        industry='tech',
        aesthetic='modern_minimal',
        tone='confident',
        arabic_font='arabic_modern',
        english_font='modern_sans',
        style_notes='avoid clutter, prefer geometric shapes',
        is_active=True,
        auto_inject_logo=True,
        auto_inject_colors=True,
    )
    defaults.update(overrides)
    return CustomerBrandProfile.objects.create(customer=customer, **defaults)


def _fake_llm_response(mega_prompt='A modern logo composition.', category='logo'):
    """Mimics the shape `_call_together_llm` returns on success."""
    return {
        'success': True,
        'data': {
            'mega_prompt': mega_prompt,
            'presentation_category': category,
            'text_overlay': None,
            'domain': 'design',
        },
    }


# ===========================================================================
# L1 — apply_brand_profile() pure logic
# ===========================================================================
class ApplyBrandProfileLogicTests(TestCase):
    """The merge function: explicit selections must always win."""

    def test_no_brand_context_is_noop(self):
        sel = {'color': 'red'}
        merged, refs, meta = apply_brand_profile(sel, None, ['ref1'])
        self.assertEqual(merged, sel)
        self.assertEqual(refs, ['ref1'])
        self.assertFalse(meta['applied'])

    def test_brand_fills_empty_color_slots(self):
        brand = {
            'brand_name': 'موس تك',
            'primary_color': '#7c3aed',
            'secondary_color': '#1e293b',
            'aesthetic': 'Modern Minimal',
            'tone': 'Confident',
            'industry': 'Tech',
        }
        merged, refs, meta = apply_brand_profile({}, brand, [])
        self.assertTrue(meta['applied'])
        self.assertTrue(meta['colors_applied'])
        self.assertEqual(merged['brand_primary_color'], '#7c3aed')
        self.assertEqual(merged['brand_name'], 'موس تك')
        self.assertEqual(merged['brand_aesthetic'], 'Modern Minimal')

    def test_explicit_color_choice_blocks_brand_override(self):
        """Customer's red wins over brand's purple."""
        brand = {'brand_name': 'X', 'primary_color': '#7c3aed', 'aesthetic': 'A', 'tone': 'T'}
        merged, _refs, meta = apply_brand_profile(
            {'primary_color': '#ff0000'}, brand, [],
        )
        self.assertTrue(meta['applied'])
        self.assertFalse(meta['colors_applied'])
        self.assertEqual(merged['primary_color'], '#ff0000')
        # Brand color must NOT be injected as brand_primary_color
        self.assertNotIn('brand_primary_color', merged)

    def test_explicit_style_choice_blocks_brand_aesthetic(self):
        brand = {'brand_name': 'X', 'aesthetic': 'Modern Minimal', 'tone': 'Confident'}
        merged, _refs, meta = apply_brand_profile(
            {'style': 'vintage retro'}, brand, [],
        )
        self.assertTrue(meta['applied'])
        self.assertFalse(meta['style_applied'])
        self.assertNotIn('brand_aesthetic', merged)

    def test_logo_described_appends_reservation_hint(self):
        brand = {
            'brand_name': 'موس تك',
            'aesthetic': 'A', 'tone': 'T',
            'logo_described': True,
        }
        _merged, refs, meta = apply_brand_profile({}, brand, ['ref1'])
        self.assertTrue(meta['logo_described'])
        joined = '\n'.join(refs)
        self.assertIn('BRAND LOGO RESERVED', joined)
        self.assertIn('موس تك', joined)

    def test_no_logo_hint_when_not_described(self):
        brand = {'brand_name': 'X', 'aesthetic': 'A', 'tone': 'T'}
        _merged, refs, meta = apply_brand_profile({}, brand, [])
        self.assertFalse(meta['logo_described'])
        self.assertEqual(refs, [])


# ===========================================================================
# L2 — Smart Router (pick_design_engine)
# ===========================================================================
@override_settings(IDEOGRAM_API_KEY='test-ideogram-key')
class SmartRouterWithIdeogramKeyTests(TestCase):
    """When Ideogram key is configured, text-critical categories route there."""

    def test_logo_routes_to_ideogram(self):
        self.assertEqual(pick_design_engine('logo'), 'ideogram')

    def test_document_routes_to_ideogram(self):
        self.assertEqual(pick_design_engine('document'), 'ideogram')

    def test_signage_routes_to_ideogram(self):
        self.assertEqual(pick_design_engine('signage'), 'ideogram')

    def test_social_post_routes_to_ideogram(self):
        self.assertEqual(pick_design_engine('social_post'), 'ideogram')

    def test_apparel_stays_on_flux(self):
        """Photo-realism category: FLUX even when Ideogram key is available."""
        self.assertEqual(pick_design_engine('apparel'), 'flux')

    def test_footwear_stays_on_flux(self):
        """Slippers, sneakers, boots — all footwear → FLUX."""
        self.assertEqual(pick_design_engine('footwear'), 'flux')

    def test_arabic_text_on_neutral_category_uses_ideogram(self):
        """Edge case: Arabic text on a non-photo-critical category."""
        self.assertEqual(
            pick_design_engine('other', has_text_content=True, has_arabic=True),
            'ideogram',
        )

    def test_arabic_text_on_apparel_still_flux(self):
        """Even with Arabic, apparel keeps FLUX — PIL overlay handles text."""
        self.assertEqual(
            pick_design_engine('apparel', has_text_content=True, has_arabic=True),
            'flux',
        )

    def test_force_engine_override_respected(self):
        self.assertEqual(pick_design_engine('apparel', force_engine='ideogram'), 'ideogram')
        self.assertEqual(pick_design_engine('logo', force_engine='flux'), 'flux')


@override_settings(IDEOGRAM_API_KEY='')
class SmartRouterWithoutIdeogramKeyTests(TestCase):
    """No Ideogram key → everything falls back to FLUX."""

    def test_logo_falls_back_to_flux(self):
        self.assertEqual(pick_design_engine('logo'), 'flux')

    def test_document_falls_back_to_flux(self):
        self.assertEqual(pick_design_engine('document'), 'flux')


# ===========================================================================
# L3 — CustomerBrandProfile.as_brand_context()
# ===========================================================================
class BrandProfileContextBridgeTests(TestCase):
    """The model's `as_brand_context()` is the contract handed to the pipeline."""

    def test_full_context_contains_expected_keys(self):
        customer = _make_customer()
        bp = _make_brand(customer)
        ctx = bp.as_brand_context()

        self.assertEqual(ctx['brand_name'], 'موس تك')
        self.assertEqual(ctx['brand_name_en'], 'Mouss Tec')
        self.assertEqual(ctx['tagline'], 'تصميم بسرعة الضوء')
        self.assertEqual(ctx['primary_color'], '#7c3aed')
        self.assertEqual(ctx['secondary_color'], '#1e293b')
        self.assertEqual(ctx['accent_color'], '#ec4899')
        self.assertIn('industry', ctx)
        self.assertIn('aesthetic', ctx)
        self.assertIn('tone', ctx)
        self.assertIn('arabic_font_pref', ctx)
        self.assertIn('english_font_pref', ctx)
        self.assertEqual(ctx['style_notes'], 'avoid clutter, prefer geometric shapes')

    def test_auto_inject_colors_disabled_strips_colors(self):
        customer = _make_customer(phone='+201000000002')
        bp = _make_brand(customer, auto_inject_colors=False)
        ctx = bp.as_brand_context()

        self.assertNotIn('primary_color', ctx)
        self.assertNotIn('secondary_color', ctx)
        self.assertNotIn('accent_color', ctx)
        # But aesthetic/identity still present
        self.assertEqual(ctx['brand_name'], 'موس تك')
        self.assertIn('aesthetic', ctx)


# ===========================================================================
# L4 — compose_mega_prompt() end-to-end with brand_context (LLM mocked)
# ===========================================================================
class ComposeMegaPromptWithBrandTests(TestCase):
    """The whole pipeline: brand context → merged selections → mega_prompt."""

    @patch('erp_core.ai.design_engine._call_together_llm')
    def test_brand_applied_meta_surfaces_in_response(self, mock_llm):
        mock_llm.return_value = _fake_llm_response(category='logo')

        customer = _make_customer()
        bp = _make_brand(customer)
        ctx = bp.as_brand_context()
        ctx['logo_described'] = True  # caller sets this when bp.has_logo

        result = compose_mega_prompt(
            raw_idea='لوجو لمطعم لبناني عصري',
            domain='restaurant',
            selections={},
            brand_context=ctx,
        )

        self.assertTrue(result.get('success'), msg=result)
        self.assertTrue(result['brand_applied']['applied'])
        self.assertEqual(result['brand_applied']['brand_name'], 'موس تك')
        self.assertTrue(result['brand_applied']['colors_applied'])
        self.assertTrue(result['brand_applied']['logo_described'])
        self.assertTrue(result['brand_applied']['style_applied'])

    @patch('erp_core.ai.design_engine._call_together_llm')
    def test_brand_fields_reach_the_llm_user_message(self, mock_llm):
        """The user_msg sent to Together MUST contain brand identity hints."""
        mock_llm.return_value = _fake_llm_response()

        customer = _make_customer()
        bp = _make_brand(customer)
        ctx = bp.as_brand_context()

        compose_mega_prompt(
            raw_idea='تصميم تيشرت',
            domain='apparel',
            selections={},
            brand_context=ctx,
        )

        # _call_together_llm(system_msg, user_msg, temperature=...)
        self.assertTrue(mock_llm.called)
        args, _kwargs = mock_llm.call_args
        user_msg = args[1] if len(args) > 1 else _kwargs.get('user_msg', '')

        self.assertIn('موس تك', user_msg)            # brand name
        self.assertIn('#7c3aed', user_msg)           # primary color
        self.assertIn('brand_aesthetic', user_msg)   # aesthetic key flowed in

    @patch('erp_core.ai.design_engine._call_together_llm')
    def test_explicit_selection_overrides_brand_in_prompt(self, mock_llm):
        mock_llm.return_value = _fake_llm_response()

        customer = _make_customer()
        bp = _make_brand(customer)
        ctx = bp.as_brand_context()

        compose_mega_prompt(
            raw_idea='تصميم بوستر',
            domain='signage',
            selections={'primary_color': '#ff0000'},
            brand_context=ctx,
        )

        args, _ = mock_llm.call_args
        user_msg = args[1]
        self.assertIn('#ff0000', user_msg)
        # The brand purple was NOT injected as brand_primary_color
        self.assertNotIn('brand_primary_color: #7c3aed', user_msg)

    @patch('erp_core.ai.design_engine._call_together_llm')
    def test_no_brand_context_means_brand_not_applied(self, mock_llm):
        mock_llm.return_value = _fake_llm_response()

        result = compose_mega_prompt(
            raw_idea='لوجو',
            domain='design',
            selections={},
            brand_context=None,
        )

        self.assertTrue(result.get('success'))
        self.assertFalse(result['brand_applied']['applied'])


# ===========================================================================
# L4b — The combined journey: brand profile + smart router decision
# ===========================================================================
@override_settings(IDEOGRAM_API_KEY='test-key')
class FullJourneyTests(TestCase):
    """Simulates: customer with brand → compose prompt → router picks engine."""

    @patch('erp_core.ai.design_engine._call_together_llm')
    def test_branded_logo_request_routes_to_ideogram(self, mock_llm):
        mock_llm.return_value = _fake_llm_response(category='logo')

        customer = _make_customer()
        _make_brand(customer)
        ctx = customer.brand_profile.as_brand_context()
        ctx['logo_described'] = True

        result = compose_mega_prompt(
            raw_idea='لوجو لشركة تكنولوجيا',
            domain='tech',
            selections={},
            brand_context=ctx,
            presentation_category='logo',
        )

        self.assertTrue(result['brand_applied']['applied'])
        engine = pick_design_engine(
            result['presentation_category'],
            has_text_content=True, has_arabic=True,
        )
        self.assertEqual(engine, 'ideogram')

    @patch('erp_core.ai.design_engine._call_together_llm')
    def test_branded_sneaker_request_routes_to_flux(self, mock_llm):
        mock_llm.return_value = _fake_llm_response(category='footwear')

        customer = _make_customer(phone='+201000000003')
        _make_brand(customer)
        ctx = customer.brand_profile.as_brand_context()

        result = compose_mega_prompt(
            raw_idea='سنيكرز رياضي للجري',
            domain='footwear',
            selections={},
            brand_context=ctx,
            presentation_category='footwear',
            subtype='sneaker',
        )

        self.assertTrue(result['brand_applied']['applied'])
        engine = pick_design_engine(result['presentation_category'])
        self.assertEqual(engine, 'flux')

    @patch('erp_core.ai.design_engine._call_together_llm')
    def test_branded_slipper_still_routes_to_flux(self, mock_llm):
        """Subtype distinction (slipper vs sneaker) is downstream of routing —
        both share the `footwear` category so both must land on FLUX."""
        mock_llm.return_value = _fake_llm_response(category='footwear')

        customer = _make_customer(phone='+201000000004')
        _make_brand(customer)
        ctx = customer.brand_profile.as_brand_context()

        result = compose_mega_prompt(
            raw_idea='شبشب صيفي مريح',
            domain='footwear',
            selections={},
            brand_context=ctx,
            presentation_category='footwear',
            subtype='slipper',
        )

        engine = pick_design_engine(result['presentation_category'])
        self.assertEqual(engine, 'flux')

        # And the subtype must have reached the LLM message
        args, _ = mock_llm.call_args
        self.assertIn('subtype=slipper', args[1])
