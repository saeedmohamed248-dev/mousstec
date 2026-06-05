"""
OBD Ingest Endpoint Tests — DMS Backlog #1
============================================
Locks down /system/api/obd/ingest/ against:

  • Bad / missing HMAC signature → 401
  • Unknown VIN → 404
  • VIN with no active job card → still 201 (orphan report) — by design
  • Invalid JSON / scan_type → 400
  • Happy path: valid signature + known VIN + active job → 201 + report linked
"""
import hashlib
import hmac
import json
from unittest import mock

from django.test import RequestFactory

from inventory.api_obd import ReceiveOBDDataView
from inventory.models import SaleInvoice, Vehicle, VehicleDiagnosticReport

from .base import ERPTenantTestCase
from .factories import make_branch, make_customer


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(body: bytes, signature: str = None):
    rf = RequestFactory()
    headers = {'content_type': 'application/json'}
    if signature is not None:
        headers['HTTP_X_OBD_SIGNATURE'] = signature
    req = rf.post('/system/api/obd/ingest/', data=body, **headers)
    return ReceiveOBDDataView.as_view()(req)


class OBDSignatureTests(ERPTenantTestCase):
    """HMAC gate: bad/missing signature when OBD_HMAC_SECRET is set."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.vehicle = Vehicle.objects.create(
            customer=self.customer, car_plate='OBD-100',
            chassis_number='WBA12345TESTVIN1',
            brand='BMW', model_name='330i',
        )

    def test_bad_signature_rejected(self):
        body = json.dumps({'vin': self.vehicle.chassis_number, 'scan_type': 'ad_hoc'}).encode()
        with mock.patch('inventory.api_obd.OBD_HMAC_SECRET', 'sekret'):
            r = _post(body, signature='wrong-sig')
        self.assertEqual(r.status_code, 401)
        self.assertIn(b'bad_signature', r.content)

    def test_missing_signature_rejected_when_secret_set(self):
        body = json.dumps({'vin': self.vehicle.chassis_number, 'scan_type': 'ad_hoc'}).encode()
        with mock.patch('inventory.api_obd.OBD_HMAC_SECRET', 'sekret'):
            r = _post(body, signature=None)
        self.assertEqual(r.status_code, 401)

    def test_valid_signature_accepted(self):
        body = json.dumps({'vin': self.vehicle.chassis_number, 'scan_type': 'ad_hoc'}).encode()
        with mock.patch('inventory.api_obd.OBD_HMAC_SECRET', 'sekret'):
            r = _post(body, signature=_sign('sekret', body))
        self.assertEqual(r.status_code, 201)

    def test_unsigned_passes_when_secret_empty(self):
        """Dev convenience: empty secret = signature check skipped."""
        body = json.dumps({'vin': self.vehicle.chassis_number, 'scan_type': 'ad_hoc'}).encode()
        with mock.patch('inventory.api_obd.OBD_HMAC_SECRET', ''):
            r = _post(body, signature=None)
        self.assertEqual(r.status_code, 201)


class OBDPayloadValidationTests(ERPTenantTestCase):
    """Body shape / VIN / scan_type validation — signature mocked off."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.vehicle = Vehicle.objects.create(
            customer=self.customer, car_plate='OBD-200',
            chassis_number='WBA98765TESTVIN2',
            brand='BMW', model_name='M4',
        )
        # Patch secret to empty for the whole class — body-level tests
        self._secret_patch = mock.patch('inventory.api_obd.OBD_HMAC_SECRET', '')
        self._secret_patch.start()

    def tearDown(self):
        self._secret_patch.stop()

    def test_invalid_json_rejected(self):
        r = _post(b'{not json')
        self.assertEqual(r.status_code, 400)
        self.assertIn(b'invalid_json', r.content)

    def test_missing_vin_rejected(self):
        r = _post(json.dumps({'scan_type': 'ad_hoc'}).encode())
        self.assertEqual(r.status_code, 400)
        self.assertIn(b'vin_required', r.content)

    def test_invalid_scan_type_rejected(self):
        r = _post(json.dumps({
            'vin': self.vehicle.chassis_number,
            'scan_type': 'bogus_value',
        }).encode())
        self.assertEqual(r.status_code, 400)
        self.assertIn(b'invalid_scan_type', r.content)

    def test_unknown_vin_returns_404(self):
        r = _post(json.dumps({
            'vin': 'NOSUCHVIN12345',
            'scan_type': 'ad_hoc',
        }).encode())
        self.assertEqual(r.status_code, 404)
        self.assertIn(b'vehicle_not_found', r.content)

    def test_invalid_data_shape_rejected(self):
        """fault_codes must be list, live_data must be dict."""
        r = _post(json.dumps({
            'vin': self.vehicle.chassis_number,
            'scan_type': 'ad_hoc',
            'fault_codes': 'P0171',  # should be a list
        }).encode())
        self.assertEqual(r.status_code, 400)
        self.assertIn(b'invalid_data_shape', r.content)


class OBDIngestSuccessTests(ERPTenantTestCase):
    """Happy path: report created, job card linked if active one exists."""

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.vehicle = Vehicle.objects.create(
            customer=self.customer, car_plate='OBD-300',
            chassis_number='WBA55555TESTVIN3',
            brand='BMW', model_name='X5',
        )
        self._secret_patch = mock.patch('inventory.api_obd.OBD_HMAC_SECRET', '')
        self._secret_patch.start()

    def tearDown(self):
        self._secret_patch.stop()

    def test_ingest_creates_report_with_severity(self):
        r = _post(json.dumps({
            'vin': self.vehicle.chassis_number,
            'scan_type': 'pre_repair',
            'fault_codes': ['P0171', 'P0300', 'P0420'],
            'live_data': {'rpm': 820, 'coolant_temp_c': 115},  # >110 → +30
        }).encode())
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.content)
        self.assertTrue(data['ok'])
        report = VehicleDiagnosticReport.objects.get(pk=data['report_id'])
        self.assertEqual(report.vehicle_id, self.vehicle.id)
        self.assertEqual(report.scan_type, 'pre_repair')
        self.assertEqual(report.fault_codes, ['P0171', 'P0300', 'P0420'])
        # 3 codes × 10 = 30, + 30 for coolant > 110 = 60
        self.assertEqual(report.severity_score, 60)

    def test_ingest_links_to_active_job_card(self):
        """When the VIN has an active job card, report.job_card must be set."""
        job_card = SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=self.branch,
            invoice_type='maintenance', status='in_progress',
            notes='OBD test job card',
        )
        r = _post(json.dumps({
            'vin': self.vehicle.chassis_number,
            'scan_type': 'pre_repair',
            'fault_codes': ['P0171'],
            'live_data': {'rpm': 800},
        }).encode())
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.content)
        self.assertEqual(data['job_card_id'], job_card.id)
        report = VehicleDiagnosticReport.objects.get(pk=data['report_id'])
        self.assertEqual(report.job_card_id, job_card.id)

    def test_ingest_orphan_when_no_active_job(self):
        """No active job card → report still created, just with job_card=None."""
        # Closed/posted invoice — not in ACTIVE_JOB_STATUSES
        SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=self.branch,
            invoice_type='maintenance', status='posted', notes='closed',
        )
        r = _post(json.dumps({
            'vin': self.vehicle.chassis_number,
            'scan_type': 'ad_hoc',
            'fault_codes': [],
            'live_data': {},
        }).encode())
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.content)
        self.assertIsNone(data['job_card_id'])

    def test_vin_match_is_case_insensitive(self):
        r = _post(json.dumps({
            'vin': self.vehicle.chassis_number.lower(),
            'scan_type': 'ad_hoc',
        }).encode())
        self.assertEqual(r.status_code, 201)
