"""The robot's learning loop, and the review fixes that keep it honest.

Learning (الروبوت يتعلم من كل حاجه):
  * a photo it has seen before is recognized by its look, within a tight
    Hamming radius — and only ever offered for confirmation;
  * shop slang staff taught ("الطرمبة") is understood inside a sentence;
  * a correction weakens the wrong memory instead of leaving it to win again;
  * "اتعلم X يعني Y" by voice is a lesson, gated by role;
  * customers' purchases are remembered as car/category preferences.

Fixes:
  * services imported `timedelta` nowhere, so /voice/ (which checks for an
    open stock-take first) and every alert raised NameError;
  * any motor move could run unbounded, or 500 on a bad number;
  * walking past the camera clocked an employee OUT minutes after clocking in;
  * an open (still-counting) stock-take could be applied to inventory.

No database: models are stubbed, like the other robot tests.
"""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from robot import customers as customers_svc
from robot import security, services, views


def _png(pattern):
    """A tiny grayscale image from an 8x8 0/1 pattern (bytes)."""
    import io
    from PIL import Image
    img = Image.new("L", (8, 8))
    img.putdata([255 if bit else 0 for bit in pattern])
    buf = io.BytesIO()
    img.resize((64, 64)).save(buf, format="PNG")
    return buf.getvalue()


class _Rows:
    """Stand-in queryset: filter/order_by/values_list/[:n] → fixed rows."""

    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a):
        return self

    def values_list(self, *a, **k):
        return self

    def __getitem__(self, item):
        return self.rows[item]

    def __iter__(self):
        return iter(self.rows)


class TheMissingImportTests(SimpleTestCase):
    """`timedelta` is used by the stock-take and alert helpers."""

    def test_open_stock_take_lookup_no_longer_crashes(self):
        models = mock.Mock()
        with mock.patch.dict("sys.modules", {"robot.models": models}):
            services.get_open_stock_take(mock.Mock())
        models.RobotStockTakeSession.objects.filter.assert_called_once()

    def test_low_battery_alert_no_longer_crashes(self):
        models = mock.Mock()
        models.RobotAlert.objects.filter.return_value.exists.return_value = False
        with mock.patch.dict("sys.modules", {"robot.models": models}):
            services.raise_low_battery_alert(mock.Mock(), 9)
        models.RobotAlert.objects.create.assert_called_once()


class MotorCommandValidationTests(SimpleTestCase):

    def test_unknown_actuator_is_refused(self):
        with self.assertRaises(ValueError):
            services.validate_motor_command("laser", "left", 100)

    def test_direction_must_fit_the_actuator(self):
        with self.assertRaises(ValueError):
            services.validate_motor_command("head", "up", 100)

    def test_stop_has_no_duration(self):
        self.assertEqual(services.validate_motor_command("track", "stop", 900),
                         ("track", "stop", 0))

    def test_zero_is_a_short_pulse_not_run_forever(self):
        _, _, ms = services.validate_motor_command("arm_left", "up", 0)
        self.assertEqual(ms, services.MOTOR_DEFAULT_MS)

    def test_long_moves_are_clamped(self):
        _, _, ms = services.validate_motor_command("track", "forward", 999999)
        self.assertEqual(ms, services.MOTOR_MAX_MS)

    def test_garbage_duration_is_a_value_error(self):
        with self.assertRaises(ValueError):
            services.validate_motor_command("head", "left", "fast")

    def test_the_motor_endpoint_answers_400_not_500(self):
        request = APIRequestFactory().post(
            "/api/robot/v1/motor/",
            {"actuator": "head", "direction": "left", "duration_ms": "fast"},
            format="json",
        )
        with mock.patch.object(views, "_device_or_401", return_value=(mock.Mock(), None)), \
                mock.patch.object(views, "_require_permission",
                                  return_value=(mock.Mock(), None)), \
                mock.patch.object(views, "MotorCommandLog") as log:
            response = views.motor(request)
        self.assertEqual(response.status_code, 400)
        log.objects.create.assert_not_called()


class VisualMemoryTests(SimpleTestCase):
    """Re-recognizing a part from a photo it was taught before."""

    PART = [1, 0] * 32

    def test_same_picture_same_hash(self):
        self.assertEqual(services.image_hash(_png(self.PART)),
                         services.image_hash(_png(self.PART)))

    def test_hamming_counts_differing_bits(self):
        self.assertEqual(services._hamming("ff", "f0"), 4)
        self.assertEqual(services._hamming("zz", "00"), 64)

    def _match(self, rows, target):
        models = mock.Mock()
        models.RobotKnowledge.objects.filter.return_value = _Rows(rows)
        inv = mock.Mock()
        inv.Product.objects.filter.side_effect = lambda pk: mock.Mock(first=lambda: f"P{pk}")
        with mock.patch.dict("sys.modules", {"robot.models": models, "inventory.models": inv}):
            return services.match_fingerprint(target)

    def test_a_near_photo_matches(self):
        product, distance = self._match([("ffffffffffffff00", 7, 3)], "ffffffffffffff03")
        self.assertEqual(product, "P7")
        self.assertEqual(distance, 2)

    def test_a_different_part_does_not(self):
        product, distance = self._match([("ffffffff00000000", 7, 3)], "00000000ffffffff")
        self.assertIsNone(product)
        self.assertIsNone(distance)

    def test_ties_go_to_the_most_confirmed(self):
        rows = [("00000000000000f0", 1, 1), ("000000000000000f", 2, 9)]
        product, _ = self._match(rows, "0000000000000000")
        self.assertEqual(product, "P2")

    def test_a_look_alone_match_is_offered_for_confirmation(self):
        request = APIRequestFactory().post(
            "/api/robot/v1/scan/", {"purpose": "pos"}, format="multipart")
        request.FILES["image"] = mock.Mock(read=lambda: b"jpeg", seek=lambda n: None)
        product = mock.Mock(id=3)
        device = mock.Mock()
        with mock.patch.object(views, "_device_or_401", return_value=(device, None)), \
                mock.patch.object(views.vision, "identify_part", return_value=("", "", 0.0)), \
                mock.patch.object(views.services, "image_hash", return_value="ab"), \
                mock.patch.object(views.services, "match_fingerprint", return_value=(product, 3)), \
                mock.patch.object(views, "RobotScanEvent") as scans, \
                mock.patch.object(views, "safe_product_payload", return_value={"id": 3}), \
                mock.patch.object(views.services, "maybe_raise_procurement_signal",
                                  return_value=None):
            scans.objects.create.return_value = mock.Mock(id=11)
            response = views.scan(request)
        self.assertTrue(response.data["found"])
        self.assertEqual(response.data["recognized_by"], "learned_look")
        self.assertTrue(response.data["needs_confirmation"])


class ShopSlangTests(SimpleTestCase):

    def _alias(self, rows, text):
        models = mock.Mock()
        models.RobotKnowledge.objects.filter.return_value = _Rows(rows)
        inv = mock.Mock()
        inv.Product.objects.filter.side_effect = lambda pk: mock.Mock(first=lambda: f"P{pk}")
        with mock.patch.dict("sys.modules", {"robot.models": models, "inventory.models": inv}):
            return services.learned_alias_in(text)

    def test_taught_word_is_found_inside_a_question(self):
        self.assertEqual(self._alias([("الطرمبة", 5)], "عندك الطرمبة بتاعة E90؟"), "P5")

    def test_the_longest_alias_wins(self):
        rows = [("طرمبة", 1), ("طرمبة مية", 2)]
        self.assertEqual(self._alias(rows, "عايز طرمبة مية"), "P2")

    def test_nothing_taught_nothing_found(self):
        self.assertIsNone(self._alias([("كنترول", 1)], "عايز فلتر"))


class CorrectionForgetsTheWrongGuessTests(SimpleTestCase):

    def _unlearn(self, hit_count):
        entry = mock.Mock(hit_count=hit_count)
        models = mock.Mock()
        models.RobotKnowledge.objects.filter.return_value.first.return_value = entry
        with mock.patch.dict("sys.modules", {"robot.models": models}):
            touched = services.unlearn(product=mock.Mock(), code="11517586925")
        return touched, entry

    def test_a_well_confirmed_mapping_is_weakened(self):
        touched, entry = self._unlearn(4)
        self.assertEqual(touched, 1)
        self.assertEqual(entry.hit_count, 3)
        entry.delete.assert_not_called()

    def test_a_single_confirmation_is_forgotten(self):
        _, entry = self._unlearn(1)
        entry.delete.assert_called_once()


class VoiceTeachingTests(SimpleTestCase):

    def _say(self, text, *, allowed):
        product = mock.Mock(id=9)
        product.name = "طرمبة مية E90"
        with mock.patch.object(views.permissions, "employee_can", return_value=allowed), \
                mock.patch.object(views.services, "find_product", return_value=product), \
                mock.patch.object(views.services, "learn_from_confirmation") as learn, \
                mock.patch.object(views.services, "get_open_stock_take") as open_st:
            result = views._handle_voice(text, mock.Mock(), mock.Mock())
        return result, learn, open_st

    def test_staff_can_teach_by_voice(self):
        (intent, reply, payload), learn, _ = self._say(
            "اتعلم الطرمبة يعني 11517586925", allowed=True)
        self.assertEqual(payload["action"], "learned")
        self.assertEqual(learn.call_args.kwargs["label"], "الطرمبة")
        self.assertIn("الطرمبة", reply)

    def test_teaching_needs_the_role(self):
        (_, _, payload), learn, _ = self._say(
            "اتعلم الطرمبة يعني 11517586925", allowed=False)
        self.assertEqual(payload["action"], "denied")
        learn.assert_not_called()

    def test_a_lesson_is_not_read_as_a_stock_count(self):
        # A trailing part number looks exactly like "<part> <qty>".
        _, _, open_st = self._say("learn pump means 11517586925", allowed=True)
        open_st.assert_not_called()


class AttendanceIsNotToggledByWalkingPastTests(SimpleTestCase):

    def _record(self, *, clock_in_min_ago=None, clock_out=None):
        rec = mock.Mock()
        rec.clock_in = (timezone.now() - timedelta(minutes=clock_in_min_ago)
                        if clock_in_min_ago is not None else None)
        rec.clock_out = clock_out
        return rec

    def _register(self, record, purpose):
        hr = mock.Mock()
        hr.AttendanceRecord.objects.get_or_create.return_value = (record, False)
        with mock.patch.dict("sys.modules", {"hr.models": hr}):
            return security.register_attendance(mock.Mock(), match_score=0.99,
                                                purpose=purpose)

    def test_first_sighting_clocks_in(self):
        action, _ = self._register(self._record(), "authorize")
        self.assertEqual(action, "clock_in")

    def test_seen_again_minutes_later_is_not_a_clock_out(self):
        rec = self._record(clock_in_min_ago=5)
        action, _ = self._register(rec, "attendance")
        self.assertEqual(action, "authorize")
        self.assertIsNone(rec.clock_out)

    def test_deliberate_check_out_after_a_shift(self):
        rec = self._record(clock_in_min_ago=8 * 60)
        action, _ = self._register(rec, "attendance")
        self.assertEqual(action, "clock_out")
        self.assertIsNotNone(rec.clock_out)

    def test_passive_sighting_only_moves_last_seen(self):
        rec = self._record(clock_in_min_ago=3 * 60)
        action, _ = self._register(rec, "authorize")
        self.assertEqual(action, "authorize")
        self.assertIsNotNone(rec.clock_out)


class StockTakeApplyTests(SimpleTestCase):

    def test_an_open_count_cannot_move_stock(self):
        session = mock.Mock(status="open")
        # __wrapped__ skips @transaction.atomic, which would need a database.
        with mock.patch.dict("sys.modules", {"inventory.models": mock.Mock()}):
            result = services.apply_stock_take.__wrapped__(session)
        self.assertFalse(result["applied"])
        session.lines.select_related.assert_not_called()


class BackOnlineAlertTests(SimpleTestCase):

    def _seen(self, minutes_ago):
        device = mock.Mock(last_seen_at=timezone.now() - timedelta(minutes=minutes_ago))
        models = mock.Mock()
        with mock.patch.dict("sys.modules", {"robot.models": models}):
            services.note_device_seen(device)
        return models.RobotAlert.objects.create

    def test_return_after_an_outage_is_announced(self):
        create = self._seen(30)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["kind"], "back_online")

    def test_a_normal_heartbeat_is_not(self):
        self._seen(0.5).assert_not_called()


class CustomerPreferenceTests(SimpleTestCase):

    def _learn(self, customer, notes=None):
        face = mock.Mock(notes=notes or {})
        models = mock.Mock()
        models.RobotCustomerFace.objects.get_or_create.return_value = (face, False)
        product = mock.Mock(car_model="BMW E90", part_category="mechanical")
        with mock.patch.dict("sys.modules", {"robot.models": models}):
            customers_svc.learn_from_purchase(customer, product, quantity=2)
        return face, models

    def test_purchases_are_tallied(self):
        face, _ = self._learn(mock.Mock(phone="01000000000"),
                              notes={"car_models": {"BMW E90": 1}})
        self.assertEqual(face.notes["car_models"]["BMW E90"], 3)
        self.assertEqual(face.notes["categories"]["mechanical"], 2)
        self.assertEqual(customers_svc.favorite(face.notes, "car_models"), "BMW E90")

    def test_the_walk_in_placeholder_learns_nothing(self):
        _, models = self._learn(mock.Mock(phone="0000000000"))
        models.RobotCustomerFace.objects.get_or_create.assert_not_called()
