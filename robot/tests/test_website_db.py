"""«📥 هات المعلومات من الموقع» (robot/website.py): the shop's site read, its terms written as replies —
off until the owner reviews them; a reply the owner turned on is never touched. The site and Gemini
are stubbed."""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from inventory.tests.base import ERPTenantTestCase
from robot import audio, website
from robot.models import RobotReply

HOME = """<html><body><nav><a href="/policies/refund-policy">Refund</a> <a href="/products/x.jpg">pic</a>
<a href="https://other.com/shipping">elsewhere</a> <a href="/pages/shipping">الشحن</a></nav>
<script>var x=1;</script><h1>فيكس اتو</h1><p>مواعيدنا من 10 الصبح لـ 10 بالليل</p></body></html>"""
PAGES = {"https://fixitauto.parts": HOME,
         "https://fixitauto.parts/policies/refund-policy": "<p>الاسترجاع خلال 14 يوم والقطعة بحالتها</p>",
         "https://fixitauto.parts/pages/shipping": "<p>الشحن لكل المحافظات 75 جنيه</p>"}


def _site():
    def get(url, timeout=None, headers=None):
        resp = mock.Mock(status_code=200 if url in PAGES else 404, headers={"Content-Type": "text/html"})
        resp.text = PAGES.get(url, "")
        return resp
    return mock.patch("requests.get", side_effect=get)


SAID = {"replies": [
    {"topic": "returns", "answer": "تقدر ترجّع القطعة خلال 14 يوم وهي بحالتها.", "questions": ["ينفع أرجّع؟", "فيه استرجاع؟"]},
    {"topic": "delivery", "answer": "بنشحن لكل المحافظات بـ 75 جنيه.", "questions": ["بتوصلوا؟"]},
    {"topic": "hours", "answer": "مواعيدنا من 10 الصبح لـ 10 بالليل.", "questions": ["مواعيدكم إيه؟"]},
]}


@override_settings(ALLOWED_HOSTS=["*"])
class WebsiteRepliesTests(ERPTenantTestCase):

    def test_the_sites_pages_are_read_and_its_terms_become_drafts(self):
        with _site() as get, mock.patch.object(audio, "gemini_generate", return_value=json.dumps(SAID)) as ai:
            got = website.read("fixitauto.parts")
        read = [c.args[0] for c in get.call_args_list]
        self.assertEqual(read, ["https://fixitauto.parts", "https://fixitauto.parts/policies/refund-policy",
                                "https://fixitauto.parts/pages/shipping"])          # not the picture, not elsewhere
        sent = ai.call_args.args[0][1]["text"]
        self.assertIn("14 يوم", sent)
        self.assertNotIn("var x", sent)                                             # no scripts
        self.assertEqual((got["pages"], got["added"]), (3, 3))
        returns = RobotReply.objects.get(topic="returns")
        self.assertFalse(returns.is_active)                                         # the owner turns it on
        self.assertIn("ينفع أرجّع؟", returns.questions)

    def test_the_owners_reply_is_kept_and_a_draft_brought_up_to_date(self):
        boss = User.objects.create_user("site_boss", password="x")
        RobotReply.objects.create(topic="hours", questions="مواعيدكم؟", answer="من 9 لـ 9.", is_active=True,
                                  updated_by=boss)
        with _site(), mock.patch.object(audio, "gemini_generate", return_value=json.dumps(SAID)):
            website.read("https://fixitauto.parts")
        self.assertEqual(RobotReply.objects.get(topic="hours").answer, "من 9 لـ 9.")
        changed = {"replies": [{"topic": "delivery", "answer": "الشحن بقى 90 جنيه.", "questions": ["بتوصلوا؟"]}]}
        with _site(), mock.patch.object(audio, "gemini_generate", return_value=json.dumps(changed)):
            got = website.read()                                                    # the url remembered
        self.assertEqual(got["changed"], 1)
        self.assertEqual(RobotReply.objects.get(topic="delivery").answer, "الشحن بقى 90 جنيه.")

    def test_from_the_replies_page(self):
        boss = User.objects.create_user("site_page_boss", password="x", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(boss)
        url = reverse("robot_ui:replies")
        self.assertContains(client.get(url, HTTP_HOST=self.domain.domain), "import_site")
        with _site(), mock.patch.object(audio, "gemini_generate", return_value=json.dumps(SAID)):
            page = client.post(url, {"import_site": "1", "site_url": "fixitauto.parts"}, HTTP_HOST=self.domain.domain)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(RobotReply.objects.filter(is_active=False).count(), 3)
        with mock.patch("requests.get", side_effect=__import__("requests").ConnectionError("down")):
            page = client.post(url, {"import_site": "1", "site_url": "nowhere.example"}, HTTP_HOST=self.domain.domain)
        self.assertContains(page, "الموقع مردّش")
