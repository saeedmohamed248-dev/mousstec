"""Super-Admin /api/admin/packages/ + /grants/ + /features/ endpoints.

Covers the new admin control panel that lets Mousstec management build
dynamic SubscriptionPackages out of the Feature catalog, set prices,
and assign packages (or single features) to specific tenants.

Permission contract: only users with `is_staff=True` (IsAdminUser DRF
permission) can hit any of these endpoints. We verify both the happy
path AND the rejection path on every endpoint.
"""
from __future__ import annotations

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client as RequestClient
from django.utils import timezone

from bmw_ecu.models import (
    Feature,
    SubscriptionPackage,
    TenantFeatureGrant,
    TenantPackageGrant,
)
from bmw_ecu.tests.base import (
    BmwEcuTenantTestCase as TestCase,
    setup_module_tenant, teardown_module_tenant,
)


def setUpModule() -> None:
    setup_module_tenant()


def tearDownModule() -> None:
    teardown_module_tenant()


User = get_user_model()

# Tenant created in setUpModule lives at this hostname — the
# TenantMainMiddleware uses it to map the request to the right schema.
# Without HTTP_HOST set, the test client uses 'testserver' which the
# tenant middleware rejects with a 404.
_TENANT_HOST = "test-bmw-ecu.mousstec.com"

# Per-test sequence so every User row carries a unique username + email
# (auth_user lives in the PUBLIC schema, which TransactionTestCase does
# NOT flush between tests in our setup).
_user_seq = 0


def _next_seq() -> int:
    global _user_seq
    _user_seq += 1
    return _user_seq


def _make_admin():
    n = _next_seq()
    user = User.objects.create_user(
        username=f"mousstec_admin_{n}", password="x",
        email=f"admin{n}@mousstec.com",
    )
    user.is_staff = True
    user.save()
    return user


def _make_normal_user():
    n = _next_seq()
    return User.objects.create_user(
        username=f"random_tech_{n}", password="x",
        email=f"tech{n}@workshop.com",
    )


def _json_post(client, url, payload):
    return client.post(url, data=json.dumps(payload),
                       content_type="application/json",
                       HTTP_HOST=_TENANT_HOST)


def _json_patch(client, url, payload):
    return client.patch(url, data=json.dumps(payload),
                        content_type="application/json",
                        HTTP_HOST=_TENANT_HOST)


def _get(client, url):
    return client.get(url, HTTP_HOST=_TENANT_HOST)


def _delete(client, url):
    return client.delete(url, HTTP_HOST=_TENANT_HOST)


# ─────────────────────────────────────────────────────────────────────
class FeaturesListTests(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.client = RequestClient()
        self.client.force_login(self.admin)

    def test_list_returns_seeded_features(self):
        resp = _get(self.client, "/api/admin/features/")
        self.assertEqual(resp.status_code, 200)
        codes = {row["code"] for row in resp.json()["results"]}
        # Spot-check the headliner codes seeded by migration 0006.
        for must_have in ("frm_repair", "key_programming",
                          "egs_isn_reset", "acsm_crash_reset"):
            self.assertIn(must_have, codes)

    def test_list_filters_by_active(self):
        Feature.objects.create(code="inactive_feature", name="Off",
                               is_active=False)
        resp = _get(self.client, "/api/admin/features/?active=1")
        codes = {row["code"] for row in resp.json()["results"]}
        self.assertNotIn("inactive_feature", codes)

    def test_anonymous_blocked(self):
        anon = RequestClient()
        resp = _get(anon, "/api/admin/features/")
        self.assertIn(resp.status_code, (401, 403))

    def test_non_admin_blocked(self):
        anon = RequestClient()
        normal = _make_normal_user()
        anon.force_login(normal)
        resp = _get(anon, "/api/admin/features/")
        self.assertEqual(resp.status_code, 403)


# ─────────────────────────────────────────────────────────────────────
class PackagesCollectionTests(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.client = RequestClient()
        self.client.force_login(self.admin)

    def test_list_returns_seeded_packages(self):
        resp = _get(self.client, "/api/admin/packages/")
        self.assertEqual(resp.status_code, 200)
        codes = {row["code"] for row in resp.json()["results"]}
        for must_have in ("pkg_starter", "pkg_key_master", "pkg_full_suite"):
            self.assertIn(must_have, codes)

    def test_create_package_with_features(self):
        payload = {
            "code": "pkg_test_special",
            "name": "Test Special",
            "description": "Built in test",
            "billing_mode": "time",
            "default_duration_days": 60,
            "price_egp": 2999,
            "feature_codes": ["frm_repair", "key_programming"],
        }
        resp = _json_post(self.client, "/api/admin/packages/", payload)
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["code"], "pkg_test_special")
        self.assertEqual(set(body["feature_codes"]),
                         {"frm_repair", "key_programming"})
        # And it persisted.
        pkg = SubscriptionPackage.objects.get(code="pkg_test_special")
        self.assertEqual(pkg.price_egp, Decimal("2999"))

    def test_create_rejects_duplicate_code(self):
        resp = _json_post(self.client, "/api/admin/packages/", {
            "code": "pkg_starter",  # already seeded
            "name": "Dup",
        })
        self.assertEqual(resp.status_code, 409)

    def test_create_rejects_unknown_feature_code(self):
        resp = _json_post(self.client, "/api/admin/packages/", {
            "code": "pkg_with_ghost",
            "name": "Ghost",
            "feature_codes": ["this_does_not_exist"],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown feature_codes", resp.json()["detail"])

    def test_create_rejects_bad_billing_mode(self):
        resp = _json_post(self.client, "/api/admin/packages/", {
            "code": "pkg_bad_bm",
            "name": "Bad",
            "billing_mode": "yearly_subscription_with_dlc",
        })
        self.assertEqual(resp.status_code, 400)

    def test_create_rejects_missing_required(self):
        resp = _json_post(self.client, "/api/admin/packages/", {
            "name": "Anon",   # no code
        })
        self.assertEqual(resp.status_code, 400)


# ─────────────────────────────────────────────────────────────────────
class PackageDetailTests(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.client = RequestClient()
        self.client.force_login(self.admin)
        self.pkg = SubscriptionPackage.objects.get(code="pkg_starter")

    def test_get_returns_package(self):
        resp = _get(self.client, f"/api/admin/packages/{self.pkg.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], "pkg_starter")

    def test_patch_updates_metadata(self):
        resp = _json_patch(self.client, f"/api/admin/packages/{self.pkg.pk}/", {
            "price_egp": 1750,
            "is_featured": True,
        })
        self.assertEqual(resp.status_code, 200)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.price_egp, Decimal("1750"))
        self.assertTrue(self.pkg.is_featured)

    def test_patch_replaces_features(self):
        resp = _json_patch(self.client, f"/api/admin/packages/{self.pkg.pk}/", {
            "feature_codes": ["frm_repair"],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()["feature_codes"]), {"frm_repair"})

    def test_delete_soft_deactivates(self):
        resp = _delete(self.client, f"/api/admin/packages/{self.pkg.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.pkg.refresh_from_db()
        # NOT physically deleted — soft deactivation keeps the audit chain
        # intact for any TenantPackageGrants that already referenced it.
        self.assertFalse(self.pkg.is_active)


# ─────────────────────────────────────────────────────────────────────
class GrantsCollectionTests(TestCase):
    def setUp(self):
        super().setUp()
        self.admin = _make_admin()
        self.client = RequestClient()
        self.client.force_login(self.admin)
        # The module tenant 'test_bmw_ecu' is registered as a real Client row
        # in setUpModule(), so _resolve_tenant() succeeds against it.
        self.tenant_schema = "test_bmw_ecu"

    def test_issue_package_grant(self):
        resp = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "package_code": "pkg_starter",
            "duration_days": 14,
            "price_paid_egp": 1500,
            "note": "Promo trial",
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["kind"], "package")
        self.assertEqual(body["package_code"], "pkg_starter")
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["price_paid_egp"], "1500")
        # And it persists in the tenant schema.
        from django_tenants.utils import schema_context
        with schema_context(self.tenant_schema):
            grant = TenantPackageGrant.objects.get(pk=body["pk"])
            self.assertEqual(grant.package.code, "pkg_starter")
            self.assertEqual(grant.note, "Promo trial")

    def test_issue_feature_grant(self):
        resp = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "feature_code": "frm_repair",
            "billing_mode": "usage",
            "usage_quota": 5,
            "price_paid_egp": 0,
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["kind"], "feature")
        self.assertEqual(body["feature_code"], "frm_repair")
        self.assertEqual(body["usage_quota"], 5)

    def test_rejects_both_package_and_feature(self):
        resp = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "package_code": "pkg_starter",
            "feature_code": "frm_repair",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("exactly one", resp.json()["detail"])

    def test_rejects_neither_package_nor_feature(self):
        resp = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
        })
        self.assertEqual(resp.status_code, 400)

    def test_rejects_unknown_tenant(self):
        resp = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": "does_not_exist",
            "package_code": "pkg_starter",
        })
        self.assertEqual(resp.status_code, 404)

    def test_rejects_unknown_package(self):
        resp = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "package_code": "pkg_imaginary",
        })
        self.assertEqual(resp.status_code, 404)

    def test_list_grants_for_tenant(self):
        # Seed two grants so the list returns multiple kinds.
        _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "package_code": "pkg_starter",
        })
        _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "feature_code": "frm_repair",
            "billing_mode": "usage", "usage_quota": 3,
        })
        resp = _get(self.client,
                    f"/api/admin/grants/?tenant={self.tenant_schema}")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        kinds = sorted({r["kind"] for r in rows})
        self.assertEqual(kinds, ["feature", "package"])

    def test_revoke_active_grant(self):
        create = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "package_code": "pkg_starter",
        })
        pk = create.json()["pk"]
        resp = _json_post(self.client,
                          f"/api/admin/grants/{pk}/revoke/",
                          {"kind": "package",
                           "tenant_schema": self.tenant_schema})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "revoked")

    def test_revoke_idempotent(self):
        create = _json_post(self.client, "/api/admin/grants/", {
            "tenant_schema": self.tenant_schema,
            "feature_code": "frm_repair",
        })
        pk = create.json()["pk"]
        url = f"/api/admin/grants/{pk}/revoke/"
        body = {"kind": "feature", "tenant_schema": self.tenant_schema}
        first = _json_post(self.client, url, body)
        second = _json_post(self.client, url, body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["detail"], "already revoked")


# ─────────────────────────────────────────────────────────────────────
class PermissionTests(TestCase):
    """Permission contract — non-admins must be locked out everywhere."""

    def setUp(self):
        super().setUp()
        normal = _make_normal_user()
        self.client = RequestClient()
        self.client.force_login(normal)

    def test_features_blocked(self):
        self.assertEqual(_get(self.client, "/api/admin/features/").status_code, 403)

    def test_packages_blocked(self):
        self.assertEqual(_get(self.client, "/api/admin/packages/").status_code, 403)

    def test_grants_blocked(self):
        self.assertEqual(_get(self.client,
            "/api/admin/grants/?tenant=test_bmw_ecu").status_code, 403)

    def test_create_package_blocked(self):
        resp = _json_post(self.client, "/api/admin/packages/", {
            "code": "pkg_sneaky", "name": "Sneaky",
        })
        self.assertEqual(resp.status_code, 403)
