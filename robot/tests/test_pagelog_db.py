"""«📜 لوج الروبوت»: what «مخ موس» prints goes up with its heartbeat and shows on its page — and
«🔄 أعد تشغيل المخ» there restarts it — the robot's computer, inside the robot, never opened."""

import unittest
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot.brain import pagelog as brainlog
from robot.models import RobotDevice


class TheBrainSendsItsLogTests(unittest.TestCase):

    def test_lines_go_up_and_come_back_when_they_couldnt(self):
        shown = []
        log = brainlog.PageLog(shown.append)
        log("[EARS] heard «يا موس»")
        log("[VOICE] spoke 6 s after")
        self.assertEqual(shown, ["[EARS] heard «يا موس»", "[VOICE] spoke 6 s after"])
        sent = log.take()
        self.assertTrue(sent[0].endswith("[EARS] heard «يا موس»"))
        self.assertEqual(log.take(), [])
        log.back(sent)                                   # the server was down
        self.assertEqual(log.take(), sent)

    def test_the_server_asks_for_a_restart_once(self):
        monitor = mock.Mock(last_heartbeat={"restart": True})
        said = []
        self.assertTrue(brainlog.restart_asked(monitor, said.append))
        self.assertFalse(brainlog.restart_asked(monitor, said.append))


@override_settings(ALLOWED_HOSTS=["*"])
class TheRobotsPageShowsItTests(ERPTenantTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-pagelog", branch=branch)
        self.token = self.device.issue_token()
        self.device.save()

    def beat(self, **body):
        return Client().post("/api/robot/v1/heartbeat/", {"firmware_version": "brain-0.9.26", **body},
                             content_type="application/json", HTTP_X_ROBOT_TOKEN=self.token,
                             HTTP_HOST=self.domain.domain)

    def test_the_log_shows_and_a_restart_goes_down_with_the_next_beat(self):
        self.assertEqual(self.beat(log=["12:00:01 [EARS] يا موس", "12:00:03 [VOICE] spoke"]).status_code, 200)
        boss = User.objects.create_user("log_boss", password="x", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(boss)
        url = reverse("robot_ui:device_control", args=[self.device.pk])
        page = client.get(url, HTTP_HOST=self.domain.domain)
        self.assertContains(page, "[VOICE] spoke")
        self.assertNotIn("restart", self.beat().json())
        client.post(url, {"action": "brain_restart"}, HTTP_HOST=self.domain.domain)
        self.assertTrue(self.beat().json()["restart"])
        self.assertNotIn("restart", self.beat().json())                       # once
