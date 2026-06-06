"""
OBD Ingest Endpoint Tests — DMS Backlog #2 (per-device HMAC)
=============================================================
Enforces the new header contract on /system/api/obd/ingest/:

    X-OBD-Device-Id, X-OBD-Timestamp, X-OBD-Nonce, X-OBD-Signature
    signature = hex(hmac_sha256(secret, f"{ts}.{nonce}.{sha256(body)}"))

Coverage:
  • Missing/short headers              → 401 missing_auth_headers / bad_nonce
  • Unknown device                     → 401 unauthorized
  • Suspended / revoked device         → 401 unauthorized
  • Stale or future timestamp          → 401 stale_timestamp
  • Bad signature                      → 401 unauthorized
  • Nonce replay                       → 401 replay_detected
  • Happy path                         → 201 + report created in tenant schema
  • Branch-mismatch guard              → 403 branch_mismatch
  • Rotated secret invalidates old one → 401 unauthorized
  • Body validation (json/scan_type)   → 400 (after auth)
"""
import hashlib
import hmac
import json
import secrets
import time

from cryptography.fernet import Fernet
from django.db import connection
from django.test import RequestFactory, override_settings
from django_tenants.utils import schema_context

from inventory.api_obd import ReceiveOBDDataView
from inventory.models import SaleInvoice, Vehicle, VehicleDiagnosticReport

from .base import ERPTenantTestCase
from .factories import make_branch, make_customer


# A fixed test KEK so we don't depend on the operator's environment.
_TEST_KEK = Fernet.generate_key().decode()


def _sign(secret: str, *, ts: str, nonce: str, body: bytes) -> str:
    body_digest = hashlib.sha256(body).hexdigest()
    message = f"{ts}.{nonce}.{body_digest}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _headers(device_id, ts, nonce, sig):
    return {
        "HTTP_X_OBD_DEVICE_ID": device_id,
        "HTTP_X_OBD_TIMESTAMP": ts,
        "HTTP_X_OBD_NONCE": nonce,
        "HTTP_X_OBD_SIGNATURE": sig,
    }


def _post(body: bytes, *, device_id="", ts="", nonce="", sig=""):
    rf = RequestFactory()
    req = rf.post(
        "/system/api/obd/ingest/", data=body,
        content_type="application/json",
        **_headers(device_id, ts, nonce, sig),
    )
    return ReceiveOBDDataView.as_view()(req)


def _signed_post(secret, body: bytes, *, device_id, ts=None, nonce=None):
    ts = ts or str(int(time.time()))
    nonce = nonce or secrets.token_hex(16)
    sig = _sign(secret, ts=ts, nonce=nonce, body=body)
    return _post(body, device_id=device_id, ts=ts, nonce=nonce, sig=sig)


@override_settings(OBD_DEVICE_SECRET_KEK=_TEST_KEK)
class _OBDBase(ERPTenantTestCase):
    """Shared scaffolding — provisions one device pinned to the test branch."""

    def setUp(self):
        # Tenant-schema fixtures
        self.branch = make_branch()
        self.customer = make_customer()
        self.vehicle = Vehicle.objects.create(
            customer=self.customer, car_plate="OBD-100",
            chassis_number="WBA12345TESTVIN1",
            brand="BMW", model_name="330i",
        )

        # Device record lives in PUBLIC schema → switch, write, switch back.
        from clients.obd_device_models import OBDDevice
        tenant_schema = connection.schema_name
        with schema_context("public"):
            self.device, self.secret = OBDDevice.provision(
                tenant=self.tenant, branch_id=self.branch.pk,
                label="test-rig",
            )
            self.device_id = self.device.device_id
        # Restore tenant context for the test body
        connection.set_tenant(self.tenant)


class OBDAuthHeaderTests(_OBDBase):

    def test_missing_all_headers_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        r = _post(body)
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"missing_auth_headers", r.content)

    def test_short_nonce_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        ts = str(int(time.time()))
        sig = _sign(self.secret, ts=ts, nonce="abc", body=body)
        r = _post(body, device_id=self.device_id, ts=ts, nonce="abc", sig=sig)
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"bad_nonce", r.content)

    def test_unknown_device_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        r = _signed_post(self.secret, body, device_id="obd_does_not_exist")
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"unauthorized", r.content)

    def test_suspended_device_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        from clients.obd_device_models import OBDDevice
        with schema_context("public"):
            OBDDevice.objects.filter(pk=self.device.pk).update(
                status=OBDDevice.STATUS_SUSPENDED,
            )
        connection.set_tenant(self.tenant)
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 401)

    def test_stale_timestamp_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        old_ts = str(int(time.time()) - 9999)
        r = _signed_post(self.secret, body, device_id=self.device_id, ts=old_ts)
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"stale_timestamp", r.content)

    def test_bad_signature_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        r = _post(body, device_id=self.device_id, ts=ts, nonce=nonce,
                  sig="0" * 64)
        self.assertEqual(r.status_code, 401)
        self.assertIn(b"unauthorized", r.content)

    def test_nonce_replay_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        sig = _sign(self.secret, ts=ts, nonce=nonce, body=body)

        r1 = _post(body, device_id=self.device_id, ts=ts, nonce=nonce, sig=sig)
        self.assertEqual(r1.status_code, 201)

        connection.set_tenant(self.tenant)  # post() may have switched schema
        r2 = _post(body, device_id=self.device_id, ts=ts, nonce=nonce, sig=sig)
        self.assertEqual(r2.status_code, 401)
        self.assertIn(b"replay_detected", r2.content)

    def test_rotated_secret_invalidates_old(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        from clients.obd_device_models import OBDDevice
        with schema_context("public"):
            fresh = OBDDevice.objects.get(pk=self.device.pk).rotate_secret()
        connection.set_tenant(self.tenant)

        # Old secret no longer works
        r_old = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r_old.status_code, 401)

        # New secret works
        connection.set_tenant(self.tenant)
        r_new = _signed_post(fresh, body, device_id=self.device_id)
        self.assertEqual(r_new.status_code, 201)


class OBDIngestSuccessTests(_OBDBase):

    def test_happy_path_creates_report(self):
        body = json.dumps({
            "vin": self.vehicle.chassis_number,
            "scan_type": "pre_repair",
            "fault_codes": ["P0171", "P0300", "P0420"],
            "live_data": {"rpm": 820, "coolant_temp_c": 115},
        }).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.content)
        self.assertTrue(data["ok"])

        connection.set_tenant(self.tenant)
        report = VehicleDiagnosticReport.objects.get(pk=data["report_id"])
        self.assertEqual(report.vehicle_id, self.vehicle.id)
        # 3 codes × 10 = 30 + 30 (coolant > 110) = 60
        self.assertEqual(report.severity_score, 60)
        self.assertEqual(report.device_id, self.device_id)

    def test_vin_match_case_insensitive(self):
        body = json.dumps({
            "vin": self.vehicle.chassis_number.lower(),
            "scan_type": "ad_hoc",
        }).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 201)

    def test_device_telemetry_updated(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "ad_hoc"}).encode()
        _signed_post(self.secret, body, device_id=self.device_id)

        from clients.obd_device_models import OBDDevice
        with schema_context("public"):
            refreshed = OBDDevice.objects.get(pk=self.device.pk)
        self.assertIsNotNone(refreshed.last_seen_at)
        self.assertEqual(refreshed.last_seen_generation,
                         refreshed.rotation_generation)


class OBDBranchMismatchTests(_OBDBase):

    def test_branch_mismatch_rejected(self):
        """Device pinned to branch A; VIN's active job card is in branch B → 403."""
        other_branch = make_branch(name="فرع آخر")
        SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=other_branch,
            invoice_type="maintenance", status="in_progress",
            notes="job in other branch",
        )

        body = json.dumps({
            "vin": self.vehicle.chassis_number,
            "scan_type": "pre_repair",
        }).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"branch_mismatch", r.content)

    def test_matching_branch_accepted(self):
        SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=self.branch,
            invoice_type="maintenance", status="in_progress",
            notes="job in our branch",
        )
        body = json.dumps({
            "vin": self.vehicle.chassis_number,
            "scan_type": "pre_repair",
        }).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 201)


class OBDPayloadValidationTests(_OBDBase):
    """Body validation runs AFTER auth — uses real signed requests."""

    def test_invalid_json_rejected(self):
        r = _signed_post(self.secret, b"{not json", device_id=self.device_id)
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"invalid_json", r.content)

    def test_missing_vin_rejected(self):
        body = json.dumps({"scan_type": "ad_hoc"}).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"vin_required", r.content)

    def test_invalid_scan_type_rejected(self):
        body = json.dumps({"vin": self.vehicle.chassis_number,
                           "scan_type": "bogus"}).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"invalid_scan_type", r.content)

    def test_unknown_vin_returns_404(self):
        body = json.dumps({"vin": "NOSUCHVIN12345",
                           "scan_type": "ad_hoc"}).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 404)
        self.assertIn(b"vehicle_not_found", r.content)

    def test_invalid_data_shape_rejected(self):
        body = json.dumps({
            "vin": self.vehicle.chassis_number,
            "scan_type": "ad_hoc",
            "fault_codes": "P0171",  # should be list
        }).encode()
        r = _signed_post(self.secret, body, device_id=self.device_id)
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"invalid_data_shape", r.content)
