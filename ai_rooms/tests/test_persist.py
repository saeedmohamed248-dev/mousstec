"""Tests for ai_rooms persistence and hub."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from ai_rooms.models import AIRoomConversation
from ai_rooms.services.persist import persist_turn, close_conversation


User = get_user_model()


class _Sess(dict):
    modified = False


def _req_factory(user):
    rf = RequestFactory()
    req = rf.get('/')
    req.user = user
    req.session = _Sess()
    return req


class PersistTurnTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('saeed', password='x')

    def test_creates_conversation_on_first_turn(self):
        req = _req_factory(self.user)
        conv = persist_turn(
            req, room='repair_atlas', audience='shop',
            user_text='ازاي أفك الدينمو؟', assistant_text='افصل البطارية…',
            vehicle={'brand': 'Toyota', 'model_name': 'Corolla'},
        )
        self.assertIsNotNone(conv)
        self.assertEqual(conv.turn_count, 2)
        self.assertEqual(conv.brand, 'Toyota')
        self.assertEqual(conv.title, 'ازاي أفك الدينمو؟')
        self.assertIn(conv.id, req.session.values())

    def test_second_turn_appends_to_same_conversation(self):
        req = _req_factory(self.user)
        c1 = persist_turn(req, room='repair_atlas', audience='shop',
                          user_text='q1', assistant_text='a1')
        c2 = persist_turn(req, room='repair_atlas', audience='shop',
                          user_text='q2', assistant_text='a2')
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(c2.turn_count, 4)
        self.assertEqual(AIRoomConversation.objects.count(), 1)

    def test_different_room_opens_new_conversation(self):
        req = _req_factory(self.user)
        c1 = persist_turn(req, room='repair_atlas', audience='shop',
                          user_text='q', assistant_text='a')
        c2 = persist_turn(req, room='auto_diagnostic', audience='shop',
                          user_text='q', assistant_text='a')
        self.assertNotEqual(c1.id, c2.id)
        self.assertEqual(AIRoomConversation.objects.count(), 2)

    def test_meta_is_persisted_on_assistant_turn(self):
        req = _req_factory(self.user)
        conv = persist_turn(
            req, room='repair_atlas', audience='shop',
            user_text='q', assistant_text='a',
            meta={'tier': 'high', 'confidence': 92},
        )
        last = conv.turns[-1]
        self.assertEqual(last['role'], 'assistant')
        self.assertEqual(last['meta']['tier'], 'high')
        self.assertEqual(last['meta']['confidence'], 92)

    def test_close_clears_session_link(self):
        req = _req_factory(self.user)
        first = persist_turn(req, room='repair_atlas', audience='shop',
                              user_text='q', assistant_text='a')
        close_conversation(req, room='repair_atlas', audience='shop')
        second = persist_turn(req, room='repair_atlas', audience='shop',
                               user_text='q2', assistant_text='a2')
        self.assertEqual(AIRoomConversation.objects.count(), 2)
        self.assertNotEqual(first.id, second.id)
        first.refresh_from_db()
        self.assertIsNotNone(first.closed_at)

    def test_audience_separation(self):
        req = _req_factory(self.user)
        c_shop = persist_turn(req, room='repair_atlas', audience='shop',
                               user_text='q', assistant_text='a')
        c_cust = persist_turn(req, room='repair_atlas', audience='customer',
                               user_text='q', assistant_text='a')
        self.assertNotEqual(c_shop.id, c_cust.id)
        self.assertEqual(c_shop.audience, 'shop')
        self.assertEqual(c_cust.audience, 'customer')

    def test_unauthenticated_user_skipped(self):
        rf = RequestFactory()
        req = rf.get('/')
        from django.contrib.auth.models import AnonymousUser
        req.user = AnonymousUser()
        req.session = _Sess()
        out = persist_turn(req, room='repair_atlas', user_text='q',
                            assistant_text='a')
        self.assertIsNone(out)

    def test_invalid_room_returns_none(self):
        req = _req_factory(self.user)
        out = persist_turn(req, room='not_a_room', user_text='q',
                            assistant_text='a')
        self.assertIsNone(out)
