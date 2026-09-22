"""The live MJPEG stream has to actually stream, and stay inside its tenant.

This project serves over ASGI (daphne). Django hands a *sync* iterator on a
StreamingHttpResponse to `sync_to_async(list)`, which drains the whole
generator before sending anything — so a sync 90-second MJPEG loop shows the
viewer nothing for 90 seconds, holds every frame of it in memory, and blocks a
thread-sensitive worker with `time.sleep` the whole time. The stream must be an
async generator for the frames to leave the server as they are produced.

The frame read also has to re-enter the viewer's own schema. It runs in a
worker thread whose connection carries whatever schema was last served there,
and serving the wrong one means showing a supervisor another shop's camera.

No database: the device read is stubbed, like the other robot tests.
"""

import asyncio
import inspect
import time
from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from robot import views_ui


def _drain(response, *, stop_after=None):
    """Consume the response the way Django's ASGI handler does.

    Returns (parts, seconds_until_first_part).
    """
    started = time.monotonic()
    first_at = None
    parts = []

    async def run():
        nonlocal first_at
        async for part in response.__aiter__():
            if first_at is None:
                first_at = time.monotonic() - started
            parts.append(part)
            if stop_after is not None and len(parts) >= stop_after:
                break

    asyncio.run(run())
    return parts, first_at


class TheStreamIsIncrementalTests(SimpleTestCase):
    """A viewer sees the first frame straight away, not at the end of the session."""

    def _response(self, *, frames):
        request = RequestFactory().get("/robot/device/1/mjpeg/")
        calls = {"n": 0}

        def _fake_next_frame(pk, schema_name):
            calls["n"] += 1
            return b"JPEGBYTES%d" % calls["n"], calls["n"]

        with mock.patch.object(views_ui, "_next_frame", _fake_next_frame), \
                mock.patch.object(views_ui, "_MJPEG_MAX_SECONDS", 4.0), \
                mock.patch.object(views_ui, "_MJPEG_FPS", 20):
            response = views_ui.live_mjpeg.__wrapped__.__wrapped__(request, 1)
            return _drain(response, stop_after=frames)

    def test_the_first_frame_arrives_without_waiting_out_the_session(self):
        # Pre-fix this returns only after the full window, because the sync
        # generator is drained into a list before a byte is sent.
        parts, first_at = self._response(frames=1)
        self.assertEqual(len(parts), 1)
        self.assertLess(
            first_at, 1.0,
            "the first MJPEG part should arrive immediately, not after the "
            "whole viewing session has been buffered",
        )

    def test_frames_keep_coming(self):
        parts, _ = self._response(frames=3)
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertIn(b"Content-Type: image/jpeg", part)
            self.assertIn(views_ui._MJPEG_BOUNDARY.encode(), part)

    def test_the_generator_is_async(self):
        """The whole point, asserted without reaching into the view's internals.

        Django only streams a StreamingHttpResponse part by part when it is
        handed an async iterator; a sync one is drained into a list first.
        Nothing here is patched but the device lookup, so this holds against
        any implementation of the view.
        """
        request = RequestFactory().get("/robot/device/1/mjpeg/")
        with mock.patch.object(views_ui.RobotDevice, "objects") as objects:
            objects.filter.return_value.only.return_value.first.return_value = None
            response = views_ui.live_mjpeg.__wrapped__.__wrapped__(request, 1)
        self.assertTrue(
            inspect.isasyncgen(response.streaming_content),
            "StreamingHttpResponse must be given an async generator under ASGI, "
            "or Django buffers the whole viewing session before sending a byte",
        )


class UnchangedFramesAreNotResentTests(SimpleTestCase):

    def test_the_same_frame_is_sent_once(self):
        request = RequestFactory().get("/robot/device/1/mjpeg/")

        # Same timestamp every read: the shop is still, nothing new to send.
        def _same_frame(pk, schema_name):
            return b"JPEGBYTES", "2026-09-22T00:00:00Z"

        with mock.patch.object(views_ui, "_next_frame", _same_frame), \
                mock.patch.object(views_ui, "_MJPEG_MAX_SECONDS", 0.5), \
                mock.patch.object(views_ui, "_MJPEG_FPS", 40):
            response = views_ui.live_mjpeg.__wrapped__.__wrapped__(request, 1)
            parts, _ = _drain(response)
        self.assertEqual(len(parts), 1)


class TheFrameReadStaysInItsTenantTests(SimpleTestCase):
    """`_next_frame` must re-enter the schema it was given."""

    def test_it_enters_the_schema_it_was_handed(self):
        entered = []

        class _Ctx:
            def __init__(self, name):
                entered.append(name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.dict("sys.modules"):
            with mock.patch("django_tenants.utils.schema_context", _Ctx), \
                    mock.patch.object(views_ui.RobotDevice, "objects") as objects:
                objects.filter.return_value.only.return_value.first.return_value = None
                views_ui._next_frame(1, "tenant_b")
        self.assertEqual(entered, ["tenant_b"])

    def test_the_view_captures_the_requesting_schema(self):
        source = inspect.getsource(views_ui.live_mjpeg)
        self.assertIn("connection.schema_name", source)
