"""Every staff-facing robot route is role-gated — including the ones serving data.

The robot pages expose a live shop camera, remote control of a physical machine,
customer records and the device token. Gating the *page* is not enough: the URL
that serves its image or data has to carry the same check, or an employee who
guesses it watches the floor anyway.

This walks the URLconf so a route added later is covered without editing a list.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import get_resolver

from robot import urls_ui


# Views deliberately reachable by any signed-in employee. Empty on purpose:
# add a name here only with a reason, and the test then stops guarding it.
_PUBLIC_TO_STAFF: set[str] = set()


def _is_role_gated(view) -> bool:
    """True if `role_required` wraps this view.

    The decorator wraps with functools.wraps, so the closure is what gives it
    away — its cell contents hold the allowed roles tuple.
    """
    closure = getattr(view, "__closure__", None) or ()
    for cell in closure:
        try:
            contents = cell.cell_contents
        except ValueError:
            continue
        if isinstance(contents, tuple) and contents and all(
            isinstance(role, str) for role in contents
        ):
            known = {"owner", "admin", "manager", "supervisor", "sales",
                     "cashier", "stock", "purchasing", "tech", "engineer",
                     "accountant", "hr", "viewer"}
            if set(contents) & known:
                return True
        if callable(contents) and _is_role_gated(contents):
            return True
    return False


class EveryRobotUiRouteIsGatedTests(SimpleTestCase):

    def test_the_urlconf_has_routes_to_check(self):
        self.assertGreater(len(urls_ui.urlpatterns), 0)

    def test_every_route_requires_a_role(self):
        ungated = []
        for pattern in urls_ui.urlpatterns:
            name = pattern.name
            if name in _PUBLIC_TO_STAFF:
                continue
            if not _is_role_gated(pattern.callback):
                ungated.append(name)
        self.assertEqual(
            ungated, [],
            f"robot UI route(s) reachable by any signed-in employee: {ungated}",
        )

    def test_the_live_camera_frame_is_gated(self):
        # Called out by name: it serves a live shop camera, and it was the one
        # route whose page was gated while its image URL was not.
        frame = next(p for p in urls_ui.urlpatterns if p.name == "live_frame")
        self.assertTrue(_is_role_gated(frame.callback))


class LiveFrameDoesNotAccumulateTests(SimpleTestCase):
    """A 24/7 camera must not fill the disk with every frame it ever sent."""

    def _post_frame(self, device):
        from unittest import mock

        from rest_framework.test import APIRequestFactory

        from robot import views

        image = SimpleUploadedFile("f.jpg", b"\xff\xd8\xff", content_type="image/jpeg")
        request = APIRequestFactory().post(
            "/api/robot/v1/camera-frame/", {"image": image}, format="multipart"
        )
        with mock.patch.object(views, "_device_or_401", return_value=(device, None)), \
             mock.patch.object(views.services, "is_after_hours", return_value=False), \
             mock.patch.object(views.services, "take_camera_commands", return_value=[]), \
             mock.patch.object(views.enrollment, "camera_prompt", return_value=None):
            return views.camera_frame(request)

    def test_the_previous_frame_is_deleted_when_replaced(self):
        from unittest import mock

        previous = mock.Mock()
        device = mock.Mock(camera_always_on=True, last_frame=previous)

        self._post_frame(device)

        previous.delete.assert_called_once()
        self.assertFalse(
            previous.delete.call_args.kwargs.get("save", True),
            "deleting the old file must not re-save the row",
        )

    def test_a_first_frame_has_nothing_to_delete(self):
        from unittest import mock

        device = mock.Mock(camera_always_on=True, last_frame=None)
        response = self._post_frame(device)  # must not raise
        self.assertEqual(response.status_code, 200)

    def test_nothing_is_stored_when_the_camera_is_switched_off(self):
        from unittest import mock

        previous = mock.Mock()
        device = mock.Mock(camera_always_on=False, last_frame=previous)

        self._post_frame(device)

        previous.delete.assert_not_called()
