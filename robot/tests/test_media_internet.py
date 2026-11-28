"""«شغّل من الإنترنت»: «شغلي أم كلثوم إنت عمري» / «شغل نجوم اف ام» looked up on the internet by «مخ موس»
(a song, or a station's live broadcast), and the Quran from mp3quran.net's stations with nothing set."""

import threading
import unittest
from urllib.parse import quote

import numpy as np

from robot import assistant
from robot.brain import knowledge, media, server


class SaidTests(unittest.TestCase):

    def test_a_song_or_a_station_by_name(self):
        for parse in (assistant.media_command, knowledge.media_command):        # server and offline: the same
            self.assertEqual(parse("شغلي أم كلثوم إنت عمري"),
                             {"do": "play", "what": "song", "query": "ام كلثوم انت عمري"})
            self.assertEqual(parse("شغل نجوم اف ام")["what"], "song")
            self.assertEqual(parse("شغل القرآن"), {"do": "play", "what": "quran"})
            self.assertEqual(parse("شغلي اغنيه"), {"do": "play", "what": "calm"})
            self.assertIsNone(parse("افتح الفاتورة"))                           # «افتح»: not a song

    def test_it_goes_to_the_brain_in_a_header_and_in_json(self):
        command = assistant.media_command("شغلي أم كلثوم")
        header = assistant.media_header({"media": command})
        self.assertEqual(header, "play:song:ام كلثوم")
        quote(header, safe=":").encode("latin-1")                               # a header can carry it
        answer = server.answer_from({"reply": "حاضر", "media": command}, None)
        self.assertEqual(answer.media, "play:song:ام كلثوم")


class _Sink:
    def __init__(self):
        self.written, self.closed = [], False

    def write(self, chunk):
        self.written.append(chunk)

    def close(self):
        self.closed = True


class PlayedTests(unittest.TestCase):

    def player(self):
        self.sink = _Sink()
        logged = []
        player = media.MediaPlayer({}, sink_factory=lambda: self.sink,
                                   decoder=lambda src: iter([np.ones(10, np.int16)] if "ok" in str(src) else []),
                                   log=logged.append)
        return player, logged

    def test_the_song_found_is_played_once(self):
        player, logged = self.player()
        done = threading.Event()
        found = lambda q: (done.set() or ("https://audio/ok", "أم كلثوم - إنت عمري"))
        self.assertEqual(player.play_song("ام كلثوم انت عمري", finder=found), "")
        player._worker.join(2)
        self.assertTrue(done.is_set() and self.sink.written and self.sink.closed)
        self.assertTrue(any("إنت عمري" in line for line in logged))
        self.assertEqual(player.kind, "")                                         # played once: done

    def test_nothing_found_says_so_in_the_log(self):
        player, logged = self.player()
        player.play_song("كلام مش موجود", finder=lambda q: None)
        player._worker.join(2)
        self.assertTrue(any("nothing found" in line for line in logged))

    def test_the_quran_from_the_internet_with_nothing_set(self):
        player, _ = self.player()
        self.assertTrue(all(u.startswith("https://") for u in player.playlist("quran")))
        self.assertTrue(player.can_play("quran"))
        mine = media.MediaPlayer({"quran": "https://my.station/quran"})
        self.assertEqual(mine.playlist("quran"), ["https://my.station/quran"])   # the shop's own, first
