"""«📺 الروبوت في لايف فيسبوك» (robot/fblive.py): the comments on the shop's live are read in the
background while the robot polls, each new one answered once — under the comment and aloud — and
spam, the page's own comments and the ones from before it was turned on are left.
Facebook and Gemini are stubbed."""

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch
from robot import agent, fblive
from robot.models import RobotCommand, RobotDevice


def _when(seconds_ago=0):
    return (timezone.now() - timezone.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _facebook(comments, live="LIVE1"):
    """requests.get answering Facebook: the page's live, then its comments (newest first)."""
    def get(url, params=None, timeout=None):
        resp = mock.Mock(status_code=200)
        resp.raise_for_status.return_value = None
        if url.endswith("/live_videos"):
            resp.json.return_value = {"data": [{"id": live, "status": "LIVE"}] if live else []}
        else:
            resp.json.return_value = {"data": list(reversed(comments))}
        return resp
    return mock.patch("requests.get", side_effect=get)


def _comment(cid, message, name="كريم سامي", who="u1", seconds_ago=0):
    return {"id": cid, "message": message, "from": {"id": who, "name": name}, "created_time": _when(seconds_ago)}


@override_settings(ALLOWED_HOSTS=["*"])
class FacebookLiveTests(ERPTenantTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-fblive", branch=branch)
        self.token = self.device.issue_token()
        self.device.save()
        patcher = mock.patch.object(fblive, "page", return_value=("PAGE", "page-token"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def said(self):
        return [c.payload["text"] for c in RobotCommand.objects.filter(device=self.device, kind="say")]

    def test_each_new_comment_is_answered_under_it_and_aloud_once(self):
        fblive.start(self.device)
        comments = [_comment("c1", "عندكم فانوس F30 زينون؟")]
        answer = "أهلاً يا كريم، أيوه فانوس F30 زينون موجود بـ 14999 جنيه."
        with _facebook(comments), mock.patch.object(agent, "answer", return_value=answer) as asked, \
                mock.patch("omnichannel.services.meta_api.reply_to_comment") as replied:
            self.assertEqual(fblive.turn(self.device), 1)
            self.assertEqual(fblive.turn(self.device), 0)                # answered once
        replied.assert_called_once_with(access_token="page-token", comment_id="c1", message=answer)
        self.assertIn("لايف", asked.call_args.kwargs["context"])
        self.assertIn("كريم سامي", asked.call_args.kwargs["context"])
        self.assertEqual(self.said(), [answer])                         # his name is in it already
        self.assertEqual(fblive.state(self.device)["answered"], 1)
        self.assertEqual(fblive.state(self.device)["video"], "LIVE1")

    def test_the_comment_itself_is_never_read_aloud(self):
        fblive.start(self.device)
        with _facebook([_comment("c1", "كلام وحش 01012345678", name="Troll Name")]), \
                mock.patch.object(agent, "answer", return_value="أهلاً بيك، تحب أساعدك في قطعة؟"), \
                mock.patch("omnichannel.services.meta_api.reply_to_comment"):
            fblive.turn(self.device)
        self.assertEqual(self.said(), ["يا Troll، أهلاً بيك، تحب أساعدك في قطعة؟"])

    def test_spam_old_comments_and_the_pages_own_are_left(self):
        fblive.start(self.device)
        comments = [_comment("old", "كان قبل التشغيل", seconds_ago=600),
                    _comment("mine", "رد الصفحة", who="PAGE"),
                    _comment("spam", "اربح ١٠٠٠ دولار من البيت")]
        with _facebook(comments), mock.patch.object(agent, "answer", return_value="SKIP") as asked, \
                mock.patch("omnichannel.services.meta_api.reply_to_comment") as replied:
            self.assertEqual(fblive.turn(self.device), 0)
        self.assertEqual([c.args[2] for c in asked.call_args_list], ["اربح ١٠٠٠ دولار من البيت"])
        replied.assert_not_called()
        self.assertEqual(self.said(), [])

    def test_a_flood_answers_the_newest_and_lets_the_oldest_go(self):
        fblive.start(self.device)
        comments = [_comment(f"c{i}", f"سؤال رقم {i}") for i in range(10)]
        with _facebook(comments), mock.patch.object(agent, "answer", return_value="تمام") as asked, \
                mock.patch("omnichannel.services.meta_api.reply_to_comment"):
            fblive.turn(self.device)
        self.assertEqual([c.args[2] for c in asked.call_args_list], ["سؤال رقم 4", "سؤال رقم 5"])
        seen = fblive.state(self.device)["seen"]
        self.assertIn("c0", seen)                                        # let go
        self.assertNotIn("c6", seen)                                     # next turn

    def test_off_nothing_is_read_and_turned_off_midway_nothing_more_is_said(self):
        with _facebook([_comment("c1", "سؤال")]) as get:
            self.assertEqual(fblive.turn(self.device), 0)
        get.assert_not_called()
        fblive.start(self.device)

        def stops(*args, **kwargs):
            fblive.stop(self.device)
            return "رد"
        with _facebook([_comment("c1", "سؤال"), _comment("c2", "سؤال تاني")]), \
                mock.patch.object(agent, "answer", side_effect=stops) as asked, \
                mock.patch("omnichannel.services.meta_api.reply_to_comment"):
            fblive.turn(self.device)
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(fblive.state(self.device), {})                 # not brought back

    def test_no_live_on_the_page_nothing_happens(self):
        fblive.start(self.device)
        with _facebook([], live=""), mock.patch.object(agent, "answer") as asked:
            self.assertEqual(fblive.turn(self.device), 0)
        asked.assert_not_called()

    def test_the_robots_poll_starts_a_reading_in_the_background_never_waits_on_it(self):
        from robot import tasks

        url = "/api/robot/v1/commands/pending/"
        headers = {"HTTP_X_ROBOT_TOKEN": self.token, "HTTP_HOST": self.domain.domain}
        with mock.patch.object(tasks.fblive_turn, "delay") as delay:
            self.assertEqual(Client().get(url, **headers).status_code, 200)
            delay.assert_not_called()                                    # off
            fblive.start(self.device)
            self.assertEqual(Client().get(url, **headers).status_code, 200)
            self.assertEqual(delay.call_args.args[1], self.device.pk)
            Client().get(url, **headers)
            self.assertEqual(delay.call_count, 1)                        # not again till it's done
        fblive.done(self.device)
        self._due_again()
        with mock.patch.object(tasks.fblive_turn, "delay", side_effect=ConnectionError("broker down")):
            self.assertEqual(Client().get(url, **headers).status_code, 200)   # the poll still answers
        self._due_again()
        self.assertTrue(fblive.due(self.device))                         # and the lock was let go

    def _due_again(self):
        cache.set(f"robot:fblive:{self.device.pk}", {**fblive.state(self.device), "polled": 0}, 60)

    def test_the_task_answers_and_lets_the_next_reading_go(self):
        from django.db import connection

        from robot import tasks

        fblive.start(self.device)
        self.assertTrue(fblive.due(self.device))
        with mock.patch.object(fblive, "turn", return_value=2) as turned:
            self.assertEqual(tasks.fblive_turn(connection.schema_name, self.device.pk), 2)
        turned.assert_called_once()
        self._due_again()
        self.assertTrue(fblive.due(self.device))                         # the lock was let go

    def test_the_owner_turns_it_on_and_off_from_the_robots_page(self):
        boss = User.objects.create_user("fblive_boss", password="x", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(boss)
        url = reverse("robot_ui:device_control", args=[self.device.pk])
        page = client.get(url, HTTP_HOST=self.domain.domain)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "اربط صفحة المحل")                     # no page connected yet
        from omnichannel.models import TenantChannelConfig

        cfg = TenantChannelConfig(tenant=self.tenant, facebook_page_id="PAGE")
        cfg.meta_access_token = "base-token"
        cfg.save()
        self.assertContains(client.get(url, HTTP_HOST=self.domain.domain), "fblive_start")
        client.post(url, {"action": "fblive_start", "video_id": " 123 "}, HTTP_HOST=self.domain.domain)
        self.assertEqual(fblive.state(self.device)["video"], "123")
        self.assertContains(client.get(url, HTTP_HOST=self.domain.domain), "fblive_stop")
        client.post(url, {"action": "fblive_stop"}, HTTP_HOST=self.domain.domain)
        self.assertEqual(fblive.state(self.device), {})


@override_settings(ROBOT_AGENT=True, GEMINI_API_KEY="gm-key")
class LiveCommentsAreNotThePersonInFrontTests(ERPTenantTestCase):

    def test_a_live_comment_uses_nobodys_memory(self):
        from inventory.models import Customer
        from robot import memory

        cache.clear()
        self.addCleanup(cache.clear)
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        device = RobotDevice(name="موس", device_uid="esp-fblive-2", branch=branch)
        device.issue_token()
        device.save()
        customer = Customer.objects.create(name="زبون قدام الروبوت", phone="01022223333")
        memory.remember(device, customer_id=customer.pk)
        resp = mock.Mock(status_code=200, text="")
        resp.json.return_value = {"candidates": [{"content": {"role": "model", "parts": [{"text": "أهلاً يا منى!"}]}}]}
        with mock.patch("requests.post", return_value=resp) as post:
            agent.answer(device, None, "صباح الخير", context=fblive.LIVE_CONTEXT.format(name="منى"))
        system = post.call_args.kwargs["json"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("منى", system)
        self.assertNotIn("زبون قدام الروبوت", system)
