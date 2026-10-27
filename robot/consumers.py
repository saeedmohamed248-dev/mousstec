"""
robot/consumers.py — «مخ موس» talking live (ws/robot/voice/).

Over POST /voice/ the sentence's recording went up only once the customer had
stopped talking, and a teacher started hearing it only then. Over this socket
its sound comes up while it's still being said: when they stop, the server
has it already — and Azure, when a teacher is to hear it, has heard most of
it (teachers.AzureStream). The same turn as /voice/ (views._voice_turn), the
same device token — in the `X-Robot-Token` header, never the URL that logs
keep. The robot falls back to /voice/ whenever the socket isn't there.

The robot's messages:
  {"type": "start", "id": n, "teach": 0|1}   a sentence begins (teach=1: a teacher will likely hear it)
  <binary>                                     its sound so far: G.711 µ-law, 16 kHz mono, in pieces
  {"type": "teach", "id": n}                   it's said to the robot after all (its name, heard while
                                               it's said): Azure starts on it now, from its start
  {"type": "drop", "id": n}                    not one for the server after all: forgotten
  {"type": "end", "id": n, "audio": 0|1, "keep": bytes, …}
                                               answer it — with its sound (its first `keep` bytes)
                                               or without; … = /voice/'s own fields (transcript,
                                               check, confidence, heard, known, caps, said_ago_ms,
                                               ptt, reply_audio)
  {"type": "ping"}                             → {"type": "pong"}
  {"type": "have", "voices": [key, …]}         the replies' voices kept on the robot (voicebank.key):
                                               those come back as words alone ("voice": "kept")
The server's:
  {"type": "ready"}                            connected
  {"type": "reply", "id": n, "timing": "hear=..", "audio": true|false, …/voice/'s body…}
  <binary>                                     the reply spoken (a WAV), right after it, when audio=true
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from types import SimpleNamespace

from asgiref.sync import ThreadSensitiveContext
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from rest_framework.utils.encoders import JSONEncoder      # what /voice/'s JSON is written with

logger = logging.getLogger("mouss_tec_core")

RATE = 16000
MOST_BYTES = 30 * RATE           # 30 s of µ-law: a question, not a speech (the robot cuts at 12)
# The /voice/ fields a sentence's `end` may carry — nothing else reaches the turn
# (no face_embedding/employee_id: the robot's microphone has no face).
FIELDS = ("transcript", "check", "confidence", "heard", "known", "caps", "said_ago_ms", "ptt", "reply_audio",
          "talking")
TURNS_A_MINUTE = 60              # nobody says more; a runaway or stolen token is held to it
MOST_TEXT = 64 * 1024
MOST_VOICES = 500                # the robot keeps 300 (voicebank.REPLIES_MOST)
_VOICE_KEY = re.compile(r"^[0-9a-f]{24}$")


def _not_a_number(word: str):
    raise ValueError(f"{word} isn't a number")


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return ""


def _ip(scope):
    """The robot's address (behind Caddy: X-Forwarded-For, read by the tenant middleware),
    or None when it isn't one — RobotDevice.last_ip takes addresses only."""
    raw = scope.get("client_ip") or (scope.get("client") or [None])[0]
    try:
        return str(ipaddress.ip_address(str(raw).strip()))
    except ValueError:
        return None


class RobotVoiceConsumer(AsyncWebsocketConsumer):
    # No channel layer: a robot's sentences are its own socket's, nothing is broadcast —
    # and Redis down mustn't take the robot's voice with it.
    channel_layer_alias = None

    async def connect(self):
        self.device_id, self.sentence, self.sound, self.stream = None, None, bytearray(), None
        self._stream_ok, self._stream_checked_until = False, 0.0
        self.voices = None                   # the replies' voices the robot has (its `have`, and sent since);
        #                                      None: a robot that keeps none (before brain-0.9.8)
        token = _header(self.scope, b"x-robot-token")
        if self.scope.get("tenant") is None or not token:
            await self.close()               # refused at the handshake (403)
            return
        self.device_id = await self._in_shop(self._device, token)
        if self.device_id is None:
            await self.close()
            return
        await self.accept()
        await self.send(text_data=json.dumps({"type": "ready"}))

    async def disconnect(self, code):
        await self._let_go()

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None:
            self._heard(bytes_data)
            return
        if len(text_data or "") > MOST_TEXT:
            return                           # a sentence's fields are a few hundred bytes
        try:
            # 🐛 [FIX]: «"keep": Infinity» (json takes it) raised OverflowError out of the
            # handler and took the socket down: numbers that aren't numbers are refused.
            message = json.loads(text_data or "", parse_constant=_not_a_number)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "start":
            await self._start(message)
        elif kind == "teach":
            if message.get("id") is not None and message.get("id") == self.sentence:
                await self._hear_live()
        elif kind == "drop":
            await self._let_go()
        elif kind == "end":
            await self._end(message)
        elif kind == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))
        elif kind == "have":
            keys = message.get("voices")
            if isinstance(keys, list):
                self.voices = {k for k in keys[:MOST_VOICES] if isinstance(k, str) and _VOICE_KEY.match(k)}

    # ---- a sentence
    async def _start(self, message):
        await self._let_go()                 # one never ended: forgotten
        self.sentence = message.get("id")
        if str(message.get("teach", "")).lower() in ("1", "true", "yes"):
            await self._hear_live()

    async def _hear_live(self) -> None:
        """Azure hears the sentence as it comes — what came of it so far first."""
        from . import audio as audio_svc
        from . import teachers

        if self.stream is not None or not await self._in_shop(self._may_hear_live):
            return
        self.stream = teachers.AzureStream()
        if self.sound:
            self.stream.feed(audio_svc.ulaw_decode(bytes(self.sound)))

    def _may_hear_live(self) -> bool:
        """Azure's minutes have room (asked once a minute, not every sentence), and this robot
        hasn't started more live hearings this minute than anyone says sentences: each one let go
        is billed, so a stolen token looping «start» would spend the month's minutes."""
        from django.core.cache import cache

        from . import teachers

        now = time.time()
        if now >= self._stream_checked_until:
            self._stream_ok = teachers.can_stream()
            self._stream_checked_until = now + 60
        if not self._stream_ok or cache.get(teachers._STREAM_OFF):    # refused since: off at once
            return False
        key = f"robot:live:streams:{self.device_id}:{int(now // 60)}"
        try:
            cache.add(key, 0, 90)
            return cache.incr(key) <= TURNS_A_MINUTE
        except Exception:                    # the cache down: the limit waits
            return True

    def _heard(self, data: bytes) -> None:
        if self.sentence is None or len(self.sound) >= MOST_BYTES:
            return                           # no sentence begun (or a speech): let go
        data = data[:MOST_BYTES - len(self.sound)]
        self.sound += data
        if self.stream is not None:
            from . import audio as audio_svc
            self.stream.feed(audio_svc.ulaw_decode(data))

    async def _let_go(self) -> None:
        """Forget the sentence in progress — and Azure's hearing of it, its
        seconds counted (teachers.settle)."""
        stream, self.stream = self.stream, None
        self.sentence, self.sound = None, bytearray()
        if stream is not None:
            stream.cancel()
            try:
                await self._in_shop(self._settle, stream)
            except Exception as exc:         # the count is a guard, never a crash
                logger.warning("robot live voice: Azure's seconds not counted: %s", exc)

    def _settle(self, stream) -> None:
        from . import teachers
        from .models import RobotDevice

        device = RobotDevice.objects.filter(pk=self.device_id).first()
        if device is not None:
            teachers.settle(device, stream)

    async def _end(self, message):
        sentence = message.get("id")
        if sentence is None or sentence != self.sentence:
            await self._let_go()             # not the sentence begun: answered without its sound
        stream, sound = self.stream, bytes(self.sound)
        self.stream, self.sentence, self.sound = None, None, bytearray()
        try:
            keep = int(message.get("keep"))
        except (TypeError, ValueError, OverflowError):
            keep = len(sound)
        sound = sound[:max(0, keep)]
        wanted = str(message.get("audio", "")).lower() in ("1", "true", "yes")
        data = {k: message[k] for k in FIELDS if message.get(k) is not None}
        try:
            body, wav, timing = await self._in_shop(self._turn, data, sound if wanted else None, stream,
                                                    frozenset(self.voices or ()))
        except Exception as exc:             # one bad sentence mustn't end the robot's socket
            logger.exception("robot live voice: the turn failed")
            body, wav, timing = {"intent": "ignored", "addressed": False, "reason": "server_error",
                                 "detail": type(exc).__name__, "reply": ""}, None, ""
        await self.send(text_data=json.dumps({**body, "type": "reply", "id": sentence, "timing": timing,
                                              "audio": bool(wav)}, ensure_ascii=False, cls=JSONEncoder))
        if wav:
            await self.send(bytes_data=wav)
            self._kept(body.get("reply") or "")
        if stream is not None and not stream.taken:
            # 🐛 [FIX]: let go of — and billed — before the reply was voiced: up to a second more
            # on the answer. Now after it's gone out, and on every path (device off, a failure too).
            try:
                await self._in_shop(self._settle, stream)
            except Exception as exc:         # the count is a guard, never a crash
                logger.warning("robot live voice: Azure's seconds not counted: %s", exc)

    def _kept(self, reply: str) -> None:
        """A reply's voice sent: the robot keeps it (voicebank.ReplyVoices) — not sent again."""
        from .brain import voicebank

        if self.voices is not None and voicebank.ReplyVoices.worth(reply) and len(self.voices) < MOST_VOICES:
            self.voices.add(voicebank.key(reply))

    def _turn(self, data, sound, stream, kept=frozenset()):
        """The sentence answered, as /voice/ answers it: (body, the reply spoken or None, timing).
        `kept`: the replies whose voice the robot has — not voiced again, nor sent."""
        from django.core.cache import cache

        from . import audio as audio_svc
        from . import views, wakename
        from .models import RobotDevice

        timing = {"t0": time.monotonic()}
        device = RobotDevice.objects.filter(pk=self.device_id, is_active=True).first()
        if device is None:                   # switched off on the dashboard since it connected
            return {"intent": "ignored", "addressed": False, "reason": "device_off", "reply": ""}, None, ""
        key = f"robot:live:turns:{device.pk}:{int(time.time() // 60)}"
        try:
            cache.add(key, 0, 90)
            too_many = cache.incr(key) > TURNS_A_MINUTE
        except Exception:                    # the cache down: the limit waits, the answer doesn't
            too_many = False
        if too_many:
            return {"intent": "ignored", "addressed": False, "reason": "too_many", "reply": ""}, None, ""
        recording = audio_svc.make_wav(sound, RATE, audio_svc.ULAW) if sound else None
        body, listen_after = views._voice_turn(SimpleNamespace(data=data), device, recording, timing,
                                               streamed=stream)
        from .brain import voicebank
        reply = body.get("reply") or ""
        if kept and data.get("reply_audio") and voicebank.ReplyVoices.worth(reply) and voicebank.key(reply) in kept:
            # «الرقم ده مش صح…» said before: its words alone — no TTS here, nothing big on the shop's line.
            body["voice"], wav, voices = "kept", None, ["kept"]
            timing["voice"] = time.monotonic()
        else:
            wav, voices = views._voice_audio(data, timing, body)
        if listen_after:
            wakename.keep_listening(device, speaking=body.get("reply") or "")
        return body, wav, views._timing_line(timing, voices)

    def _device(self, token):
        from . import security

        device = security.device_for_token(token, _ip(self.scope))
        return device.pk if device is not None else None

    # ---- the shop's own schema, on a thread of the sentence's own
    async def _in_shop(self, fn, *args):
        """`fn(*args)` in this shop's schema. A sentence's AI and voice take
        seconds: each runs on a thread of its own, so another robot's sentence
        (another shop's too) never waits behind it."""
        async with ThreadSensitiveContext():
            return await database_sync_to_async(self._shop_call)(fn, *args)

    def _shop_call(self, fn, *args):
        from django.db import connection
        from django_tenants.utils import tenant_context

        try:
            with tenant_context(self.scope["tenant"]):
                return fn(*args)
        finally:
            # A thread for this call alone: its connection isn't kept for the next one.
            if not connection.in_atomic_block:
                connection.close()
