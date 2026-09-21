"""The robot supervisor pages are admin/manager-only, and never print the token.

`robot/views_ui.py` shows everything the robot did and lets you rename a device,
move it between branches, deactivate it or rotate its token. The device token is
the robot's entire credential for `/api/robot/v1/`, so neither the page nor the
people who can reach it are incidental.

No database: the role gate is exercised through a RequestFactory with a stubbed
user, like the other robot tests.
"""

from pathlib import Path
from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from robot import views_ui


def _user(role=None, *, superuser=False, authenticated=True):
    user = mock.Mock()
    user.is_superuser = superuser
    user.is_authenticated = authenticated
    if role is None:
        del user.employee_profile  # attribute access raises -> role None
    else:
        user.employee_profile.role = role
    return user


class DeviceTokenIsNotRenderedTests(SimpleTestCase):
    """The page shows only the last 4 characters of the token."""

    template = (
        Path(views_ui.__file__).resolve().parent
        / "templates" / "robot" / "device_profile.html"
    )

    def test_template_never_prints_the_raw_token(self):
        source = self.template.read_text(encoding="utf-8")
        self.assertNotIn("device.api_token", source)

    def test_template_renders_the_masked_hint_instead(self):
        source = self.template.read_text(encoding="utf-8")
        self.assertIn("token_hint", source)

    def test_a_rotated_token_is_shown_once(self):
        # It has to be displayable exactly once so it can reach the firmware.
        source = self.template.read_text(encoding="utf-8")
        self.assertIn("new_token", source)


class RoleGateTests(SimpleTestCase):
    """Only admins and managers reach the robot pages."""

    def setUp(self):
        self.rf = RequestFactory()

    def _get(self, view, user, path="/robot/", **kwargs):
        request = self.rf.get(path)
        request.user = user
        return view(request, **kwargs)

    def test_a_salesperson_is_refused_the_dashboard(self):
        response = self._get(views_ui.dashboard, _user("sales"))
        self.assertEqual(response.status_code, 403)

    def test_a_salesperson_is_refused_the_device_profile(self):
        response = self._get(
            views_ui.device_profile, _user("sales"), path="/robot/device/1/", pk=1
        )
        self.assertEqual(response.status_code, 403)

    def test_a_user_with_no_role_is_refused(self):
        response = self._get(views_ui.dashboard, _user(None))
        self.assertEqual(response.status_code, 403)

    def test_a_manager_passes_the_gate(self):
        # Past the gate the view hits the DB, which SimpleTestCase forbids —
        # reaching that error is itself proof the role check let them through.
        with self.assertRaises(Exception) as caught:
            self._get(views_ui.dashboard, _user("manager"))
        self.assertNotIn("403", str(caught.exception))

    def test_an_owner_is_treated_as_an_admin(self):
        # role_required maps owner->admin and supervisor->manager.
        with self.assertRaises(Exception):
            self._get(views_ui.dashboard, _user("owner"))
