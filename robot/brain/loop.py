"""The robot itself: hear → understand here → ask the ERP → say the answer.

One utterance at a time, like a person behind the counter: while the robot
answers it doesn't listen, and what the mic caught meanwhile (its own voice)
is dropped before it listens again.
"""

import threading
import time
from pathlib import Path

from . import VERSION, wavs
from .ears import hints
from .server import ServerDown, Unauthorized
from .vad import Segmenter

HEARTBEAT_S = 30
CATALOG_S = 15 * 60                  # the stock changes; the hint follows
OFFLINE_TEXT = "معلش، النت فاصل دلوقتي. جرّب كمان شوية."
OFFLINE_REPEAT_S = 30


class Brain:
    def __init__(self, cfg, *, ears, server, mic, speaker, monitor=None, log=print):
        self.cfg, self.ears, self.server, self.mic, self.speaker = cfg, ears, server, mic, speaker
        self.monitor = monitor or server      # heartbeat + catalog: its own HTTP session
        self.log = log
        self.prompt = hints({}, cfg.name)
        self.online = None
        self._offline_said = 0.0
        self._stop = threading.Event()
        self.voice_dir = Path(cfg.home) / "voice"

    # ---- background: heartbeat, the catalog for the hint, the offline phrase
    def refresh(self) -> None:
        try:
            catalog = self.monitor.catalog()
        except (ServerDown, Unauthorized) as exc:
            self.log(f"[EARS] no catalog for the hint ({exc})")
            return
        if catalog.get("parts"):
            self.prompt = hints(catalog, self.cfg.name)
            self.log(f"[EARS] hint: {catalog.get('part_count', len(catalog['parts']))} parts "
                     f"in the catalog ({len(self.prompt)} characters)")

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

    # ---- one utterance
    def say_offline(self) -> None:
        path = self.voice_dir / "offline.wav"
        if time.monotonic() - self._offline_said < OFFLINE_REPEAT_S or not path.is_file():
            return
        self._offline_said = time.monotonic()
        self.speaker.play_wav(path.read_bytes())

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
                self.log(f'[EARS] "{text}" — heard in {took:.1f} s ({self.ears.model_name})')
                t_heard = time.monotonic()
                answer = self.server.voice(transcript=text)
            else:                                # no Whisper here: the server listens
                t_heard = t0
                answer = self.server.voice(wav=wavs.pcm_wav(utt.samples))
        except ServerDown as exc:
            self.log(f"[NET] server unreachable: {exc}")
            self.online = False
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
            self.speaker.play_wav(wav)
        else:
            self.log("[VOICE] no voice for the reply (the server's TTS is down?)")

    # ---- the loop
    def run(self) -> None:
        cfg = self.cfg
        if self.ears.ready:
            self.log(f"[EARS] Whisper {self.ears.model_name} ready, on this computer")
        else:
            self.log(f"[EARS] Whisper not available ({self.ears.error}): the server will listen (Gemini)")
        worker = threading.Thread(target=self._background, daemon=True)
        worker.start()
        seg = Segmenter(min_start=cfg.min_start, max_start=cfg.max_start,
                        end_silence_ms=cfg.end_silence_ms)
        self.log(f"[MIC] listening on «{self.mic.name}» — say «يا {cfg.name}». Ctrl+C to stop.")
        try:
            for frame in self.mic.frames():
                utt = seg.feed(frame)
                if utt is None:
                    continue
                try:
                    self.handle(utt)
                except Exception as exc:         # one bad answer mustn't end the robot
                    self.log(f"[VOICE] {type(exc).__name__}: {exc}")
                self.mic.drain()                 # its own voice, and anything said meanwhile
                seg.reset()
        finally:
            self._stop.set()
