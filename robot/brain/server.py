"""The ERP's robot API (/api/robot/v1/…), as the brain uses it.

The same endpoints and token as the ESP32 bridge: /voice/ takes the words
already written down (`transcript`) and, asked for `reply_audio=ulaw`,
answers with the spoken reply itself in one trip; /speak/ voices any text;
/sync/pull/ is the retail-only catalog (names for the ears' hint);
/commands/pending/ is what the server queued for the robot to say (a
greeting at clock-in, a page for an employee, a scan's answer), acked on
/commands/ack/ once said.
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote


class ServerDown(Exception):
    """No answer from the server: the internet (or the server) is down."""


class Unauthorized(Exception):
    """401: wrong ROBOT token or API base (another workshop's address)."""


@dataclass
class Answer:
    wav: Optional[bytes]             # the reply, already spoken (None: words only)
    reply: str
    transcript: str
    addressed: bool                  # False: not said to the robot (no «يا موس»)
    reason: str = ""                 # e.g. "no_transcript", "stt_down"
    detail: str = ""                 # why, when the server says (stt_down: Gemini's answer)
    intent: str = ""
    timing: str = ""                 # "hear=..;think=..;voice=.." (ms, server side)
    media: str = ""                  # «play:quran», «stop», «volume:up» — for the player


class Server:
    def __init__(self, api_base: str, token: str, *, timeout: float = 20, session=None):
        import requests
        self.base = api_base.rstrip("/")
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers["X-Robot-Token"] = token

    def _post(self, path: str, **kwargs):
        return self._send("post", path, **kwargs)

    def _send(self, method: str, path: str, **kwargs):
        import requests
        try:
            resp = getattr(self.http, method)(self.base + path, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ServerDown(str(exc)) from exc
        if resp.status_code == 401:
            raise Unauthorized(f"{path} → 401: check token and api_base in brain.ini")
        if resp.status_code >= 500:
            raise ServerDown(f"{path} → {resp.status_code}")
        return resp

    def heartbeat(self, version: str) -> int:
        return self._post("/heartbeat/", json={"firmware_version": version}).status_code

    def voice(self, *, transcript: Optional[str] = None, wav: Optional[bytes] = None,
              ptt: bool = False, caps: str = "", said_ago_ms: Optional[int] = None) -> Answer:
        """Ask the robot's brain. Send the words (`transcript`) — or, when the
        ears couldn't load, the recording (`wav`) for the server to transcribe.
        `caps`: what this robot can do besides talking ("media": play sound).
        `said_ago_ms`: how long ago they started saying it — the server checks
        its follow-up window then, not after this computer's hearing time."""
        # µ-law both ways: half the bytes up and down, the server's own codec.
        data = {"ptt": "1" if ptt else "0", "reply_audio": "ulaw"}
        if caps:
            data["caps"] = caps
        if said_ago_ms is not None:
            data["said_ago_ms"] = str(int(said_ago_ms))
        files = None
        if transcript is not None:
            data["transcript"] = transcript
        if wav is not None:
            files = {"audio": ("voice.wav", wav, "audio/wav")}
        resp = self._post("/voice/", data=data, files=files)
        head = resp.headers
        if resp.status_code == 200 and head.get("Content-Type", "").startswith("audio/"):
            return Answer(wav=resp.content, reply=unquote(head.get("X-Robot-Reply", "")),
                          transcript=unquote(head.get("X-Robot-Transcript", "")),
                          addressed=head.get("X-Robot-Addressed", "1") == "1",
                          intent=unquote(head.get("X-Robot-Intent", "")),
                          timing=head.get("X-Robot-Timing", ""),
                          media=head.get("X-Robot-Media", ""))
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200:
            return Answer(wav=None, reply="", transcript=transcript or "", addressed=False,
                          reason=f"http_{resp.status_code}")
        media = body.get("media") if isinstance(body.get("media"), dict) else {}
        return Answer(wav=None, reply=str(body.get("reply") or ""),
                      transcript=str(body.get("transcript") or transcript or ""),
                      addressed=bool(body.get("addressed", True)),
                      reason=str(body.get("reason") or ""), intent=str(body.get("intent") or ""),
                      detail=str(body.get("detail") or ""),
                      media=(media.get("do", "") + (f":{media['what']}" if media.get("what") else ""))
                      if media else "")

    def speak(self, text: str, *, codec: str = "pcm") -> Optional[bytes]:
        """16 kHz WAV of `text` in the robot's voice (PCM, or µ-law: half the
        bytes, for the clips kept on disk), or None."""
        data = {"text": text, "format": "wav"}
        if codec == "ulaw":
            data["codec"] = "ulaw"
        resp = self._post("/speak/", data=data)
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("audio/"):
            return resp.content
        return None

    def catalog(self) -> dict:
        """The branch's retail-only catalog ({} when the server won't give it)."""
        resp = self._post("/sync/pull/")
        try:
            return resp.json() if resp.status_code == 200 else {}
        except ValueError:
            return {}

    def commands(self) -> list:
        """What the server queued for the robot ([{command_id, kind, payload}])."""
        resp = self._send("get", "/commands/pending/")
        try:
            body = resp.json() if resp.status_code == 200 else {}
        except ValueError:
            body = {}
        commands = body.get("commands") if isinstance(body, dict) else None
        return [c for c in commands or [] if isinstance(c, dict)]

    def ack(self, command_id, ok: bool = True) -> None:
        self._post("/commands/ack/", json={"command_id": command_id, "ok": 1 if ok else 0})

    def sync_push(self, events: list) -> dict:
        """What was done offline (outbox.py) → {"applied", "skipped_duplicates", "failed"}."""
        resp = self._post("/sync/push/", json={"events": events})
        if resp.status_code != 200:
            raise ServerDown(f"/sync/push/ → {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}
