"""Unit tests for webhook payload parsing / tenant routing (no DB needed)."""
from django.test import SimpleTestCase

from omnichannel.services.routing import (
    CHANNEL_MESSENGER,
    CHANNEL_WHATSAPP,
    extract_inbound_messages,
)


class WhatsAppExtractionTests(SimpleTestCase):
    def _payload(self, **overrides):
        base = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "PN_123"},
                        "contacts": [{"wa_id": "201000", "profile": {"name": "Ahmed"}}],
                        "messages": [{
                            "from": "201000",
                            "id": "wamid.ABC",
                            "type": "text",
                            "text": {"body": "  عايز سعر فلتر زيت  "},
                        }],
                    }
                }]
            }],
        }
        base.update(overrides)
        return base

    def test_extracts_text_message(self):
        msgs = extract_inbound_messages(self._payload())
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual(m.channel, CHANNEL_WHATSAPP)
        self.assertEqual(m.route_key, "PN_123")
        self.assertEqual(m.sender_id, "201000")
        self.assertEqual(m.text, "عايز سعر فلتر زيت")  # trimmed
        self.assertEqual(m.message_id, "wamid.ABC")
        self.assertEqual(m.sender_name, "Ahmed")

    def test_ignores_status_updates(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": "PN_123"},
                "statuses": [{"status": "delivered"}],
            }}]}],
        }
        self.assertEqual(extract_inbound_messages(payload), [])

    def test_ignores_non_text_type(self):
        payload = self._payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "image"
        self.assertEqual(extract_inbound_messages(payload), [])


class MessengerExtractionTests(SimpleTestCase):
    def test_extracts_messenger_text(self):
        payload = {
            "object": "page",
            "entry": [{
                "id": "PAGE_999",
                "messaging": [{
                    "sender": {"id": "PSID_1"},
                    "message": {"mid": "m_1", "text": "hello"},
                }],
            }],
        }
        msgs = extract_inbound_messages(payload)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].channel, CHANNEL_MESSENGER)
        self.assertEqual(msgs[0].route_key, "PAGE_999")
        self.assertEqual(msgs[0].sender_id, "PSID_1")
        self.assertEqual(msgs[0].text, "hello")

    def test_skips_echo(self):
        payload = {
            "object": "page",
            "entry": [{"id": "PAGE_999", "messaging": [{
                "sender": {"id": "PAGE_999"},
                "message": {"is_echo": True, "text": "auto"},
            }]}],
        }
        self.assertEqual(extract_inbound_messages(payload), [])

    def test_unknown_object_ignored(self):
        self.assertEqual(extract_inbound_messages({"object": "instagram"}), [])
        self.assertEqual(extract_inbound_messages({}), [])
        self.assertEqual(extract_inbound_messages(None), [])
