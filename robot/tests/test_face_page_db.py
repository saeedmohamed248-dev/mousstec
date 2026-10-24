"""The face enrollment page (/robot/device/<pk>/faces/) opens — before,
during and after a round."""

from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import Client, override_settings
from django.urls import reverse

from hr.models import Employee
from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import enrollment, security
from robot.models import RobotDevice, RobotFaceEnrollment, RobotSnapshot


@override_settings(ALLOWED_HOSTS=["*"], DEBUG_PROPAGATE_EXCEPTIONS=True)
class FacePageTests(ERPTenantTestCase):

    def setUp(self):
        branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-faces", branch=branch)
        self.device.issue_token()
        self.device.save()
        self.boss = User.objects.create_user("faces_boss", password="x", is_staff=True, is_superuser=True,
                                             first_name="سعيد")
        self.saeed = Employee.objects.create(user=self.boss, employee_id="E-1")
        karim = User.objects.create_user("karim", password="x", first_name="كريم")
        self.karim = Employee.objects.create(user=karim, employee_id="E-2", face_encoding=[0.1] * 128)
        self.client = Client()
        self.client.force_login(self.boss)

    def page(self):
        return self.client.get(reverse("robot_ui:face_enrollment", args=[self.device.pk]),
                               HTTP_HOST=self.domain.domain)

    def test_before_any_round(self):
        self.assertEqual(self.page().status_code, 200)

    def test_the_start_button_starts_a_round(self):
        url = reverse("robot_ui:face_enrollment", args=[self.device.pk])
        with mock.patch.object(security, "matching_available", return_value=True):
            response = self.client.post(url, {"action": "start"}, HTTP_HOST=self.domain.domain)
        self.assertEqual(response.status_code, 302)                     # was a 500 (order_by "name")
        session = RobotFaceEnrollment.objects.get(device=self.device, status="active")
        self.assertEqual([e["name"] for e in session.entries], ["سعيد"])  # Karim already has a face
        self.assertEqual(self.page().status_code, 200)

    def test_during_a_round(self):
        with mock.patch.object(security, "matching_available", return_value=True):
            enrollment.start(self.device, only_missing=False, user=self.boss)
        self.assertEqual(self.page().status_code, 200)

    def test_after_a_round_with_its_pictures(self):
        snap = RobotSnapshot.objects.create(device=self.device)
        snap.image.save("face.jpg", ContentFile(b"\\xff\\xd8\\xff"), save=True)
        empty = RobotSnapshot.objects.create(device=self.device)     # a capture whose file never came
        RobotFaceEnrollment.objects.create(device=self.device, status="done", entries=[
            {"employee_id": self.saeed.pk, "name": "سعيد", "status": "done", "samples": [[0.1]],
             "snapshot_id": snap.pk},
            {"employee_id": self.karim.pk, "name": "كريم", "status": "skipped", "samples": [],
             "snapshot_id": empty.pk},
        ])
        self.assertEqual(self.page().status_code, 200)
