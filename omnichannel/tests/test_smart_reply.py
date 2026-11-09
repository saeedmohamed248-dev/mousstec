"""
🤖 «الموظف الذكي» على واتساب/ماسنجر — التلات تصليحات:

🐛 قبل كده:
  • الفويس والصور كانوا بيتجاهلوا من غير أي رد (نص الزباين في مصر بيبعتوا فويس أو صورة القطعة).
  • كل رسالة كانت بتتبعت للموديل لوحدها — «طيب بكام؟» بعد «عندك فلتر X5؟» ماكانتش بتتفهم.
  • صاحب المحل لما يرد بنفسه، البوت كان بيفضل يرد هو كمان على نفس الزبون.
"""
import uuid
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from clients.models import Client
from omnichannel import tasks
from omnichannel.models import ChannelMessageLog, TenantChannelConfig
from omnichannel.services import llm
from omnichannel.services.routing import CHANNEL_INSTAGRAM, extract_inbound_messages


def _wa(msg):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PN"}, "contacts": [{"wa_id": "201000", "profile": {"name": "أحمد"}}],
        "messages": [{"from": "201000", "id": "wamid.X", **msg}]}}]}]}


class MediaRoutingTests(SimpleTestCase):
    def test_whatsapp_voice_note(self):
        [m] = extract_inbound_messages(_wa({"type": "audio", "audio": {"id": "MID1", "mime_type": "audio/ogg"}}))
        self.assertEqual((m.media_type, m.media_id, m.media_mime, m.text), ("audio", "MID1", "audio/ogg", ""))

    def test_whatsapp_image_with_caption(self):
        [m] = extract_inbound_messages(_wa({"type": "image", "image": {"id": "IMG", "caption": " دي لـ X5 "}}))
        self.assertEqual((m.media_type, m.media_id, m.text), ("image", "IMG", "دي لـ X5"))

    def test_media_without_id_is_ignored(self):
        self.assertEqual(extract_inbound_messages(_wa({"type": "audio", "audio": {}})), [])

    def test_instagram_attachment(self):
        payload = {"object": "instagram", "entry": [{"id": "IG1", "messaging": [{
            "sender": {"id": "U9"}, "message": {"mid": "m1", "attachments": [
                {"type": "image", "payload": {"url": "https://cdn.example/x.jpg"}}]}}]}]}
        [m] = extract_inbound_messages(payload)
        self.assertEqual((m.channel, m.media_type, m.media_url), (CHANNEL_INSTAGRAM, "image", "https://cdn.example/x.jpg"))


class HistoryPromptTests(SimpleTestCase):
    def test_gemini_contents_alternate_roles_and_end_with_customer(self):
        c = llm._gemini_contents([("عندك فلتر زيت X5؟", "أيوه بـ 450"), ("و للـ X3؟", "")], "طيب بكام؟")
        self.assertEqual([x["role"] for x in c], ["user", "model", "user"])
        self.assertIn("و للـ X3؟", c[-1]["parts"][0]["text"])
        self.assertTrue(c[-1]["parts"][0]["text"].endswith("طيب بكام؟"))

    @override_settings(GEMINI_API_KEY="k")
    def test_generate_reply_sends_history_to_the_model(self):
        cfg = TenantChannelConfig(tenant=Client(name="مركز"))
        with mock.patch.object(llm.requests, "post") as post:
            post.return_value.json.return_value = {"candidates": [{"content": {"parts": [{"text": "بـ 450 ج"}]}}]}
            out = llm.generate_reply(cfg, "طيب بكام؟", "", history=[("عندك فلتر X5؟", "أيوه متوفر")])
        self.assertEqual(out, "بـ 450 ج")
        contents = post.call_args.kwargs["json"]["contents"]
        self.assertEqual(contents[0]["parts"][0]["text"], "عندك فلتر X5؟")
        self.assertEqual(contents[1], {"role": "model", "parts": [{"text": "أيوه متوفر"}]})


@override_settings(OMNICHANNEL_HUMAN_PAUSE_MINUTES=30)
class SmartReplyTaskTests(TestCase):
    def setUp(self):
        p = mock.patch.object(Client, "auto_create_schema", False)
        p.start()
        self.addCleanup(p.stop)
        schema = f"tst_{uuid.uuid4().hex[:8]}"
        Client.objects.bulk_create([Client(schema_name=schema, name="مركز", owner_name="م", phone="01000000000")])
        self.tenant = Client.objects.get(schema_name=schema)
        self.config = TenantChannelConfig.objects.create(
            tenant=self.tenant, is_subscription_active=True, ai_enabled=True, whatsapp_phone_number_id="PN")
        self.config.meta_access_token = "TOKEN"
        self.config.save()
        for target, value in (("omnichannel.tasks.build_catalog_context", ""),
                              ("omnichannel.tasks.meta_api.send_whatsapp_text", {})):
            pt = mock.patch(target, return_value=value)
            setattr(self, target.rsplit(".", 1)[1], pt.start())
            self.addCleanup(pt.stop)

    def run_task(self, text="", **kw):
        tasks.process_inbound_message.run(config_id=self.config.pk, channel="whatsapp", sender_id="201000",
                                          text=text, message_id=uuid.uuid4().hex, **kw)

    def last_log(self):
        return ChannelMessageLog.objects.filter(tenant=self.tenant).order_by("-created_at", "-pk").first()

    def test_follow_up_question_gets_the_conversation_history(self):
        ChannelMessageLog.objects.create(tenant=self.tenant, channel="whatsapp", sender_id="201000",
                                         inbound_text="عندك فلتر زيت X5؟", outbound_text="أيوه متوفر",
                                         status="replied", created_at=timezone.now() - timedelta(minutes=3))
        with mock.patch("omnichannel.tasks.generate_reply", return_value="بـ 450 ج") as gen:
            self.run_task("طيب بكام؟")
        self.assertEqual(gen.call_args.kwargs["history"], [("عندك فلتر زيت X5؟", "أيوه متوفر")])
        # المخزن بيتدوّر فيه بالصنف اللي اتذكر قبل كده كمان
        self.assertIn("فلتر زيت X5", self.build_catalog_context.call_args.args[0])
        self.send_whatsapp_text.assert_called_once()

    def test_bot_stays_silent_while_a_human_is_replying(self):
        ChannelMessageLog.objects.create(tenant=self.tenant, channel="whatsapp", sender_id="201000",
                                         outbound_text="معاك محمد، ثواني", status="replied", is_human=True,
                                         created_at=timezone.now() - timedelta(minutes=5))
        with mock.patch("omnichannel.tasks.generate_reply") as gen:
            self.run_task("تمام مستني")
        gen.assert_not_called()
        self.send_whatsapp_text.assert_not_called()
        self.assertEqual((self.last_log().status, self.last_log().error), ("skipped", "human_active"))

    def test_bot_answers_again_after_the_pause(self):
        ChannelMessageLog.objects.create(tenant=self.tenant, channel="whatsapp", sender_id="201000",
                                         outbound_text="رد قديم", status="replied", is_human=True,
                                         created_at=timezone.now() - timedelta(minutes=45))
        with mock.patch("omnichannel.tasks.generate_reply", return_value="أهلاً"):
            self.run_task("السلام عليكم")
        self.send_whatsapp_text.assert_called_once()

    def test_voice_note_is_transcribed_and_answered(self):
        with mock.patch("omnichannel.services.media.download", return_value=(b"OGG", "audio/ogg")) as dl, \
                mock.patch("omnichannel.services.media.understand", return_value="عندك طرمبة بنزين لـ X5؟") as und, \
                mock.patch("omnichannel.tasks.generate_reply", return_value="أيوه متوفرة") as gen:
            self.run_task("", media_type="audio", media_id="MID1", media_mime="audio/ogg")
        dl.assert_called_once_with(media_id="MID1", media_url="", access_token="TOKEN")
        self.assertEqual(und.call_args.kwargs["kind"], "audio")
        self.assertEqual(gen.call_args.args[1], "(رسالة صوتية) عندك طرمبة بنزين لـ X5؟")
        self.assertEqual(self.send_whatsapp_text.call_args.kwargs["text"], "أيوه متوفرة")
        self.assertIn("طرمبة", self.last_log().inbound_text)

    def test_media_we_cannot_understand_gets_a_polite_reply_not_silence(self):
        self.config.notify_on_handoff = True       # تنبيه صاحب المحل (اختياري في الإعدادات)
        self.config.save(update_fields=["notify_on_handoff"])
        with mock.patch("omnichannel.services.media.download", side_effect=RuntimeError("meta down")), \
                mock.patch("omnichannel.tasks.generate_reply") as gen, \
                mock.patch("omnichannel.tasks._notify_handoff") as notify:
            self.run_task("", media_type="image", media_id="IMG")
        gen.assert_not_called()
        self.assertIn("الصورة", self.send_whatsapp_text.call_args.kwargs["text"])
        self.assertIn("اسم القطعة", self.send_whatsapp_text.call_args.kwargs["text"])
        notify.assert_called_once()


class MediaUnderstandTests(SimpleTestCase):
    @override_settings(GEMINI_API_KEY="k")
    def test_voice_mime_params_are_stripped_and_text_returned(self):
        from omnichannel.services import media
        cfg = TenantChannelConfig(tenant=Client(name="مركز"))
        with mock.patch.object(media.requests, "post") as post:
            post.return_value.json.return_value = {"candidates": [{"content": {"parts": [{"text": " عندك كبالن؟ "}]}}]}
            out = media.understand(cfg, kind="audio", data=b"OGG", mime="audio/ogg; codecs=opus")
        self.assertEqual(out, "عندك كبالن؟")
        inline = post.call_args.kwargs["json"]["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(inline["mimeType"], "audio/ogg")

    def test_no_key_means_no_call(self):
        from omnichannel.services import media
        cfg = TenantChannelConfig(tenant=Client(name="مركز"))
        with override_settings(GEMINI_API_KEY=""), mock.patch.object(media.requests, "post") as post:
            self.assertEqual(media.understand(cfg, kind="image", data=b"x", mime=""), "")
        post.assert_not_called()
