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
        except ImportError:
            self.skipTest('cairosvg not installed in this environment')

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
        except ImportError:
            self.skipTest('cairosvg not installed in this environment')

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
