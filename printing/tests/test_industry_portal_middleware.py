"""
🛡️ IndustryPortalMiddleware — exemption tests (Phase N.6 hotfix)
=====================================================================
Before this fix, the middleware redirected EVERY non-landing path on the
portal subdomains (auto./print.) to `/`. That broke admin access and any
tenant API call (e.g. /printing/ai/status/) initiated from those hosts,
which manifested as a misleading "Error: HTTP 200" in the JS console when
the redirected GET landed on the landing page (which IS a 200 HTML page).

These tests pin the new exemption list so future churn doesn't regress.
"""
from django.test import TestCase, RequestFactory, override_settings

from erp_core.middleware import IndustryPortalMiddleware


def _make_request(path, host='print.mousstec.com'):
    rf = RequestFactory()
    req = rf.get(path, HTTP_HOST=host)
    return req


class IndustryPortalMiddlewareExemptionTests(TestCase):
    """Verifies the Phase N.6 hotfix: portal subdomain doesn't intercept
    admin or tenant API paths."""

    def setUp(self):
        # The middleware needs a get_response callable. Use a sentinel so
        # we can detect when the middleware short-circuited vs passed through.
        self.sentinel_response = 'PASSED_THROUGH'
        self.middleware = IndustryPortalMiddleware(
            get_response=lambda req: self.sentinel_response,
        )

    # ── Portal subdomain — exempt paths (passes through) ─────────────
    def test_admin_path_passes_through_on_portal_subdomain(self):
        """`/secure-portal/...` on print.mousstec.com must NOT redirect."""
        req = _make_request('/secure-portal/printing/aistudiosession/')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_printing_ai_status_passes_through(self):
        """The bug that started this hotfix — /printing/ai/status/ on
        portal subdomain was redirecting to / and breaking the JS."""
        req = _make_request('/printing/ai/status/')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_printing_ai_generate_passes_through(self):
        req = _make_request('/printing/ai/generate/')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_system_paths_pass_through(self):
        """Automotive tenant APIs under /system/ — same exemption."""
        req = _make_request('/system/dashboard/', host='auto.mousstec.com')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_api_paths_pass_through(self):
        req = _make_request('/api/v1/something/')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_marketplace_paths_pass_through(self):
        """Customer marketplace must reach its views even from portal host."""
        req = _make_request('/marketplace/design-store/')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_superadmin_paths_pass_through(self):
        req = _make_request('/superadmin/dashboard/')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_static_and_media_still_pass_through(self):
        """Pre-existing exemption — keep behavior."""
        for path in ('/static/css/main.css', '/media/logos/x.png'):
            result = self.middleware(_make_request(path))
            self.assertEqual(result, self.sentinel_response, f'{path} failed')

    # ── Portal subdomain — legacy redirect behavior preserved ────────
    def test_random_path_still_redirects_to_root(self):
        """Unknown paths on the portal subdomain still redirect to landing.
        This is the LEGACY behavior — we only added carve-outs, not
        removed the catch-all."""
        req = _make_request('/random-unknown-path/')
        result = self.middleware(req)
        # redirect response — status 302
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result['Location'], '/')

    def test_pricing_path_still_redirects_to_main_domain(self):
        """Legacy carve-out: /pricing/ goes to root domain."""
        req = _make_request('/pricing/')
        result = self.middleware(req)
        self.assertEqual(result.status_code, 302)
        # Must be the main domain, not the portal subdomain
        self.assertIn('mousstec.com/pricing/', result['Location'])
        self.assertNotIn('print.mousstec.com', result['Location'])

    # ── Non-portal hosts — middleware passes through entirely ────────
    def test_tenant_subdomain_passes_through_for_any_path(self):
        """Tenant subdomains (printco.mousstec.com) aren't in _PORTAL_MAP,
        so the middleware should not interfere at all."""
        req = _make_request(
            '/printing/ai/status/', host='printco.mousstec.com',
        )
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    def test_root_domain_passes_through_for_admin(self):
        req = _make_request('/secure-portal/', host='mousstec.com')
        result = self.middleware(req)
        self.assertEqual(result, self.sentinel_response)

    @override_settings(ADMIN_URL='my-custom-portal')
    def test_admin_exemption_honors_custom_admin_url_setting(self):
        """The exemption reads settings.ADMIN_URL on each call so a custom
        admin path (set via env var) still gets the carve-out."""
        # Rebuild the middleware so it sees the override
        mw = IndustryPortalMiddleware(get_response=lambda r: 'OK')
        req = _make_request('/my-custom-portal/dashboard/')
        self.assertEqual(mw(req), 'OK')
