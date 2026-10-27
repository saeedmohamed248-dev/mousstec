"""«🎓 تعلّم السمع» end to end: «مخ موس» wasn't sure what it heard, sent the
recording with `check=1`; a teacher (Azure, then Google) heard it, its words
answered, and the pair taught the robot — within each teacher's minutes."""

import math
import os
import tempfile
from unittest import mock

import numpy as np
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, teachers, views
from robot.brain import heardfix, talk, wavs
from robot.models import RobotDevice, RobotHeard, RobotHeardFix

AZURE = {"ROBOT_AZURE_SPEECH_KEY": "az-key", "ROBOT_AZURE_SPEECH_REGION": "westeurope",
         "ROBOT_GOOGLE_STT_KEY": "", "ROBOT_AZURE_FREE_MINUTES": "300", "ROBOT_GOOGLE_FREE_MINUTES": "60"}
BOTH = dict(AZURE, ROBOT_GOOGLE_STT_KEY="g-key")
SECONDS = 2.5


def _recording(seconds=SECONDS):
    return wavs.ulaw_wav(np.full(int(16000 * seconds), 800, dtype=np.int16))


class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def azure_says(text, confidence=0.93):
    return _Resp({"RecognitionStatus": "Success", "DisplayText": text,
                  "NBest": [{"Display": text, "Confidence": confidence}]})


def azure_heard(*hearings):
    """Azure's answer with its NBest: [(text, confidence)], likeliest first."""
    return _Resp({"RecognitionStatus": "Success", "DisplayText": hearings[0][0],
                  "NBest": [{"Display": text, "Confidence": conf} for text, conf in hearings]})


def google_says(text):
    return _Resp({"results": [{"alternatives": [{"transcript": text, "confidence": 0.9}]}],
                  "totalBilledTime": "3s"})


@override_settings(GEMINI_API_KEY="")             # Gemini, the last teacher, only where a test says so
class TeacherTests(ERPTenantTestCase):

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        media = override_settings(MEDIA_ROOT=self.media.name)
        media.enable()
        self.addCleanup(media.disable)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-teach", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(self.oil, self.branch, quantity=3)

    def ask(self, said="يا موس عندك فلترا الزي", check="1", **extra):
        body = {"transcript": said, "check": check, "confidence": "0.55",
                "audio": SimpleUploadedFile("voice.wav", _recording(), content_type="audio/wav"), **extra}
        request = APIRequestFactory().post("/api/robot/v1/voice/", body, format="multipart",
                                           HTTP_X_ROBOT_TOKEN=self.token)
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            return views.voice(request).data

    def test_the_teacher_hears_it_and_the_answer_is_its(self):
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_says("يا موس، عندك فلتر زيت؟")) as post:
            data = self.ask()
        self.assertEqual((data["intent"], data["heard_by"], data["taught"]),
                         ("inventory_query", "azure", "عندك فلتر زيت"))
        self.assertIn("فلتر زيت موجود", data["reply"])
        call = post.call_args
        self.assertIn("westeurope.stt.speech.microsoft.com", call.args[0])
        self.assertEqual((call.kwargs["params"]["language"], call.kwargs["headers"]["Ocp-Apim-Subscription-Key"]),
                         ("ar-EG", "az-key"))
        self.assertEqual(call.kwargs["data"][20:22], b"\x01\x00")              # PCM, not the robot's µ-law
        heard = RobotHeard.objects.get()
        self.assertEqual((heard.local_text, heard.teacher, heard.teacher_text, heard.billed_seconds),
                         ("عندك فلترا الزي", "azure", "عندك فلتر زيت", math.ceil(SECONDS)))
        self.assertAlmostEqual(heard.local_confidence, 0.55)
        self.assertTrue(heard.audio.name.endswith(".wav"))                   # kept, to train on later

    def test_learned_when_seen_twice_and_sent_with_the_catalog(self):
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_says("يا موس عندك فلتر زيت")):
            self.ask()
            self.assertEqual(teachers.fixes(), [])                          # once is not a lesson
            self.ask()
        self.assertEqual(teachers.fixes(), [{"heard": "فلترا الزي", "means": "فلتر زيت"}])
        self.assertIn({"heard": "فلترا الزي", "means": "فلتر زيت"},
                      services.offline_catalog(self.branch)["heard_fixes"])
        RobotHeardFix.objects.update(is_active=False)                       # the owner turned it off
        self.assertEqual(teachers.fixes(), [])

    def test_never_a_lesson_in_money(self):
        teachers.learn("سجل مصروف مية جنيه", "سجل مصروف ميتين جنيه")
        teachers.learn("سجل مصروف مية جنيه", "سجل مصروف ميتين جنيه")
        self.assertFalse(RobotHeardFix.objects.exists())

    def test_azure_first_then_google_within_their_minutes(self):
        limits = dict(BOTH, ROBOT_AZURE_FREE_MINUTES=str(4 / 60))           # 4 s of Azure left
        with mock.patch.dict(os.environ, limits), \
                mock.patch("requests.post", side_effect=[azure_says("يا موس عندك فلتر زيت"),
                                                         google_says("يا موس عندك فلتر زيت")]) as post:
            self.assertEqual(self.ask()["heard_by"], "azure")                # 3 s billed: 1 s isn't enough
            self.assertEqual(self.ask()["heard_by"], "google")
        self.assertIn("speech.googleapis.com", post.call_args.args[0])
        config = post.call_args.kwargs["json"]["config"]
        self.assertEqual(config["languageCode"], "ar-EG")
        self.assertIn("فلتر زيت", config["speechContexts"][0]["phrases"])    # the shop's parts as hints
        self.assertEqual(teachers.used_seconds("google"), 3)

    def test_out_of_minutes_or_down_the_robots_own_words(self):
        with mock.patch.dict(os.environ, dict(AZURE, ROBOT_AZURE_FREE_MINUTES="0")), \
                mock.patch("requests.post") as post:
            data = self.ask("يا موس عندك فلتر زيت")
        post.assert_not_called()                                             # never past its budget
        self.assertNotIn("heard_by", data)
        self.assertIn("فلتر زيت موجود", data["reply"])
        with mock.patch.dict(os.environ, BOTH), \
                mock.patch("requests.post", side_effect=[RuntimeError("down"), google_says("يا موس عندك فلتر زيت")]):
            self.assertEqual(self.ask()["heard_by"], "google")              # Azure down: the next one

    def test_only_when_the_robot_asks(self):
        with mock.patch.dict(os.environ, AZURE), mock.patch("requests.post") as post:
            self.ask("عندك فلتر زيت")                                       # not said to the robot
            self.ask("يا موس عندك فلتر زيت", check="0")
        post.assert_not_called()
        self.assertFalse(RobotHeard.objects.exists())

    def test_a_whole_sentence_heard_the_same_twice_is_learned(self):
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_says("يا موس، عندك فلتر زيت؟")):
            self.ask("يا موس عندك سلطة الزيت")
            self.assertEqual(teachers.sentences(), [])                      # once may be a slip
            self.ask("يا موس عندك سلطة الزيت")
        lesson = {"heard": "عندك سلطه الزيت", "means": "عندك فلتر زيت", "seen": 2}   # the name taken off
        self.assertEqual(teachers.sentences(), [lesson])
        self.assertIn({"heard": "عندك سلطه الزيت", "means": "عندك فلتر زيت"},
                      services.offline_catalog(self.branch)["heard_sentences"])
        # The owner turns off the sentence on «🎓 تعلّم السمع»: not sent any more.
        RobotHeardFix.objects.create(heard="عندك سلطه الزيت", means="عندك فلتر زيت", seen=2, is_active=False)
        self.assertEqual(teachers.sentences(), [])

    def test_two_different_hearings_or_a_number_teach_no_sentence(self):
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", side_effect=[azure_says("يا موس عندك فلتر زيت"),
                                                         azure_says("يا موس عندك فلتر هوا"),
                                                         azure_says("يا موس سجل مصروف ميتين جنيه"),
                                                         azure_says("يا موس سجل مصروف ميتين جنيه"),
                                                         azure_says("You know you."),
                                                         azure_says("You know you.")]):
            for said in ("يا موس عندك فلتر", "يا موس عندك فلتر", "يا موس سجل مصروف مية جنيه",
                         "يا موس سجل مصروف مية جنيه", "يا موس اي", "يا موس اي"):
                self.ask(said)
        self.assertEqual(teachers.sentences(), [])

    @override_settings(GEMINI_API_KEY="gm-key")
    def test_gemini_is_the_last_teacher_with_the_shops_parts_in_mind(self):
        with mock.patch.dict(os.environ, dict(AZURE, ROBOT_AZURE_SPEECH_KEY="")), \
                mock.patch.object(teachers.audio_svc, "transcribe", return_value="يا موس عندك فلتر زيت") as gemini:
            data = self.ask()
        self.assertEqual((data["heard_by"], data["taught"]), ("gemini", "عندك فلتر زيت"))
        self.assertIn("فلتر زيت", gemini.call_args.kwargs["hints"])
        self.assertLessEqual(gemini.call_args.kwargs["budget"], teachers.TEACH_BUDGET_S)
        self.assertEqual(RobotHeard.objects.get().teacher, "gemini")
        with mock.patch.dict(os.environ, dict(AZURE, ROBOT_AZURE_SPEECH_KEY="", ROBOT_GEMINI_TEACH_MINUTES="0")), \
                mock.patch.object(teachers.audio_svc, "transcribe") as gemini:
            self.assertNotIn("heard_by", self.ask())
        gemini.assert_not_called()                                           # never past its budget

    def test_a_learned_sentence_heard_again_is_checked_against_the_ears(self):
        # The robot sends what a lesson made of Vosk's words, and Vosk's own words beside them:
        # the teacher's hearing is kept against those, so a wrong lesson can be unlearned.
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_says("يا موس عندك فلتر هوا")):
            data = self.ask("يا موس عندك فلتر زيت", heard="يا موس عندك سلطة الزيت")
        self.assertEqual(data["taught"], "عندك فلتر هوا")                 # the teacher's words answer
        self.assertEqual(RobotHeard.objects.get().local_text, "عندك سلطة الزيت")

    def test_the_hearing_that_makes_sense_in_the_shop_is_taken(self):
        # Azure's likeliest «عندك فانوس كابوس» means nothing here; its next, «فلتر زيت», is a part.
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_heard(("يا موس عندك فانوس كابوس", 0.81),
                                                                    ("يا موس عندك فلتر زيت", 0.78))):
            data = self.ask()
        self.assertEqual(data["taught"], "عندك فلتر زيت")
        self.assertIn("فلتر زيت موجود", data["reply"])
        self.assertEqual(RobotHeard.objects.get().teacher_text, "عندك فلتر زيت")    # and that's learned from

    def test_the_likeliest_stands_when_it_makes_sense_or_the_rest_is_a_guess(self):
        for hearings, taught in (
                ([("يا موس عندك فلتر زيت", 0.8), ("يا موس عندك فانوس كابوس", 0.79)], "عندك فلتر زيت"),
                ([("يا موس عندك فانوس كابوس", 0.9), ("يا موس عندك فلتر زيت", 0.4)], "عندك فانوس كابوس")):
            with self.subTest(taught), mock.patch.dict(os.environ, AZURE), \
                    mock.patch("requests.post", return_value=azure_heard(*hearings)):
                self.assertEqual(self.ask()["taught"], taught)

    def test_googles_other_hearings_too(self):
        said = _Resp({"results": [{"alternatives": [{"transcript": "يا موس عندك فانوس كابوس", "confidence": 0.7},
                                                     {"transcript": "يا موس عندك فلتر زيت"}]}],
                      "totalBilledTime": "3s"})
        with mock.patch.dict(os.environ, dict(BOTH, ROBOT_AZURE_SPEECH_KEY="")), \
                mock.patch("requests.post", return_value=said) as post:
            data = self.ask()
        self.assertEqual(post.call_args.kwargs["json"]["config"]["maxAlternatives"], teachers.ALTERNATIVES)
        self.assertEqual((data["heard_by"], data["taught"]), ("google", "عندك فلتر زيت"))

    def test_a_teacher_this_unsure_of_nonsense_asks_again_instead_of_guessing(self):
        # 🐛 The AI answered a sentence nobody said. Unsure and meaningless here: asked again, no AI waited on.
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_heard(("يا موس انظر الى الكابوس", 0.3))), \
                mock.patch.object(services, "ai_reply", return_value="كلام من الذكاء الاصطناعي") as ai:
            body = {"transcript": "يا موس انظر الى كابوس", "check": "1", "confidence": "0.4",
                    "audio": SimpleUploadedFile("voice.wav", _recording(), content_type="audio/wav")}
            request = APIRequestFactory().post("/api/robot/v1/voice/", body, format="multipart",
                                               HTTP_X_ROBOT_TOKEN=self.token)
            data = views.voice(request).data
        self.assertEqual(data["reply"], talk.HEARD_BADLY)
        self.assertTrue(data["unsure"])
        ai.assert_not_called()
        # As unsure, but of a part: answered.
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_heard(("يا موس عندك فلتر زيت", 0.3))):
            self.assertIn("فلتر زيت موجود", self.ask()["reply"])

    def test_the_words_the_ears_write_right_go_with_the_catalog(self):
        for _ in range(heardfix.TRUST_SEEN):
            RobotHeard.objects.create(device=self.device, local_text="عندك فلتر زيت", teacher="azure",
                                      teacher_text="عندك فلتر زيت", seconds=2)
        self.assertEqual(set(teachers.trusted_words()), {"عندك", "فلتر", "زيت"})
        self.assertEqual(set(services.offline_catalog(self.branch)["heard_trusted"]), {"عندك", "فلتر", "زيت"})
        # A slip after that: worked out again at once (the catalog's lessons aren't kept stale).
        RobotHeard.objects.create(device=self.device, local_text="فلتر زيت", teacher="azure",
                                  teacher_text="فانوس زيت", seconds=2)
        self.assertNotIn("فلتر", services.offline_catalog(self.branch)["heard_trusted"])

    def test_what_was_answered_without_a_teacher_is_counted(self):
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": "يا موس عندك فلتر زيت",
                                                                    "known": "trusted"},
                                           format="multipart", HTTP_X_ROBOT_TOKEN=self.token)
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            views.voice(request)
        from robot.models import RobotVoiceInteraction
        self.assertEqual(RobotVoiceInteraction.objects.get().payload["heard_as"], "trusted")

    def test_old_recordings_are_let_go_their_words_kept(self):
        from datetime import timedelta

        from django.utils import timezone

        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=azure_says("يا موس عندك فلتر زيت")):
            self.ask()
            self.ask()
        old, new = RobotHeard.objects.order_by("pk")
        RobotHeard.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=121))
        path = old.audio.path
        self.assertEqual(teachers.prune_audio(), 1)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual((old.audio.name, old.teacher_text), ("", "عندك فلتر زيت"))   # the words stay
        self.assertFalse(os.path.exists(path))
        self.assertTrue(new.audio.name)                                     # this month's: kept
        self.assertEqual(teachers.prune_audio(), 0)
        with mock.patch.dict(os.environ, {"ROBOT_HEARD_AUDIO_DAYS": "0"}):   # 0: kept for ever
            RobotHeard.objects.filter(pk=new.pk).update(created_at=timezone.now() - timedelta(days=999))
            self.assertEqual(teachers.prune_audio(), 0)

    def test_the_teachers_share_one_time_budget(self):
        # 🐛 Azure hanging 6 s, then Google 6 s: 12 s before thinking, and the robot waits 15.
        clock = [100.0]

        def slow_azure(*args, **kwargs):
            clock[0] += teachers.TEACH_BUDGET_S - 1                          # most of the budget, then down
            raise RuntimeError("timed out")

        with mock.patch.dict(os.environ, BOTH), mock.patch.object(teachers.time, "monotonic", lambda: clock[0]), \
                mock.patch("requests.post", side_effect=slow_azure) as post:
            data = self.ask("يا موس عندك فلتر زيت")
        self.assertEqual(post.call_count, 1)                                 # no time left for Google
        self.assertNotIn("heard_by", data)
        self.assertIn("فلتر زيت موجود", data["reply"])                       # its own words answered


@override_settings(ALLOWED_HOSTS=["*"])
class HearingPageTests(ERPTenantTestCase):

    def test_the_minutes_and_the_lessons_on_the_page(self):
        RobotHeardFix.objects.create(heard="فلترا الزي", means="فلتر زيت", seen=3)
        boss = User.objects.create_user("hearing_boss", password="x", is_staff=True, is_superuser=True)
        client = Client()
        client.force_login(boss)
        host, url = self.domain.domain, reverse("robot_ui:hearing")
        with mock.patch.dict(os.environ, AZURE):
            page = client.get(url, HTTP_HOST=host)
        self.assertContains(page, "فلترا الزي")
        self.assertContains(page, "اتستعمل 0 من 300 دقيقة الشهر ده")
        fix = RobotHeardFix.objects.get()
        client.post(url, {"fix_id": fix.pk}, HTTP_HOST=host)
        self.assertFalse(RobotHeardFix.objects.get().is_active)
        client.post(url, {"fix_id": fix.pk, "remove": "1"}, HTTP_HOST=host)
        self.assertFalse(RobotHeardFix.objects.exists())
