"""«إرسال الصوت أول بأول», end to end: «مخ موس» — its own loop and its own
socket (robot/brain/live.py) — talks to this server's own consumer
(robot/consumers.py), no network in between. Vosk writes «عندك سلطة الزيت»
where «عندك فلتر زيت» was said; the sound goes up while it's said, Azure
starts on it the moment its name is heard, and the answer comes back on the
same socket: one trip to Azure, none to /voice/.
"""

import asyncio
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import numpy as np
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import override_settings

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, views
from robot.brain import config, live, loop, server, vad
from robot.models import RobotDevice, RobotHeard
from robot.tests.test_brain import _speech
from robot.tests.test_brain_offline import _clip
from robot.tests.test_brain_teach import _Ears
from robot.tests.test_learning_e2e_db import API, FOUND, _Wire
from robot.tests.test_live_voice_db import _Azure, _shop_app
from robot.tests.test_teachers_db import AZURE


class _Bridge:
    """websocket-client's socket for the brain, answered by the server's own
    consumer through Channels' test communicator, on an event loop of its own."""

    def __init__(self, app, token):
        self.closed = False
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.comm = WebsocketCommunicator(app, "/ws/robot/voice/", headers=[(b"x-robot-token", token.encode())])
        connected, _ = self._run(self.comm.connect(timeout=15))
        if not connected:
            raise ConnectionError("refused")

    def _run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def send(self, payload):
        self._run(self.comm.send_input({"type": "websocket.receive", "text": payload}))

    def send_binary(self, payload):
        self._run(self.comm.send_input({"type": "websocket.receive", "bytes": payload}))

    def recv(self):
        message = self._run(self.comm.receive_output(timeout=3600), timeout=None)
        if message["type"] != "websocket.send":
            raise ConnectionError("closed")
        return message.get("text") if message.get("text") is not None else message["bytes"]

    def settimeout(self, timeout):
        pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.loop.call_soon_threadsafe(self.comm.output_queue.put_nowait, {"type": "websocket.close"})
        self._run(self.comm.send_input({"type": "websocket.disconnect", "code": 1000}))


@override_settings(GEMINI_API_KEY="")
class LiveVoiceEndToEndTests(ERPTenantTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        media = override_settings(MEDIA_ROOT=self.tmp.name)
        media.enable()
        self.addCleanup(media.disable)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-live-e2e", branch=self.branch)
        token = self.device.issue_token()
        self.device.save()
        make_inventory(make_product(name="فلتر زيت", part_number="11427953125", retail_price="450"), self.branch,
                       quantity=3)
        self.voice = _clip(5, 160)
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

        self.cfg = config.Config(api_base=API, token=token, home=Path(self.tmp.name) / "brain")
        self.cfg.ears_use, self.cfg.teach_all = "always", True                # brain.ini: teach = yes
        self.sockets = []

        def connect(url, token_, timeout):
            self.sockets.append(_Bridge(_shop_app(self.tenant), token_))
            return self.sockets[-1]
        self.lines = []
        self.live = live.LiveVoice(API, token, log=self.lines.append, connect=connect).start()
        self.addCleanup(self._stop)
        self.http = server.Server(API, token, session=_Wire())
        self.brain = loop.Brain(self.cfg, ears=_Ears("يا موس عندك سلطة الزيت"), server=self.http, mic=None,
                                speaker=mock.Mock(), log=self.lines.append, live=self.live)
        self.brain._ask = lambda **kwargs: self.brain._voice(**kwargs)   # on this thread: /voice/ in the shop's schema
        self.brain._check = lambda *args: None
        self.assertTrue(self.brain.refresh())
        for clip in self.brain.knowledge.clip_texts():
            self.brain.bank.put(clip, _clip(100, 160))
        self.brain.online = True
        for _ in range(500):
            if self.live.ready:
                break
            threading.Event().wait(0.02)

    def _stop(self):
        self.live.stop()
        for bridge in self.sockets:
            bridge.close()

    def test_the_answer_comes_back_on_the_socket_it_went_up_on(self):
        frames = _speech(1600, np.random.default_rng(21))
        self.assertTrue(self.live.ready)
        self.brain._live_begin()                                       # the sentence starts
        self.assertTrue(self.brain._live_on)
        for i, frame in enumerate(frames):
            self.brain.live.feed(frame)
            if i == 10:                                                # «يا موس …» heard while it's said
                self.brain._live_teach(SimpleNamespace(partial="يا موس عندك"), SimpleNamespace(gapped=True, said_ms=1000))
        self.brain.handle(vad.Utterance(samples=np.concatenate(frames), speech_ms=1600, quietest=40,
                                        loudest=2500, cut=False))
        self.brain._live_drop()

        self.assertIn("[TEACH] «يا موس عندك سلطة الزيت» — azure heard «عندك فلتر زيت»", self.lines)
        self.assertIn(f"🔊 {FOUND}", self.lines)
        self.brain.speaker.play_wav.assert_called_once_with(self.voice)
        self.assertTrue(any("(live; up " in line and "hear=" in line for line in self.lines), self.lines)
        self.assertEqual(self.azure.kinds(), ["live"])                 # one trip, while it was said
        heard = RobotHeard.objects.get()
        self.assertEqual((heard.local_text, heard.teacher_text), ("عندك سلطة الزيت", "عندك فلتر زيت"))
        self.assertEqual(heard.billed_seconds, 2)                      # 1.6 s of sound
        self.assertTrue(heard.audio)

    def test_the_socket_gone_the_same_answer_over_http(self):
        frames = _speech(1600, np.random.default_rng(22))
        self.brain._live_begin()
        for frame in frames:
            self.brain.live.feed(frame)
        self.sockets[0].close()                                        # the server restarted mid-sentence
        for _ in range(500):
            if not self.live.ready:
                break
            threading.Event().wait(0.02)
        self.brain.handle(vad.Utterance(samples=np.concatenate(frames), speech_ms=1600, quietest=40,
                                        loudest=2500, cut=False))
        self.assertIn("[LIVE] the socket dropped: the whole recording, as before", self.lines)
        self.assertIn(f"🔊 {FOUND}", self.lines)
        self.assertEqual(self.azure.kinds(), ["whole"])                # /voice/, with the whole recording
        self.assertEqual(RobotHeard.objects.count(), 1)
