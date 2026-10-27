"""Queued motor moves expire instead of replaying later.

With the motors not fitted yet (bridge HAS_MOTORS 0) or the net down, head
turns toward a speaker and dashboard moves pile up unacknowledged. When the
bridge polls again it must not run that backlog — only moves queued in the
last MOTOR_COMMAND_TTL. Runs on a tenant schema like the inventory suite.
"""

from django.utils import timezone
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import services, views
from robot.models import MotorCommandLog, RobotDevice


class MotorQueueExpiryTests(ERPTenantTestCase):

    def setUp(self):
        branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-motor-test", branch=branch)
        self.token = self.device.issue_token()
        self.device.save()

    def _queue(self, direction, *, age=None):
        cmd = MotorCommandLog.objects.create(
            device=self.device, actuator="head", direction=direction, duration_ms=400)
        if age is not None:   # created_at is auto_now_add: back-date it in the DB
            MotorCommandLog.objects.filter(pk=cmd.pk).update(created_at=timezone.now() - age)
        return cmd

    def _poll(self):
        request = APIRequestFactory().get(
            "/api/robot/v1/motor/pending/", HTTP_X_ROBOT_TOKEN=self.token)
        response = views.motor_pending(request)
        self.assertEqual(response.status_code, 200)
        return [c["command_id"] for c in response.data["commands"]]

    def test_a_stale_backlog_is_never_sent(self):
        stale = self._queue("left", age=services.MOTOR_COMMAND_TTL * 2)
        fresh = self._queue("right")

        self.assertEqual(self._poll(), [fresh.pk])
        stale.refresh_from_db()
        fresh.refresh_from_db()
        # The record says the truth: the old move was never executed.
        self.assertFalse(stale.acknowledged)
        self.assertIsNone(stale.acknowledged_at)
        self.assertTrue(fresh.acknowledged)
        # And it doesn't come back on a later poll either.
        self.assertEqual(self._poll(), [])

    def test_a_move_just_inside_the_window_is_still_sent(self):
        cmd = self._queue("left", age=services.MOTOR_COMMAND_TTL / 2)
        self.assertEqual(self._poll(), [cmd.pk])
