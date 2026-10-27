"""«مخ موس» with the internet down: the stock and prices it keeps on disk, the
answers it works out itself, and the voice clips it says them in.

robot/tests/test_brain_offline_db.py checks the same answers against the
server's own, word for word, on a real catalog.
"""

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

from robot.brain import config, knowledge, loop, server, vad, voicebank, wavs

CATALOG = {
    "generated_at": "2026-10-24T09:00:00+02:00",
    "wake": {"name": "موس", "aliases": []},
    "parts": [
        {"part_number": "11427953125", "name": "فلتر زيت", "car_model": "", "barcode": "6221",
         "retail_price": 450.5, "stock": 3},
        {"part_number": "WP-E90", "name": "طرمبة مية", "car_model": "E90", "retail_price": 1800, "stock": 1},
        {"part_number": "WP-F30", "name": "طرمبة مية", "car_model": "F30", "retail_price": 2100, "stock": 1},
        {"part_number": "SP-1", "name": "مساعدين خلفي فيرنا", "retail_price": 950, "stock": 0},
        {"part_number": "BK-1", "name": "تيل فرامل أمامي", "retail_price": 0, "stock": 4},
        {"part_number": "OLD-1", "name": "فلتر بنزين قديم", "retail_price": 100, "stock": 2, "active": False},
    ],
    "aliases": [{"kind": "label", "key": "الطرمبة الكبيرة", "part_number": "WP-F30"}],
}


class AnswerTests(unittest.TestCase):

    def setUp(self):
        self.k = knowledge.Knowledge(CATALOG)

    def say(self, text, now=100.0):
        return self.k.answer(text, now=now)

    def test_in_stock_with_the_retail_price(self):
        r = self.say("يا موس عندك فلتر زيت؟")
        self.assertEqual(r.text, "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه و50 قرش.")
        self.assertEqual(r.segments, ["أيوه،", "فلتر زيت", "موجود، عندي منه 3 حتت", "بسعر 450 جنيه و50 قرش"])

    def test_out_of_stock_still_says_the_price(self):
        r = self.say("يا موس فيه مساعدين خلفي فيرنا")
        # Nothing to offer instead: it asks for a number (robot/restock.py).
        self.assertEqual(r.text, "مساعدين خلفي فيرنا مش موجود هنا دلوقتي، وسعره 950 جنيه. "
                                 "لو تحب أبلغك أول ما ييجي، قولي رقم موبايلك.")
        self.assertEqual(r.segments[-2:], ["وسعره 950 جنيه", "لو تحب أبلغك أول ما ييجي، قولي رقم موبايلك."])

    def test_no_price_is_never_zero_pounds(self):
        r = self.say("يا موس عندك تيل فرامل أمامي")
        self.assertIn("بس سعره مش متسجل", r.text)
        self.assertNotIn("0 جنيه", r.text)

    def test_the_car_model_picks_and_a_tie_is_asked(self):
        self.assertIn("بسعر 1800 جنيه", self.say("يا موس طرمبه ميه E90 بكام").text)
        # Same customer: their car (the E90) picks it (talk.fit, robot/memory.py).
        self.assertIn("بسعر 1800 جنيه", self.say("يا موس طرمبة مية بكام").text)
        self.say("يا موس مع السلامة")                       # someone new: no car known
        r = self.say("يا موس طرمبة مية بكام")
        self.assertEqual(r.text, "عندي أكتر من صنف: طرمبة مية (E90)، طرمبة مية (F30). تقصد أنهي؟")
        # Voiced as name + model: one clip per name, one per model.
        self.assertEqual(r.segments, ["عندي أكتر من صنف:", "طرمبة مية", "E90", "طرمبة مية", "F30", "تقصد أنهي؟"])

    def test_a_part_number_in_groups_and_a_barcode(self):
        self.assertIn("فلتر زيت", self.say("يا موس بكام ١١ ٤٢ ٧ ٩٥٣ ١٢٥").text)
        self.assertIn("فلتر زيت", self.say("يا موس 6221").text)

    def test_shop_slang_taught_to_the_robot(self):
        self.assertIn("بسعر 2100 جنيه", self.say("يا موس عندك الطرمبة الكبيرة؟").text)

    def test_inactive_parts_arent_matched_by_words(self):
        # Like the server: words in a sentence skip inactive parts (its exact
        # name still finds it, as find_product does).
        self.assertEqual(self.say("يا موس قديم بنزين").text, knowledge.NOT_FOUND)

    def test_the_name_alone_and_people_talking(self):
        self.assertEqual(self.say("يا موس").text, "أيوه، تحت أمرك.")
        self.assertIsNone(self.say("هات الشاي يا محمد", now=500.0))

    def test_a_follow_up_needs_no_name_for_20_seconds(self):
        self.say("يا موس عندك فلتر زيت", now=100.0)
        self.assertIsNotNone(self.say("وطرمبة مية E90؟", now=115.0))
        self.assertIsNone(self.say("وطرمبة مية E90؟", now=140.0))

    def test_a_follow_up_heard_late_counts_when_it_was_said(self):
        # 🐛 [FIX]: said 5 s after «أيوه، تحت أمرك», heard 16 s later on a slow computer.
        self.say("يا موس", now=100.0)
        self.assertIsNotNone(self.k.answer("عندك فلتر زيت؟", now=121.0, said=105.0))
        self.assertIsNone(self.k.answer("عندك فلتر زيت؟", now=170.0, said=150.0))

    def test_vosks_news_arabic_spelling_still_finds_it(self):
        # «فلترا» for «فلتر»: with another «زيت» part, only «زيت» matched — and it asked which.
        k = knowledge.Knowledge(dict(CATALOG, parts=CATALOG["parts"] + [
            {"part_number": "OP-1", "name": "كرتيرة زيت", "retail_price": 900, "stock": 1}]))
        self.assertIn("بسعر 450 جنيه و50 قرش", k.answer("يا موس عندك فلترا زيت", now=100.0).text)

    def test_the_shops_own_words_for_a_part(self):
        k = knowledge.Knowledge(dict(CATALOG, parts=CATALOG["parts"] + [
            {"part_number": "TR-1", "name": "إطار ميشلان", "retail_price": 2500, "stock": 2},
            {"part_number": "SP-9", "name": "شمعة احتراق", "retail_price": 120, "stock": 8}]))
        self.assertIn("بسعر 2500 جنيه", k.answer("يا موس عندك كاوتش؟", now=100.0).text)
        self.assertIn("بسعر 120 جنيه", k.answer("يا موس عامل كام البوجيهات يا باشا", now=200.0).text)
        self.assertIn("تيل فرامل أمامي", k.answer("يا موس عايز بريك باد امامي", now=300.0).text)

    def test_a_follow_up_after_the_catalog_came_again_says_the_new_price(self):
        self.say("يا موس عندك فلتر زيت", now=100.0)
        self.k.update(dict(CATALOG, parts=[dict(CATALOG["parts"][0], retail_price=480)] + CATALOG["parts"][1:]))
        self.assertIn("بسعر 480 جنيه", self.say("وبكام؟", now=110.0).text)

    def test_glued_vocative_and_spellings_of_the_name(self):
        self.assertIn("فلتر زيت", self.say("ياموس فلتر زيت").text)
        self.assertIn("فلتر زيت", self.say("يمس عندك فلتر زيت", now=300.0).text)   # Vosk's «يا موس»
        self.assertIn("فلتر زيت", self.say("موسى عندك فلتر زيت", now=400.0).text)  # Whisper's
        self.assertIsNone(self.say("السلك ده يمس الموتور", now=900.0))
        self.assertIn("فلتر زيت", self.say("يا ماوس فلتر زيت").text)

    def test_every_answer_can_be_voiced_from_the_clip_list(self):
        texts = set(self.k.clip_texts())
        for question in ("يا موس عندك فلتر زيت؟", "يا موس طرمبة مية", "يا موس مساعدين خلفي فيرنا",
                         "يا موس تيل فرامل أمامي", "يا موس فانوس", "يا موس"):
            for segment in self.say(question).segments:
                self.assertIn(segment, texts, (question, segment))

    def test_in_stock_parts_come_first_in_the_clip_list(self):
        texts = self.k.clip_texts()
        self.assertLess(texts.index("تيل فرامل أمامي"), texts.index("مساعدين خلفي فيرنا"))

    def test_kept_on_disk(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            self.k.save(path)
            again = knowledge.Knowledge.load(path)
        self.assertEqual(len(again.parts), len(self.k.parts))
        self.assertEqual(again.generated_at, CATALOG["generated_at"])
        self.assertEqual(knowledge.Knowledge.load(Path("/nonexistent/x.json")).parts, [])


def _clip(value, n=1600):
    return wavs.pcm_wav(np.full(n, value, dtype=np.int16))


class VoiceBankTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank = voicebank.VoiceBank(Path(self.tmp.name))

    def test_clips_in_a_row_with_a_breath_between(self):
        self.bank.put("أيوه،", _clip(1000))
        self.bank.put("فلتر زيت", _clip(2000, 800))
        samples, rate = self.bank.compose(["أيوه،", "فلتر زيت"])
        gap = 16000 * voicebank.GAP_MS // 1000
        self.assertEqual((rate, len(samples)), (16000, 1600 + gap + 800))
        self.assertEqual((samples[0], samples[1600], samples[-1]), (1000, 0, 2000))

    def test_one_missing_clip_means_no_composed_answer(self):
        self.bank.put("أيوه،", _clip(1))
        self.assertIsNone(self.bank.compose(["أيوه،", "فلتر زيت"]))
        self.assertEqual(self.bank.missing(["أيوه،", "فلتر زيت"]), ["فلتر زيت"])

    def test_ulaw_clips_from_the_server_are_played(self):
        from robot import audio as server_audio
        pcm = np.array([0, 3000, -3000], dtype="<i2").tobytes()
        self.bank.put("x", server_audio.make_wav(server_audio.ulaw_encode(pcm), codec=server_audio.ULAW))
        samples, _ = self.bank.compose(["x"])
        self.assertEqual(len(samples), 3)
        self.assertAlmostEqual(int(samples[1]), 3000, delta=150)

    def test_not_audio_is_never_kept(self):
        self.bank.put("x", b'{"detail": "throttled"}')
        self.assertFalse(self.bank.has("x"))

    def test_prefetch_pace_budget_and_failure(self):
        stop = threading.Event()
        calls = []

        def fetch(text):
            calls.append(text)
            if text == "c":
                raise server.ServerDown("offline")
            return _clip(5)

        with mock.patch.object(stop, "wait"):
            self.assertEqual(self.bank.prefetch(["a", "b"], fetch, stop=stop, budget=1), 1)
            self.assertEqual(self.bank.prefetch(["a", "b", "c", "d"], fetch, stop=stop), 1)
        self.assertEqual(calls, ["a", "b", "c"])        # «a» was on disk; stopped at the failure
        self.assertEqual(self.bank.coverage(["a", "b", "c", "d"]), (2, 4))


class _Ears:
    model_name, ready, error = "small", True, ""

    def __init__(self, text):
        self.text = text

    def timed(self, samples, prompt=""):
        return self.text, 0.3


class OfflineBrainTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.server = mock.Mock()
        self.speaker = mock.Mock()
        self.lines = []

    def _brain(self, text="يا موس عندك فلتر زيت؟"):
        return loop.Brain(self.cfg, ears=_Ears(text), server=self.server, mic=None,
                          speaker=self.speaker, log=self.lines.append)

    def _utt(self):
        return vad.Utterance(samples=np.zeros(8000, dtype=np.int16), speech_ms=500,
                             quietest=40, loudest=2500, cut=False)

    def _learn_catalog_and_voice(self, brain):
        self.server.catalog.return_value = CATALOG
        brain.refresh()
        for text in brain.knowledge.clip_texts():
            brain.bank.put(text, _clip(100, 160))

    def test_the_net_drops_mid_question_and_it_answers_itself(self):
        brain = self._brain()
        self._learn_catalog_and_voice(brain)
        self.server.voice.side_effect = server.ServerDown("no route")
        brain.handle(self._utt())
        self.speaker.play.assert_called_once()
        self.assertIn("[OFFLINE] 🔊 أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه و50 قرش.", self.lines)
        self.assertFalse(brain.online)

    def test_known_offline_doesnt_wait_on_the_server(self):
        brain = self._brain()
        self._learn_catalog_and_voice(brain)
        brain.online = False
        brain.handle(self._utt())
        self.server.voice.assert_not_called()
        self.speaker.play.assert_called_once()

    def test_it_knows_the_stock_from_the_start_with_no_net(self):
        first = self._brain()
        self._learn_catalog_and_voice(first)
        self.server.reset_mock()
        restarted = self._brain()                       # the mini PC rebooted, still offline
        restarted.online = False
        restarted.handle(self._utt())
        self.server.catalog.assert_not_called()
        self.speaker.play.assert_called_once()
        self.assertIn("فلتر زيت", restarted.prompt)       # the ears' hint survives too

    def test_no_voice_yet_says_the_net_is_down(self):
        brain = self._brain()
        self.server.catalog.return_value = CATALOG
        brain.refresh()
        voice = Path(self.tmp.name) / "voice"
        voice.mkdir(exist_ok=True)
        (voice / "offline.wav").write_bytes(b"OFFLINE")
        brain.online = False
        brain.handle(self._utt())
        self.speaker.play.assert_not_called()
        self.speaker.play_wav.assert_called_once_with(b"OFFLINE")
        self.assertTrue(any(line.startswith("[OFFLINE] no voice yet") for line in self.lines))

    def test_not_for_the_robot_stays_quiet_offline_too(self):
        brain = self._brain(text="هات الشاي يا محمد")
        self._learn_catalog_and_voice(brain)
        brain.online = False
        brain.handle(self._utt())
        self.speaker.play.assert_not_called()
        self.speaker.play_wav.assert_not_called()

    def test_voice_is_filled_only_online_in_rounds(self):
        brain = self._brain()
        self.server.catalog.return_value = CATALOG
        brain.refresh()
        self.server.speak.return_value = _clip(1)
        brain.online = False
        self.assertEqual(brain.fill_voice(), 0)
        brain.online = True
        with mock.patch.object(loop, "CLIP_ROUND", 5), mock.patch.object(brain._stop, "wait"):
            self.assertEqual(brain.fill_voice(), 5)
        self.assertEqual(self.server.speak.call_args.kwargs, {"codec": "ulaw"})


class ReviewFixTests(unittest.TestCase):
    """What the code review found, each pinned."""

    def test_choices_between_different_names_can_be_voiced_offline(self):
        k = knowledge.Knowledge({"parts": [
            {"part_number": "A", "name": "فلتر زيت", "car_model": "E90", "stock": 2, "retail_price": 100},
            {"part_number": "B", "name": "فلتر هواء", "car_model": "F30", "stock": 2, "retail_price": 90},
        ]})
        reply = k.answer("يا موس عندك فلتر؟", now=0.0)
        self.assertEqual(reply.text, "عندي أكتر من صنف: فلتر زيت (E90)، فلتر هواء (F30). تقصد أنهي؟")
        self.assertTrue(set(reply.segments) <= set(k.clip_texts()))

    def test_offline_finds_additional_numbers_and_a_code_stored_as_a_string(self):
        k = knowledge.Knowledge({"parts": [
            {"part_number": "P1", "name": "حساس أكسجين", "oem": "ABC12345", "additional": ["ZX-777"], "stock": 1,
             "retail_price": 500},
        ]})
        self.assertIn("حساس أكسجين", k.answer("يا موس ZX-777", now=0.0).text)
        # A string oem used to be walked letter by letter: the whole code missed.
        self.assertIn("حساس أكسجين", k.answer("يا موس ABC12345", now=0.0).text)
        self.assertEqual(k.answer("يا موس QQ999", now=0.0).text, knowledge.NOT_FOUND)

    def test_an_alternative_whose_number_has_letters(self):
        k = knowledge.Knowledge({"parts": [
            {"part_number": "MAIN-1", "name": "كويل إشعال", "oem": ["LR012345"], "stock": 0, "retail_price": 900},
            {"part_number": "LR012345", "name": "كويل بوش", "stock": 2, "retail_price": 700},
        ]})
        self.assertIn("بس عندي بديله كويل بوش موجود بسعر 700 جنيه", k.answer("يا موس كويل إشعال", now=0.0).text)

    def test_a_refused_clip_isnt_asked_for_every_round(self):
        with TemporaryDirectory() as tmp:
            bank = voicebank.VoiceBank(Path(tmp))
            stop = threading.Event()
            fetch = mock.Mock(return_value=None)        # /speak/ gives nothing for it
            with mock.patch.object(stop, "wait"):
                bank.prefetch(["اسم غريب"], fetch, stop=stop)
                bank.prefetch(["اسم غريب"], fetch, stop=stop)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(bank.missing_now(["اسم غريب"]), [])
            with mock.patch.object(voicebank.time, "monotonic", return_value=10 ** 9):
                self.assertEqual(bank.missing_now(["اسم غريب"]), ["اسم غريب"])   # tried again later

    def test_the_budget_counts_requests_not_successes(self):
        with TemporaryDirectory() as tmp:
            bank = voicebank.VoiceBank(Path(tmp))
            stop = threading.Event()
            fetch = mock.Mock(return_value=None)
            with mock.patch.object(stop, "wait"):
                bank.prefetch([str(i) for i in range(10)], fetch, stop=stop, budget=3)
            self.assertEqual(fetch.call_count, 3)
