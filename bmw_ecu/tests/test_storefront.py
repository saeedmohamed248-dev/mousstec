"""Storefront UI + tenant-facing /api/ecu/storefront/ + /subscribe/ endpoints.

Sub-commit 3 of the Granular SaaS Monetization Epic. Covers:

  • The HTML page renders 200 with the bilingual bootstrap markup
    (AR by default, EN with ?lang=en) and embeds the CSRF token.
  • GET /api/ecu/storefront/packages/ returns the active package
    catalog + the workshop's current wallet balance.
  • POST /api/ecu/subscribe/ atomically deducts the wallet AND creates
    a TenantPackageGrant on success.
  • Insufficient wallet → 402 + structured detail, no wallet mutation,
    no grant created.
  • Unknown package code → 404.
  • Anonymous → 401/403 on every endpoint.
"""
from __future__ import annotations

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client as RequestClient
from django.utils import timezone

from bmw_ecu.models import SubscriptionPackage, TenantPackageGrant
from bmw_ecu.tests.base import (
    BmwEcuTenantTestCase as TestCase,
    setup_module_tenant, teardown_module_tenant,
)


def setUpModule() -> None:
    setup_module_tenant()


def tearDownModule() -> None:
    teardown_module_tenant()


User = get_user_model()

# Resolves to the schema seeded by setUpModule(); see test_admin_packages.py
_TENANT_HOST = "test-bmw-ecu.mousstec.com"
_TENANT_SCHEMA = "test_bmw_ecu"

# Per-test sequence so usernames never collide across TransactionTestCase
# runs (contrib.auth lives in the public schema which we don't flush).
_seq = 0


def _next() -> int:
    global _seq
    _seq += 1
    return _seq


def _make_tech():
    n = _next()
    return User.objects.create_user(
        username=f"tech_{n}", password="x",
        email=f"tech{n}@workshop.com",
    )


def _set_wallet(amount: Decimal) -> None:
    """Top up the tenant's Client.wallet_balance directly. The signed-in
    technician is just a session — the wallet hangs off the workshop's
    Client row in the public schema."""
    from clients.models import Client
    Client.objects.filter(schema_name=_TENANT_SCHEMA).update(
        wallet_balance=amount,
    )


def _get_wallet() -> Decimal:
    from clients.models import Client
    row = Client.objects.filter(schema_name=_TENANT_SCHEMA).values(
        "wallet_balance").first()
    return Decimal(str(row["wallet_balance"])) if row else Decimal("0")


def _get(client, url):
    return client.get(url, HTTP_HOST=_TENANT_HOST)


def _json_post(client, url, payload):
    return client.post(url, data=json.dumps(payload),
                       content_type="application/json",
                       HTTP_HOST=_TENANT_HOST)


# ─────────────────────────────────────────────────────────────────────
class StorefrontPageTests(TestCase):
    def setUp(self):
        super().setUp()
        self.tech = _make_tech()
        self.client = RequestClient()
        self.client.force_login(self.tech)

    def test_page_renders_arabic_by_default(self):
        resp = _get(self.client, "/bmw-ecu/storefront/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        # RTL marker + Arabic CTA copy
        self.assertIn('dir="rtl"', body)
        self.assertIn("اشترك", body)
        # CSRF token meta tag for the subscribe POST
        self.assertIn('name="csrf-token"', body)
        # JS bootstrap must point at the storefront feed endpoint
        self.assertIn("/api/ecu/storefront/packages/", body)
        self.assertIn("/api/ecu/subscribe/", body)

    def test_page_renders_english_with_lang_param(self):
        resp = _get(self.client, "/bmw-ecu/storefront/?lang=en")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn('dir="ltr"', body)
        self.assertIn("Subscribe", body)

    def test_page_requires_login(self):
        anon = RequestClient()
        resp = _get(anon, "/bmw-ecu/storefront/")
        # login_required → redirect to login (302) or 401/403 depending
        # on the project's auth middleware. Any non-200 confirms the
        # contract.
        self.assertNotEqual(resp.status_code, 200)


# ─────────────────────────────────────────────────────────────────────
class StorefrontPackagesFeedTests(TestCase):
    def setUp(self):
        super().setUp()
        self.tech = _make_tech()
        self.client = RequestClient()
        self.client.force_login(self.tech)

    def test_lists_active_packages(self):
        resp = _get(self.client, "/api/ecu/storefront/packages/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        codes = {row["code"] for row in body["results"]}
        # Seeded packages with is_active=True must appear.
        for must_have in ("pkg_starter", "pkg_key_master", "pkg_full_suite"):
            self.assertIn(must_have, codes)

    def test_excludes_inactive_packages(self):
        pkg = SubscriptionPackage.objects.get(code="pkg_starter")
        pkg.is_active = False
        pkg.save(update_fields=["is_active", "updated_at"])
        try:
            resp = _get(self.client, "/api/ecu/storefront/packages/")
            codes = {row["code"] for row in resp.json()["results"]}
            self.assertNotIn("pkg_starter", codes)
        finally:
            pkg.is_active = True
            pkg.save(update_fields=["is_active", "updated_at"])

    def test_carries_wallet_balance(self):
        _set_wallet(Decimal("3250.00"))
        resp = _get(self.client, "/api/ecu/storefront/packages/")
        body = resp.json()
        self.assertEqual(body["tenant_schema"], _TENANT_SCHEMA)
        # Decimal serialised as string so JS doesn't lose precision.
        self.assertEqual(Decimal(body["wallet_balance_egp"]),
                         Decimal("3250.00"))

    def test_each_row_carries_feature_codes_and_names(self):
        resp = _get(self.client, "/api/ecu/storefront/packages/")
        starter = next(r for r in resp.json()["results"]
                       if r["code"] == "pkg_starter")
        self.assertIsInstance(starter["feature_codes"], list)
        self.assertIsInstance(starter["feature_names"], list)
        self.assertEqual(len(starter["feature_codes"]),
                         len(starter["feature_names"]))
        self.assertIn("diagnostic_room", starter["feature_codes"])

    def test_anonymous_blocked(self):
        anon = RequestClient()
        resp = _get(anon, "/api/ecu/storefront/packages/")
        self.assertIn(resp.status_code, (401, 403))


# ─────────────────────────────────────────────────────────────────────
class SubscribeEndpointTests(TestCase):
    def setUp(self):
        super().setUp()
        self.tech = _make_tech()
        self.client = RequestClient()
        self.client.force_login(self.tech)
        # Generous wallet — individual tests adjust it as needed.
        _set_wallet(Decimal("10000.00"))

    def test_subscribe_deducts_wallet_and_creates_grant(self):
        starter = SubscriptionPackage.objects.get(code="pkg_starter")
        price = starter.price_egp
        wallet_before = _get_wallet()

        resp = _json_post(self.client, "/api/ecu/subscribe/",
                          {"package_code": "pkg_starter"})
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["package_code"], "pkg_starter")
        self.assertEqual(Decimal(body["price_paid_egp"]), Decimal(price))

        # Wallet deducted exactly once.
        wallet_after = _get_wallet()
        self.assertEqual(wallet_after, wallet_before - Decimal(price))
        self.assertEqual(Decimal(body["wallet_after_egp"]), wallet_after)

        # Grant row created in the tenant schema and tied to this workshop.
        grant = TenantPackageGrant.objects.get(pk=body["grant_pk"])
        self.assertEqual(grant.tenant_schema, _TENANT_SCHEMA)
        self.assertEqual(grant.package.code, "pkg_starter")
        self.assertEqual(grant.price_paid_egp, Decimal(price))
        self.assertEqual(grant.status, "active")
        # granted_by is prefixed so a forensic audit can tell apart
        # superadmin grants from self-service wallet purchases.
        self.assertTrue(grant.granted_by.startswith("wallet:"))

    def test_rejects_insufficient_wallet_402(self):
        _set_wallet(Decimal("10.00"))
        wallet_before = _get_wallet()

        resp = _json_post(self.client, "/api/ecu/subscribe/",
                          {"package_code": "pkg_full_suite"})
        self.assertEqual(resp.status_code, 402)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "insufficient_balance")

        # Wallet untouched, no grant created.
        self.assertEqual(_get_wallet(), wallet_before)
        self.assertFalse(
            TenantPackageGrant.objects
            .filter(tenant_schema=_TENANT_SCHEMA,
                    package__code="pkg_full_suite",
                    granted_by__startswith="wallet:")
            .exists(),
        )

    def test_unknown_package_returns_404(self):
        resp = _json_post(self.client, "/api/ecu/subscribe/",
                          {"package_code": "pkg_does_not_exist"})
        self.assertEqual(resp.status_code, 404)

    def test_inactive_package_returns_404(self):
        pkg = SubscriptionPackage.objects.get(code="pkg_lighting_coding")
        pkg.is_active = False
        pkg.save(update_fields=["is_active", "updated_at"])
        try:
            resp = _json_post(self.client, "/api/ecu/subscribe/",
                              {"package_code": "pkg_lighting_coding"})
            self.assertEqual(resp.status_code, 404)
        finally:
            pkg.is_active = True
            pkg.save(update_fields=["is_active", "updated_at"])

    def test_missing_package_code_returns_400(self):
        resp = _json_post(self.client, "/api/ecu/subscribe/", {})
        self.assertEqual(resp.status_code, 400)

    def test_anonymous_blocked(self):
        anon = RequestClient()
        resp = _json_post(anon, "/api/ecu/subscribe/",
                          {"package_code": "pkg_starter"})
        self.assertIn(resp.status_code, (401, 403))


# ─────────────────────────────────────────────────────────────────────
class SubscribeIntegrationTests(TestCase):
    """End-to-end: storefront feed → subscribe → feed reflects the new
    wallet balance. Mirrors what the JS controller does on the page."""

    def setUp(self):
        super().setUp()
        self.tech = _make_tech()
        self.client = RequestClient()
        self.client.force_login(self.tech)
        _set_wallet(Decimal("5000.00"))

    def test_feed_then_subscribe_then_feed(self):
        feed1 = _get(self.client, "/api/ecu/storefront/packages/").json()
        before = Decimal(feed1["wallet_balance_egp"])

        starter = next(r for r in feed1["results"]
                       if r["code"] == "pkg_starter")
        price = Decimal(starter["price_egp"])

        sub = _json_post(self.client, "/api/ecu/subscribe/",
                         {"package_code": "pkg_starter"}).json()
        self.assertTrue(sub["ok"])
        self.assertEqual(Decimal(sub["wallet_after_egp"]), before - price)

        feed2 = _get(self.client, "/api/ecu/storefront/packages/").json()
        self.assertEqual(Decimal(feed2["wallet_balance_egp"]), before - price)
