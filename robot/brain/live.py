"""«إرسال الصوت أول بأول»: a sentence's sound goes up to the server while it's
still being said (ws/robot/voice/ — robot/consumers.py on the server).

Over /voice/ the recording went up only once the customer had stopped, and a
teacher started hearing it only then. Here the server has it the moment they
stop — and Azure, when a teacher is to hear it, has heard most of it already:
the answer comes a second or so sooner.

One socket, kept open by a thread of its own that reads it (the server's
pings answered, its replies handed over) and comes back after a drop. The
microphone's loop only queues the sound — a slow line never holds the
microphone; another thread sends it. Whenever the socket can't take a
sentence (a server not updated yet, the net down, websocket-client missing,
a line too slow for it) the sentence goes over /voice/, as before.

    [server]
    live = no        ; /voice/ only (on by default)
"""

import json
import queue
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from . import wavs
from .server import CONNECT_S, VOICE_S, ServerDown, answer_from, voice_fields

PATH = "/ws/robot/voice/"
PIECE = 4096                     # µ-law bytes a message: a quarter of a second
BEHIND = 3 * wavs.RATE           # more sound than this (3 s) waiting to go up: the line can't keep up — /voice/
QUIET_S = 45                     # nothing from the server this long (it pings every 20 s): the line is dead
RETRY_S = (5, 15, 60, 120)       # after a drop, or with the net down: the next try this much later
REFUSED_S = 15 * 60              # the server refused the socket (not updated yet, or the token): rarely
_CLOSED, _UNSENT = object(), object()


class LiveDown(Exception):
    """The socket couldn't take this sentence: nothing of it was answered — /voice/ then."""


def socket_url(api_base: str) -> str:
    """https://shop.mousstec.com/api/robot/v1 → wss://shop.mousstec.com/ws/robot/voice/"""
    parts = urlsplit(api_base.strip())
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, PATH, "", ""))


def _connect(url: str, token: str, timeout: float):
    """websocket-client's socket: the token in its header (never the URL — logs
    keep URLs), trusting what `requests` trusts (certifi)."""
    import websocket

    options = {}
    try:
        import certifi
        options["sslopt"] = {"ca_certs": certifi.where()}
    except ImportError:
        pass
    return websocket.create_connection(url, timeout=timeout, header=[f"X-Robot-Token: {token}"],
                                       enable_multithread=True, **options)


class LiveVoice:
    """begin() when a sentence starts, feed() its sound as it comes, teach()
    once it's clearly said to the robot, then end() for the answer — or
    drop() when the server isn't asked after all."""

    def __init__(self, api_base: str, token: str, *, log=print, connect=None):
        self.url, self.token, self.log = socket_url(api_base), token, log
        self._connect = connect or _connect
        self._ws = None
        self._gen = 0                    # the connection's number: a sentence begun on one ends on it
        self._out: queue.Queue = queue.Queue()      # what goes up, in order: (connection, payload, is_end)
        self._replies: queue.Queue = queue.Queue()  # what came down: answers, and their voices
        self._stop = threading.Event()
        self._behind = 0                 # sound queued and not sent yet (bytes)
        self._lock = threading.Lock()
        self._said = ""                  # what the log last said about the socket (said once)
        self._n = 0                      # the sentence's number
        self._on_gen = 0
        self.active = False              # a sentence's sound is going up
        self.teaching = False            # and Azure is to hear it as it comes
        self.lagging = False             # the line couldn't keep up with it
        self._sent = 0                   # samples of it queued so far
        self._piece = bytearray()
        self._gain = wavs.StreamGain()
        self._end_done = threading.Event()   # the sender is done with its `end`…
        self._end_ok = False                  # …and it went out: an answer may be on its way
        self._end_at = 0.0                    # when it went out

    @property
    def ready(self) -> bool:
        return self._ws is not None

    def start(self) -> "LiveVoice":
        for target in (self._keep, self._send):
            threading.Thread(target=target, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._out.put(None)
        self._close(self._ws)

    # ---- a sentence
    def begin(self, teach: bool = False) -> bool:
        """A sentence starts: its sound goes up as it comes. `teach`: a teacher
        will likely hear it (Azure starts hearing it now). False when the
        socket isn't there."""
        self.active = False
        if self._ws is None:
            return False
        self._n += 1
        self._on_gen = self._gen
        self._sent, self._piece, self._gain = 0, bytearray(), wavs.StreamGain()
        self.active, self.teaching, self.lagging = True, bool(teach), False
        self._put({"type": "start", "id": self._n, "teach": int(bool(teach))})
        return True

    def feed(self, samples) -> None:
        """More of the sentence (int16, 16 kHz): queued, never waited on."""
        if not self.active or self.lagging:
            return
        x = self._gain(samples)
        self._sent += len(x)
        self._piece += wavs.ulaw_encode(x)
        if len(self._piece) >= PIECE:
            self._flush()

    def teach(self) -> None:
        """It's said to the robot, it turns out (its name, heard while it's
        said): Azure starts hearing it now, from its start."""
        if self.active and not self.teaching:
            self.teaching = True
            self._put({"type": "teach", "id": self._n})

    def drop(self) -> None:
        """Not one for the server after all: forgotten there too."""
        if self.active:
            self.active = False
            self._put({"type": "drop", "id": self._n})

    def end(self, samples, *, audio: bool, timeout: float = VOICE_S, **fields):
        """The sentence's answer, over the socket. `fields` are /voice/'s
        (server.voice_fields); `audio`: with its sound — `samples` is the
        sentence as it was cut, most of it gone up already.

        Raises LiveDown when the socket couldn't take it (nothing was
        answered: /voice/ then), ServerDown when the answer didn't come."""
        if not self.active:
            raise LiveDown("no sentence going up")
        if audio and len(samples) > self._sent:
            self.feed(samples[self._sent:])          # its last frame(s), not sent yet
        self._flush()
        if self.lagging or self._ws is None or self._on_gen != self._gen:
            self.drop()
            raise LiveDown("the line couldn't keep up" if self.lagging else "the socket dropped")
        self.active, n = False, self._n
        while True:                                   # an answer that came too late, before
            try:
                self._replies.get_nowait()
            except queue.Empty:
                break
        self._end_done.clear()
        self._end_ok = False
        asked = time.monotonic()
        self._put({"type": "end", "id": n, "audio": int(audio), "keep": len(samples) if audio else 0,
                   **voice_fields(**fields)}, is_end=True)
        deadline = asked + timeout
        body, wav = None, None
        while body is None:
            try:
                got = self._replies.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise ServerDown("the live socket: no answer in time") from None
            if got is _UNSENT:
                raise LiveDown("the socket dropped")
            if got is _CLOSED:
                # 🐛 [FIX]: the socket closing as the `end` went out could leave this waiting
                # its whole 15 s for an «unsent» that never came. The sender says which it was.
                self._end_done.wait(2)
                if self._end_ok:                      # asked, and gone before the answer
                    raise ServerDown("the live socket closed before the answer")
                raise LiveDown("the socket dropped")
            if not isinstance(got, bytes) and got.get("id") == n:
                body = got                            # (anything before it: an older answer, and its voice)
        t_body = time.monotonic()
        if body.get("audio"):
            try:
                wav = self._replies.get(timeout=max(1.0, deadline - time.monotonic()))
            except queue.Empty:
                wav = None
            wav = wav if isinstance(wav, bytes) else None
        answer = answer_from(body, wav, fields.get("transcript"))
        answer.live = True
        # Where the time went, for the log: up (the sound still to send), the answer, its voice.
        sent = self._end_at if self._end_ok and self._end_at >= asked else asked
        parts = [f"up {sent - asked:.1f} s", f"answer {t_body - sent:.1f} s"]
        if wav is not None:
            parts.append(f"voice {len(wav) // 1024} KB in {time.monotonic() - t_body:.1f} s")
        elif body.get("audio"):
            parts.append("voice lost")
        answer.transfer = ", ".join(parts)
        return answer

    def _flush(self) -> None:
        if not self._piece or self.lagging:
            return
        if self._behind > BEHIND:                     # the shop's line: slower than the speaking
            self.lagging = True
            return
        piece, self._piece = bytes(self._piece), bytearray()
        self._put(piece)

    def _put(self, payload, is_end: bool = False) -> None:
        if isinstance(payload, bytes):
            with self._lock:
                self._behind += len(payload)
        else:
            payload = json.dumps(payload, ensure_ascii=False)
        self._out.put((self._gen, payload, is_end))

    # ---- the socket's threads
    def _send(self) -> None:
        while True:
            item = self._out.get()
            if item is None:
                return
            gen, payload, is_end = item
            ws = self._ws
            try:
                if ws is None or gen != self._gen:    # begun on a socket that's gone
                    if is_end:
                        self._replies.put(_UNSENT)
                elif isinstance(payload, bytes):
                    ws.send_binary(payload)
                else:
                    ws.send(payload)
                    if is_end:
                        self._end_at, self._end_ok = time.monotonic(), True
            except Exception:                         # broken: the reader comes back on a new one
                if is_end:
                    self._replies.put(_UNSENT)
                if self._ws is ws:
                    self._ws = None
                self._close(ws)
            finally:
                if is_end:
                    self._end_done.set()
                if isinstance(payload, bytes):
                    with self._lock:
                        self._behind -= len(payload)

    def _keep(self) -> None:
        tries = 0
        while not self._stop.is_set():
            ws = None
            try:
                ws = self._connect(self.url, self.token, CONNECT_S)
                hello = json.loads(ws.recv())
                if not isinstance(hello, dict) or hello.get("type") != "ready":
                    raise ValueError(f"the server said {hello!r}")
            except ImportError as exc:
                self._say(f"[LIVE] websocket-client isn't installed ({exc}): the whole recording "
                          "after each sentence, as before")
                return
            except Exception as exc:
                self._close(ws)
                refused = getattr(exc, "status_code", None)
                # 🐛 [FIX]: a 502/503 from the proxy while the server restarts (every deploy) waited
                # 15 minutes like a refusal: only «not here» (an old server: 500, 404) and the token
                # (401/403) are rare tries; the server coming back is tried again soon.
                if refused in (502, 503, 504):
                    refused = None
                wait = REFUSED_S if refused else RETRY_S[min(tries, len(RETRY_S) - 1)]
                tries += 1
                if refused:
                    self._say(f"[LIVE] the server didn't take the live socket (HTTP {refused}: not updated "
                              f"yet, or the token) — the whole recording after each sentence, as before")
                else:
                    self._say(f"[LIVE] no live socket ({type(exc).__name__}) — the whole recording after "
                              "each sentence, as before")
                self._stop.wait(wait)
                continue
            tries = 0
            ws.settimeout(QUIET_S)
            self._gen += 1
            self._ws = ws
            self._say("[LIVE] connected: the sound goes up while it's said — the answers come sooner")
            why = self._read(ws)
            if self._ws is ws:
                self._ws = None
            self._replies.put(_CLOSED)
            self._close(ws)
            if not self._stop.is_set():
                self._say(f"[LIVE] the live socket dropped ({why}) — the whole recording after each "
                          "sentence till it's back")
            self._stop.wait(RETRY_S[0])

    def _read(self, ws) -> str:
        """Hand over what comes down, till the socket's gone; why it went."""
        while not self._stop.is_set():
            try:
                message = ws.recv()
            except Exception as exc:                  # closed, or silent past QUIET_S: gone
                name = type(exc).__name__
                return "nothing heard from the server for 45 s" if "Timeout" in name else name
            if isinstance(message, bytes):
                self._replies.put(message)
                continue
            try:
                body = json.loads(message)
            except (TypeError, ValueError):
                continue
            if isinstance(body, dict) and body.get("type") == "reply":
                self._replies.put(body)
        return "stopped"

    @staticmethod
    def _close(ws) -> None:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _say(self, line: str) -> None:
        if line != self._said:
            self._said = line
            self.log(line)
