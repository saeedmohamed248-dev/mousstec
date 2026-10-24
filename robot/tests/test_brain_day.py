"""«مخ موس» runs the shop's sound: the Quran at opening, calm music when a
manager asks, a word before closing — and never talks over the Quran.

The player is checked with a fake output (every 100 ms chunk it would play,
at the gain it would play it), the schedule with a fake clock, the loop with
the server's real media commands («play:quran», «stop», «volume:up»).
"""

import configparser
import time
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

from robot import assistant
from robot.brain import config, knowledge, loop, media, routines, server, wavs


class _Sink:
    def __init__(self):
        self.chunks, self.closed = [], False

    def write(self, chunk):
        self.chunks.append(chunk.copy())
        time.sleep(0.001)

    def close(self):
        self.closed = True


def _tone_files(folder, names=("01.wav", "02.wav"), seconds=0.3, level=10000):
    folder.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        (folder / name).write_bytes(wavs.pcm_wav(np.full(int(16000 * seconds), level + i, dtype=np.int16)))
    return folder


def _wait(cond, timeout=3.0):
    t0 = time.monotonic()
    while not cond() and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    return cond()


class PlayerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.sinks = []
        self.player = media.MediaPlayer(
            {"quran": str(_tone_files(root / "quran")), "calm": str(_tone_files(root / "calm", ("a.wav",)))},
            volume=0.5, sink_factory=lambda: self.sinks.append(_Sink()) or self.sinks[-1], log=lambda *_: None)
        self.addCleanup(self.player.stop)

    def test_plays_the_folder_in_order_round_and_round(self):
        self.assertTrue(self.player.play("quran"))
        self.assertTrue(_wait(lambda: len(self.sinks[0].chunks) > 12))
        self.player.stop()
        levels = [int(c[0]) for c in self.sinks[0].chunks]
        self.assertEqual(levels[0], 5000)                     # 10000 × volume 0.5
        self.assertIn(5000, levels[6:])                       # 01.wav again after 02.wav
        self.assertTrue(self.sinks[0].closed)
        self.assertEqual(self.player.kind, "")

    def test_the_quran_pauses_while_the_robot_speaks_and_goes_on(self):
        self.player.play("quran")
        self.assertTrue(_wait(lambda: len(self.sinks[0].chunks) >= 2))
        with self.player.speaking():
            before = len(self.sinks[0].chunks)
            time.sleep(0.15)
            during = len(self.sinks[0].chunks) - before
        self.assertLessEqual(during, 1)                       # at most the chunk in flight
        self.assertTrue(_wait(lambda: len(self.sinks[0].chunks) > before + 3))

    def test_music_is_turned_down_not_stopped(self):
        self.player.play("calm")
        self.assertTrue(_wait(lambda: len(self.sinks[0].chunks) >= 1))
        with self.player.speaking():
            n = len(self.sinks[0].chunks)
            self.assertTrue(_wait(lambda: len(self.sinks[0].chunks) > n + 1))
            ducked = int(self.sinks[0].chunks[-1][0])
        self.assertEqual(ducked, int(10000 * 0.5 * media.DUCK))

    def test_one_replaces_the_other(self):
        self.player.play("calm")
        self.player.play("quran")
        self.assertEqual(self.player.kind, "quran")
        self.assertTrue(self.sinks[0].closed)

    def test_volume_steps_and_limits(self):
        self.assertEqual(self.player.louder(), 0.65)
        for _ in range(10):
            self.player.louder()
        self.assertEqual(self.player.volume, 1.0)
        for _ in range(20):
            self.player.quieter()
        self.assertEqual(self.player.volume, 0.05)

    def test_nothing_to_play(self):
        empty = media.MediaPlayer({"quran": str(Path(self.tmp.name) / "none")}, log=lambda *_: None)
        self.assertFalse(empty.can_play("quran"))
        self.assertFalse(empty.play("quran"))
        self.assertFalse(empty.play("calm"))

    def test_a_broken_file_stops_cleanly(self):
        bad = media.MediaPlayer({"quran": "x"}, sink_factory=_Sink, log=lambda *_: None,
                                decoder=mock.Mock(side_effect=ValueError("not audio")))
        with mock.patch.object(bad, "playlist", return_value=["x.mp3"]):
            bad.play("quran")
        self.assertTrue(_wait(lambda: bad.kind == ""))

    def test_a_stream_url_is_a_playlist(self):
        radio = media.MediaPlayer({"quran": "https://radio.example/quran"}, log=lambda *_: None)
        self.assertEqual(radio.playlist("quran"), ["https://radio.example/quran"])

    def test_no_files_means_the_stations_in_order(self):
        empty = str(Path(self.tmp.name) / "none")
        chain = media.MediaPlayer({"quran": f"{empty} | https://a.example/q | https://b.example/q"},
                                  log=lambda *_: None)
        self.assertEqual(chain.playlist("quran"), ["https://a.example/q", "https://b.example/q"])
        with_files = media.MediaPlayer({"quran": f"{self.player.sources['quran']} | https://a.example/q"},
                                       log=lambda *_: None)
        self.assertTrue(all(str(p).endswith(".wav") for p in with_files.playlist("quran")))

    def test_a_dead_station_hands_over_to_the_next(self):
        def decode(item):
            if "a.example" in item:
                raise ConnectionError("down")
            yield np.full(media.CHUNK, 1000, dtype=np.int16)
        logs = []
        sinks = []
        chain = media.MediaPlayer({"quran": "https://a.example/q | https://b.example/q"}, volume=1.0,
                                  sink_factory=lambda: sinks.append(_Sink()) or sinks[-1],
                                  decoder=decode, log=logs.append)
        self.addCleanup(chain.stop)
        chain.play("quran")
        self.assertTrue(_wait(lambda: sinks and sinks[0].chunks))
        self.assertEqual(int(sinks[0].chunks[0][0]), 1000)
        self.assertTrue(any("trying the next" in line for line in logs))

    def test_the_quran_started_mid_reply_waits_for_the_reply(self):
        with self.player.speaking():
            self.player.play("quran")
            time.sleep(0.2)
            self.assertEqual(self.sinks[0].chunks if self.sinks else [], [])
        self.assertTrue(_wait(lambda: self.sinks and len(self.sinks[0].chunks) > 0))


def _day(text):
    ini = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    ini.read_string(text)
    return ini


DAY = """
[day]
open  = 12:00
close = 23:00
[routine quran]
from = open
to   = open+2:00
play = quran
[routine closing]
at  = close-0:30
say = فاضل نص ساعة على القفل
"""


class ScheduleTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "day.json"
        self.day = routines.Routines.from_ini(_day(DAY), state_path=self.state, log=lambda *_: None)

    def at(self, hhmm, day=24):
        h, m = map(int, hhmm.split(":"))
        return datetime(2026, 10, day, h, m)

    def test_the_quran_from_opening_for_two_hours(self):
        self.assertEqual(self.day.due(self.at("11:59"), ""), [])
        self.assertEqual(self.day.due(self.at("12:00"), ""), [("play", "quran")])
        self.assertEqual(self.day.due(self.at("12:30"), "quran"), [])
        self.assertEqual(self.day.due(self.at("14:00"), "quran"), [("stop", "quran")])

    def test_stopped_by_hand_stays_stopped_today_only(self):
        self.day.due(self.at("12:00"), "")
        self.day.stopped_by_hand("quran", self.at("12:20"))
        again = routines.Routines.from_ini(_day(DAY), state_path=self.state, log=lambda *_: None)
        self.assertEqual(again.due(self.at("12:40"), ""), [])                 # a restart doesn't undo it
        self.assertEqual(again.due(self.at("12:00", day=25), ""), [("play", "quran")])

    def test_a_restart_inside_the_window_starts_it_again(self):
        self.day.due(self.at("12:00"), "")
        again = routines.Routines.from_ini(_day(DAY), state_path=self.state, log=lambda *_: None)
        self.assertEqual(again.due(self.at("13:10"), ""), [("play", "quran")])

    def test_music_someone_chose_isnt_replaced(self):
        self.assertEqual(self.day.due(self.at("12:00"), "calm"), [])
        self.assertEqual(self.day.due(self.at("14:00"), "calm"), [])        # and not stopped either

    def test_a_word_before_closing_once(self):
        self.assertEqual(self.day.due(self.at("22:30"), ""), [("say", "فاضل نص ساعة على القفل")])
        self.assertEqual(self.day.due(self.at("22:31"), ""), [])
        again = routines.Routines.from_ini(_day(DAY), state_path=self.state, log=lambda *_: None)
        self.assertEqual(again.due(self.at("22:32"), ""), [])               # said already today
        self.assertEqual(self.day.due(self.at("22:45", day=25), ""), [])     # too late to say it

    def test_a_window_past_midnight(self):
        late = routines.Routines.from_ini(_day("""
[day]
open = 18:00
close = 01:00
[routine late]
from = close-2:00
to = close
play = calm
"""), log=lambda *_: None)
        self.assertEqual(late.due(self.at("23:00"), ""), [("play", "calm")])
        self.assertEqual(late.due(self.at("00:30", day=25), "calm"), [])
        self.assertEqual(late.due(self.at("01:00", day=25), "calm"), [("stop", "calm")])

    def test_days_and_bad_lines(self):
        logs = []
        week = routines.Routines.from_ini(_day("""
[day]
open = 12:00
days = sat,sun,mon,tue,wed,thu
[routine quran]
from = open
to = open+1:00
play = quran
[routine broken]
from = whenever
play = quran
"""), log=logs.append)
        self.assertEqual(len(week.routines), 1)
        self.assertTrue(any("broken" in line for line in logs))
        self.assertEqual(week.due(datetime(2026, 10, 23, 12, 0), ""), [])    # a Friday
        self.assertEqual(week.due(datetime(2026, 10, 24, 12, 0), ""), [("play", "quran")])

    def test_a_window_into_an_excluded_day_still_ends(self):
        week = routines.Routines.from_ini(_day("""
[day]
open = 18:00
close = 01:00
days = sat,sun,mon,tue,wed,thu
[routine late]
from = 23:00
to = close
play = quran
"""), log=lambda *_: None)
        thursday, friday = datetime(2026, 10, 22, 23, 0), datetime(2026, 10, 23, 1, 0)
        self.assertEqual(week.due(thursday, ""), [("play", "quran")])
        self.assertEqual(week.due(friday, "quran"), [("stop", "quran")])
        self.assertEqual(week.due(datetime(2026, 10, 23, 23, 0), ""), [])   # Friday's own window: off

    def test_times(self):
        self.assertEqual(routines.resolve("open+2:00", 720, 1380), 840)
        self.assertEqual(routines.resolve("close-0:30", 720, 1380), 1350)
        self.assertEqual(routines.resolve("open+90", 720, 1380), 810)
        self.assertEqual(routines.resolve("09:15", 0, 0), 555)
        self.assertIsNone(routines.resolve("noon", 0, 0))


class _Ears:
    model_name, ready, error = "small", True, ""

    def __init__(self, text):
        self.text = text

    def timed(self, samples, prompt=""):
        return self.text, 0.2


class BrainSoundTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.server = mock.Mock()
        self.speaker = mock.Mock()
        self.player = mock.Mock(kind="", speaking=mock.MagicMock())
        self.player.can_play.return_value = True
        self.lines = []

    def _brain(self, text, day=None, now=datetime(2026, 10, 24, 12, 0)):
        return loop.Brain(self.cfg, ears=_Ears(text), server=self.server, mic=None, speaker=self.speaker,
                          media=self.player, routines=day, log=self.lines.append, clock=lambda: now)

    def _utt(self):
        from robot.brain.vad import Utterance
        return Utterance(samples=np.zeros(8000, dtype=np.int16), speech_ms=500, quietest=40,
                         loudest=2500, cut=False)

    def test_says_it_can_play_and_plays_after_saying_so(self):
        order = []
        self.speaker.play_wav.side_effect = lambda wav: order.append("said")
        self.player.play.side_effect = lambda kind: order.append(f"play {kind}")
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="حاضر، هشغّل القرآن.", transcript="",
                                                       addressed=True, media="play:quran")
        self._brain("يا موس شغل قرآن").handle(self._utt())
        self.assertEqual(self.server.voice.call_args.kwargs["caps"], "media")
        self.assertEqual(order, ["said", "play quran"])

    def test_stop_goes_quiet_before_answering(self):
        order = []
        self.player.kind = "quran"
        self.player.stop.side_effect = lambda: order.append("stop")
        self.speaker.play_wav.side_effect = lambda wav: order.append("said")
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="حاضر، وقفت الصوت.", transcript="",
                                                       addressed=True, media="stop")
        day = mock.Mock()
        self._brain("يا موس وقف القرآن", day=day).handle(self._utt())
        self.assertEqual(order, ["stop", "said"])
        day.stopped_by_hand.assert_called_once()                  # the schedule won't restart it today

    def test_allowed_but_no_files_says_so(self):
        self.player.can_play.return_value = False
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="حاضر، هشغّل القرآن.", transcript="",
                                                       addressed=True, media="play:quran")
        self.server.speak.return_value = wavs.pcm_wav(np.zeros(160, dtype=np.int16))
        brain = self._brain("يا موس شغل قرآن")
        brain.monitor = self.server
        brain.handle(self._utt())
        self.player.play.assert_not_called()
        self.speaker.play_wav.assert_not_called()                # not the server's «حاضر»
        self.assertIn(loop.NO_FILES["quran"], self.server.speak.call_args.args)

    def test_volume(self):
        self.player.louder.return_value = 0.75
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="حاضر، علّيت الصوت.", transcript="",
                                                       addressed=True, media="volume:up")
        self._brain("يا موس علي الصوت").handle(self._utt())
        self.player.louder.assert_called_once()

    def test_the_robot_never_talks_over_the_quran(self):
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه.", transcript="", addressed=True)
        self._brain("يا موس عندك فلتر").handle(self._utt())
        self.player.speaking.assert_called()                     # the answer was said inside speaking()

    def test_the_day_starts_the_quran_and_says_its_words(self):
        day = routines.Routines.from_ini(_day(DAY), log=lambda *_: None)
        brain = self._brain("", day=day)
        brain.run_day()
        self.player.play.assert_called_once_with("quran")
        brain.clock = lambda: datetime(2026, 10, 24, 22, 30)
        brain.online = True
        brain.monitor = self.server
        self.server.speak.return_value = wavs.pcm_wav(np.zeros(160, dtype=np.int16))
        brain.run_day()
        self.server.speak.assert_called_once_with("فاضل نص ساعة على القفل", codec="ulaw")
        self.speaker.play.assert_called_once()

    def test_while_the_quran_plays_only_the_name_counts(self):
        self.player.kind = "quran"
        self._brain("إِنَّ اللَّهَ مَعَ الصَّابِرِينَ").handle(self._utt())     # the speakers, heard by the mic
        self.server.voice.assert_not_called()
        self.server.voice.return_value = server.Answer(wav=b"WAV", reply="أيوه.", transcript="", addressed=True)
        self._brain("يا موس عندك فلتر زيت").handle(self._utt())
        self.server.voice.assert_called_once()

    def test_the_days_own_words_are_dropped_from_the_mic(self):
        brain = self._brain("")
        brain.speak_samples(np.zeros(10, dtype=np.int16), 16000)
        self.assertTrue(brain._heard_itself.is_set())

    def test_without_speakers_set_up_the_server_isnt_promised_media(self):
        self.server.voice.return_value = server.Answer(wav=None, reply="", transcript="", addressed=False)
        brain = loop.Brain(self.cfg, ears=_Ears("يا موس"), server=self.server, mic=None,
                           speaker=self.speaker, log=self.lines.append)
        brain.handle(self._utt())
        self.assertEqual(self.server.voice.call_args.kwargs["caps"], "")


class CommandParityTests(unittest.TestCase):
    """The brain reads a command the way the server does — so offline it
    declines it instead of looking for a part called «شغل قرآن»."""

    PHRASES = ("شغّل قرآن", "شغلي موسيقى هادية", "وقّف الموسيقى", "علّي الصوت", "وطّي الصوت شوية",
               "عندك فلتر زيت", "وقف العربية قدام المحل", "بعنا كام النهارده", "إيه اللي قرّب يخلص",
               "النواقص", "سعر الطرمبة", "ممكن تشغل قرآن", "لو سمحت شغلنا سورة البقرة", "كفاية قرآن",
               "بسأل على كاتم صوت", "عندك كاتم صوت", "الزيت عندي قرب يخلص", "عندك نواقص فرامل",
               "حط القطعة على الرف")

    def test_what_is_and_isnt_a_command(self):
        commands = {"شغّل قرآن": "play", "ممكن تشغل قرآن": "play", "كفاية قرآن": "stop",
                    "علّي الصوت": "volume", "وطّي الصوت شوية": "volume"}
        for phrase, do in commands.items():
            self.assertEqual(assistant.media_command(phrase)["do"], do, phrase)
        # «على» normalizes to «علي»: a muffler («كاتم صوت») is not «علّي الصوت».
        for phrase in ("بسأل على كاتم صوت", "عندك كاتم صوت", "حط القطعة على الرف", "وقف العربية قدام المحل"):
            self.assertIsNone(assistant.media_command(phrase), phrase)
        for phrase in ("الزيت عندي قرب يخلص", "عندك نواقص فرامل"):
            self.assertIsNone(assistant.staff_question(phrase), phrase)

    def test_same_parse(self):
        for phrase in self.PHRASES:
            with self.subTest(phrase):
                self.assertEqual(knowledge.media_command(phrase), assistant.media_command(phrase))
                self.assertEqual(knowledge.staff_question(phrase), assistant.staff_question(phrase))

    def test_offline_it_declines_instead_of_guessing_a_part(self):
        k = knowledge.Knowledge({"parts": [{"part_number": "1", "name": "قرآن كريم مصحف", "stock": 1}]})
        reply = k.answer("يا موس شغل قرآن", now=0.0)
        self.assertEqual(reply.text, knowledge.NEEDS_NET)
        self.assertIn(knowledge.NEEDS_NET, k.clip_texts())



class QueuedWordsTests(unittest.TestCase):
    """What the server queues for the robot to say — the greeting at
    clock-in, a page for an employee, a scan's answer — «مخ موس» says and
    acks, like the ESP32 bridge did."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(api_base="x", token="y", home=Path(self.tmp.name))
        self.server = mock.Mock()
        self.speaker = mock.Mock()
        self.player = mock.Mock(kind="quran", speaking=mock.MagicMock())
        self.brain = loop.Brain(self.cfg, ears=_Ears(""), server=self.server, mic=None, speaker=self.speaker,
                                media=self.player, log=lambda *_: None)
        self.brain.online = True

    def test_says_each_and_acks_it(self):
        self.server.commands.return_value = [
            {"command_id": 7, "kind": "say", "payload": {"text": "أهلاً يا كريم، سجّلت حضورك."}},
            {"command_id": 8, "kind": "page", "payload": {"text": "كريم، مطلوب في المكتب."}},
            {"command_id": 9, "kind": "move", "payload": {"dir": "left"}},
            {"command_id": 10, "kind": "stream_start", "payload": {}},
        ]
        self.server.speak.return_value = b"WAV"
        self.assertEqual(self.brain.run_commands(), 4)
        self.assertEqual([c.args[0] for c in self.server.speak.call_args_list],
                         ["أهلاً يا كريم، سجّلت حضورك.", "كريم، مطلوب في المكتب."])
        self.assertEqual(self.speaker.play_wav.call_count, 2)
        self.player.speaking.assert_called()                     # the Quran waits while it speaks
        self.assertEqual([(c.args[0], c.args[1]) for c in self.server.ack.call_args_list],
                         [(7, True), (8, True), (9, False), (10, True)])   # no motors on this computer

    def test_no_voice_is_a_failed_ack_not_a_lost_page(self):
        self.server.commands.return_value = [{"command_id": 3, "kind": "say", "payload": {"text": "مرحبا"}}]
        self.server.speak.return_value = None
        self.brain.run_commands()
        self.server.ack.assert_called_once_with(3, False)

    def test_offline_it_doesnt_ask(self):
        self.brain.online = False
        self.assertEqual(self.brain.run_commands(), 0)
        self.server.commands.assert_not_called()

    def test_the_client_reads_and_acks_the_queue(self):
        http = mock.Mock()
        http.headers = {}
        http.get.return_value = mock.Mock(status_code=200, json=lambda: {"commands": [
            {"command_id": 1, "kind": "say", "payload": {"text": "x"}}, "junk"]})
        http.post.return_value = mock.Mock(status_code=200)
        api = server.Server("http://erp/api/robot/v1", "tok", session=http)
        self.assertEqual(api.commands(), [{"command_id": 1, "kind": "say", "payload": {"text": "x"}}])
        self.assertEqual(http.get.call_args.args[0], "http://erp/api/robot/v1/commands/pending/")
        api.ack(1, False)
        self.assertEqual(http.post.call_args.kwargs["json"], {"command_id": 1, "ok": 0})


if __name__ == "__main__":
    unittest.main()
