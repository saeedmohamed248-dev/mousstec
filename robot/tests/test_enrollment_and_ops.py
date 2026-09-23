"""Staff face enrollment, and the operational features around it.

Enrollment ("الروبوت ينادي الموظفين واحد واحد ويسجّل وشهم"):
  * refuses to start without a real face model;
  * calls the first name, re-calls, and skips no-shows;
  * keeps only one-face captures that agree with each other;
  * stores the averaged embedding after SAMPLES_NEEDED samples;
  * never puts a face already enrolled under another name on a second person.

Plus: hashed device tokens, the no-overselling guard, motor-current
predictive maintenance, learned used-part pricing, shelf pointing, and voice
commands that borrow the face the camera just recognized.

No database: models are stubbed, like the other robot tests.
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from robot import enrollment, services, views


class _Session:
    """In-memory RobotFaceEnrollment."""

    def __init__(self, names):
        self.pk = 1
        self.device = mock.Mock()
        self.status = "active"
        self.entries = [{"employee_id": i + 1, "name": n, "status": "pending",
                         "samples": [], "announce_count": 0}
                        for i, n in enumerate(names)]
        self.saved = 0

    def save(self, update_fields=None):
        self.saved += 1

    def current(self):
        for i, e in enumerate(self.entries):
            if e.get("status") == "current":
                return i, e
        return None, None


def _said(say_mock):
    return [c.args[1] for c in say_mock.call_args_list]


class EnrollmentStartTests(SimpleTestCase):

    def test_refuses_without_a_real_face_model(self):
        with mock.patch("robot.security.matching_available", return_value=False):
            with self.assertRaises(enrollment.EnrollmentUnavailable):
                enrollment.start(mock.Mock())

    def test_calls_the_first_name(self):
        session = _Session(["أحمد", "محمد"])
        with mock.patch.object(enrollment, "_say") as say:
            enrollment._advance(session)
        self.assertEqual(session.entries[0]["status"], "current")
        self.assertEqual(session.entries[1]["status"], "pending")
        self.assertIn("أحمد", _said(say)[0])

    def test_skip_moves_to_the_next_person(self):
        session = _Session(["أحمد", "محمد"])
        with mock.patch.object(enrollment, "_say") as say:
            enrollment._advance(session)
            enrollment.skip_current(session)
        self.assertEqual(session.entries[0]["status"], "skipped")
        self.assertEqual(session.entries[1]["status"], "current")
        self.assertIn("محمد", _said(say)[-1])

    def test_the_round_ends_with_a_summary(self):
        session = _Session(["أحمد"])
        with mock.patch.object(enrollment, "_say") as say:
            enrollment._advance(session)
            enrollment.skip_current(session)
        self.assertEqual(session.status, "done")
        self.assertIn("أحمد", _said(say)[-1])  # listed as not enrolled


class CameraPromptTests(SimpleTestCase):

    def _prompt(self, session):
        with mock.patch.object(enrollment, "active_session", return_value=session), \
                mock.patch.object(enrollment, "_say") as say:
            return enrollment.camera_prompt(mock.Mock()), say

    def test_tells_the_camera_who_to_capture(self):
        session = _Session(["أحمد"])
        session.entries[0].update(status="current", announced_at=timezone.now().isoformat(),
                                  announce_count=1)
        prompt, _ = self._prompt(session)
        self.assertEqual(prompt["name"], "أحمد")
        self.assertEqual(prompt["needed"], enrollment.SAMPLES_NEEDED)

    def test_recalls_the_name_when_nobody_came(self):
        session = _Session(["أحمد"])
        old = (timezone.now() - timedelta(seconds=45)).isoformat()
        session.entries[0].update(status="current", announced_at=old, announce_count=1)
        _, say = self._prompt(session)
        self.assertEqual(session.entries[0]["announce_count"], 2)
        self.assertIn("تاني", _said(say)[0])

    def test_skips_a_no_show_after_enough_calls(self):
        session = _Session(["أحمد", "محمد"])
        old = (timezone.now() - timedelta(seconds=45)).isoformat()
        session.entries[0].update(status="current", announced_at=old,
                                  announce_count=enrollment.MAX_ANNOUNCEMENTS)
        prompt, _ = self._prompt(session)
        self.assertEqual(session.entries[0]["status"], "skipped")
        self.assertEqual(prompt["name"], "محمد")


class CaptureTests(SimpleTestCase):

    def _capture(self, session, extracted, *, others=(), employee=None):
        hr = mock.Mock()
        hr.Employee.objects.exclude.return_value.exclude.return_value = list(others)
        hr.Employee.objects.filter.return_value.first.return_value = employee or mock.Mock(pk=1)
        with mock.patch.object(enrollment, "active_session", return_value=session), \
                mock.patch.object(enrollment, "_say") as say, \
                mock.patch("robot.faces.extract_single_face", return_value=extracted), \
                mock.patch("robot.security._threshold", return_value=0.9), \
                mock.patch.dict("sys.modules", {"hr.models": hr}):
            return enrollment.capture(mock.Mock(), b"jpeg"), say

    def _current(self, samples=None):
        session = _Session(["أحمد", "محمد"])
        session.entries[0].update(status="current", samples=samples or [])
        return session

    def test_no_face_is_not_a_sample(self):
        session = self._current()
        result, _ = self._capture(session, (None, "no_face"))
        self.assertEqual(result["status"], "no_face")
        self.assertEqual(session.entries[0]["samples"], [])

    def test_two_people_in_frame_are_asked_to_step_apart(self):
        result, say = self._capture(self._current(), (None, "multiple_faces"))
        self.assertEqual(result["status"], "multiple_faces")
        self.assertIn("لوحده", _said(say)[0])

    def test_a_different_face_mid_enrollment_is_dropped(self):
        session = self._current(samples=[[1.0, 0.0]])
        result, _ = self._capture(session, ([-1.0, 0.0], "ok"))
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(len(session.entries[0]["samples"]), 1)

    def test_samples_accumulate(self):
        session = self._current(samples=[[1.0, 0.0]])
        result, _ = self._capture(session, ([1.0, 0.01], "ok"))
        self.assertEqual(result["status"], "sample_ok")
        self.assertEqual(result["samples"], 2)

    def test_enough_samples_enrolls_the_average_and_moves_on(self):
        session = self._current(samples=[[1.0, 0.0], [1.0, 0.02]])
        employee = mock.Mock(pk=1)
        with mock.patch("robot.models.RobotSnapshot"):
            result, say = self._capture(session, ([1.0, 0.04], "ok"), employee=employee)
        self.assertEqual(result["status"], "enrolled")
        self.assertEqual(employee.face_encoding, [1.0, 0.02])
        employee.save.assert_called_once_with(update_fields=["face_encoding"])
        self.assertEqual(session.entries[0]["status"], "done")
        self.assertEqual(session.entries[0]["samples"], [])
        self.assertEqual(session.entries[1]["status"], "current")
        self.assertIn("محمد", _said(say)[-1])

    def test_a_face_already_on_someone_else_is_not_enrolled_again(self):
        session = self._current(samples=[[1.0, 0.0], [1.0, 0.0]])
        other = mock.Mock(face_encoding=[1.0, 0.0])
        other.name = "كريم"
        employee = mock.Mock(pk=1)
        result, _ = self._capture(session, ([1.0, 0.0], "ok"),
                                  others=[other], employee=employee)
        self.assertEqual(result["status"], "duplicate")
        self.assertIn("كريم", session.entries[0]["note"])
        employee.save.assert_not_called()

    def test_nothing_happens_without_an_active_round(self):
        with mock.patch.object(enrollment, "active_session", return_value=None):
            self.assertEqual(enrollment.capture(mock.Mock(), b"x"), {"status": "idle"})


class HashedTokenTests(SimpleTestCase):

    def test_only_the_hash_is_stored(self):
        from robot.models import RobotDevice
        device = RobotDevice()
        token = device.issue_token()
        self.assertEqual(len(token), 48)
        self.assertEqual(device.api_token, RobotDevice.hash_token(token))
        self.assertNotIn(token, device.api_token)

    def test_the_device_is_looked_up_by_hash(self):
        from robot import security
        from robot.models import RobotDevice
        request = APIRequestFactory().get("/", HTTP_X_ROBOT_TOKEN="plain-token")
        with mock.patch.object(RobotDevice, "objects") as objects:
            objects.filter.return_value.first.return_value = None
            security.authenticate_device(request)
        self.assertEqual(objects.filter.call_args.kwargs["api_token"],
                         RobotDevice.hash_token("plain-token"))


class NoOversellingTests(SimpleTestCase):

    def _sell(self, *, on_hand, body):
        request = APIRequestFactory().post("/api/robot/v1/sale/", body, format="json")
        product = mock.Mock(scrap_price=Decimal("0"), retail_price=Decimal("100"))
        product.name = "فلتر"
        fake_inventory = mock.Mock()
        fake_inventory.Customer.objects.get_or_create.return_value = (mock.Mock(), False)
        with mock.patch.object(views, "_device_or_401", return_value=(mock.Mock(), None)), \
                mock.patch.object(views, "_require_permission", return_value=(mock.Mock(), None)), \
                mock.patch.object(views.services, "find_product", return_value=product), \
                mock.patch.object(views.services, "branch_stock", return_value=on_hand), \
                mock.patch.dict("sys.modules", {"inventory.models": fake_inventory}), \
                mock.patch.object(views.services, "create_robot_sale") as create:
            create.return_value = mock.Mock(id=1, total_amount=Decimal("100"))
            with mock.patch.object(views.services, "maybe_raise_procurement_signal"):
                response = views.sale(request)
        return response, create

    def test_more_than_on_hand_is_refused(self):
        response, create = self._sell(on_hand=1, body={"part_number": "F", "quantity": 3})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["on_hand"], 1)
        create.assert_not_called()

    def test_an_explicit_backorder_is_allowed(self):
        response, create = self._sell(
            on_hand=0, body={"part_number": "F", "quantity": 1, "allow_backorder": True})
        self.assertEqual(response.status_code, 201)
        create.assert_called_once()


class MotorHealthTests(SimpleTestCase):

    def _record(self, health, currents):
        device = mock.Mock(motor_health=health)
        models = mock.Mock()
        models.RobotAlert.objects.filter.return_value.exists.return_value = False
        with mock.patch.dict("sys.modules", {"robot.models": models}):
            alerts = services.record_motor_currents(device, currents)
        return device, alerts, models

    def test_a_stall_current_raises_a_fault(self):
        _, alerts, models = self._record({}, {"arm_left": 12.0})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(models.RobotAlert.objects.create.call_args.kwargs["kind"], "motor_fault")

    def test_healthy_readings_build_the_baseline(self):
        device, alerts, _ = self._record({}, {"head": 2.0})
        self.assertEqual(alerts, [])
        self.assertEqual(device.motor_health["head"]["avg_a"], 2.0)
        self.assertEqual(device.motor_health["head"]["samples"], 1)

    def test_a_sustained_rise_over_its_own_baseline_is_wear(self):
        base = {"track": {"avg_a": 4.0, "samples": 50}}
        device, alerts, _ = self._record(base, {"track": 7.0})
        self.assertEqual(len(alerts), 1)
        # A failing reading doesn't drag the baseline up.
        self.assertEqual(device.motor_health["track"]["avg_a"], 4.0)

    def test_unknown_motors_and_junk_are_ignored(self):
        device, alerts, _ = self._record({}, {"laser": 99, "head": "x"})
        self.assertEqual(alerts, [])
        self.assertEqual(device.motor_health, {})


class UsedPriceLearningTests(SimpleTestCase):

    def _product(self):
        return mock.Mock(retail_price=Decimal("1000"), ai_suggested_price=Decimal("0"),
                         scrap_price=Decimal("200"))

    def test_calibration_nudges_the_suggestion(self):
        base = services.suggest_used_price(self._product(), 0.5)
        higher = services.suggest_used_price(self._product(), 0.5, 1.2)
        self.assertEqual(base, Decimal("600.00"))
        self.assertEqual(higher, Decimal("720.00"))

    def test_calibration_never_leaves_the_retail_band(self):
        self.assertEqual(services.suggest_used_price(self._product(), 1.0, 1.3),
                         Decimal("1000.00"))
        self.assertEqual(services.suggest_used_price(self._product(), 0.0, 0.7),
                         Decimal("200.00"))

    def _calibrate(self, pairs):
        events = [mock.Mock(sale_invoice_id=i, product_id=i, suggested_price=Decimal(str(s)))
                  for i, (s, _sold) in enumerate(pairs)]
        sold = {i: Decimal(str(p)) for i, (_s, p) in enumerate(pairs)}
        models = mock.Mock()
        models.RobotScanEvent.objects.filter.return_value.order_by.return_value.__getitem__ = \
            lambda self_, k: events
        inv = mock.Mock()
        inv.SaleInvoiceItem.objects.filter.side_effect = lambda invoice_id, product_id: mock.Mock(
            values_list=lambda *a, **k: mock.Mock(first=lambda: sold[invoice_id]))
        with mock.patch.dict("sys.modules", {"robot.models": models, "inventory.models": inv}):
            return services.used_price_calibration()

    def test_needs_enough_sales_to_learn(self):
        self.assertEqual(self._calibrate([(100, 150)] * 3), 1.0)

    def test_median_of_sold_over_suggested(self):
        self.assertAlmostEqual(self._calibrate([(100, 110)] * 6), 1.1)

    def test_bounded(self):
        self.assertEqual(self._calibrate([(100, 500)] * 6), 1.3)


class ShelfPointingTests(SimpleTestCase):

    def _point(self, shelf_map, location):
        device = mock.Mock(shelf_map=shelf_map)
        models = mock.Mock()
        with mock.patch.dict("sys.modules", {"robot.models": models}):
            services.point_to_shelf(device, location)
        return models.MotorCommandLog.objects.create

    def test_longest_prefix_wins(self):
        create = self._point({"B": {"direction": "left", "ms": 900},
                              "B3": {"direction": "right", "ms": 300}}, "b3-02")
        self.assertEqual(create.call_args.kwargs["direction"], "right")
        self.assertEqual(create.call_args.kwargs["duration_ms"], 300)

    def test_unmapped_shelf_does_nothing(self):
        self._point({"A": {"direction": "left"}}, "C1").assert_not_called()


class VoiceUsesTheFaceJustSeenTests(SimpleTestCase):

    def test_a_recent_face_match_on_this_device_counts(self):
        employee = mock.Mock()
        employee.user = None
        recent = mock.Mock(employee=employee)
        request = APIRequestFactory().post("/", {}, format="json")
        request.data = {}
        with mock.patch.object(views, "_authorized_employee", return_value=None), \
                mock.patch.object(views, "RobotAccessLog") as logs:
            logs.objects.filter.return_value.select_related.return_value \
                .order_by.return_value.first.return_value = recent
            self.assertIs(views._voice_employee(request, mock.Mock()), employee)
        window = logs.objects.filter.call_args.kwargs["created_at__gte"]
        self.assertGreater(window, timezone.now() - timedelta(seconds=61))
