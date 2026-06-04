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
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

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


# ===========================================================================
# L5 — Logo Compositing (PIL)
# ===========================================================================
class LogoCompositingTests(TestCase):
    """The PIL compositor must place logos by category, skip excluded ones,
    avoid the text-overlay zone, and fail soft on bad inputs."""

    def _solid_jpeg_bytes(self, size=(800, 800), color=(200, 200, 200)):
        from PIL import Image
        import io
        img = Image.new('RGB', size, color)
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        return buf.getvalue()

    def _logo_png_bytes(self, size=(200, 200), color=(255, 0, 0, 220)):
        from PIL import Image
        import io
        img = Image.new('RGBA', size, color)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    def _mock_requests_get(self, image_bytes):
        """Patch context for `requests.get` returning our test image."""
        from unittest.mock import MagicMock
        m = MagicMock()
        m.status_code = 200
        m.content = image_bytes
        return m

    def test_skip_logo_category(self):
        """Compositing onto a 'logo' design must be refused — would corrupt it."""
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        result = composite_logo_on_image_url(
            image_url='https://example.com/canvas.jpg',
            logo_source='https://example.com/logo.png',
            category='logo',
        )
        self.assertFalse(result['success'])
        self.assertTrue(result.get('skipped'))
        self.assertEqual(result.get('error'), 'category_excluded')

    def test_skip_illustration_and_character(self):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        for cat in ('illustration', 'character'):
            r = composite_logo_on_image_url(
                image_url='https://x/a.jpg',
                logo_source='https://x/l.png',
                category=cat,
            )
            self.assertTrue(r.get('skipped'), msg=f'{cat} should skip')

    def test_missing_image_url_errors_clean(self):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        r = composite_logo_on_image_url('', 'https://x/l.png', 'apparel')
        self.assertFalse(r['success'])
        self.assertEqual(r['error'], 'no_image_url')

    def test_missing_logo_source_errors_clean(self):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        r = composite_logo_on_image_url('https://x/a.jpg', '', 'apparel')
        self.assertFalse(r['success'])
        self.assertEqual(r['error'], 'no_logo_source')

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_apparel_composite_succeeds_and_returns_chest_placement(self, mock_get):
        """End-to-end: an apparel image gets a logo composited on left chest."""
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        # Two HTTP calls happen: one for canvas (image_url), one for logo
        mock_get.side_effect = [
            self._mock_requests_get(self._solid_jpeg_bytes((1024, 1024))),
            self._mock_requests_get(self._logo_png_bytes((300, 300))),
        ]
        result = composite_logo_on_image_url(
            image_url='https://example.com/tshirt.jpg',
            logo_source='https://example.com/brand.png',
            category='apparel',
        )
        self.assertTrue(result['success'], msg=result)
        self.assertEqual(result['placement'], 'chest_left')
        self.assertAlmostEqual(result['width_ratio'], 0.10, places=2)
        self.assertIn('.jpg', result['url'])

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_footwear_routes_to_side_panel(self, mock_get):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        mock_get.side_effect = [
            self._mock_requests_get(self._solid_jpeg_bytes()),
            self._mock_requests_get(self._logo_png_bytes()),
        ]
        result = composite_logo_on_image_url(
            image_url='https://x/sneaker.jpg',
            logo_source='https://x/brand.png',
            category='footwear',
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['placement'], 'side_panel')

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_unknown_category_uses_default_placement(self, mock_get):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        mock_get.side_effect = [
            self._mock_requests_get(self._solid_jpeg_bytes()),
            self._mock_requests_get(self._logo_png_bytes()),
        ]
        result = composite_logo_on_image_url(
            image_url='https://x/a.jpg',
            logo_source='https://x/l.png',
            category='something_unknown',
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['placement'], 'bottom_right')

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_text_overlay_at_chest_pushes_logo_away(self, mock_get):
        """If text is on the chest, logo must NOT overlap it."""
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        mock_get.side_effect = [
            self._mock_requests_get(self._solid_jpeg_bytes((1024, 1024))),
            self._mock_requests_get(self._logo_png_bytes((300, 300))),
        ]
        result = composite_logo_on_image_url(
            image_url='https://x/tshirt.jpg',
            logo_source='https://x/brand.png',
            category='apparel',
            text_overlay_position='chest',  # default apparel placement is also chest!
        )
        self.assertTrue(result['success'])
        # The rerouter should have detected overlap and flagged avoided=False
        # (means it had to MOVE the logo away from the original chest position)
        self.assertFalse(result['avoided_text'])

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_text_overlay_at_bottom_keeps_logo_at_default(self, mock_get):
        """Apparel default is chest_left — text at bottom does NOT collide."""
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        mock_get.side_effect = [
            self._mock_requests_get(self._solid_jpeg_bytes((1024, 1024))),
            self._mock_requests_get(self._logo_png_bytes((300, 300))),
        ]
        result = composite_logo_on_image_url(
            image_url='https://x/tshirt.jpg',
            logo_source='https://x/brand.png',
            category='apparel',
            text_overlay_position='bottom',
        )
        self.assertTrue(result['success'])
        self.assertTrue(result['avoided_text'])

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_canvas_http_failure_is_clean(self, mock_get):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        from unittest.mock import MagicMock
        bad = MagicMock(status_code=404, content=b'')
        mock_get.return_value = bad
        result = composite_logo_on_image_url(
            image_url='https://x/missing.jpg',
            logo_source='https://x/l.png',
            category='apparel',
        )
        self.assertFalse(result['success'])
        self.assertIn('canvas_http_404', result['error'])

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_width_ratio_override_is_clamped(self, mock_get):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        mock_get.side_effect = [
            self._mock_requests_get(self._solid_jpeg_bytes()),
            self._mock_requests_get(self._logo_png_bytes()),
        ]
        # Try to set absurdly large width — must clamp to 0.30
        result = composite_logo_on_image_url(
            image_url='https://x/a.jpg',
            logo_source='https://x/l.png',
            category='apparel',
            width_ratio_override=5.0,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['width_ratio'], 0.30)

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_position_override_respected(self, mock_get):
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        mock_get.side_effect = [
            self._mock_requests_get(self._solid_jpeg_bytes()),
            self._mock_requests_get(self._logo_png_bytes()),
        ]
        result = composite_logo_on_image_url(
            image_url='https://x/a.jpg',
            logo_source='https://x/l.png',
            category='apparel',
            position_override='top_left',
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['placement'], 'top_left')


# ===========================================================================
# L5b — SVG Logo Support (CairoSVG → PNG → PIL)
# ===========================================================================
class SVGLogoSupportTests(TestCase):
    """B2B merch customers ship SVG logos. They MUST composite, not silently
    fail. _load_logo_pil() rasterizes SVG via cairosvg before PIL.open."""

    _MINIMAL_SVG = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" '
        b'viewBox="0 0 200 200">'
        b'<rect width="200" height="200" fill="#7c3aed"/>'
        b'<circle cx="100" cy="100" r="60" fill="#ec4899"/>'
        b'</svg>'
    )

    def test_sniff_detects_svg_by_content(self):
        from erp_core.ai.logo_overlay import _sniff_svg
        self.assertTrue(_sniff_svg(self._MINIMAL_SVG))

    def test_sniff_detects_svg_with_bom(self):
        from erp_core.ai.logo_overlay import _sniff_svg
        bom_svg = b'\xef\xbb\xbf' + self._MINIMAL_SVG
        self.assertTrue(_sniff_svg(bom_svg))

    def test_sniff_detects_svg_by_extension_hint(self):
        from erp_core.ai.logo_overlay import _sniff_svg
        # Even if data is garbage, an .svg name hint should detect
        self.assertTrue(_sniff_svg(b'garbage', hint_name='brand.svg'))

    def test_sniff_rejects_png_bytes(self):
        from erp_core.ai.logo_overlay import _sniff_svg
        png_magic = b'\x89PNG\r\n\x1a\n' + b'\x00' * 50
        self.assertFalse(_sniff_svg(png_magic))

    def test_sniff_handles_empty(self):
        from erp_core.ai.logo_overlay import _sniff_svg
        self.assertFalse(_sniff_svg(b''))

    def test_bytes_to_pil_rasterizes_svg(self):
        """If cairosvg is installed, SVG bytes must produce a valid PIL image."""
        try:
            import cairosvg  # noqa: F401
            # cairosvg imports lazily — force libcairo dlopen so we skip
            # cleanly on machines where the dylib isn't on the loader path.
            cairosvg.svg2png(bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
        except (ImportError, OSError) as e:
            self.skipTest(f'cairosvg/libcairo not usable in this env: {e}')

        from erp_core.ai.logo_overlay import _bytes_to_pil
        img = _bytes_to_pil(self._MINIMAL_SVG, hint_name='brand.svg')
        self.assertIsNotNone(img)
        self.assertEqual(img.mode, 'RGBA')
        # Default raster width is 1024 — preserved aspect ratio for square SVG
        self.assertEqual(img.size, (1024, 1024))

    @patch('erp_core.ai.logo_overlay._svg_to_png_bytes')
    def test_bytes_to_pil_returns_none_when_cairosvg_missing(self, mock_svg2png):
        """If cairosvg can't rasterize (missing/broken), we return None
        and the design ships logo-less rather than crashing."""
        from erp_core.ai.logo_overlay import _bytes_to_pil
        mock_svg2png.return_value = None  # simulates ImportError or rasterize failure
        img = _bytes_to_pil(self._MINIMAL_SVG, hint_name='brand.svg')
        self.assertIsNone(img)

    @patch('erp_core.ai.logo_overlay.requests.get')
    def test_svg_logo_composites_end_to_end_on_apparel(self, mock_get):
        """Full pipeline: SVG logo URL → CairoSVG → PIL → composite onto apparel."""
        try:
            import cairosvg  # noqa: F401
            # cairosvg imports lazily — force libcairo dlopen so we skip
            # cleanly on machines where the dylib isn't on the loader path.
            cairosvg.svg2png(bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
        except (ImportError, OSError) as e:
            self.skipTest(f'cairosvg/libcairo not usable in this env: {e}')

        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        # Canvas response (JPEG), then logo response (SVG)
        from PIL import Image
        import io
        canvas_buf = io.BytesIO()
        Image.new('RGB', (1024, 1024), (220, 220, 220)).save(canvas_buf, 'JPEG')

        from unittest.mock import MagicMock
        canvas_resp = MagicMock(status_code=200, content=canvas_buf.getvalue())
        svg_resp = MagicMock(status_code=200, content=self._MINIMAL_SVG)
        mock_get.side_effect = [canvas_resp, svg_resp]

        result = composite_logo_on_image_url(
            image_url='https://example.com/tshirt.jpg',
            logo_source='https://example.com/brand.svg',
            category='apparel',
        )
        self.assertTrue(result['success'], msg=result)
        self.assertEqual(result['placement'], 'chest_left')

    @patch('erp_core.ai.logo_overlay.requests.get')
    @patch('erp_core.ai.logo_overlay._svg_to_png_bytes')
    def test_svg_composite_fails_gracefully_without_cairosvg(self, mock_svg2png, mock_get):
        """No cairosvg → SVG path returns clean error, no exception."""
        from erp_core.ai.logo_overlay import composite_logo_on_image_url
        from PIL import Image
        import io
        canvas_buf = io.BytesIO()
        Image.new('RGB', (800, 800), (200, 200, 200)).save(canvas_buf, 'JPEG')

        from unittest.mock import MagicMock
        mock_get.side_effect = [
            MagicMock(status_code=200, content=canvas_buf.getvalue()),
            MagicMock(status_code=200, content=self._MINIMAL_SVG),
        ]
        mock_svg2png.return_value = None  # simulate missing cairosvg

        result = composite_logo_on_image_url(
            image_url='https://x/a.jpg',
            logo_source='https://x/brand.svg',
            category='apparel',
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'logo_load_failed')


# ===========================================================================
# N.1 — DesignConversation / DesignConversationTurn model layer
# ===========================================================================
class DesignConversationModelTests(TestCase):
    """Smoke tests for the new conversational design models: FK semantics,
    JSON round-trip, lock detection, turn/image limit enforcement."""

    def _make_conv(self, phone='+201000000010', **overrides):
        from clients.models import DesignConversation
        customer = _make_customer(phone=phone)
        defaults = dict(
            customer=customer,
            stage='planning',
            accumulated_context={'raw_idea': 'تيشرت رياضي', 'selections': {}},
            brand_profile_snapshot={'brand_name': 'X'},
        )
        defaults.update(overrides)
        return DesignConversation.objects.create(**defaults)

    def test_create_minimal_conversation(self):
        conv = self._make_conv()
        self.assertIsNotNone(conv.conversation_code)
        self.assertEqual(conv.stage, 'planning')
        self.assertEqual(conv.turn_count, 0)
        self.assertEqual(conv.image_count, 0)
        self.assertTrue(conv.is_active)

    def test_accumulated_context_json_roundtrip(self):
        from clients.models import DesignConversation
        ctx = {
            'raw_idea': 'تيشرت',
            'selections': {'color_primary': 'navy', 'style': 'minimal'},
            'history': [{'turn': 1, 'patch': {'raw_idea': 'تيشرت'}}],
        }
        conv = self._make_conv(accumulated_context=ctx)
        conv.refresh_from_db()
        self.assertEqual(conv.accumulated_context['selections']['color_primary'], 'navy')
        self.assertEqual(len(conv.accumulated_context['history']), 1)

    def test_is_locked_respects_expiry(self):
        from django.utils import timezone
        from datetime import timedelta
        conv = self._make_conv()
        # Not locked initially
        self.assertFalse(conv.is_locked)
        # Lock 30s into the future
        conv.locked_until = timezone.now() + timedelta(seconds=30)
        conv.save(update_fields=['locked_until'])
        self.assertTrue(conv.is_locked)
        # Expired lock
        conv.locked_until = timezone.now() - timedelta(seconds=1)
        conv.save(update_fields=['locked_until'])
        self.assertFalse(conv.is_locked)

    def test_can_send_another_turn_blocks_at_turn_limit(self):
        from django.test import override_settings
        with override_settings(DESIGN_CHAT_MAX_TURNS=3):
            conv = self._make_conv()
            conv.turn_count = 3
            conv.save(update_fields=['turn_count'])
            allowed, reason = conv.can_send_another_turn()
            self.assertFalse(allowed)
            self.assertEqual(reason, 'max_turns_reached')

    def test_can_send_another_turn_blocks_at_image_limit(self):
        from django.test import override_settings
        with override_settings(DESIGN_CHAT_MAX_IMAGES=2):
            conv = self._make_conv()
            conv.image_count = 2
            conv.save(update_fields=['image_count'])
            allowed, reason = conv.can_send_another_turn()
            self.assertFalse(allowed)
            self.assertEqual(reason, 'max_images_reached')

    def test_can_send_another_turn_blocks_when_finalized(self):
        conv = self._make_conv(stage='finalized')
        allowed, reason = conv.can_send_another_turn()
        self.assertFalse(allowed)
        self.assertEqual(reason, 'closed')

    def test_can_send_another_turn_blocks_when_in_flight(self):
        from django.utils import timezone
        from datetime import timedelta
        conv = self._make_conv()
        conv.locked_until = timezone.now() + timedelta(seconds=20)
        conv.save(update_fields=['locked_until'])
        allowed, reason = conv.can_send_another_turn()
        self.assertFalse(allowed)
        self.assertEqual(reason, 'in_flight')

    def test_can_send_another_turn_allows_under_limits(self):
        conv = self._make_conv()
        allowed, reason = conv.can_send_another_turn()
        self.assertTrue(allowed)
        self.assertEqual(reason, '')

    def test_customer_cascade_deletes_conversation(self):
        """When a customer is deleted, their conversations cascade."""
        from clients.models import DesignConversation
        conv = self._make_conv()
        conv_id = conv.pk
        conv.customer.delete()
        self.assertFalse(DesignConversation.objects.filter(pk=conv_id).exists())

    def test_turn_creation_and_ordering(self):
        from clients.models import DesignConversationTurn
        conv = self._make_conv()
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='user',
            content='ابدأ بتيشرت أزرق',
            intent='chat', intent_confidence=0.85,
        )
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='assistant',
            content='تمام، يا تحب نضيف نص؟',
            intent='chat', intent_confidence=0.9,
        )
        turns = list(conv.turns.all())
        self.assertEqual(len(turns), 2)
        # ordering = [conversation, turn_index, created_at]
        self.assertEqual(turns[0].role, 'user')
        self.assertEqual(turns[1].role, 'assistant')

    def test_unique_constraint_blocks_duplicate_role_per_turn(self):
        """Same (conversation, turn_index, role) tuple cannot repeat."""
        from clients.models import DesignConversationTurn
        from django.db import IntegrityError
        conv = self._make_conv()
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='user', content='A',
        )
        with self.assertRaises(IntegrityError):
            DesignConversationTurn.objects.create(
                conversation=conv, turn_index=1, role='user', content='B',
            )

    def test_design_snapshot_set_null_on_design_delete(self):
        """Deleting a CustomerDesign nulls the snapshot FK — turn history
        survives for analytics even after the design is gone."""
        from clients.models import (
            DesignConversationTurn, CustomerDesign, MarketplaceCustomer,
        )
        conv = self._make_conv()
        design = CustomerDesign.objects.create(
            customer=conv.customer,
            title='Test design',
            description='Test',
            category='other',
        )
        turn = DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='assistant',
            content='Generated', intent='generate',
            design_snapshot=design,
        )
        design.delete()
        turn.refresh_from_db()
        self.assertIsNone(turn.design_snapshot)

    def test_context_patch_stores_intent_extracted_changes(self):
        from clients.models import DesignConversationTurn
        conv = self._make_conv()
        patch = {
            'selections.color_primary': 'navy',
            'selections.style': 'minimal',
        }
        turn = DesignConversationTurn.objects.create(
            conversation=conv, turn_index=2, role='user',
            content='خليه أزرق ومينيمال',
            intent='refine', intent_confidence=0.92,
            context_patch=patch,
        )
        turn.refresh_from_db()
        self.assertEqual(turn.context_patch['selections.color_primary'], 'navy')


# ===========================================================================
# N.2 — Intent Classifier (classify_chat_intent + apply_context_patch)
# ===========================================================================
def _make_llm_response(intent, confidence, extracted=None, reasoning='ok'):
    """Shape that _call_together_llm returns on success — used in mocks."""
    return {
        'success': True,
        'data': {
            'intent': intent,
            'confidence': confidence,
            'extracted_changes': extracted or {},
            'reasoning_brief': reasoning,
        },
        'model_used': 'meta-llama/Llama-3-8B-Instruct-Turbo',
    }


class IntentClassifierArabicTests(TestCase):
    """Arabic-language intent classification — the primary user surface."""

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_generate__make_a_tshirt(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('generate', 0.95)
        r = classify_chat_intent('اعمل لي تيشرت رياضي')
        self.assertEqual(r['intent'], 'generate')
        self.assertGreaterEqual(r['confidence'], 0.7)
        self.assertIsNone(r['fallback_reason'])

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_generate__start_a_poster(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('generate', 0.92)
        r = classify_chat_intent('ابدأ بتصميم بوستر للمطعم')
        self.assertEqual(r['intent'], 'generate')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_refine__change_color(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.93, {'color': 'navy'},
        )
        r = classify_chat_intent('غيّر اللون للأزرق', has_current_design=True)
        self.assertEqual(r['intent'], 'refine')
        self.assertEqual(r['extracted_changes']['color'], 'navy')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_refine__remove_text(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.90, {'remove_elements': ['text']},
        )
        r = classify_chat_intent('اشيل النص', has_current_design=True)
        self.assertEqual(r['intent'], 'refine')
        self.assertIn('text', r['extracted_changes']['remove_elements'])

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_refine__move_logo(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.88, {'position_change': 'right'},
        )
        r = classify_chat_intent('حرّك اللوجو لليمين', has_current_design=True)
        self.assertEqual(r['intent'], 'refine')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_chat__color_question(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('chat', 0.85)
        r = classify_chat_intent('إيه أحسن لون لمنتج فاخر؟')
        self.assertEqual(r['intent'], 'chat')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_chat__show_examples(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('chat', 0.80)
        r = classify_chat_intent('اعرض لي أمثلة')
        self.assertEqual(r['intent'], 'chat')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_chat__pricing_question(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('chat', 0.95)
        r = classify_chat_intent('كم تكلفة التصميم؟')
        self.assertEqual(r['intent'], 'chat')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_generate__generate_logo(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('generate', 0.97)
        r = classify_chat_intent('ولّد لوجو لشركة تكنولوجيا')
        self.assertEqual(r['intent'], 'generate')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_arabic_refine__make_smaller(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.85, {'size_change': 'smaller'},
        )
        r = classify_chat_intent('خليه أصغر شوية', has_current_design=True)
        self.assertEqual(r['intent'], 'refine')


class IntentClassifierEnglishAndCodeSwitchTests(TestCase):
    """English + Arabic-English code-switched messages."""

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_english_generate(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('generate', 0.93)
        r = classify_chat_intent('Generate a flyer for the restaurant')
        self.assertEqual(r['intent'], 'generate')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_english_refine_change_color(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.91, {'color': 'navy'},
        )
        r = classify_chat_intent('change the color to navy', has_current_design=True)
        self.assertEqual(r['intent'], 'refine')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_english_refine_make_minimal(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.89, {'style_change': 'minimal'},
        )
        r = classify_chat_intent('make it more minimal', has_current_design=True)
        self.assertEqual(r['intent'], 'refine')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_codeswitch_refine(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.86, {'color': 'navy'},
        )
        # Mixed: Arabic verb + English color
        r = classify_chat_intent('change the لون to navy', has_current_design=True)
        self.assertEqual(r['intent'], 'refine')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_english_chat__font_advice(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('chat', 0.88)
        r = classify_chat_intent('what fonts work best for restaurants?')
        self.assertEqual(r['intent'], 'chat')


class IntentClassifierEdgeCaseTests(TestCase):
    """The hard cases: ambiguity, errors, downgrades, threshold edges."""

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_low_confidence_falls_back_to_chat(self, mock_llm):
        """confidence < 0.7 → 'chat' regardless of raw intent."""
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('generate', 0.50)
        r = classify_chat_intent('خليه حلو')
        self.assertEqual(r['intent'], 'chat')
        self.assertEqual(r['raw_intent'], 'generate')
        self.assertEqual(r['fallback_reason'], 'low_confidence')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_exact_threshold_passes(self, mock_llm):
        """Confidence == 0.70 should pass (>= threshold)."""
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('generate', 0.70)
        r = classify_chat_intent('اعمل تصميم')
        self.assertEqual(r['intent'], 'generate')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_refine_without_current_design_downgrades_to_generate(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response(
            'refine', 0.90, {'color': 'navy'},
        )
        # has_current_design defaults to False
        r = classify_chat_intent('غيّر اللون للأزرق')
        self.assertEqual(r['intent'], 'generate')
        self.assertEqual(r['raw_intent'], 'refine')
        self.assertTrue(r['downgraded'])

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_classifier_llm_failure_returns_safe_chat(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = {'success': False, 'error': 'together_timeout'}
        r = classify_chat_intent('اعمل تصميم')
        self.assertEqual(r['intent'], 'chat')
        self.assertEqual(r['fallback_reason'], 'classifier_error')
        self.assertFalse(r['success'])

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_invalid_intent_string_falls_back(self, mock_llm):
        """If LLM hallucinates an intent like 'maybe_generate', fall back."""
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('maybe_generate', 0.95)
        r = classify_chat_intent('اعمل حاجة')
        self.assertEqual(r['intent'], 'chat')
        self.assertEqual(r['fallback_reason'], 'invalid_intent')
        self.assertEqual(r['raw_intent'], 'maybe_generate')

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_confidence_clamped_above_one(self, mock_llm):
        """LLM returns 1.5 → clamped to 1.0."""
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('generate', 1.5)
        r = classify_chat_intent('اعمل تصميم')
        self.assertEqual(r['confidence'], 1.0)

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_confidence_clamped_below_zero(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('chat', -0.3)
        r = classify_chat_intent('اعمل تصميم')
        self.assertEqual(r['confidence'], 0.0)

    def test_empty_message_returns_chat_without_llm_call(self):
        """Empty input shouldn't even reach the LLM."""
        from erp_core.ai.design_chat import classify_chat_intent
        r = classify_chat_intent('   ')
        self.assertEqual(r['intent'], 'chat')
        self.assertEqual(r['fallback_reason'], 'empty_message')
        self.assertEqual(r['cost_usd'], 0.0)

    @patch('erp_core.ai.design_chat._call_together_llm')
    def test_recent_turns_context_included_in_prompt(self, mock_llm):
        from erp_core.ai.design_chat import classify_chat_intent
        mock_llm.return_value = _make_llm_response('refine', 0.85)
        recent = [
            {'role': 'user', 'content': 'اعمل تيشرت'},
            {'role': 'assistant', 'content': 'اتعمل التصميم.'},
        ]
        classify_chat_intent(
            'دلوقتي خليه أزرق',
            has_current_design=True,
            recent_turns=recent,
        )
        args, _ = mock_llm.call_args
        user_msg = args[1]
        # Recent turn content should appear in the prompt
        self.assertIn('اعمل تيشرت', user_msg)
        self.assertIn('دلوقتي خليه أزرق', user_msg)


class ContextPatchTests(TestCase):
    """apply_context_patch — merges extracted_changes into accumulated_context."""

    def test_empty_patch_only_appends_history(self):
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {'selections': {}, 'history': []}
        new_ctx, applied = apply_context_patch(ctx, {}, turn_index=1)
        self.assertEqual(applied, {})
        self.assertEqual(len(new_ctx['history']), 1)
        self.assertEqual(new_ctx['history'][0]['turn'], 1)

    def test_color_change_updates_selections(self):
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {'selections': {'style': 'minimal'}, 'history': []}
        new_ctx, applied = apply_context_patch(
            ctx, {'color': 'navy'}, turn_index=2,
        )
        self.assertEqual(new_ctx['selections']['color_primary'], 'navy')
        self.assertEqual(new_ctx['selections']['style'], 'minimal')  # preserved
        self.assertEqual(applied['selections.color_primary'], 'navy')

    def test_explicit_override_replaces_prior_value(self):
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {'selections': {'color_primary': 'red'}, 'history': []}
        new_ctx, _ = apply_context_patch(
            ctx, {'color': 'blue'}, turn_index=2,
        )
        # New explicit choice WINS — explicit selections always do
        self.assertEqual(new_ctx['selections']['color_primary'], 'blue')

    def test_remove_elements_accumulates(self):
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {
            'selections': {'remove_elements': ['text']},
            'history': [],
        }
        new_ctx, _ = apply_context_patch(
            ctx, {'remove_elements': ['logo']}, turn_index=3,
        )
        self.assertEqual(
            sorted(new_ctx['selections']['remove_elements']),
            ['logo', 'text'],
        )

    def test_remove_elements_dedupes(self):
        """Same element requested twice doesn't duplicate."""
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {'selections': {'remove_elements': ['text']}, 'history': []}
        new_ctx, _ = apply_context_patch(
            ctx, {'remove_elements': ['text', 'shadow']}, turn_index=2,
        )
        self.assertEqual(
            sorted(new_ctx['selections']['remove_elements']),
            ['shadow', 'text'],
        )

    def test_null_values_dont_clear_existing(self):
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {'selections': {'color_primary': 'red'}, 'history': []}
        new_ctx, applied = apply_context_patch(
            ctx, {'color': None, 'style_change': ''}, turn_index=2,
        )
        # Existing red survives — null doesn't wipe it
        self.assertEqual(new_ctx['selections']['color_primary'], 'red')
        self.assertEqual(applied, {})

    def test_other_field_appends_to_extra_notes(self):
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {'selections': {}, 'history': []}
        new_ctx, _ = apply_context_patch(
            ctx, {'other': 'avoid bright colors'}, turn_index=2,
        )
        self.assertIn('avoid bright colors', new_ctx['selections']['extra_notes'])

    def test_history_accumulates_per_turn(self):
        from erp_core.ai.design_chat import apply_context_patch
        ctx = {'selections': {}, 'history': []}
        ctx, _ = apply_context_patch(ctx, {'color': 'red'}, turn_index=1)
        ctx, _ = apply_context_patch(ctx, {'color': 'blue'}, turn_index=2)
        ctx, _ = apply_context_patch(ctx, {'style_change': 'minimal'}, turn_index=3)
        self.assertEqual(len(ctx['history']), 3)
        self.assertEqual([h['turn'] for h in ctx['history']], [1, 2, 3])

    def test_input_dict_not_mutated(self):
        """apply_context_patch returns a copy — original ctx unchanged."""
        from erp_core.ai.design_chat import apply_context_patch
        original = {'selections': {'color_primary': 'red'}, 'history': []}
        import json
        snapshot = json.dumps(original)
        apply_context_patch(original, {'color': 'blue'}, turn_index=1)
        self.assertEqual(json.dumps(original), snapshot)


# ===========================================================================
# N.3 — Orchestrator HTTP endpoints
# ===========================================================================
from django.test import Client as DjangoClient, override_settings


def _authed_client(customer):
    """Return a Django test Client with the mp_session cookie set."""
    c = DjangoClient()
    c.cookies['mp_session'] = str(customer.session_token)
    return c


class _TenantDomainProvisionMixin:
    """django-tenants' TenantMainMiddleware bounces requests whose hostname
    doesn't map to a registered Domain row → returns 404. The Django test
    Client defaults to host='testserver', which isn't in any tenant's
    domains. We provision a `testserver` Domain pointing at the public
    schema tenant for the lifetime of the test class.

    Marketplace endpoints live on the public schema, so this is sufficient.
    """
    _provisioned_domain = None
    _provisioned_tenant = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_model, get_tenant_domain_model
        TenantModel = get_tenant_model()
        DomainModel = get_tenant_domain_model()
        # Reuse or create the public tenant
        public = TenantModel.objects.filter(schema_name='public').first()
        if public is None:
            public = TenantModel(
                schema_name='public', name='Public', owner_name='Test',
                phone='000',
            )
            public.auto_create_schema = False
            public.save(verbosity=0)
            cls._provisioned_tenant = public
        # Add testserver domain if missing
        existing = DomainModel.objects.filter(domain='testserver').first()
        if existing is None:
            cls._provisioned_domain = DomainModel.objects.create(
                tenant=public, domain='testserver', is_primary=False,
            )

    @classmethod
    def tearDownClass(cls):
        if cls._provisioned_domain is not None:
            try:
                cls._provisioned_domain.delete()
            except Exception:
                pass
        if cls._provisioned_tenant is not None:
            try:
                cls._provisioned_tenant.delete(force_drop=False)
            except Exception:
                pass
        super().tearDownClass()


@override_settings(DESIGN_CHAT_ENABLED=True)
class DesignChatStartTests(_TenantDomainProvisionMixin, TestCase):

    def test_disabled_flag_returns_404(self):
        from django.test import override_settings as _os
        with _os(DESIGN_CHAT_ENABLED=False):
            customer = _make_customer()
            c = _authed_client(customer)
            r = c.post(
                '/marketplace/design-chat/start/',
                data='{}', content_type='application/json',
            )
            self.assertEqual(r.status_code, 404)

    def test_unauthenticated_returns_401(self):
        c = DjangoClient()
        r = c.post(
            '/marketplace/design-chat/start/',
            data='{}', content_type='application/json',
        )
        self.assertEqual(r.status_code, 401)

    def test_creates_conversation_with_brand_snapshot(self):
        customer = _make_customer()
        _make_brand(customer)
        c = _authed_client(customer)
        r = c.post(
            '/marketplace/design-chat/start/',
            data=json.dumps({'initial_message': 'تيشرت رياضي'}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertIn('conversation_code', data)
        self.assertTrue(data['brand_applied'])
        self.assertEqual(data['turn_count'], 1)

    def test_no_initial_message_creates_empty_conversation(self):
        customer = _make_customer()
        c = _authed_client(customer)
        r = c.post(
            '/marketplace/design-chat/start/',
            data='{}', content_type='application/json',
        )
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertEqual(data['turn_count'], 0)
        self.assertFalse(data['brand_applied'])


@override_settings(DESIGN_CHAT_ENABLED=True)
class DesignChatMessageTests(_TenantDomainProvisionMixin, TestCase):

    def _start_conv(self, customer):
        from clients.models import DesignConversation
        return DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={'raw_idea': '', 'selections': {}, 'history': []},
            brand_profile_snapshot={},
        )

    def test_disabled_flag_returns_404(self):
        from django.test import override_settings as _os
        with _os(DESIGN_CHAT_ENABLED=False):
            customer = _make_customer()
            conv = self._start_conv(customer)
            c = _authed_client(customer)
            r = c.post(
                f'/marketplace/design-chat/{conv.conversation_code}/message/',
                data=json.dumps({'message': 'hi'}),
                content_type='application/json',
            )
            self.assertEqual(r.status_code, 404)

    def test_other_customer_conversation_returns_404_not_403(self):
        """Existence-leak protection — wrong owner = 404."""
        c1 = _make_customer(phone='+201000000020')
        c2 = _make_customer(phone='+201000000021')
        conv = self._start_conv(c1)
        client = _authed_client(c2)
        r = client.post(
            f'/marketplace/design-chat/{conv.conversation_code}/message/',
            data=json.dumps({'message': 'hi'}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 404)

    def test_empty_message_returns_400(self):
        customer = _make_customer()
        conv = self._start_conv(customer)
        c = _authed_client(customer)
        r = c.post(
            f'/marketplace/design-chat/{conv.conversation_code}/message/',
            data=json.dumps({'message': '   '}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 400)

    @patch('clients.views.design_chat_views.classify_chat_intent')
    @patch('clients.views.design_chat_views.generate_chat_reply')
    def test_chat_intent_creates_two_turns_no_image(self, mock_reply, mock_classify):
        from clients.models import DesignConversationTurn
        mock_classify.return_value = {
            'intent': 'chat', 'confidence': 0.85,
            'extracted_changes': {}, 'reasoning_brief': '',
            'raw_intent': 'chat', 'downgraded': False,
            'fallback_reason': None, 'cost_usd': 0.0001, 'success': True,
        }
        mock_reply.return_value = {
            'success': True, 'reply': 'تمام، إيه القطاع بتاعك؟',
            'suggested_next': None, 'cost_usd': 0.0002,
        }
        customer = _make_customer()
        conv = self._start_conv(customer)
        c = _authed_client(customer)
        r = c.post(
            f'/marketplace/design-chat/{conv.conversation_code}/message/',
            data=json.dumps({'message': 'أنا مش متأكد من اللون'}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data['intent'], 'chat')
        self.assertIsNone(data['image_url'])
        # User + assistant turn both at turn_index=1 (paired by UniqueConstraint)
        conv.refresh_from_db()
        self.assertEqual(conv.turn_count, 1)
        self.assertEqual(DesignConversationTurn.objects.filter(conversation=conv).count(), 2)

    @patch('clients.views.design_chat_views.classify_chat_intent')
    @patch('clients.views.design_chat_views.compose_mega_prompt', create=True)
    @patch('clients.views.design_chat_views.generate_design_image', create=True)
    def test_generate_intent_creates_customerdesign_and_links(
        self, mock_gen, mock_compose, mock_classify,
    ):
        # Patch the internal imports inside _exec_generate
        from erp_core.ai import design_engine, printing_copilot
        mock_classify.return_value = {
            'intent': 'generate', 'confidence': 0.95,
            'extracted_changes': {}, 'reasoning_brief': '',
            'raw_intent': 'generate', 'downgraded': False,
            'fallback_reason': None, 'cost_usd': 0.0001, 'success': True,
        }
        with patch.object(design_engine, 'compose_mega_prompt') as mc, \
             patch.object(printing_copilot, 'generate_design_image') as mg:
            mc.return_value = {
                'success': True,
                'mega_prompt': 'A modern tshirt design.',
                'negative_prompt': '',
                'recommended_size': '1024x1024',
                'presentation_category': 'apparel',
                'brand_applied': {'applied': False},
            }
            mg.return_value = {
                'success': True,
                'url': 'https://cdn.test/generated.jpg',
                'engine': 'flux',
                'provider': 'together',
            }

            customer = _make_customer()
            conv = self._start_conv(customer)
            conv.accumulated_context = {
                'raw_idea': 'تيشرت أزرق', 'selections': {}, 'history': [],
            }
            conv.save()
            c = _authed_client(customer)
            r = c.post(
                f'/marketplace/design-chat/{conv.conversation_code}/message/',
                data=json.dumps({'message': 'اعمل تيشرت رياضي'}),
                content_type='application/json',
            )
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data['intent'], 'generate')
            self.assertEqual(data['image_url'], 'https://cdn.test/generated.jpg')
            self.assertIsNotNone(data['design_id'])
            self.assertEqual(data['stage'], 'generated')
            conv.refresh_from_db()
            self.assertIsNotNone(conv.current_design)
            self.assertEqual(conv.image_count, 1)

    @patch('clients.views.design_chat_views.classify_chat_intent')
    def test_refine_intent_with_current_design_calls_kontext(self, mock_classify):
        from erp_core.ai import printing_copilot
        from clients.models import CustomerDesign
        mock_classify.return_value = {
            'intent': 'refine', 'confidence': 0.91,
            'extracted_changes': {'color': 'navy'},
            'reasoning_brief': '', 'raw_intent': 'refine',
            'downgraded': False, 'fallback_reason': None,
            'cost_usd': 0.0001, 'success': True,
        }
        customer = _make_customer()
        prior_design = CustomerDesign.objects.create(
            customer=customer, title='Prior', description='', category='other',
            image_url='https://cdn.test/prior.jpg', model_used='flux',
        )
        conv = self._start_conv(customer)
        conv.current_design = prior_design
        conv.stage = 'generated'
        conv.image_count = 1
        conv.save()

        with patch.object(printing_copilot, '_gen_via_flux_kontext') as mk:
            mk.return_value = {
                'success': True,
                'url': 'https://cdn.test/refined.jpg',
            }
            c = _authed_client(customer)
            r = c.post(
                f'/marketplace/design-chat/{conv.conversation_code}/message/',
                data=json.dumps({'message': 'غيّر اللون للأزرق'}),
                content_type='application/json',
            )
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(data['intent'], 'refine')
            self.assertEqual(data['image_url'], 'https://cdn.test/refined.jpg')
            self.assertEqual(data['engine_used'], 'kontext')
            conv.refresh_from_db()
            self.assertEqual(conv.image_count, 2)
            self.assertEqual(conv.stage, 'refining')
            # Prior design preserved as a turn snapshot — survives for undo
            self.assertNotEqual(conv.current_design.pk, prior_design.pk)

    def test_explicit_intent_override_bypasses_classifier(self):
        """When intent='chat' is in the POST body, classifier is NOT called."""
        with patch('clients.views.design_chat_views.classify_chat_intent') as mc, \
             patch('clients.views.design_chat_views.generate_chat_reply') as mr:
            mr.return_value = {
                'success': True, 'reply': 'OK', 'suggested_next': None,
                'cost_usd': 0.0,
            }
            customer = _make_customer()
            conv = self._start_conv(customer)
            c = _authed_client(customer)
            r = c.post(
                f'/marketplace/design-chat/{conv.conversation_code}/message/',
                data=json.dumps({'message': 'اعمل تيشرت', 'intent': 'chat'}),
                content_type='application/json',
            )
            self.assertEqual(r.status_code, 200)
            self.assertFalse(mc.called)  # classifier bypassed
            self.assertTrue(mr.called)

    def test_turn_limit_returns_429(self):
        with override_settings(DESIGN_CHAT_MAX_TURNS=3):
            customer = _make_customer()
            conv = self._start_conv(customer)
            conv.turn_count = 3
            conv.save(update_fields=['turn_count'])
            c = _authed_client(customer)
            r = c.post(
                f'/marketplace/design-chat/{conv.conversation_code}/message/',
                data=json.dumps({'message': 'hi'}),
                content_type='application/json',
            )
            self.assertEqual(r.status_code, 429)
            self.assertEqual(r.json()['error'], 'turn_limit_reached')

    def test_in_flight_lock_returns_409(self):
        from datetime import timedelta
        customer = _make_customer()
        conv = self._start_conv(customer)
        conv.locked_until = timezone.now() + timedelta(seconds=30)
        conv.save(update_fields=['locked_until'])
        c = _authed_client(customer)
        r = c.post(
            f'/marketplace/design-chat/{conv.conversation_code}/message/',
            data=json.dumps({'message': 'hi'}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 409)

    def test_finalized_conversation_returns_410(self):
        customer = _make_customer()
        conv = self._start_conv(customer)
        conv.stage = 'finalized'
        conv.save(update_fields=['stage'])
        c = _authed_client(customer)
        r = c.post(
            f'/marketplace/design-chat/{conv.conversation_code}/message/',
            data=json.dumps({'message': 'hi'}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 410)


@override_settings(DESIGN_CHAT_ENABLED=True)
class DesignChatUndoTests(_TenantDomainProvisionMixin, TestCase):

    def test_nothing_to_undo_returns_400(self):
        from clients.models import DesignConversation
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        c = _authed_client(customer)
        r = c.post(f'/marketplace/design-chat/{conv.conversation_code}/undo/')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'nothing_to_undo')

    def test_undo_reverts_to_prior_design(self):
        from clients.models import (
            CustomerDesign, DesignConversation, DesignConversationTurn,
        )
        customer = _make_customer()
        d1 = CustomerDesign.objects.create(
            customer=customer, title='D1', description='', category='other',
            image_url='https://cdn.test/d1.jpg',
        )
        d2 = CustomerDesign.objects.create(
            customer=customer, title='D2', description='', category='other',
            image_url='https://cdn.test/d2.jpg',
        )
        conv = DesignConversation.objects.create(
            customer=customer, stage='refining',
            accumulated_context={}, brand_profile_snapshot={},
            current_design=d2, turn_count=2, image_count=2,
        )
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='assistant',
            content='generated', intent='generate', design_snapshot=d1,
        )
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=2, role='assistant',
            content='refined', intent='refine', design_snapshot=d2,
        )
        c = _authed_client(customer)
        r = c.post(f'/marketplace/design-chat/{conv.conversation_code}/undo/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['image_url'], 'https://cdn.test/d1.jpg')
        conv.refresh_from_db()
        self.assertEqual(conv.current_design.pk, d1.pk)

    def test_undo_on_finalized_returns_410(self):
        from clients.models import DesignConversation
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='finalized',
            accumulated_context={}, brand_profile_snapshot={},
        )
        c = _authed_client(customer)
        r = c.post(f'/marketplace/design-chat/{conv.conversation_code}/undo/')
        self.assertEqual(r.status_code, 410)


@override_settings(DESIGN_CHAT_ENABLED=True)
class DesignChatFinalizeTests(_TenantDomainProvisionMixin, TestCase):

    def test_finalize_without_design_returns_400(self):
        from clients.models import DesignConversation
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        c = _authed_client(customer)
        r = c.post(f'/marketplace/design-chat/{conv.conversation_code}/finalize/')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()['error'], 'no_design_to_finalize')

    def test_finalize_sets_stage_and_timestamp(self):
        from clients.models import CustomerDesign, DesignConversation
        customer = _make_customer()
        d = CustomerDesign.objects.create(
            customer=customer, title='X', description='', category='other',
            image_url='https://cdn.test/x.jpg',
        )
        conv = DesignConversation.objects.create(
            customer=customer, stage='generated',
            accumulated_context={}, brand_profile_snapshot={},
            current_design=d,
        )
        c = _authed_client(customer)
        r = c.post(f'/marketplace/design-chat/{conv.conversation_code}/finalize/')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data['stage'], 'finalized')
        self.assertEqual(data['design_id'], d.pk)
        conv.refresh_from_db()
        self.assertEqual(conv.stage, 'finalized')
        self.assertIsNotNone(conv.finalized_at)

    def test_already_finalized_returns_410(self):
        from clients.models import DesignConversation
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='finalized',
            accumulated_context={}, brand_profile_snapshot={},
        )
        c = _authed_client(customer)
        r = c.post(f'/marketplace/design-chat/{conv.conversation_code}/finalize/')
        self.assertEqual(r.status_code, 410)


@override_settings(DESIGN_CHAT_ENABLED=True)
class DesignChatStateTests(_TenantDomainProvisionMixin, TestCase):

    def test_state_returns_full_transcript(self):
        from clients.models import (
            DesignConversation, DesignConversationTurn,
        )
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={'brand_name': 'X'},
            turn_count=1,
        )
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='user',
            content='ابدأ تيشرت', intent='chat', intent_confidence=0.8,
        )
        c = _authed_client(customer)
        r = c.get(f'/marketplace/design-chat/{conv.conversation_code}/')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data['stage'], 'planning')
        self.assertEqual(len(data['turns']), 1)
        self.assertEqual(data['turns'][0]['content'], 'ابدأ تيشرت')
        self.assertTrue(data['brand_applied'])
        self.assertFalse(data['can_finalize'])  # no current_design

    def test_state_unauthenticated_returns_401(self):
        from clients.models import DesignConversation
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        c = DjangoClient()  # no cookie
        r = c.get(f'/marketplace/design-chat/{conv.conversation_code}/')
        self.assertEqual(r.status_code, 401)


@override_settings(DESIGN_CHAT_ENABLED=True)
class DesignChatLockRaceTests(_TenantDomainProvisionMixin, TestCase):
    """Verifies the advisory lock prevents concurrent-turn corruption."""

    def test_concurrent_lock_acquire_only_one_wins(self):
        """Atomic _acquire_lock returns True for exactly one caller."""
        from clients.models import DesignConversation
        from clients.views.design_chat_views import _acquire_lock
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        first = _acquire_lock(conv)
        # Reload fresh — simulate a parallel request hitting the same row
        conv2 = DesignConversation.objects.get(pk=conv.pk)
        second = _acquire_lock(conv2)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_expired_lock_can_be_reacquired(self):
        from datetime import timedelta
        from clients.models import DesignConversation
        from clients.views.design_chat_views import _acquire_lock
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
            locked_until=timezone.now() - timedelta(seconds=10),
        )
        self.assertTrue(_acquire_lock(conv))


# ===========================================================================
# N.4 — Page render view
# ===========================================================================
class DesignChatPageRenderTests(_TenantDomainProvisionMixin, TestCase):
    """The HTML shell that consumes the 5 API endpoints."""

    @override_settings(DESIGN_CHAT_ENABLED=False)
    def test_disabled_flag_returns_404(self):
        customer = _make_customer()
        c = _authed_client(customer)
        r = c.get('/marketplace/design-chat/')
        self.assertEqual(r.status_code, 404)

    @override_settings(DESIGN_CHAT_ENABLED=True)
    def test_unauthenticated_redirects_to_marketplace(self):
        c = DjangoClient()
        r = c.get('/marketplace/design-chat/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/marketplace/', r.url)

    @override_settings(DESIGN_CHAT_ENABLED=True)
    def test_authenticated_renders_html_shell(self):
        customer = _make_customer()
        c = _authed_client(customer)
        r = c.get('/marketplace/design-chat/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode('utf-8')
        # Sanity-check the shell loaded with key markers
        self.assertIn('محادثة التصميم', html)
        self.assertIn('id="transcript"', html)
        self.assertIn('id="canvas-img"', html)
        self.assertIn('/marketplace/design-chat', html)  # API base
        self.assertIn('mp_design_chat_active', html)     # localStorage key
        # All 5 API endpoints reachable from the template's JS
        for path in ('/start/', '/message/', '/undo/', '/finalize/'):
            self.assertIn(path, html)

    @override_settings(DESIGN_CHAT_ENABLED=True)
    def test_render_includes_rtl_direction(self):
        """RTL is critical for chat bubble alignment."""
        customer = _make_customer()
        c = _authed_client(customer)
        r = c.get('/marketplace/design-chat/')
        self.assertIn('dir="rtl"', r.content.decode('utf-8'))


# ===========================================================================
# N.5 — Resume banner / "from chat" badge / stale-prune service + command
# ===========================================================================
class GetActiveConversationTests(TestCase):
    """The resume-banner lookup: most recent non-terminal conv within idle window."""

    def test_returns_none_when_no_conversations(self):
        from clients.services.design_chat import get_active_conversation
        customer = _make_customer()
        self.assertIsNone(get_active_conversation(customer))

    def test_returns_none_when_customer_is_none(self):
        from clients.services.design_chat import get_active_conversation
        self.assertIsNone(get_active_conversation(None))

    def test_returns_most_recent_active_conversation(self):
        from clients.models import DesignConversation
        from clients.services.design_chat import get_active_conversation
        customer = _make_customer()
        DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        latest = DesignConversation.objects.create(
            customer=customer, stage='generated',
            accumulated_context={}, brand_profile_snapshot={},
        )
        result = get_active_conversation(customer)
        self.assertEqual(result.pk, latest.pk)

    def test_excludes_finalized_and_abandoned(self):
        from clients.models import DesignConversation
        from clients.services.design_chat import get_active_conversation
        customer = _make_customer()
        DesignConversation.objects.create(
            customer=customer, stage='finalized',
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversation.objects.create(
            customer=customer, stage='abandoned',
            accumulated_context={}, brand_profile_snapshot={},
        )
        self.assertIsNone(get_active_conversation(customer))

    def test_excludes_stale_conversations_beyond_idle_window(self):
        """Idle > DESIGN_CHAT_IDLE_MINUTES → don't surface, even if not abandoned."""
        from clients.models import DesignConversation
        from clients.services.design_chat import get_active_conversation
        from datetime import timedelta
        customer = _make_customer()
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        # Bypass auto_now by direct UPDATE
        DesignConversation.objects.filter(pk=conv.pk).update(
            updated_at=timezone.now() - timedelta(minutes=90),
        )
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            self.assertIsNone(get_active_conversation(customer))

    def test_scoped_to_customer(self):
        from clients.models import DesignConversation
        from clients.services.design_chat import get_active_conversation
        c1 = _make_customer(phone='+201000000030')
        c2 = _make_customer(phone='+201000000031')
        DesignConversation.objects.create(
            customer=c1, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        # c2 has no conversations — must not see c1's
        self.assertIsNone(get_active_conversation(c2))


class AnnotateDesignsFromChatTests(TestCase):
    """The 'Generated via Chat' annotation: True iff any turn references the design."""

    def test_design_with_no_turn_reference_is_false(self):
        from clients.models import CustomerDesign
        from clients.services.design_chat import annotate_designs_from_chat
        customer = _make_customer()
        d = CustomerDesign.objects.create(
            customer=customer, title='Manual', description='', category='other',
            image_url='https://x/d.jpg',
        )
        result = list(annotate_designs_from_chat(CustomerDesign.objects.filter(pk=d.pk)))
        self.assertFalse(result[0].from_conversation)

    def test_design_referenced_by_turn_is_true(self):
        from clients.models import (
            CustomerDesign, DesignConversation, DesignConversationTurn,
        )
        from clients.services.design_chat import annotate_designs_from_chat
        customer = _make_customer()
        d = CustomerDesign.objects.create(
            customer=customer, title='Chat-made', description='', category='other',
            image_url='https://x/d.jpg',
        )
        conv = DesignConversation.objects.create(
            customer=customer, stage='generated',
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='assistant',
            content='generated', intent='generate', design_snapshot=d,
        )
        result = list(annotate_designs_from_chat(CustomerDesign.objects.filter(pk=d.pk)))
        self.assertTrue(result[0].from_conversation)

    def test_mixed_queryset_annotates_correctly(self):
        from clients.models import (
            CustomerDesign, DesignConversation, DesignConversationTurn,
        )
        from clients.services.design_chat import annotate_designs_from_chat
        customer = _make_customer()
        manual = CustomerDesign.objects.create(
            customer=customer, title='Manual', description='', category='other',
            image_url='https://x/m.jpg',
        )
        chat = CustomerDesign.objects.create(
            customer=customer, title='Chat', description='', category='other',
            image_url='https://x/c.jpg',
        )
        conv = DesignConversation.objects.create(
            customer=customer, stage='generated',
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='assistant',
            content='', intent='generate', design_snapshot=chat,
        )
        result = {
            d.pk: d.from_conversation
            for d in annotate_designs_from_chat(
                CustomerDesign.objects.filter(customer=customer)
            )
        }
        self.assertFalse(result[manual.pk])
        self.assertTrue(result[chat.pk])


class PruneStaleConversationsTests(TestCase):
    """The core prune logic — shared by management command and lazy cleanup."""

    def _make_stale_conv(self, customer, stage='planning', minutes_old=120):
        from clients.models import DesignConversation
        from datetime import timedelta
        conv = DesignConversation.objects.create(
            customer=customer, stage=stage,
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversation.objects.filter(pk=conv.pk).update(
            updated_at=timezone.now() - timedelta(minutes=minutes_old),
        )
        return conv

    def test_abandons_stale_planning_conversations(self):
        from clients.models import DesignConversation
        from clients.services.design_chat import prune_stale_conversations
        customer = _make_customer()
        stale = self._make_stale_conv(customer)
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            result = prune_stale_conversations()
        self.assertEqual(result['abandoned'], 1)
        stale.refresh_from_db()
        self.assertEqual(stale.stage, 'abandoned')
        self.assertIsNotNone(stale.abandoned_at)

    def test_dry_run_does_not_mutate(self):
        from clients.services.design_chat import prune_stale_conversations
        customer = _make_customer()
        stale = self._make_stale_conv(customer)
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            result = prune_stale_conversations(dry_run=True)
        self.assertEqual(result['abandoned'], 0)
        self.assertEqual(result['inspected'], 1)
        self.assertTrue(result['dry_run'])
        stale.refresh_from_db()
        self.assertEqual(stale.stage, 'planning')  # unchanged

    def test_skips_fresh_conversations(self):
        from clients.models import DesignConversation
        from clients.services.design_chat import prune_stale_conversations
        customer = _make_customer()
        fresh = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            result = prune_stale_conversations()
        self.assertEqual(result['abandoned'], 0)
        fresh.refresh_from_db()
        self.assertEqual(fresh.stage, 'planning')

    def test_skips_already_terminal_stages(self):
        from clients.services.design_chat import prune_stale_conversations
        customer = _make_customer()
        self._make_stale_conv(customer, stage='finalized')
        self._make_stale_conv(customer, stage='abandoned')
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            result = prune_stale_conversations()
        self.assertEqual(result['abandoned'], 0)

    def test_handles_generated_and_refining_stages(self):
        from clients.services.design_chat import prune_stale_conversations
        customer = _make_customer()
        self._make_stale_conv(customer, stage='generated')
        self._make_stale_conv(customer, stage='refining')
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            result = prune_stale_conversations()
        self.assertEqual(result['abandoned'], 2)
        self.assertEqual(result['by_stage']['generated'], 1)
        self.assertEqual(result['by_stage']['refining'], 1)

    def test_clears_lingering_locks_on_abandon(self):
        from datetime import timedelta
        from clients.models import DesignConversation
        from clients.services.design_chat import prune_stale_conversations
        customer = _make_customer()
        conv = self._make_stale_conv(customer)
        DesignConversation.objects.filter(pk=conv.pk).update(
            locked_until=timezone.now() + timedelta(hours=1),
        )
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            prune_stale_conversations()
        conv.refresh_from_db()
        self.assertIsNone(conv.locked_until)

    def test_idempotent_repeat_runs(self):
        from clients.services.design_chat import prune_stale_conversations
        customer = _make_customer()
        self._make_stale_conv(customer)
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            r1 = prune_stale_conversations()
            r2 = prune_stale_conversations()
        self.assertEqual(r1['abandoned'], 1)
        self.assertEqual(r2['abandoned'], 0)  # already abandoned

    def test_scoped_to_customer(self):
        from clients.services.design_chat import prune_stale_conversations
        c1 = _make_customer(phone='+201000000040')
        c2 = _make_customer(phone='+201000000041')
        s1 = self._make_stale_conv(c1)
        s2 = self._make_stale_conv(c2)
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            result = prune_stale_conversations(customer=c1)
        self.assertEqual(result['abandoned'], 1)
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertEqual(s1.stage, 'abandoned')
        self.assertEqual(s2.stage, 'planning')  # untouched


@override_settings(DESIGN_CHAT_ENABLED=True)
class ResumeBannerAndBadgeIntegrationTests(_TenantDomainProvisionMixin, TestCase):
    """End-to-end: design_store_my view passes active_conversation + annotated
    designs to the template, and the template renders banner + badge."""

    def test_no_active_conv_renders_no_banner(self):
        customer = _make_customer()
        c = _authed_client(customer)
        r = c.get('/marketplace/design-store/my/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode('utf-8')
        self.assertNotIn('عندك محادثة تصميم شغّالة', html)

    def test_active_conv_renders_resume_banner(self):
        from clients.models import DesignConversation
        customer = _make_customer()
        DesignConversation.objects.create(
            customer=customer, stage='generated',
            accumulated_context={}, brand_profile_snapshot={},
            turn_count=4, image_count=2,
        )
        c = _authed_client(customer)
        r = c.get('/marketplace/design-store/my/')
        html = r.content.decode('utf-8')
        self.assertIn('عندك محادثة تصميم شغّالة', html)
        self.assertIn('/marketplace/design-chat/', html)
        self.assertIn('4 تيرنات', html)
        self.assertIn('2 صور', html)

    @override_settings(DESIGN_CHAT_ENABLED=False)
    def test_feature_flag_off_hides_banner_even_with_active_conv(self):
        """Don't tempt users into a feature that's flag-disabled."""
        from clients.models import DesignConversation
        customer = _make_customer()
        DesignConversation.objects.create(
            customer=customer, stage='generated',
            accumulated_context={}, brand_profile_snapshot={},
        )
        c = _authed_client(customer)
        r = c.get('/marketplace/design-store/my/')
        html = r.content.decode('utf-8')
        self.assertNotIn('عندك محادثة تصميم شغّالة', html)

    def test_chat_generated_design_renders_badge(self):
        from clients.models import (
            CustomerDesign, DesignConversation, DesignConversationTurn,
        )
        customer = _make_customer()
        d = CustomerDesign.objects.create(
            customer=customer, title='Chat tee', description='', category='other',
            image_url='https://cdn.test/d.jpg',
        )
        conv = DesignConversation.objects.create(
            customer=customer, stage='generated',
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='assistant',
            content='made', intent='generate', design_snapshot=d,
        )
        c = _authed_client(customer)
        r = c.get('/marketplace/design-store/my/')
        html = r.content.decode('utf-8')
        self.assertIn('من المحادثة', html)

    def test_manual_design_does_not_render_chat_badge(self):
        from clients.models import CustomerDesign
        customer = _make_customer()
        CustomerDesign.objects.create(
            customer=customer, title='Manual tee', description='', category='other',
            image_url='https://cdn.test/m.jpg',
        )
        c = _authed_client(customer)
        r = c.get('/marketplace/design-store/my/')
        html = r.content.decode('utf-8')
        self.assertNotIn('من المحادثة', html)


@override_settings(DESIGN_CHAT_ENABLED=True)
class LazyPruneOnStartTests(_TenantDomainProvisionMixin, TestCase):
    """design_chat_start should abandon this customer's stale convs before
    creating a new one — keeps the get_active_conversation read consistent."""

    def test_start_abandons_stale_conv_for_same_customer(self):
        from datetime import timedelta
        from clients.models import DesignConversation
        customer = _make_customer()
        stale = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversation.objects.filter(pk=stale.pk).update(
            updated_at=timezone.now() - timedelta(minutes=120),
        )
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            c = _authed_client(customer)
            r = c.post(
                '/marketplace/design-chat/start/',
                data='{}', content_type='application/json',
            )
        self.assertEqual(r.status_code, 201)
        stale.refresh_from_db()
        self.assertEqual(stale.stage, 'abandoned')

    def test_start_does_not_abandon_other_customers_stale_convs(self):
        """Lazy prune is customer-scoped — never touches other rows."""
        from datetime import timedelta
        from clients.models import DesignConversation
        c1 = _make_customer(phone='+201000000050')
        c2 = _make_customer(phone='+201000000051')
        other = DesignConversation.objects.create(
            customer=c1, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversation.objects.filter(pk=other.pk).update(
            updated_at=timezone.now() - timedelta(minutes=120),
        )
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            client = _authed_client(c2)
            client.post(
                '/marketplace/design-chat/start/',
                data='{}', content_type='application/json',
            )
        other.refresh_from_db()
        self.assertEqual(other.stage, 'planning')  # untouched


class PruneManagementCommandTests(TestCase):
    """The cron-callable: `python manage.py prune_stale_design_chats`."""

    def _make_stale_conv(self, customer, minutes_old=120):
        from datetime import timedelta
        from clients.models import DesignConversation
        conv = DesignConversation.objects.create(
            customer=customer, stage='planning',
            accumulated_context={}, brand_profile_snapshot={},
        )
        DesignConversation.objects.filter(pk=conv.pk).update(
            updated_at=timezone.now() - timedelta(minutes=minutes_old),
        )
        return conv

    def test_command_abandons_stale_conversations(self):
        from io import StringIO
        from django.core.management import call_command
        customer = _make_customer()
        stale = self._make_stale_conv(customer)
        out = StringIO()
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            call_command('prune_stale_design_chats', stdout=out)
        self.assertIn('abandoned', out.getvalue().lower())
        stale.refresh_from_db()
        self.assertEqual(stale.stage, 'abandoned')

    def test_command_dry_run_does_not_mutate(self):
        from io import StringIO
        from django.core.management import call_command
        customer = _make_customer()
        stale = self._make_stale_conv(customer)
        out = StringIO()
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            call_command('prune_stale_design_chats', '--dry-run', stdout=out)
        self.assertIn('dry-run', out.getvalue().lower())
        stale.refresh_from_db()
        self.assertEqual(stale.stage, 'planning')

    def test_command_json_output_is_parseable(self):
        from io import StringIO
        import json as _json
        from django.core.management import call_command
        customer = _make_customer()
        self._make_stale_conv(customer)
        out = StringIO()
        with override_settings(DESIGN_CHAT_IDLE_MINUTES=60):
            call_command('prune_stale_design_chats', '--json', stdout=out)
        parsed = _json.loads(out.getvalue().strip())
        self.assertIn('abandoned', parsed)
        self.assertIn('by_stage', parsed)
        self.assertEqual(parsed['abandoned'], 1)

    def test_command_respects_idle_minutes_override(self):
        from io import StringIO
        from django.core.management import call_command
        customer = _make_customer()
        # 30 minutes old — would survive default 60min cutoff
        stale30 = self._make_stale_conv(customer, minutes_old=30)
        out = StringIO()
        # Override: 20 minute cutoff makes the 30min conv stale
        call_command('prune_stale_design_chats', '--idle-minutes', '20', stdout=out)
        stale30.refresh_from_db()
        self.assertEqual(stale30.stage, 'abandoned')
