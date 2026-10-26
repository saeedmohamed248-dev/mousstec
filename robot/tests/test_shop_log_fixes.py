"""From the shop's log of 26/10 (brain-0.8.2): what the robot heard, said and
waited for — each case as it happened, and the fix that answers it.

robot/tests/test_shop_log_fixes_db.py asks the server the same questions.
"""

import io
import os
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

from robot import audio, brain_package, services, teachers
from robot.brain import config, knowledge, loop, replies, server, slang, talk, vad
from robot.tests.test_brain_offline import _clip

HOURS = {"topic": "hours", "questions": ["مواعيدكم", "مواعيد الشغل", "بتفتحوا امتى"], "answer": "من 10 لـ 11."}
PHONE = {"topic": "phone", "questions": ["رقم تليفونكم", "رقمكم", "رقم المحل"], "answer": "0 1 1 2."}
WARRANTY = {"topic": "warranty", "questions": ["فيه ضمان"], "answer": "3 شهور."}
FILTERS = [{"name": "فلتر زيت MINI - JP", "model": ""}, {"name": "فلتر زيت - E90 - JP", "model": ""},
           {"name": "فلتر زيت - BOSCH - E90", "model": ""}]
CATALOG = {
    "generated_at": "2026-10-26T12:00:00+02:00",
    "wake": {"name": "موس", "aliases": []},
    "parts": [
        {"part_number": "OF-MINI", "name": "فلتر زيت MINI - JP", "retail_price": 350, "stock": 2},
        {"part_number": "OF-E90", "name": "فلتر زيت - E90 - JP", "retail_price": 300, "stock": 4},
        {"part_number": "OF-BOSCH", "name": "فلتر زيت - BOSCH - E90", "retail_price": 420, "stock": 1},
        {"part_number": "HOSE-1", "name": "خرطوم علوي من موتور لمبرد الزيت F25 N20 - RAPRO",
         "retail_price": 567, "stock": 0},
        {"part_number": "OIL-1", "name": "زيت موتور - SHELL", "retail_price": 900, "stock": 5},
        {"part_number": "SENS-1", "name": "حساس ساعة الزيت - F30 - BST", "retail_price": 1500, "stock": 0},
    ],
    "replies": [HOURS, PHONE],
}


class ShopRepliesHeardRightTests(unittest.TestCase):
    """«مواعيد الشغل بتاعتكو الساعة كام؟» → «تقصد حساس ساعة الزيت؟»; «مش الرقم
    بتاعها» → the branch's phone number."""

    def match(self, said):
        found = replies.match(said, [HOURS, PHONE, WARRANTY])
        return (found[0]["topic"], found[1]) if found else None

    def test_the_time_and_whose_are_no_part(self):
        self.assertEqual(self.match("مواعيد الشغل بتاعتكو الساعة كام؟"), ("hours", ""))
        self.assertEqual(self.match("مواعيد الشغل بتاعتكو النهارده الساعة كام"), ("hours", ""))
        self.assertEqual(self.match("فيه ضمان على الطرمبة؟"), ("warranty", "الطرمبه"))   # still the part's

    def test_the_parts_number_is_not_the_shops_phone(self):
        for said in ("مش الرقم بتاعها", "معرفش رقمها", "مش عارف الرقم", "الرقم كام"):
            with self.subTest(said):
                self.assertIsNone(self.match(said))
        self.assertEqual(self.match("رقمكم كام؟"), ("phone", ""))
        self.assertEqual(self.match("ممكن رقم المحل"), ("phone", ""))

    def test_offline_the_same(self):
        k = knowledge.Knowledge(CATALOG)
        self.assertEqual(k.answer("يا موس مواعيد الشغل بتاعتكو الساعة كام؟", now=10.0).text, HOURS["answer"])
        self.assertNotEqual(k.answer("يا موس مش الرقم بتاعها", now=11.0).text, PHONE["answer"])


class SmallTalkTests(unittest.TestCase):
    """«الساعة كام؟» was «تقصد حساس ساعة الزيت؟»; «خطأ» waited 8 s on the AI to say sorry."""

    def reply(self, said, now=None):
        chat = talk.small_talk(said)
        self.assertIsNotNone(chat, said)
        return talk.small_talk_reply(chat, "موس", now=now)

    def test_what_time_it_is_the_egyptian_way(self):
        for at, said in ((datetime(2026, 10, 26, 16, 20), "الساعة أربعة وتلت العصر."),
                         (datetime(2026, 10, 26, 16, 43), "الساعة خمسة إلا ربع العصر."),
                         (datetime(2026, 10, 26, 10, 0), "الساعة عشرة الصبح."),
                         (datetime(2026, 10, 26, 23, 58), "الساعة اتناشر بالليل.")):
            with self.subTest(at):
                self.assertEqual(self.reply("الساعة كام؟", at)[0], said)

    def test_wrong_is_a_sorry_and_a_new_start(self):
        said, _clips, over = self.reply("خطأ")
        self.assertEqual(said, talk.MISUNDERSTOOD)
        self.assertTrue(over)
        self.assertIn(talk.MISUNDERSTOOD, talk.PHRASES)            # in the voice bank: said offline too


class BrandsAndOriginsTests(unittest.TestCase):
    """«عندك فلتر زيت ميني كوبر؟» asked «تقصد أنهي؟» among three, then «بكام سعر
    الياباني؟» was «ملقتش القطعة»."""

    def test_arabic_said_english_written(self):
        self.assertIn("mini", slang.forms("ميني"))
        self.assertIn("jp", slang.forms("ياباني"))
        self.assertIn("bosch", slang.forms("بوش"))

    def test_picked_from_what_was_offered(self):
        self.assertEqual(talk.pick_choice("البوش", FILTERS), 2)
        self.assertEqual(talk.pick_choice("الياباني بتاع الميني", FILTERS), 0)
        self.assertIsNone(talk.pick_choice("بكام سعر الياباني", FILTERS))    # two of them are JP: asked

    def test_the_mini_filter_at_once(self):
        k = knowledge.Knowledge(CATALOG)
        reply = k.answer("يا موس عندك فلتر زيت ميني كوبر؟", now=10.0)
        self.assertIn("فلتر زيت MINI - JP", reply.text)
        self.assertIn("350 جنيه", reply.text)
        reply = k.answer("يا موس فلتر الزيت بوش", now=50.0)
        self.assertIn("420 جنيه", reply.text)


class OneWordIsNotAPartTests(unittest.TestCase):
    """«الزيت» said the hose's price (find_product took the first name with it in) —
    fixed on the server in PR 250, and here on the shop's computer now."""

    def test_asked_not_priced(self):
        k = knowledge.Knowledge(CATALOG)
        reply = k.answer("يا موس الزيت", now=10.0)
        self.assertNotIn("567", reply.text)
        self.assertIsNone(k.lookup("الزيت")[0])
        self.assertEqual(k.lookup("زيت موتور - SHELL")[0].part_number, "OIL-1")      # the whole name still is


class TeacherGuessTests(unittest.TestCase):
    """«أي» → Azure wrote «You know you.» → the robot answered in English."""

    def test_a_few_english_words_for_an_arabic_one_dont_count(self):
        self.assertFalse(teachers.usable("You know you."))
        self.assertTrue(teachers.usable("عندك، فلترز it؟"))
        self.assertTrue(teachers.usable("Do you have an oil filter?"))


class TeachEveryQuestionTests(unittest.TestCase):
    """Vosk was sure, and wrong («سلطة الزيت بوش»): with `teach = yes` every question
    to the robot goes to the teacher while the net is up."""

    def _cfg(self, value):
        with TemporaryDirectory() as tmp:
            ini = Path(tmp) / "brain.ini"
            ini.write_text(f"[server]\napi_base = x\ntoken = y\n[robot]\nteach = {value}\n", encoding="utf-8")
            cfg = config.load(ini)
        return cfg.teach, cfg.teach_all

    def test_the_settings(self):
        self.assertEqual(self._cfg("yes"), (True, True))
        self.assertEqual(self._cfg("unsure"), (True, False))
        self.assertEqual(self._cfg("no"), (False, False))

    def test_sure_and_understood_goes_too(self):
        from robot.tests.test_brain_teach import TeachTests, _Ears
        case = TeachTests("test_sure_and_understood_is_answered_here_as_before")
        case.setUp()
        try:
            case.cfg.teach_all = True
            brain = case._brain(_Ears("يا موس عندك فلتر زيت؟", [0.3, 0.2, 0.97, 0.95, 0.96]))
            case._say(brain)
            self.assertTrue(case._checked())
        finally:
            case.doCleanups()


class _Ears:
    model_name, ready, error = "vosk", True, ""

    def __init__(self, text):
        self.text = text
        self.words, self.confs = [], []

    def timed(self, samples, prompt=""):
        return self.text, 0.2


class BrainWithTheServerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.cfg.ears_use, self.cfg.answer_here, self.cfg.teach = "always", False, False
        self.server = mock.Mock()
        self.server.catalog.return_value = CATALOG
        self.server.speak.return_value = _clip(5, 160)
        self.speaker = mock.Mock()
        self.lines = []
        self.ears = _Ears("")
        self.brain = loop.Brain(self.cfg, ears=self.ears, server=self.server, mic=None,
                                speaker=self.speaker, log=self.lines.append)
        self.brain.refresh()
        for clip in self.brain.knowledge.clip_texts() + self.brain._day_texts():
            self.brain.bank.put(clip, _clip(100, 160))
        self.brain.online = True

    def say(self, text):
        self.ears.text = text
        self.brain.handle(vad.Utterance(samples=np.zeros(8000, np.int16), speech_ms=500,
                                        quietest=40, loudest=2500, cut=False))

    def test_the_net_cut_mid_conversation_keeps_it(self):
        # «يا موس» … the server answered «تقصد أنهي؟», then the next sentence timed out:
        # «[OFFLINE] not addressed to me» — the conversation was the server's.
        self.server.voice.return_value = server.Answer(
            wav=None, reply="عندي أكتر من صنف: فلتر زيت MINI - JP، فلتر زيت - E90 - JP. تقصد أنهي؟",
            transcript="", addressed=True)
        self.say("يا موس عندك فلتر زيت")
        self.server.voice.side_effect = server.ServerDown("Read timed out. (read timeout=20)")
        self.say("فلتر الزيت بوش")
        self.assertFalse(any("not addressed to me" in line for line in self.lines), self.lines)
        self.assertTrue(any(line.startswith("[OFFLINE] 🔊") and "420 جنيه" in line for line in self.lines),
                        self.lines)

    def test_a_long_wait_is_never_silent(self):
        def slow(**_kwargs):
            time.sleep(0.3)
            return server.Answer(wav=None, reply="تمام.", transcript="", addressed=True)
        self.server.voice.side_effect = slow
        with mock.patch.object(loop, "HOLD_ON_S", 0.05):
            self.say("يا موس فيه حاجة للميني")
        self.assertIn(f"[VOICE] 🔊 {loop.HOLD_ON} (the server is taking long)", self.lines)
        self.assertEqual(self.speaker.play.call_count, 1)                  # «ثانية واحدة» from the bank
        self.assertEqual(self.speaker.play_wav.call_count, 1)              # then the answer

    def test_a_quick_answer_has_no_hold_on(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="تمام.", transcript="", addressed=True)
        self.say("يا موس فيه حاجة للميني")
        self.assertFalse(any(loop.HOLD_ON in line for line in self.lines))


class TimeoutTests(unittest.TestCase):

    def test_down_is_known_in_seconds(self):
        http = mock.Mock()
        http.headers = {}
        http.post.return_value = mock.Mock(status_code=200, headers={}, json=lambda: {"reply": "", "addressed": False})
        s = server.Server("https://x/api/robot/v1", "t", session=http)
        s.voice(transcript="يا موس")
        self.assertEqual(http.post.call_args.kwargs["timeout"], (server.CONNECT_S, server.VOICE_S))
        s.heartbeat("v")
        self.assertEqual(http.post.call_args.kwargs["timeout"][0], server.CONNECT_S)


AZURE = {"ROBOT_AZURE_SPEECH_KEY": "az-key", "ROBOT_AZURE_SPEECH_REGION": "westeurope"}


class AzureVoiceTests(unittest.TestCase):
    """«voice=3086», «voice=5046»: edge-tts took 3–5 s an answer; Azure's same voice, straight PCM."""

    def setUp(self):
        audio._azure_tts_rest["until"] = 0.0
        self.addCleanup(audio._azure_tts_rest.update, until=0.0)

    def test_azure_first_when_its_key_is_set(self):
        pcm = b"\x01\x00" * 800
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=mock.Mock(status_code=200, content=pcm)) as post, \
                mock.patch.object(audio, "_synthesize_with") as edge:
            self.assertEqual(audio._voiced_pcm("أيوه، تحت أمرك.", "ar", 16000), (pcm, "azure"))
        edge.assert_not_called()
        call = post.call_args
        self.assertIn("westeurope.tts.speech.microsoft.com", call.args[0])
        self.assertEqual(call.kwargs["headers"]["X-Microsoft-OutputFormat"], "raw-16khz-16bit-mono-pcm")
        self.assertIn("ar-EG-ShakirNeural", call.kwargs["data"].decode())
        self.assertTrue(audio._keepable("azure"))

    def test_a_refused_key_rests_and_edge_speaks(self):
        with mock.patch.dict(os.environ, AZURE), \
                mock.patch("requests.post", return_value=mock.Mock(status_code=401, content=b"", text="no")) as post, \
                mock.patch.object(audio, "_synthesize_with", return_value=(None, "")):
            audio._voiced_pcm("أيوه.", "ar", 16000)
            audio._voiced_pcm("أيوه.", "ar", 16000)
        self.assertEqual(post.call_count, 1)                                # not asked again for a while

    def test_no_key_no_azure(self):
        with mock.patch.dict(os.environ, {"ROBOT_AZURE_SPEECH_KEY": "", "ROBOT_AZURE_SPEECH_REGION": ""}), \
                mock.patch("requests.post") as post, \
                mock.patch.object(audio, "_synthesize_with", return_value=(None, "")):
            audio._voiced_pcm("أيوه.", "ar", 16000)
        post.assert_not_called()


class GeminiFirstTests(unittest.TestCase):
    """«بكام سعر الياباني؟» waited 10 s: the general LLM gateway was asked first."""

    def test_gemini_answers_first_and_knows_the_time(self):
        with mock.patch.object(audio, "gemini_generate", return_value="بنفتح الساعة 10.") as gen, \
                mock.patch("inventory.ai_services.call_llm_layer") as llm:
            self.assertEqual(services.ai_reply("بتفتحوا امتى"), "بنفتح الساعة 10.")
        llm.assert_not_called()
        kwargs = gen.call_args.kwargs
        self.assertEqual((kwargs["timeout"], kwargs["budget"]), (services.REPLY_TIMEOUT_S, services.REPLY_BUDGET_S))
        prompt = gen.call_args.args[0][0]["text"]
        self.assertIn("النهارده", prompt)
        self.assertIn("والساعة", prompt)
        self.assertNotIn("Mouss Tec", prompt)


class BrainZipTests(unittest.TestCase):

    def names(self, data):
        return set(zipfile.ZipFile(io.BytesIO(data)).namelist())

    def test_with_its_key_ready_to_run(self):
        data = brain_package.build("https://fixit.mousstec.com/api/robot/v1", "TOKEN-123", "موس")
        names = self.names(data)
        for name in ("brain/__init__.py", "brain/loop.py", "run_windows.bat",
                     "brain/models/wake_features.npz", "brain.ini", "README.md"):
            self.assertIn(name, names)
        self.assertFalse(any("__pycache__" in n or n.endswith(".pyc") for n in names))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            ini = zf.read("brain.ini").decode("utf-8")
            bat = zf.read("run_windows.bat")
        self.assertIn("api_base = https://fixit.mousstec.com/api/robot/v1", ini)
        self.assertIn("token    = TOKEN-123", ini)
        self.assertIn("name = موس", ini)
        self.assertNotIn("PASTE_DEVICE_TOKEN", ini)
        self.assertIn(b"\r\n", bat)                                         # cmd.exe needs CRLF
        cfg_dir = TemporaryDirectory()
        self.addCleanup(cfg_dir.cleanup)
        path = Path(cfg_dir.name) / "brain.ini"
        path.write_text(ini, encoding="utf-8")
        cfg = config.load(path)
        self.assertEqual((cfg.api_base, cfg.token, cfg.name, cfg.teach_all),
                         ("https://fixit.mousstec.com/api/robot/v1", "TOKEN-123", "موس", True))

    def test_an_update_has_no_key(self):
        names = self.names(brain_package.build("https://x/api/robot/v1"))
        self.assertNotIn("brain.ini", names)
        self.assertIn("brain/loop.py", names)


if __name__ == "__main__":
    unittest.main()
