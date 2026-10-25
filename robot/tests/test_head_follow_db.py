"""«الراس يلف مع اللي بيتكلم»: while someone talks with the robot, the face in
the camera's frames turns its head toward them — a short pulse, after the face
was seen twice in the same place, and never again before the head settles.
Runs on a tenant schema like the inventory suite; the face detector is faked
(dlib isn't in CI) except for the one test that needs the real one."""

import importlib.util
import io
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import headfollow, services, views
from robot.models import MotorCommandLog, RobotDevice

ON = {"ROBOT_HEAD_FOLLOW": "1", "ROBOT_HEAD_FLIP": "", "ROBOT_HEAD_EDGE_MS": ""}


def _jpeg(width=640, height=480):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (120, 120, 120)).save(out, "JPEG")
    return out.getvalue()


class _Box:
    """What dlib's detector returns, as far as face_offset reads it."""

    def __init__(self, left, width):
        self._left, self._width = left, width

    def left(self):
        return self._left

    def right(self):
        return self._left + self._width

    def width(self):
        return self._width


def _detector(*boxes):
    return mock.patch.object(headfollow, "_detector", return_value=lambda image, up: list(boxes))


class WhereTheFaceIsTests(unittest.TestCase):
    """The camera's VGA frame is looked at 480 wide: the boxes are in those pixels."""

    def test_the_middle_is_zero_and_the_edges_one(self):
        with _detector(_Box(190, 100)):                       # centered on 240 of 480
            self.assertAlmostEqual(headfollow.face_offset(_jpeg()), 0.0)
        with _detector(_Box(340, 100)):                       # centered on 390
            self.assertAlmostEqual(headfollow.face_offset(_jpeg()), 390 / 240 - 1)
        with _detector(_Box(0, 60)):
            self.assertAlmostEqual(headfollow.face_offset(_jpeg()), 30 / 240 - 1)

    def test_the_nearest_face_is_followed(self):
        with _detector(_Box(20, 50), _Box(300, 120)):         # far on the left, near on the right
            self.assertAlmostEqual(headfollow.face_offset(_jpeg()), 360 / 240 - 1)

    def test_of_two_alike_the_one_it_was_facing(self):
        boxes = (_Box(40, 100), _Box(340, 110))
        with _detector(*boxes):
            self.assertGreater(headfollow.face_offset(_jpeg()), 0)          # the bigger one
            self.assertLess(headfollow.face_offset(_jpeg(), near=-0.6), 0)  # but it was talking to this one

    def test_the_frame_is_looked_at_480_wide_at_twice_the_size(self):
        seen = []

        def detector(image, up):
            seen.append((image.shape, up))
            return [_Box(190, 80)]

        with mock.patch.object(headfollow, "_detector", return_value=detector):
            for frame in (_jpeg(), _jpeg(1600, 1200)):
                self.assertAlmostEqual(headfollow.face_offset(frame), (190 + 40) / 240 - 1)
        self.assertEqual(seen, [((360, 480), 1)] * 2)

    def test_no_face_no_dlib_or_no_picture_is_none(self):
        with _detector():
            self.assertIsNone(headfollow.face_offset(_jpeg()))
        with mock.patch.object(headfollow, "_detector", return_value=None):
            self.assertIsNone(headfollow.face_offset(_jpeg()))
        with _detector(_Box(190, 100)):
            self.assertIsNone(headfollow.face_offset(b"not a jpeg"))
            self.assertIsNone(headfollow.face_offset(b""))

    @unittest.skipUnless(importlib.util.find_spec("dlib"), "dlib is in the server image, not CI")
    def test_the_real_detector_sees_no_face_in_a_blank_frame(self):
        headfollow._detector.cache_clear()
        self.assertIsNone(headfollow.face_offset(_jpeg()))


class HeadFollowTests(ERPTenantTestCase):

    def setUp(self):
        branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-head-follow", branch=branch)
        self.token = self.device.issue_token()
        self.device.listening_until = timezone.now() + timedelta(minutes=5)   # someone's talking
        self.device.save()
        for what in ("seen", "moved"):
            cache.delete(headfollow._key(self.device, what))
            self.addCleanup(cache.delete, headfollow._key(self.device, what))
        env = mock.patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)

    def look(self, *offsets, start=None, every=0.4):
        """Frames with the face at each offset, `every` seconds apart → the moves."""
        start = start or timezone.now()
        moves = []
        for i, offset in enumerate(offsets):
            with mock.patch.object(headfollow, "face_offset", return_value=offset):
                moves.append(headfollow.follow(self.device, b"jpeg",
                                               now=start + timedelta(seconds=i * every)))
        return moves

    def test_seen_twice_in_one_place_the_head_turns_toward_them(self):
        first, second = self.look(0.6, 0.62)
        self.assertIsNone(first)                                  # once could be someone passing by
        self.assertEqual((second.actuator, second.direction, second.duration_ms), ("head", "right", 155))
        self.assertEqual(MotorCommandLog.objects.count(), 1)
        left = self.look(-0.5, -0.5, start=timezone.now() + timedelta(seconds=5))[1]
        self.assertEqual((left.direction, left.duration_ms), ("left", 125))

    def test_a_face_passing_by_is_not_chased(self):
        self.assertEqual(self.look(0.7, -0.3, 0.5), [None, None, None])
        self.assertFalse(MotorCommandLog.objects.exists())

    def test_seen_too_long_ago_counts_once(self):
        self.assertEqual(self.look(0.6, 0.6, every=headfollow.SEEN_WITHIN_S + 0.5), [None, None])

    def test_already_facing_them_it_stays_put(self):
        self.assertEqual(self.look(0.1, -0.05, 0.12), [None, None, None])
        self.assertFalse(MotorCommandLog.objects.exists())

    def test_it_waits_for_the_head_to_settle(self):
        start = timezone.now()
        move = self.look(0.8, 0.8, start=start)[1]
        self.assertIsNotNone(move)
        settled = move.created_at + timedelta(milliseconds=move.duration_ms) + headfollow.SETTLE
        with mock.patch.object(headfollow, "face_offset") as offset:
            self.assertIsNone(headfollow.follow(self.device, b"jpeg", now=settled - timedelta(milliseconds=50)))
        offset.assert_not_called()                                # frames while it runs aren't looked at
        # Then it looks afresh: twice again from where the head stopped.
        self.assertEqual([m is None for m in self.look(0.4, 0.4, start=settled)], [True, False])

    def test_a_move_that_brings_them_no_nearer_isnt_repeated(self):
        # The head at its limit switch, or the camera not on the head: the face
        # stays where it was however the head is told to turn.
        start = timezone.now()
        rounds = [self.look(0.6, 0.6, start=start + timedelta(seconds=s))[1] for s in (0, 3, 6, 9)]
        self.assertEqual([m is not None for m in rounds], [True, True, False, False])
        later = start + timedelta(seconds=3.4 + headfollow.RETRY_AFTER_S + 1)
        self.assertIsNotNone(self.look(0.6, 0.6, start=later)[1])     # it tries again later
        # Facing them, or the other way, is a fresh start.
        self.assertEqual(self.look(0.6, -0.4, -0.4, start=later + timedelta(seconds=3))[2].direction, "left")

    def test_a_shelf_pointed_at_is_not_undone_at_once(self):
        MotorCommandLog.objects.create(device=self.device, actuator="head", direction="left", duration_ms=600)
        self.assertEqual(self.look(0.6, 0.6), [None, None])

    def test_nobody_talking_or_off_it_stays_put(self):
        self.device.listening_until = timezone.now() - timedelta(seconds=1)
        self.assertEqual(self.look(0.6, 0.6), [None, None])
        self.device.listening_until = timezone.now() + timedelta(minutes=5)
        with mock.patch.dict(os.environ, {"ROBOT_HEAD_FOLLOW": ""}):
            self.assertEqual(self.look(0.6, 0.6), [None, None])
        self.assertFalse(MotorCommandLog.objects.exists())

    def test_no_face_starts_over(self):
        self.assertEqual(self.look(0.6, None, 0.6), [None, None, None])

    def test_the_head_is_calibrated_in_the_env(self):
        with mock.patch.dict(os.environ, {"ROBOT_HEAD_FLIP": "1", "ROBOT_HEAD_EDGE_MS": "400"}):
            move = self.look(1.0, 1.0)[1]
        self.assertEqual((move.direction, move.duration_ms), ("left", 400))   # mounted the other way round
        with mock.patch.dict(os.environ, {"ROBOT_HEAD_EDGE_MS": "99999"}):
            self.assertEqual(services.head_edge_ms(), services.MOTOR_MAX_MS)
        with mock.patch.dict(os.environ, {"ROBOT_HEAD_EDGE_MS": "fast"}):
            self.assertEqual(services.head_edge_ms(), services.HEAD_EDGE_MS)
        self.assertIsNone(services.look_at(self.device, "nan"))           # not a full turn right
        self.assertEqual(services.look_at(self.device, 0.151).duration_ms, services.HEAD_MIN_MS)


class CameraFrameFollowsTests(ERPTenantTestCase):
    """The ESP32-CAM's live frames are what the head follows."""

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        media = override_settings(MEDIA_ROOT=self.media.name)
        media.enable()
        self.addCleanup(media.disable)
        branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-cam-follow", branch=branch)
        self.token = self.device.issue_token()
        self.device.listening_until = timezone.now() + timedelta(minutes=5)
        self.device.save()
        for what in ("seen", "moved"):
            cache.delete(headfollow._key(self.device, what))
            self.addCleanup(cache.delete, headfollow._key(self.device, what))

    def push(self):
        body = {"image": SimpleUploadedFile("frame.jpg", _jpeg(), content_type="image/jpeg"), "motion": "0"}
        request = APIRequestFactory().post("/api/robot/v1/camera/frame/", body, format="multipart",
                                           HTTP_X_ROBOT_TOKEN=self.token)
        response = views.camera_frame(request)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_two_frames_of_a_face_on_the_right_turn_the_head_right(self):
        with mock.patch.dict(os.environ, ON), _detector(_Box(360, 100)):
            first = self.push()
            self.push()
        self.assertEqual(first["push_interval_ms"], headfollow.PUSH_MS)   # faster while following
        move = MotorCommandLog.objects.get()
        self.assertEqual((move.actuator, move.direction), ("head", "right"))
        self.device.refresh_from_db()
        self.assertTrue(self.device.last_frame.name)                       # the live view still gets it
        self.device.last_frame.open("rb")
        self.addCleanup(self.device.last_frame.close)
        self.assertEqual(self.device.last_frame.read(), _jpeg())

    def test_off_the_frames_arent_looked_at(self):
        with mock.patch.dict(os.environ, {"ROBOT_HEAD_FOLLOW": ""}), \
                mock.patch.object(headfollow, "face_offset") as offset:
            data = self.push()
        offset.assert_not_called()
        self.assertEqual(data["push_interval_ms"], self.device.desired_push_interval_ms())
        self.assertFalse(MotorCommandLog.objects.exists())

    def test_a_failure_never_costs_the_upload(self):
        with mock.patch.dict(os.environ, ON), \
                mock.patch.object(headfollow, "follow", side_effect=RuntimeError("dlib crashed")):
            data = self.push()
        self.assertTrue(data["ok"])
        self.device.refresh_from_db()
        self.assertTrue(self.device.last_frame.name)
