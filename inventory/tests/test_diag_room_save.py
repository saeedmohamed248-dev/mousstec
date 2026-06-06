"""
Diagnostics Room → Job Card persistence — Backlog #11
======================================================
Locks down `save_diagnostic_session`:

  • VIN required → DiagnosticSaveError("vin_required")
  • Unknown VIN → DiagnosticSaveError("vehicle_not_found", 404)
  • Explicit job_card_id that belongs to ANOTHER vehicle → mismatch
  • Explicit job_card_id matching the VIN → linked
  • No job_card_id + active job card for VIN exists → auto-linked
  • No job_card_id + no active card → orphan (job_card=None) is OK
  • Photos decode and persist; bad data URLs are skipped, not raised
  • ai_summary trimmed to 8000 chars; DTCs normalised + capped at 40
"""
import base64
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer

from inventory.models import (
    SaleInvoice, Vehicle, VehicleDiagnosticReport, VehicleDiagnosticPhoto,
)
from smart_diagnostics.services.diag_room_persistence import (
    DiagnosticSaveError,
    list_active_job_cards_for_vin,
    save_diagnostic_session,
)


# Minimal 1x1 white JPEG, base64
_TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
    "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB/9k="
)
_TINY_DATA_URL = f"data:image/jpeg;base64,{_TINY_JPEG_B64}"


class DiagRoomSaveBase(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.vehicle = Vehicle.objects.create(
            customer=self.customer, car_plate="DRS-100",
            chassis_number="WBA12345SAVEVIN1",
            brand="BMW", model_name="330i",
        )


class VINResolutionTests(DiagRoomSaveBase):

    def test_missing_vin_raises(self):
        with self.assertRaises(DiagnosticSaveError) as cm:
            save_diagnostic_session(
                vin="", dtcs=[], live_data={}, ai_summary="x", photos=[],
            )
        self.assertEqual(cm.exception.code, "vin_required")
        self.assertEqual(cm.exception.status, 400)

    def test_unknown_vin_raises_404(self):
        with self.assertRaises(DiagnosticSaveError) as cm:
            save_diagnostic_session(
                vin="NOSUCHVIN9999", dtcs=[], live_data={},
                ai_summary="x", photos=[],
            )
        self.assertEqual(cm.exception.code, "vehicle_not_found")
        self.assertEqual(cm.exception.status, 404)


class JobCardLinkingTests(DiagRoomSaveBase):

    def test_auto_links_to_active_job_card(self):
        jc = SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=self.branch,
            invoice_type='maintenance', status='in_progress',
            notes="active",
        )
        result = save_diagnostic_session(
            vin=self.vehicle.chassis_number, dtcs=["P0102"], live_data={},
            ai_summary="auto-link test", photos=[],
        )
        self.assertEqual(result["job_card_id"], jc.id)
        report = VehicleDiagnosticReport.objects.get(pk=result["report_id"])
        self.assertEqual(report.job_card_id, jc.id)
        self.assertEqual(report.source, VehicleDiagnosticReport.SOURCE_DIAG_ROOM)

    def test_no_active_job_card_saves_as_orphan(self):
        result = save_diagnostic_session(
            vin=self.vehicle.chassis_number, dtcs=["P0420"], live_data={},
            ai_summary="orphan", photos=[],
        )
        self.assertIsNone(result["job_card_id"])
        report = VehicleDiagnosticReport.objects.get(pk=result["report_id"])
        self.assertIsNone(report.job_card_id)

    def test_explicit_job_card_for_different_vehicle_rejected(self):
        other_vehicle = Vehicle.objects.create(
            customer=self.customer, car_plate="DRS-OTHER",
            chassis_number="WBAOTHERVEHICLE0",
            brand="BMW", model_name="X5",
        )
        other_jc = SaleInvoice.objects.create(
            customer=self.customer, vehicle=other_vehicle, branch=self.branch,
            invoice_type='maintenance', status='in_progress',
        )
        with self.assertRaises(DiagnosticSaveError) as cm:
            save_diagnostic_session(
                vin=self.vehicle.chassis_number,
                dtcs=[], live_data={},
                ai_summary="x", photos=[],
                job_card_id=other_jc.id,
            )
        self.assertEqual(cm.exception.code, "job_card_mismatch")

    def test_explicit_job_card_matching_vehicle_links(self):
        jc = SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=self.branch,
            invoice_type='maintenance', status='in_progress',
        )
        result = save_diagnostic_session(
            vin=self.vehicle.chassis_number, dtcs=[], live_data={},
            ai_summary="explicit", photos=[],
            job_card_id=jc.id,
        )
        self.assertEqual(result["job_card_id"], jc.id)


class PhotoPersistenceTests(DiagRoomSaveBase):

    def test_valid_photo_persisted(self):
        result = save_diagnostic_session(
            vin=self.vehicle.chassis_number, dtcs=[], live_data={},
            ai_summary="photo test", photos=[_TINY_DATA_URL],
        )
        self.assertEqual(result["photos_saved"], 1)
        self.assertEqual(result["photos_skipped"], 0)
        photo = VehicleDiagnosticPhoto.objects.get(report_id=result["report_id"])
        self.assertTrue(photo.image.name.endswith(".jpg"))
        self.assertGreater(photo.image.size, 0)

    def test_bad_data_url_skipped_not_raised(self):
        result = save_diagnostic_session(
            vin=self.vehicle.chassis_number, dtcs=[], live_data={},
            ai_summary="mixed", photos=[
                _TINY_DATA_URL,
                "not-a-data-url",                       # bad scheme
                "data:application/pdf;base64,JVBERi0=", # bad mime
                "data:image/jpeg;base64,!!!!",          # bad b64
            ],
        )
        self.assertEqual(result["photos_saved"], 1)
        self.assertEqual(result["photos_skipped"], 3)


class NormalisationTests(DiagRoomSaveBase):

    def test_ai_summary_truncated_at_8000(self):
        huge = "ا" * 9000
        result = save_diagnostic_session(
            vin=self.vehicle.chassis_number, dtcs=[], live_data={},
            ai_summary=huge, photos=[],
        )
        report = VehicleDiagnosticReport.objects.get(pk=result["report_id"])
        self.assertEqual(len(report.ai_summary), 8000)

    def test_dtcs_deduped_and_capped(self):
        # 50 codes incl. dupes; expect 40 after normalisation
        raw = [f"P{i:04d}" for i in range(45)] + ["p0001", "P0001"]
        result = save_diagnostic_session(
            vin=self.vehicle.chassis_number, dtcs=raw, live_data={},
            ai_summary="x", photos=[],
        )
        report = VehicleDiagnosticReport.objects.get(pk=result["report_id"])
        self.assertEqual(len(report.fault_codes), 40)
        self.assertEqual(report.fault_codes[0], "P0000")
        # All upper-cased
        self.assertTrue(all(c == c.upper() for c in report.fault_codes))


class ListJobCardsTests(DiagRoomSaveBase):

    def test_lists_only_active_for_vin(self):
        SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=self.branch,
            invoice_type='maintenance', status='in_progress', notes="A",
        )
        SaleInvoice.objects.create(
            customer=self.customer, vehicle=self.vehicle, branch=self.branch,
            invoice_type='maintenance', status='posted', notes="closed",
        )
        rows = list_active_job_cards_for_vin(self.vehicle.chassis_number)
        self.assertEqual(len(rows), 1)
        self.assertIn("status", rows[0])
        self.assertEqual(rows[0]["status"], "in_progress")

    def test_empty_for_unknown_vin(self):
        self.assertEqual(list_active_job_cards_for_vin("UNKNOWN"), [])
