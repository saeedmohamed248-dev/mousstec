"""The robot itself: hear → understand here → ask the ERP → say the answer.

One utterance at a time, like a person behind the counter: while the robot
answers it doesn't listen, and what the mic caught meanwhile (its own voice)
is dropped before it listens again.

With the internet down it answers stock and price questions itself — from
the branch's catalog on disk and the voice clips kept beside it — in the
same words the server would use.

The shop's sound is its job too: the Quran when the shop opens, calm music
when a manager asks (the server checks who's asking — robot/assistant.py),
the Quran paused — never talked over — whenever the robot speaks.
"""

import threading
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from . import VERSION, wavs
from .ears import hints
from .knowledge import Knowledge, strip_name
from .server import ServerDown, Unauthorized
from .vad import Segmenter
from .voicebank import VoiceBank

HEARTBEAT_S = 30
CATALOG_S = 15 * 60                  # the stock changes; the hint follows
OFFLINE_TEXT = "معلش، النت فاصل دلوقتي. جرّب كمان شوية."
OFFLINE_REPEAT_S = 30
CLIP_ROUND = 60                      # clips per round, then a heartbeat's rest
ROUTINE_TICK_S = 20
NO_FILES = {
    "quran": "مش لاقي ملفات القرآن على الجهاز. حط الملفات في الفولدر اللي مكتوب في brain.ini.",
    "calm": "مش لاقي ملفات موسيقى على الجهاز. حط الملفات في الفولدر اللي مكتوب في brain.ini.",
}


class Brain:
    def __init__(self, cfg, *, ears, server, mic, speaker, monitor=None, media=None,
                 routines=None, log=print, clock=datetime.now):
        self.cfg, self.ears, self.server, self.mic, self.speaker = cfg, ears, server, mic, speaker
        self.monitor = monitor or server      # heartbeat + catalog: its own HTTP session
        self.media, self.routines, self.clock = media, routines, clock
        self.log = log
        self._voice_lock = threading.Lock()   # one voice at a time: answers and the day's words
        self._heard_itself = threading.Event()  # the day spoke: drop what the mic caught
        self._voice_done = ""                  # the catalog whose every clip is on disk
        self.online = None
        self._offline_said = 0.0
        self._stop = threading.Event()
        self.voice_dir = Path(cfg.home) / "voice"
        self.catalog_path = Path(cfg.home) / "catalog.json"
        # What it knew last time: the stock and prices, before any network.
        self.knowledge = Knowledge.load(self.catalog_path)
        self.bank = VoiceBank(self.voice_dir / "clips")
        self.prompt = hints(self.knowledge.catalog, cfg.name)

    # ---- background: heartbeat, the catalog for the hint, the offline phrase
    def refresh(self) -> None:
        try:
            catalog = self.monitor.catalog()
        except (ServerDown, Unauthorized) as exc:
            self.log(f"[EARS] no catalog for the hint ({exc})")
            return
        if catalog.get("parts"):
            self.knowledge.update(catalog)
            self.knowledge.save(self.catalog_path)
            self.prompt = hints(catalog, self.cfg.name)
            self.log(f"[EARS] catalog: {catalog.get('part_count', len(catalog['parts']))} parts "
                     f"kept for offline (hint {len(self.prompt)} characters)")

    def keep_offline_phrase(self) -> None:
        """«النت فاصل» in the robot's own voice, kept on disk for when it's true."""
        path = self.voice_dir / "offline.wav"
        if path.is_file():
            return
        try:
            wav = self.monitor.speak(OFFLINE_TEXT)
        except (ServerDown, Unauthorized):
            return
        if wav:
            self.voice_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav)

    def beat(self) -> None:
        try:
            code = self.monitor.heartbeat(VERSION)
            up = code == 200
        except Unauthorized as exc:
            self.log(f"[HB] {exc}")
            up = False
        except ServerDown:
            up = False
        if up != self.online:
            self.log("[HB] server reachable (online)" if up else "[HB] server unreachable (offline)")
        self.online = up

    def _background(self) -> None:
        last_catalog = 0.0
        while not self._stop.is_set():
            # Anything unexpected is logged, not fatal: a dead heartbeat thread
            # shows the robot "offline" on the dashboard while it still works.
            try:
                self.beat()
                if self.online and time.monotonic() - last_catalog > CATALOG_S:
                    self.refresh()
                    self.keep_offline_phrase()
                    last_catalog = time.monotonic()
            except Exception as exc:
                self.log(f"[HB] {type(exc).__name__}: {exc}")
            self._stop.wait(HEARTBEAT_S)

    def fill_voice(self) -> int:
        """One round of clip fetching, while online (see voicebank)."""
        if not self.online or not self.knowledge.parts:
            return 0
        catalog_id = self.knowledge.generated_at or "-"
        if self._voice_done == catalog_id:
            return 0                        # all on disk for this catalog: no 15 000 file checks a minute
        texts = self.knowledge.clip_texts() + self._day_texts()
        done = self.bank.prefetch(texts, lambda t: self.monitor.speak(t, codec="ulaw"),
                                  stop=self._stop, budget=CLIP_ROUND, log=self.log)
        have, total = self.bank.coverage(texts)
        if done:
            self.log(f"[VOICE] offline voice: {have}/{total} clips")
        if not self.bank.missing_now(texts):
            self._voice_done = catalog_id
        return done

    def _voice_filler(self) -> None:
        while not self._stop.is_set():
            try:
                done = self.fill_voice()
            except Exception as exc:
                self.log(f"[VOICE] {type(exc).__name__}: {exc}")
                done = 0
            if not done:
                self._stop.wait(HEARTBEAT_S)

    # ---- speaking (the Quran pauses, music goes down)
    def _hush(self):
        return self.media.speaking() if self.media is not None else nullcontext()

    def speak_wav(self, wav: bytes) -> None:
        with self._voice_lock, self._hush():
            self.speaker.play_wav(wav)
        self._heard_itself.set()

    def speak_samples(self, samples, rate: int) -> None:
        with self._voice_lock, self._hush():
            self.speaker.play(samples, rate)
        self._heard_itself.set()

    def say_text(self, text: str) -> bool:
        """Say any sentence: from the voice bank, else fetched (and kept)."""
        if not self.bank.has(text) and self.online is not False:
            try:
                wav = self.monitor.speak(text, codec="ulaw")
            except (ServerDown, Unauthorized):
                wav = None
            if wav:
                self.bank.put(text, wav)
        audio = self.bank.compose([text])
        if audio is None:
            self.log(f"[VOICE] no voice for «{text}» (offline, not kept yet)")
            return False
        self.speak_samples(*audio)
        return True

    # ---- the shop's day
    def _day_texts(self) -> list:
        texts = list(NO_FILES.values())
        if self.routines is not None:
            texts += [r.say for r in self.routines.routines if r.say]
        return texts

    def run_day(self) -> None:
        """One look at the schedule: start / stop / say what's due now."""
        if self.routines is None:
            return
        playing = self.media.kind if self.media is not None else ""
        for action, what in self.routines.due(self.clock(), playing):
            self.log(f"[DAY] {action} {what}")
            if action == "say":
                self.say_text(what)
            elif self.media is None:
                self.log("[DAY] no [media] in brain.ini: nothing plays")
            elif action == "play":
                self.media.play(what)
            elif action == "stop":
                self.media.stop()

    def _day_ticker(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_day()
            except Exception as exc:
                self.log(f"[DAY] {type(exc).__name__}: {exc}")
            self._stop.wait(ROUTINE_TICK_S)

    def apply_media(self, command: str) -> str:
        """Do what the server allowed («play:quran», «stop», «volume:up»).
        Returns a sentence to say instead of the server's «حاضر» when it
        can't be done here (no files), else ''."""
        if self.media is None or not command:
            return ""
        do, _, what = command.partition(":")
        before = self.media.kind
        if do == "play":
            if not self.media.can_play(what):
                return NO_FILES.get(what, NO_FILES["calm"])
            if before and before != what and self.routines is not None:
                self.routines.stopped_by_hand(before, self.clock())
            self.media.play(what)
        elif do == "stop":
            self.media.stop()
            if before and self.routines is not None:
                self.routines.stopped_by_hand(before, self.clock())
        elif do == "volume":
            level = self.media.louder() if what == "up" else self.media.quieter()
            self.log(f"[MEDIA] volume {int(level * 100)}%")
        return ""

    # ---- one utterance
    def say_offline(self) -> None:
        path = self.voice_dir / "offline.wav"
        if time.monotonic() - self._offline_said < OFFLINE_REPEAT_S or not path.is_file():
            return
        self._offline_said = time.monotonic()
        self.speak_wav(path.read_bytes())

    def answer_offline(self, text: str) -> None:
        """The server's answer, worked out here, in the voice kept on disk."""
        reply = self.knowledge.answer(text)
        if reply is None:
            self.log(f"[OFFLINE] not addressed to me: start with «يا {self.cfg.name}»")
            return
        self.log(f"[OFFLINE] 🔊 {reply.text}")
        audio = self.bank.compose(reply.segments)
        if audio is None:
            missing = self.bank.missing(reply.segments)
            self.log(f"[OFFLINE] no voice yet for: {' | '.join(missing)}")
            self.say_offline()
            return
        self.speak_samples(*audio)

    def _for_me_while_playing(self, text: str) -> bool:
        """With the Quran or music on, the mic hears it too: only what's said
        to the robot by name counts (no 20 s follow-up window then)."""
        if self.media is None or not self.media.kind:
            return True
        return strip_name(self.knowledge.names, text)[0]

    def handle(self, utt) -> None:
        t0 = time.monotonic()
        if utt.never_quiet:
            # Not one quiet frame in it: a fan, a radio, a loose wire — Whisper
            # would spend seconds on it and invent words.
            self.log(f"[MIC] it never went quiet (level {utt.quietest}–{utt.loudest}): "
                     "noise, not a voice — skipped. To hear the mic: python -m brain echo")
            return
        try:
            if self.ears.ready:
                text, took = self.ears.timed(utt.samples, self.prompt)
                if not text:
                    self.log(f"[EARS] nothing understood ({utt.speech_ms} ms, level "
                             f"{utt.quietest}–{utt.loudest}, {took:.1f} s)")
                    return
                if not self._for_me_while_playing(text):
                    self.log(f'[EARS] "{text}" — ignored while {self.media.kind} plays (no «يا {self.cfg.name}»)')
                    return
                self.log(f'[EARS] "{text}" — heard in {took:.1f} s ({self.ears.model_name})')
                t_heard = time.monotonic()
                if self.online is False:         # known down: don't wait on it
                    self.answer_offline(text)
                    return
                answer = self.server.voice(transcript=text, caps=self._caps())
            else:                                # no Whisper here: the server listens
                t_heard, text = t0, ""
                answer = self.server.voice(wav=wavs.pcm_wav(utt.samples), caps=self._caps())
        except ServerDown as exc:
            self.log(f"[NET] server unreachable: {exc}")
            self.online = False
            if text:
                self.answer_offline(text)
            else:
                self.say_offline()
            return
        except Unauthorized as exc:
            self.log(f"[VOICE] {exc}")
            return
        if not answer.addressed:
            self.log("[VOICE] the server heard nothing it could use" if answer.reason == "no_transcript"
                     else f"[VOICE] not addressed to me: start with «يا {self.cfg.name}»")
            return
        if not answer.reply:
            return
        do, _, what = answer.media.partition(":")
        if do == "stop":
            self.apply_media(answer.media)       # silence first, then «حاضر»
        elif do == "play" and self.media is not None and not self.media.can_play(what):
            instead = NO_FILES.get(what, NO_FILES["calm"])   # allowed, but nothing here to play
            self.log(f"🔊 {instead}")
            self.say_text(instead)
            return
        wav = answer.wav
        if wav is None:
            try:
                wav = self.server.speak(answer.reply)
            except (ServerDown, Unauthorized):
                wav = None
        t_voice = time.monotonic()
        self.log(f"🔊 {answer.reply}")
        self.log(f"[VOICE] spoke {self.cfg.end_silence_ms / 1000 + t_voice - t0:.1f} s after you "
                 f"stopped: hearing {t_heard - t0:.1f} s here, server + internet "
                 f"{t_voice - t_heard:.1f} s{f' ({answer.timing})' if answer.timing else ''}")
        if wav:
            self.speak_wav(wav)
        else:
            self.log("[VOICE] no voice for the reply (the server's TTS is down?)")
        if do in ("play", "volume"):
            self.apply_media(answer.media)       # «حاضر، هشغّل القرآن», then the Quran

    def _caps(self) -> str:
        return "media" if self.media is not None else ""

    # ---- the loop
    def run(self) -> None:
        cfg = self.cfg
        if self.ears.ready:
            self.log(f"[EARS] Whisper {self.ears.model_name} ready, on this computer")
        else:
            self.log(f"[EARS] Whisper not available ({self.ears.error}): the server will listen (Gemini)")
        if self.knowledge.parts:
            self.log(f"[OFFLINE] {len(self.knowledge.parts)} parts known "
                     f"(catalog of {self.knowledge.generated_at or 'unknown date'})")
        for target in (self._background, self._voice_filler, self._day_ticker):
            threading.Thread(target=target, daemon=True).start()
        seg = Segmenter(min_start=cfg.min_start, max_start=cfg.max_start,
                        end_silence_ms=cfg.end_silence_ms)
        self.log(f"[MIC] listening on «{self.mic.name}» — say «يا {cfg.name}». Ctrl+C to stop.")
        try:
            for frame in self.mic.frames():
                if self._heard_itself.is_set():  # the day spoke (or played): that was its own voice
                    self._heard_itself.clear()
                    self.mic.drain()
                    seg.reset()
                    continue
                utt = seg.feed(frame)
                if utt is None:
                    continue
                try:
                    self.handle(utt)
                except Exception as exc:         # one bad answer mustn't end the robot
                    self.log(f"[VOICE] {type(exc).__name__}: {exc}")
                self.mic.drain()                 # its own voice, and anything said meanwhile
                seg.reset()
                self._heard_itself.clear()
        finally:
            self._stop.set()
