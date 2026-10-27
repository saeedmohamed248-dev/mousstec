"""«إرسال الصوت أول بأول»: the live socket (ws/robot/voice/, robot/consumers.py).

The sentence's sound comes up while it's said; `end` answers it as /voice/
would — and Azure, when a teacher is to hear it, heard it as it came
(teachers.AzureStream): one trip, its words ready at once. A live hearing
let go of is still counted against the month's minutes; one that fails (or
hears nothing where the whole recording has words) gives way to the whole
recording, as before, and is left off for a while.
"""

import json
import os
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np
from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import audio as audio_svc
from robot import consumers, services, teachers, views
from robot.brain import wavs
from robot.consumers import RobotVoiceConsumer
from robot.models import RobotDevice, RobotHeard, RobotVoiceInteraction
from robot.tests.test_teachers_db import AZURE, _Resp, azure_says

FOUND = "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه."
SOUND = wavs.ulaw_encode(np.full(int(16000 * 2.5), 800, dtype=np.int16))     # 2.5 s, as the brain sends it
ASKED = {"transcript": "يا موس عندك سلطة الزيت", "check": "1", "confidence": "0.500", "reply_audio": "ulaw",
         "said_ago_ms": "900", "ptt": "0"}


class _Azure:
    """requests.post for Azure: a live hearing's chunks are read as they come
    (b"".join waits for the last one), a whole recording at once."""

    def __init__(self, live=None, whole=None):
        self.live = live or azure_says("يا موس، عندك فلتر زيت؟")
        self.whole = whole or azure_says("يا موس، عندك فلتر زيت؟")
        self.calls = []                  # [("live" | "whole", body)]

    def __call__(self, url, params=None, headers=None, data=None, timeout=None):
        if isinstance(data, bytes):
            self.calls.append(("whole", data))
            return self.whole
        try:
            body = b"".join(data)
        except Exception:
            self.calls.append(("live", None))
            raise
        self.calls.append(("live", body))
        return self.live

    def kinds(self):
        return [kind for kind, _ in self.calls]


def _shop_app(tenant, ip="10.1.2.3"):
    """The consumer as the tenant middleware hands it a socket (robot/consumers.py)."""
    inner = RobotVoiceConsumer.as_asgi()

    async def app(scope, receive, send):
        return await inner({**scope, "tenant": tenant, "schema_name": tenant.schema_name, "client_ip": ip},
                           receive, send)
    return app


@override_settings(GEMINI_API_KEY="")
class LiveVoiceTests(ERPTenantTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        media = override_settings(MEDIA_ROOT=self.tmp.name)
        media.enable()
        self.addCleanup(media.disable)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-live", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(oil, self.branch, quantity=3)
        self.voice = audio_svc.make_wav(b"\x00\x01" * 160)
        for target, name, value in ((views.audio_svc, "synthesize_wav", self.voice),
                                    (services, "shelf_location", ""), (services, "ai_reply", "")):
            patcher = mock.patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, AZURE)
        env.start()
        self.addCleanup(env.stop)
        self.azure = _Azure()
        post = mock.patch("requests.post", side_effect=self.azure)
        post.start()
        self.addCleanup(post.stop)

    # ---- the socket
    def talk(self, *sentences, token=None, app=None, headers=None):
        """Connect and say `sentences` ([(kind, fields)]); what came back."""
        return async_to_sync(self._talk)(sentences, token, app, headers)

    async def _talk(self, sentences, token, app, headers):
        token = self.token if token is None else token
        comm = WebsocketCommunicator(app or _shop_app(self.tenant), "/ws/robot/voice/",
                                     headers=headers if headers is not None
                                     else ([(b"x-robot-token", token.encode())] if token else []))
        connected, _ = await comm.connect(timeout=15)
        out = {"connected": connected, "replies": []}
        if not connected:
            return out
        out["hello"] = json.loads(await comm.receive_from(timeout=5))
        for n, (kind, fields) in enumerate(sentences, start=1):
            sound = fields.pop("sound", b"")
            if kind in ("say", "drop"):
                await comm.send_json_to({"type": "start", "id": n, "teach": int(fields.pop("teach", 0))})
                for i in range(0, len(sound), 2048):
                    await comm.send_to(bytes_data=sound[i:i + 2048])
            if kind == "drop":
                await comm.send_json_to({"type": "drop", "id": n})
                await comm.send_json_to({"type": "ping"})
                out["replies"].append(json.loads(await comm.receive_from(timeout=5)))
                continue
            await comm.send_json_to({"type": "end", "id": n, "audio": int(bool(sound)),
                                     "keep": fields.pop("keep", len(sound)), **fields})
            reply = json.loads(await comm.receive_from(timeout=20))
            wav = await comm.receive_from(timeout=20) if reply.get("audio") else None
            out["replies"].append((reply, wav))
        await comm.disconnect()
        return out

    # ---- who may talk
    def test_only_this_shops_robot_gets_a_socket(self):
        self.assertFalse(self.talk(token="")["connected"])                    # no token
        self.assertFalse(self.talk(token="not-the-token")["connected"])
        got = self.talk()
        self.assertTrue(got["connected"])
        self.assertEqual(got["hello"], {"type": "ready"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.last_ip, "10.1.2.3")                      # seen, from where
        RobotDevice.objects.filter(pk=self.device.pk).update(is_active=False)
        self.assertFalse(self.talk()["connected"])                             # switched off on the dashboard

    def test_through_the_servers_own_stack(self):
        # erp_core/asgi.py: the shop from the Host, the Origin checked, the token from its header.
        from erp_core.asgi import application
        headers = [(b"host", self.domain.domain.encode()), (b"origin", b"http://localhost"),
                   (b"x-robot-token", self.token.encode())]
        got = self.talk(("end", {"transcript": "يا موس عندك فلتر زيت"}), app=application, headers=headers)
        self.assertTrue(got["connected"])
        self.assertEqual(got["replies"][0][0]["reply"], FOUND)
        evil = [(b"host", self.domain.domain.encode()), (b"origin", b"https://evil.example"),
                (b"x-robot-token", self.token.encode())]
        self.assertFalse(self.talk(app=application, headers=evil)["connected"])

    # ---- a sentence
    def test_the_teacher_heard_it_while_it_was_said(self):
        got = self.talk(("say", {"sound": SOUND, "teach": 1, **ASKED}))
        reply, wav = got["replies"][0]
        self.assertEqual(reply["reply"], FOUND)
        self.assertEqual((reply["heard_by"], reply["taught"]), ("azure", "عندك فلتر زيت"))
        self.assertTrue(reply["audio"])
        self.assertEqual(wav, self.voice)                                       # the reply spoken, right after
        self.assertIn("hear=", reply["timing"])
        # One trip to Azure, made while it was said: the header, then the sound itself.
        self.assertEqual(self.azure.kinds(), ["live"])
        body = self.azure.calls[0][1]
        self.assertEqual(body[:4], b"RIFF")
        self.assertEqual(body[44:], audio_svc.ulaw_decode(SOUND))
        row = RobotHeard.objects.get()
        self.assertEqual((row.local_text, row.teacher_text, row.billed_seconds), ("عندك سلطة الزيت", "عندك فلتر زيت", 3))
        self.assertTrue(row.audio)                                              # kept, for the shop's own model

    def test_a_sentence_the_server_hears_itself(self):
        # No ears fast enough on the shop's computer: no words sent, Azure's are the sentence.
        got = self.talk(("say", {"sound": SOUND, "teach": 1, "reply_audio": "ulaw"}))
        reply, _wav = got["replies"][0]
        self.assertEqual(reply["reply"], FOUND)
        self.assertEqual(self.azure.kinds(), ["live"])
        self.assertEqual(RobotHeard.objects.get().billed_seconds, 3)

    def test_the_same_answer_as_over_http(self):
        got = self.talk(("end", {"transcript": "يا موس عندك فلتر زيت", "known": "trusted"}))
        reply, wav = got["replies"][0]
        self.assertIsNone(wav)                                                  # no reply_audio asked
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": "يا موس عندك فلتر زيت"},
                                           HTTP_X_ROBOT_TOKEN=self.token)
        over_http = views.voice(request).data
        for key in ("intent", "reply", "transcript", "addressed", "lang"):
            self.assertEqual(reply[key], over_http[key])
        self.assertEqual(self.azure.calls, [])                                  # words only: no teacher
        self.assertEqual(RobotVoiceInteraction.objects.filter(payload__heard_as="trusted").count(), 1)

    def test_only_the_sentence_itself_is_kept(self):
        # The pause that ended it went up too; the robot says where the sentence ends (`keep`).
        tail = wavs.ulaw_encode(np.zeros(8000, dtype=np.int16))
        self.talk(("say", {"sound": SOUND + tail, "keep": len(SOUND), "teach": 1, **ASKED}))
        parts = audio_svc.wav_parts(RobotHeard.objects.get().audio.read())
        self.assertEqual(len(parts[2]), len(SOUND))

    # ---- what Azure costs
    def test_a_sentence_let_go_of_is_counted_all_the_same(self):
        got = self.talk(("drop", {"sound": SOUND[:24000], "teach": 1}))
        self.assertEqual(got["replies"], [{"type": "pong"}])
        row = RobotHeard.objects.get()
        self.assertEqual((row.local_text, row.teacher_text, row.billed_seconds), ("", "", 2))   # 1.5 s
        self.assertFalse(row.audio)                                             # nothing kept of it
        self.assertEqual(teachers.used_seconds("azure"), 2)
        self.assertEqual(teachers._pairs().count(), 0)                          # nothing learned from it either

    def test_no_live_hearing_without_minutes_or_a_reason(self):
        with mock.patch.dict(os.environ, {"ROBOT_AZURE_FREE_MINUTES": "0"}):
            self.talk(("say", {"sound": SOUND, "teach": 1, **ASKED}))
        self.assertEqual(self.azure.calls, [])                                  # past the month's minutes: nothing
        self.talk(("say", {"sound": SOUND, **ASKED}))                           # teach=0: the whole recording, at the end
        self.assertEqual(self.azure.kinds(), ["whole"])

    # ---- when the live hearing fails
    def test_refused_live_the_whole_recording_answers_and_live_rests(self):
        self.azure.live = _Resp({}, status=400)
        got = self.talk(("say", {"sound": SOUND, "teach": 1, **ASKED}),
                        ("say", {"sound": SOUND, "teach": 1, **ASKED}))
        for reply, _wav in got["replies"]:
            self.assertEqual((reply["reply"], reply["heard_by"]), (FOUND, "azure"))
        self.assertEqual(self.azure.kinds(), ["live", "whole", "whole"])        # the second: no live try at all
        self.assertFalse(teachers.can_stream())
        self.assertEqual([r.billed_seconds for r in RobotHeard.objects.order_by("id")], [3, 3])   # a 400 isn't billed

    def test_heard_nothing_live_where_the_whole_recording_has_words(self):
        self.azure.live = _Resp({"RecognitionStatus": "NoMatch"})
        reply, _wav = self.talk(("say", {"sound": SOUND, "teach": 1, **ASKED}))["replies"][0]
        self.assertEqual(reply["taught"], "عندك فلتر زيت")
        self.assertEqual(self.azure.kinds(), ["live", "whole"])
        self.assertEqual(RobotHeard.objects.get().billed_seconds, 6)           # both trips counted
        self.assertFalse(teachers.can_stream())                                 # its live hearing isn't right: off

    def test_once_live_hearing_has_heard_words_its_silence_is_believed(self):
        self.talk(("say", {"sound": SOUND, "teach": 1, **ASKED}))
        self.azure.live = _Resp({"RecognitionStatus": "NoMatch"})
        reply, _wav = self.talk(("say", {"sound": SOUND, "teach": 1, **ASKED}))["replies"][0]
        self.assertNotIn("heard_by", reply)                                     # the robot's own words answered
        self.assertEqual(self.azure.kinds(), ["live", "live"])                  # no second trip for nothing
        self.assertTrue(teachers.can_stream())

    def test_a_runaway_robot_is_held_to_so_many_sentences_a_minute(self):
        with mock.patch.object(consumers, "TURNS_A_MINUTE", 1):
            got = self.talk(("end", {"transcript": "يا موس عندك فلتر زيت"}),
                            ("end", {"transcript": "يا موس عندك فلتر زيت"}))
        self.assertEqual(got["replies"][0][0]["reply"], FOUND)
        self.assertEqual(got["replies"][1][0]["reason"], "too_many")
