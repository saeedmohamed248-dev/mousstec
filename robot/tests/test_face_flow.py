"""The camera's face checks: an empty frame is not a stranger, and a clock-in
is said out loud.

The camera posts a frame to /face/ on any motion. With the real face model
installed, a frame without a face used to fall through to the thumbnail
extractor and get logged — photo included — as an unknown person, every few
seconds while anything moved. No database: the models are stubbed.
"""

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from robot import faces, security, views


class NoFaceNoFallbackTests(SimpleTestCase):

    def test_the_real_model_finding_no_face_means_no_embedding(self):
        lib = mock.Mock()
        lib.face_encodings.return_value = []
        with mock.patch.object(faces, "_face_lib", return_value=lib), \
                mock.patch.object(faces, "_rgb_array", return_value="pixels"), \
                mock.patch.object(faces, "FACE_PROVIDER", "auto"), \
                mock.patch.object(faces, "_fallback_embedding") as fallback:
            self.assertIsNone(faces.extract_embedding(b"jpeg"))
        fallback.assert_not_called()

    def test_without_the_model_the_fallback_still_runs(self):
        with mock.patch.object(faces, "_face_lib", return_value=None), \
                mock.patch.object(faces, "FACE_PROVIDER", "auto"), \
                mock.patch.object(faces, "_fallback_embedding", return_value=[1.0]):
            self.assertEqual(faces.extract_embedding(b"jpeg"), [1.0])


class FaceEndpointTests(SimpleTestCase):

    def _post(self, *, embedding, employee=None, action="clock_in"):
        from rest_framework.test import APIRequestFactory
        data = {"image": SimpleUploadedFile("f.jpg", b"JPEG", "image/jpeg"), "purpose": "authorize"}
        request = APIRequestFactory().post("/api/robot/v1/face/", data, format="multipart")
        with mock.patch.object(views, "_device_or_401", return_value=(mock.Mock(), None)), \
                mock.patch.object(security.RobotDeviceThrottle, "allow_request", return_value=True), \
                mock.patch.object(views.faces, "extract_embedding", return_value=embedding), \
                mock.patch.object(views.security, "matching_available", return_value=True), \
                mock.patch.object(views.security, "identify_employee", return_value=(employee, 0.97)), \
                mock.patch.object(views.security, "register_attendance", return_value=(action, None)), \
                mock.patch.object(views, "RobotAccessLog") as log, \
                mock.patch.object(views, "RobotCommand") as command:
            data = views.face(request).data
        return data, log, command

    def test_an_empty_frame_is_not_logged(self):
        data, log, command = self._post(embedding=None)
        self.assertEqual(data["result"], "no_face")
        log.objects.create.assert_not_called()
        command.objects.create.assert_not_called()

    def test_a_clock_in_is_greeted_by_first_name(self):
        employee = mock.Mock(id=3)
        employee.name = "كريم محمود"
        data, _, command = self._post(embedding=[0.1], employee=employee)
        self.assertTrue(data["authorized"])
        text = command.objects.create.call_args.kwargs["payload"]["text"]
        self.assertIn("كريم", text)
        self.assertNotIn("محمود", text)
        self.assertIn("حضورك", text)

    def test_a_clock_out_says_goodbye(self):
        employee = mock.Mock(id=3)
        employee.name = "كريم"
        _, _, command = self._post(embedding=[0.1], employee=employee, action="clock_out")
        self.assertIn("انصرافك", command.objects.create.call_args.kwargs["payload"]["text"])

    def test_seeing_someone_already_clocked_in_stays_quiet(self):
        employee = mock.Mock(id=3)
        employee.name = "كريم"
        _, _, command = self._post(embedding=[0.1], employee=employee, action="authorize")
        command.objects.create.assert_not_called()
