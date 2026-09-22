"""Tests for the face-authorization gate on privileged robot endpoints.

Creating a sale or moving an actuator requires a *face-authorized* employee.
The firmware scans the face once (`face/`) and then echoes the returned
`employee_id` on the calls of that visit — so the backend has to prove that
echo came from a real, recent face match on that same device, and not from
anyone who simply holds the device token.

The gate's decision is what matters here, so the access-log query is stubbed
and these need no database (SimpleTestCase) — they run anywhere, like the
pricing-guard tests next door. Run:

    python manage.py test robot.tests.test_access_gate
"""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from robot import views


class _Req:
    """Minimal stand-in for the DRF request the view helper reads."""

    def __init__(self, **data):
        self.data = data


class FaceSessionGateTests(SimpleTestCase):
    """`_authorized_employee` must not trust a bare employee_id."""

    def setUp(self):
        self.device = mock.Mock(name="device")
        self.employee = mock.Mock(name="employee")
        self.employee.user = mock.Mock(is_active=True)

    def _with_log(self, granted):
        """Patch RobotAccessLog so the gate sees `granted` as the latest match."""
        qs = mock.MagicMock()
        qs.filter.return_value = qs
        qs.select_related.return_value = qs
        qs.order_by.return_value = qs
        qs.first.return_value = granted
        return mock.patch.object(views.RobotAccessLog, "objects", qs), qs

    def test_face_embedding_is_matched_directly(self):
        with mock.patch.object(
            views.security, "identify_employee", return_value=(self.employee, 0.93)
        ):
            got = views._authorized_employee(
                _Req(face_embedding=[0.1, 0.2]), self.device
            )
        self.assertIs(got, self.employee)

    def test_unmatched_face_is_refused(self):
        with mock.patch.object(
            views.security, "identify_employee", return_value=(None, 0.40)
        ):
            got = views._authorized_employee(
                _Req(face_embedding=[0.1, 0.2]), self.device
            )
        self.assertIsNone(got)

    def test_bare_employee_id_without_a_recent_match_is_refused(self):
        # The attack: hold the device token, post employee_id=1, get a sale.
        patcher, _qs = self._with_log(None)
        with patcher:
            got = views._authorized_employee(_Req(employee_id=1), self.device)
        self.assertIsNone(got)

    def test_employee_id_is_accepted_after_a_recent_granted_match(self):
        granted = mock.Mock(employee=self.employee)
        patcher, _qs = self._with_log(granted)
        with patcher:
            got = views._authorized_employee(_Req(employee_id=1), self.device)
        self.assertIs(got, self.employee)

    def test_the_lookup_is_scoped_to_device_employee_result_and_window(self):
        granted = mock.Mock(employee=self.employee)
        patcher, qs = self._with_log(granted)
        before = timezone.now()
        with patcher:
            views._authorized_employee(_Req(employee_id=7), self.device)

        kwargs = qs.filter.call_args.kwargs
        self.assertIs(kwargs["device"], self.device)
        self.assertEqual(kwargs["employee_id"], 7)
        self.assertEqual(kwargs["result"], "granted")
        # The window must be the recent past, not "any time".
        window = before - kwargs["created_at__gte"]
        self.assertAlmostEqual(
            window.total_seconds(),
            timedelta(minutes=views._FACE_SESSION_MINUTES).total_seconds(),
            delta=30,
        )

    def test_a_deactivated_employee_is_refused_even_with_a_recent_match(self):
        self.employee.user.is_active = False
        granted = mock.Mock(employee=self.employee)
        patcher, _qs = self._with_log(granted)
        with patcher:
            got = views._authorized_employee(_Req(employee_id=1), self.device)
        self.assertIsNone(got)

    def test_nothing_offered_means_no_authorization(self):
        self.assertIsNone(views._authorized_employee(_Req(), self.device))
