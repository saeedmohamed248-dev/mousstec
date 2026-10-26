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

import re
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import VERSION, heardfix, wakeheard, wavs
from .ears import hints
from .knowledge import NOT_HERE, _VOCATIVES, Knowledge, media_command, normalize, strip_name
from .ledger import Ledger
from .outbox import Outbox
from .server import ServerDown, Unauthorized
from .vad import Segmenter
from .voicebank import VoiceBank

HEARTBEAT_S = 30
WARM_S = 45                          # the voice's connection kept open: a question then needs no handshake
CATALOG_S = 15 * 60                  # the stock changes; the hint follows
FRESH_CATALOG_S = 60                 # [robot] answer_here: the stock it answers from is a minute old at most
SORRY = "معلش، "                      # before the server's word, when it differs from what was said here
# «🎓 تعلّم السمع»: a question whose words Kaldi was less sure of than this, on
# average — or one of them below the weakest — goes to a teacher with its recording.
TEACH_SURE = 0.8
TEACH_WEAKEST = 0.5
OFFLINE_TEXT = "معلش، النت فاصل دلوقتي. جرّب كمان شوية."
OFFLINE_REPEAT_S = 30
SLOW_HEARING_S = 3.0                 # Whisper's test clip slower than this: the server hears first
# 🐛 [FIX]: on the shop's Celeron the test clip took 48 s, so with the server
# down every sentence left the robot deaf for 48 s before an answer nobody
# waited for. Slower than this, Whisper here is let go: offline the robot says
# at once that the net is down (the counter page still works).
TOO_SLOW_S = 15.0
CLIP_ROUND = 60                      # clips per round, then a heartbeat's rest
ROUTINE_TICK_S = 20
COMMANDS_S = 3                       # how soon a greeting or a page is said
SAID = ("say", "page")               # commands that are words to say
QUIET = ("stream_start", "stream_stop")  # the camera's business: nothing to do here
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
        self._net = threading.Lock()           # the voice's own connection: one request at a time
        self._asked = False                    # the last answer asked something: the server has the thread
        self._catalog_parts = None
        self._net_used = time.monotonic()
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
        self.outbox = Outbox(Path(cfg.home) / "outbox.jsonl")   # what offline did, for the server
        self.ledger = Ledger(Path(cfg.home) / "sold.json", self.outbox)   # sold here, not in the catalog yet
        self.counter = None                    # the counter page (counter.py), started by run()
        self.prompt = hints(self.knowledge.catalog, cfg.name)
        self.wake_heard = wakeheard.load(cfg.home)   # how these ears hear «يا موس» (brain wake)
        self.detector = self._load_model("wake_model.npz", "Detector")      # «يا موس» by its own model (kws.py)
        self.commands = self._load_model("wake_commands.npz", "Commands")   # and the commands by their sound

    # ---- background: heartbeat, the catalog for the hint, the offline phrase
    def refresh(self) -> bool:
        try:
            catalog = self.monitor.catalog(str(self.knowledge.catalog.get("etag") or ""))
        except (ServerDown, Unauthorized) as exc:
            self.log(f"[EARS] no catalog for the hint ({exc})")
            return False
        if catalog is None:
            # 🐛 [FIX]: the same catalog every minute was parsed, written to disk
            # and every voice clip checked again, on the Celeron, while listening.
            self.ledger.catalog_fetched()      # the server's stock is the one kept
            return True
        if not catalog.get("parts"):
            return False
        self.knowledge.update(catalog)
        self.knowledge.save(self.catalog_path)
        self.ledger.catalog_fetched()          # what was sent before it is in its stock
        self.prompt = hints(catalog, self.cfg.name)
        count = catalog.get("part_count", len(catalog["parts"]))
        if count != self._catalog_parts:       # every minute now: said when it changes
            total = catalog.get("part_total") or count
            kept = f"{count} parts" if total <= count else f"{count} of {total} parts (the ones in stock first)"
            self.log(f"[EARS] catalog: {kept} kept for offline (hint {len(self.prompt)} characters)")
            self._catalog_parts = count
        return True

    def sync(self) -> int:
        """Send what was done offline; a sale sent → the stock comes back
        from the server right away (the counter page counts on it)."""
        if not self.online or not self.outbox.pending():
            return 0
        try:
            taken = self.outbox.flush(self.monitor.sync_push, log=self.log, on_sent=self.ledger.sent)
        except (ServerDown, Unauthorized) as exc:
            self.log(f"[SYNC] kept for later: {exc}")
            return 0
        if taken:
            self.log(f"[SYNC] sent {taken} thing(s) done offline")
        if self.ledger.waiting_for_catalog():
            self.refresh()
        return taken

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

    def warm(self) -> bool:
        """🐛 [FIX]: a question asked after a quiet minute opened a new
        connection to the server (TCP, then TLS: 2–3 trips over the shop's
        line) — part of the 1.2 s the server's 13 ms of thinking took to come
        back. Something small on the voice's own connection now and then
        keeps it open. Never while a question is on it."""
        if not self.online or time.monotonic() - self._net_used < WARM_S:
            return False
        if not self._net.acquire(blocking=False):
            return False
        try:
            self.server.heartbeat(VERSION)
        except (ServerDown, Unauthorized):
            pass
        finally:
            self._net_used = time.monotonic()
            self._net.release()
        return True

    def _background(self) -> None:
        last_catalog = 0.0
        while not self._stop.is_set():
            # Anything unexpected is logged, not fatal: a dead heartbeat thread
            # shows the robot "offline" on the dashboard while it still works.
            try:
                self.beat()
                self.warm()
                self.sync()
                every = FRESH_CATALOG_S if self.cfg.answer_here else CATALOG_S
                if self.online and time.monotonic() - last_catalog > every:
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

    # ---- what the server queued: a greeting at clock-in, a page, a scan's answer
    def run_commands(self) -> int:
        """Say what's queued and ack each (like the ESP32 bridge: what isn't
        words to say isn't done here — the motors aren't on this computer)."""
        if not self.online:
            return 0
        commands = self.monitor.commands()
        for cmd in commands:
            kind = str(cmd.get("kind") or "")
            text = str((cmd.get("payload") or {}).get("text") or "").strip()
            ok = kind in QUIET
            if kind in SAID and text:
                self.log(f"[SAY] 🔊 {text}")
                wav = self.monitor.speak(text)
                if wav:
                    self.speak_wav(wav)
                    ok = True
            try:
                self.monitor.ack(cmd.get("command_id"), ok)
            except (ServerDown, Unauthorized) as exc:
                self.log(f"[SAY] couldn't ack {cmd.get('command_id')}: {exc}")
        return len(commands)

    def _command_poller(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_commands()
            except (ServerDown, Unauthorized):
                pass                         # the heartbeat says so
            except Exception as exc:
                self.log(f"[SAY] {type(exc).__name__}: {exc}")
            self._stop.wait(COMMANDS_S)

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

    def answer_offline(self, text: str, said: Optional[float] = None) -> None:
        """The server's answer, worked out here, in the voice kept on disk.
        `said`: when they started saying it (time.monotonic())."""
        reply = self.knowledge.answer(text, said=said)
        if reply is None:
            self.log(f"[OFFLINE] not addressed to me: start with «يا {self.cfg.name}»")
            return
        self._asked = False                      # this computer has the thread now, not the server
        self.log(f"[OFFLINE] 🔊 {reply.text}")
        if reply.event:                      # e.g. a number left for a part that's out
            self.outbox.add(reply.event["kind"], reply.event["payload"])
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

    @property
    def server_hears_first(self) -> bool:
        """On a slow computer (a 2-core Celeron takes seconds a sentence) or with
        [ears] use = offline, the server listens while it's up — sooner — and
        Whisper here only when it's down. [ears] use = always keeps it here."""
        use = self.cfg.ears_use
        if use == "always":
            return False
        return use == "offline" or getattr(self.ears, "warmup", 0.0) > SLOW_HEARING_S

    def handle(self, utt, heard=None, woke=None, words=None, confs=None) -> None:
        """One sentence. `heard`: (text, seconds) when the ears already heard
        it while it was said (Vosk's Stream) — no second hearing then;
        `woke`: the wake model's (said?, probability, where it ended),
        `words`: when each word was said, from the same ears, and `confs`:
        how sure they were of each."""
        t0 = time.monotonic()
        # When they started talking: the follow-up window after «أيوه، تحت أمرك»
        # is checked then, not after the seconds a slow computer took to hear it.
        said = t0 - (self.cfg.end_silence_ms + utt.speech_ms) / 1000
        if utt.never_quiet:
            # Not one quiet frame in it: a fan, a radio, a loose wire — Whisper
            # would spend seconds on it and invent words.
            self.log(f"[MIC] it never went quiet (level {utt.quietest}–{utt.loudest}): "
                     "noise, not a voice — skipped. To hear the mic: python -m brain echo")
            return
        try:
            if self.ears.ready and (self.online is False or not self.server_hears_first):
                text, took = heard if heard is not None else self.ears.timed(utt.samples, self.prompt)
                if words is None:
                    words = getattr(self.ears, "words", None) or []
                    confs = getattr(self.ears, "confs", None) or []
                text = self._named(text)
                woke = self._wake(utt.samples) if woke is None else woke
                text = self._woken(text, woke, words)
                text = self._plain(text)
                text = self._commanded(text, utt.samples, woke)
                text = self._fixed(text)
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
                    self.answer_offline(text, said)
                    return
                sure = self._sureness(text, words, confs)
                if self._needs_teacher(text, sure):
                    # «🎓»: not sure it heard right — a teacher hears the recording.
                    with self._net:
                        answer = self.server.voice(
                            transcript=text, wav=wavs.ulaw_wav(wavs.louder(utt.samples)), check=True,
                            confidence=sure[0] if sure else None, caps=self._caps(),
                            said_ago_ms=self._ago(said))
                        self._net_used = time.monotonic()
                    if answer.heard_by:
                        self.log(f"[TEACH] «{text}» — {answer.heard_by} heard «{answer.taught}»")
                elif self.online and self.cfg.answer_here and self._answer_here(text, said, t0):
                    return
                else:
                    with self._net:
                        answer = self.server.voice(transcript=text, caps=self._caps(),
                                                   said_ago_ms=self._ago(said))
                        self._net_used = time.monotonic()
            else:                                # no Whisper here (or a slow one): the server listens
                t_heard, text = t0, ""
                with self._net:
                    answer = self.server.voice(wav=wavs.ulaw_wav(wavs.louder(utt.samples)), caps=self._caps(),
                                               said_ago_ms=self._ago(said))
                    self._net_used = time.monotonic()
        except ServerDown as exc:
            self.log(f"[NET] server unreachable: {exc}")
            self.online = False
            if not text and self.ears.ready:     # the server was to listen: Whisper here, then
                text, _took = self.ears.timed(utt.samples, self.prompt)
                text = self._named(text)
                text = self._woken(text, self._wake(utt.samples) if woke is None else woke,
                                   getattr(self.ears, "words", None) or [])
                text = self._fixed(self._plain(text))
            if text:
                self.answer_offline(text, said)
            else:
                self.say_offline()
            return
        except Unauthorized as exc:
            self.log(f"[VOICE] {exc}")
            return
        if not answer.addressed:
            heard = f" (heard «{answer.transcript}»)" if answer.transcript else ""
            if answer.reason == "stt_down":
                self.log(f"[VOICE] the server can't turn speech into text right now: {answer.detail} "
                         "— check GEMINI_API_KEY and its quota on the server")
            elif answer.reason == "no_transcript":
                self.log(f"[VOICE] the server heard nothing it could use ({utt.speech_ms} ms, level "
                         f"{utt.quietest}–{utt.loudest}: a voice is usually 1000+)")
            else:
                self.log(f"[VOICE] not addressed to me{heard}: start with «يا {self.cfg.name}»")
            return
        if not answer.reply:
            return
        self._asked = self._asks(answer.reply)   # «تقصد أنهي؟» from the server: the next answer is its too
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
                with self._net:
                    wav = self.server.speak(answer.reply)
            except (ServerDown, Unauthorized):
                wav = None
        t_voice = time.monotonic()
        if answer.transcript and not text:      # the server listened: what it heard, so a mishearing shows
            self.log(f"[VOICE] heard «{answer.transcript}»")
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

    def start_counter(self):
        """The counter page on the shop's Wi-Fi (brain.ini [counter] port; 0 = off)."""
        if not self.cfg.counter_port:
            return None
        from .counter import Counter, serve
        try:
            self.counter = serve(Counter(self.knowledge, self.outbox, self.ledger,
                                         online=lambda: self.online, log=self.log),
                                 self.cfg.counter_host, self.cfg.counter_port)
        except OSError as exc:
            self.log(f"[COUNTER] can't open port {self.cfg.counter_port}: {exc}")
            return None
        self.log(f"[COUNTER] the counter page: http://<this computer's address>:{self.cfg.counter_port}")
        return self.counter

    def _fixed(self, text: str) -> str:
        """What these ears write as what was said, as the teachers taught it
        («فلترا الزي» → «فلتر زيت»: robot/brain/heardfix.py) — offline too."""
        fixed, used = heardfix.apply(text, self.knowledge.heard_fixes)
        if used:
            learned = "، ".join(f"«{a}» = «{b}»" for a, b in used)
            self.log(f"[EARS] «{text}» — learned: {learned}")
        return fixed

    def _sureness(self, text: str, words, confs):
        """(average, weakest) of how sure the ears were of the question's own
        words — not the name, which Kaldi never knows — or None."""
        if not words or not confs or len(words) != len(confs):
            return None
        sure = {}
        for (word, _start, _stop), conf in zip(words, confs, strict=True):
            key = normalize(word)
            sure[key] = min(conf, sure.get(key, 1.0))
        addressed, rest = strip_name(self.knowledge.names, text)
        mine = [sure[w] for w in normalize(rest if addressed else text).split() if w in sure]
        return (sum(mine) / len(mine), min(mine)) if mine else None

    def _needs_teacher(self, text: str, sure) -> bool:
        """Said to the robot, and heard unsure — its words, or nothing here
        makes sense of them: the recording goes to a teacher ([robot] teach)."""
        if not (self.cfg.teach and self.online and self.ears.model_name == "vosk"):
            return False
        addressed, rest = strip_name(self.knowledge.names, text)
        if not addressed or not rest:
            return False                         # chatter, or the name alone: no minutes on it
        if sure is not None and (sure[0] < TEACH_SURE or sure[1] < TEACH_WEAKEST):
            return True
        return not self.knowledge.understands(text)

    def _named(self, text: str) -> str:
        """«يمس عندك …» → «يا موس عندك …», when that's how these ears hear the name."""
        named, out = wakeheard.as_named(text, self.wake_heard, self.cfg.name)
        if named:
            self.log(f"[EARS] «{text}» — «يا {self.cfg.name}» as these ears hear it")
        return out

    def _load_model(self, file: str, kind: str):
        """home/<file> from `brain wake`, if it was good enough to keep."""
        path = Path(self.cfg.home) / file
        if not path.exists():
            return None
        try:
            from . import kws
            return getattr(kws, kind).load(path)
        except Exception as exc:                 # numpy, the shipped features file…
            self.log(f"[WAKE] can't load {file} ({type(exc).__name__}: {exc})")
            return None

    def _wake(self, samples):
        """(said «يا موس»?, probability, seconds where it ended), or None."""
        if self.detector is None:
            return None
        try:
            return self.detector.heard(samples)
        except Exception as exc:                 # it only helps: never in the way
            self.log(f"[WAKE] {type(exc).__name__}: {exc}")
            return None

    def _woken(self, text: str, woke, words) -> str:
        """The wake model heard «يا موس» where the words have something
        else («انظر إلى كابوس عندك …», or nothing at all): the name goes in
        place of what was written for it. Not when the words have the name
        already, nor «يا + another name» (Vosk writes «يا محمد» right:
        someone else was called)."""
        if not woke or not woke[0]:
            return text
        if strip_name(self.knowledge.names, text)[0]:
            return text
        first = normalize(text).split()[:2]
        if len(first) == 2 and first[0] in _VOCATIVES:
            self.log(f"[WAKE] «{text}» — someone else called, not «يا {self.cfg.name}»")
            return text
        _, prob, end = woke
        if words:
            # What Vosk wrote for the name: the first words (three at most) that
            # were over by the time the name was — never the question after it.
            drop = 0
            while drop < min(3, len(words)) and words[drop][2] <= end + 0.1:
                drop += 1
            rest = " ".join(w for w, _start, _stop in words[drop:])
        else:
            rest = text
        self.log(f"[WAKE] «يا {self.cfg.name}» heard ({prob:.2f}) — the words had «{text}»")
        return f"يا {self.cfg.name} {rest}".strip()

    def _answer_here(self, text: str, said: float, t0: float) -> bool:
        """The stock and the price said at once, from this computer's catalog
        (a minute old at most) — the 1.2–3 s to the server and back saved —
        and the server asked all the same, words only: it keeps the thread
        (what was asked, the follow-up window) and the record, and when it
        says otherwise (another price, sold out a moment ago) the robot
        corrects itself at once. Only «أيوه، تحت أمرك», a part it found and
        the shop's own replies («الردود الجاهزة»):
        everything else (the books, the commands, the other branches, a part
        only the server knows) is the server's, as before; and after an answer
        that asked something («تقصد أنهي؟», «قولي رقم موبايلك») the next one
        is the server's too."""
        if self._asked:
            return False
        reply = self.knowledge.answer(text, said=said)
        if reply is None or reply.event is not None or "النت" in reply.text:
            return False
        if not (reply.intent in ("wake", "shop_reply")
                or (reply.intent == "inventory_query" and reply.part is not None)):
            return False
        audio = self.bank.compose(reply.segments)
        if audio is None:                    # not all its words in the voice bank yet
            return False
        self.log(f"[HERE] 🔊 {reply.text}")
        self.log(f"[HERE] said {self.cfg.end_silence_ms / 1000 + time.monotonic() - t0:.1f} s after you "
                 "stopped, from this computer — the server checks it")
        self._asked = self._asks(reply.text)
        threading.Thread(target=self._check, args=(text, said, reply.text), daemon=True).start()
        self.speak_samples(*audio)
        return True

    @staticmethod
    def _asks(reply: str) -> bool:
        return "؟" in reply or "رقم موبايلك" in reply

    @staticmethod
    def _differs(here: str, there: str) -> bool:
        """Another price, or in stock one way and not the other — not a
        word put differently, nor a count that's still more than none."""
        prices = lambda t: set(re.findall(r"(\d+) جنيه", t))                    # noqa: E731
        return prices(here) != prices(there) or (NOT_HERE in here) != (NOT_HERE in there)

    def _check(self, text: str, said: float, here: str) -> None:
        """The server's word on what was answered here (on a thread)."""
        try:
            with self._net:
                answer = self.server.voice(transcript=text, caps=self._caps(), said_ago_ms=self._ago(said),
                                           reply_audio="")
                self._net_used = time.monotonic()
        except (ServerDown, Unauthorized) as exc:
            self.log(f"[HERE] the server couldn't check it ({exc})")
            return
        if not answer.addressed or not answer.reply:
            return
        self._asked = self._asks(answer.reply)
        if not self._differs(here, answer.reply):
            return
        self.log(f"[HERE] the server says otherwise: «{answer.reply}» — corrected")
        self.say_text(SORRY + answer.reply)

    def _commanded(self, text: str, samples, woke) -> str:
        """«يا موس وطّي الصوت» that Vosk wrote as something else («وطّي» isn't
        one of its words): the command by its sound (kws.Commands), written
        the way the server reads it — and the server still decides who may.
        Only for a short sentence to the robot with a piece of a command in
        its words (or none at all), that isn't a part's name and isn't a
        command in its words already: a question is never turned into one."""
        if self.commands is None:
            return text
        from .kws import sounds_like_a_command
        addressed, rest = strip_name(self.knowledge.names, text)
        if not addressed or media_command(rest) or len(normalize(rest).split()) > 3:
            return text
        if not sounds_like_a_command(normalize(rest)):
            return text
        if rest and self.knowledge.lookup(rest)[0] is not None:
            return text
        try:
            kind, prob = self.commands.heard(samples, woke[2] if woke and woke[0] else 0.0)
        except Exception as exc:                 # it only helps: never in the way
            self.log(f"[WAKE] {type(exc).__name__}: {exc}")
            return text
        if kind is None:
            return text
        from .kws import COMMANDS
        self.log(f"[WAKE] «{COMMANDS[kind]}» by its sound ({prob:.2f}) — the words had «{rest}»")
        return f"يا {self.cfg.name} {COMMANDS[kind]}"

    def _plain(self, text: str) -> str:
        """«يمس فلتر زيت», «موسى زيك فيه» — for the robot, but not in its
        name's own spelling: sent as «يا موس …», so a server that doesn't
        know these spellings yet (not updated) still knows it's for it."""
        addressed, rest = strip_name(self.knowledge.names, text)
        base = normalize(self.cfg.name)
        words = normalize(text).split()
        if not addressed or base in words or f"يا{base}" in words:
            return text
        return f"يا {self.cfg.name} {rest}".strip()

    @staticmethod
    def _ago(said: float) -> int:
        return max(0, round((time.monotonic() - said) * 1000))

    def _caps(self) -> str:
        return "media" if self.media is not None else ""

    # ---- the loop
    def run(self) -> None:
        cfg = self.cfg
        if self.ears.ready and getattr(self.ears, "warmup", 0.0) > TOO_SLOW_S:
            if self.cfg.ears_use == "always":
                # «use = always» is the owner's «no Google»: kept, however slow.
                self.log(f"[EARS] Whisper takes {self.ears.warmup:.0f} s a sentence here — kept "
                         "([ears] use = always). run_windows.bat bench shows a faster model.")
            else:
                self.ears.release(f"too slow on this computer ({self.ears.warmup:.0f} s a sentence)")
                self.log("[EARS] tip: [ears] model = off in brain.ini starts the robot a minute or two sooner")
        if self.ears.ready:
            kind = "Vosk" if self.ears.model_name.startswith("vosk") else "Whisper"
            self.log(f"[EARS] {kind} {self.ears.model_name} ready, on this computer")
            if self.detector is not None:
                self.log(f"[WAKE] «يا {cfg.name}» by its own model (from run_windows.bat wake)"
                         + (", and the commands by their sound" if self.commands is not None else ""))
            if self.wake_heard:
                self.log(f"[EARS] «يا {cfg.name}» as these ears hear it: "
                         + " · ".join(" ".join(seq) for seq in self.wake_heard))
            elif kind == "Vosk" and self.detector is None:
                self.log(f"[EARS] tip: Vosk has no «{cfg.name}» in its words — run_windows.bat wake "
                         f"teaches it how it hears «يا {cfg.name}»")
            if self.server_hears_first:
                why = ("[ears] use = offline" if self.cfg.ears_use == "offline"
                       else f"a test clip took {self.ears.warmup:.1f} s here")
                self.log(f"[EARS] {why}: the server listens while it's up, Whisper here only when it's down")
        else:
            self.log(f"[EARS] Whisper not available ({self.ears.error}): the server will listen (Gemini)")
        if self.knowledge.parts:
            self.log(f"[OFFLINE] {len(self.knowledge.parts)} parts known "
                     f"(catalog of {self.knowledge.generated_at or 'unknown date'})")
        for target in (self._background, self._voice_filler, self._day_ticker, self._command_poller):
            threading.Thread(target=target, daemon=True).start()
        self.start_counter()
        seg = Segmenter(min_start=cfg.min_start, max_start=cfg.max_start,
                        end_silence_ms=cfg.end_silence_ms)
        self.log(f"[MIC] listening on «{self.mic.name}» — say «يا {cfg.name}». Ctrl+C to stop.")
        stream, fed = None, 0
        try:
            for frame in self.mic.frames():
                if self._heard_itself.is_set():  # the day spoke (or played): that was its own voice
                    self._heard_itself.clear()
                    self.mic.drain()
                    seg.reset()
                    if stream is not None:
                        stream.drop()
                    stream, fed = None, 0
                    continue
                utt = seg.feed(frame)
                rec = seg.recording
                if rec is not None:              # talking: Vosk hears it as it's said
                    if stream is None and fed == 0 and self._streams():
                        stream = self._open_stream()
                    if stream is not None:
                        for f in rec[fed:]:
                            stream.feed(f)
                    fed = len(rec)
                    continue
                fed = 0
                if utt is None:
                    if stream is not None:       # too short for a sentence: a cough, a door
                        stream.drop()
                    stream = None
                    continue
                heard = woke = words = confs = None
                if stream is not None:
                    stream.feed(frame)           # the sentence's last frame
                    # The wake model on the other core, while Kaldi finishes.
                    waking = self._wake_soon(utt.samples)
                    heard = stream.finish()
                    woke, words = waking(), getattr(stream, "words", None)
                    confs = getattr(stream, "confs", None)
                    if stream.error:
                        self.log(f"[EARS] {stream.error}: heard again, the slow way")
                        heard = words = confs = None
                    stream = None
                try:
                    self.handle(utt, heard, woke, words, confs)
                except Exception as exc:         # one bad answer mustn't end the robot
                    self.log(f"[VOICE] {type(exc).__name__}: {exc}")
                self.mic.drain()                 # its own voice, and anything said meanwhile
                seg.reset()
                self._heard_itself.clear()
        finally:
            self._stop.set()

    def _wake_soon(self, samples):
        """Start the wake model on a thread; call what it returns for the answer."""
        if self.detector is None:
            return lambda: None
        box = {}
        worker = threading.Thread(target=lambda: box.update(r=self._wake(samples)), daemon=True)
        worker.start()

        def answer():
            worker.join(10)
            return box.get("r")
        return answer

    def _streams(self) -> bool:
        """Ears that hear while it's said (Vosk), and the hearing is ours to do."""
        return (hasattr(self.ears, "stream") and self.ears.ready
                and (self.online is False or not self.server_hears_first))

    def _open_stream(self):
        try:
            return self.ears.stream()
        except Exception as exc:                 # hear it afterwards, as before
            self.log(f"[EARS] can't hear as it's said ({type(exc).__name__}: {exc})")
            return None
