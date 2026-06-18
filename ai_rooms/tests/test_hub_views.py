"""
🧪 Integration tests for the ai_rooms hub + history + detail views.

ai_rooms lives in SHARED_APPS — its tables (and `auth_user`) sit in the
public schema. Plain TestCase keeps the connection on public, and the
test client captures the rendered context on each response.

The DEBUG=True override is what makes django-tenants stop rejecting
'testserver' as a host (default TestClient HTTP_HOST) without us
having to mint a tenant just to route URLs.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from ai_rooms.models import AIRoomConversation, RoomKind, Audience
from clients.models import Client as Tenant, Domain


User = get_user_model()
_PUBLIC_HOST = 'public.test'


def _ensure_public_tenant():
    """A fresh test DB has no Domain rows, so django-tenants can't route
    any request — every URL bounces. Seed a public-schema tenant + one
    Domain row so the test client's URL hits the regular URLconf."""
    tenant, _ = Tenant.objects.get_or_create(
        schema_name='public',
        defaults={
            'name': 'public', 'owner_name': 'tester',
            'phone': '01000000000', 'max_branches': 0,
            'max_users': 0, 'max_treasuries': 0,
        },
    )
    Domain.objects.get_or_create(
        domain=_PUBLIC_HOST,
        defaults={'tenant': tenant, 'is_primary': True},
    )


@override_settings(
    ALLOWED_HOSTS=['*'], ROOT_URLCONF='erp_core.urls',
    SECURE_SSL_REDIRECT=False,
)
class AIRoomsHubViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_public_tenant()
        cls.user = User.objects.create_user(
            username='hubber', password='pw', email='h@x.com',
        )
        cls.other = User.objects.create_user(
            username='intruder', password='pw', email='i@x.com',
        )
        # 2 repair_atlas + 1 auto_diagnostic for the active user
        AIRoomConversation.objects.create(
            user=cls.user, room=RoomKind.REPAIR_ATLAS,
            audience=Audience.SHOP, title='ضفيرة',
        )
        AIRoomConversation.objects.create(
            user=cls.user, room=RoomKind.REPAIR_ATLAS,
            audience=Audience.SHOP, title='تفكيك',
        )
        AIRoomConversation.objects.create(
            user=cls.user, room=RoomKind.AUTO_DIAGNOSTIC,
            audience=Audience.SHOP, title='تشخيص',
        )
        AIRoomConversation.objects.create(
            user=cls.user, room=RoomKind.REPAIR_ATLAS,
            audience=Audience.CUSTOMER, title='customer-side',
        )
        # Cross-user noise — must never leak into the active user's hub
        AIRoomConversation.objects.create(
            user=cls.other, room=RoomKind.REPAIR_ATLAS,
            audience=Audience.SHOP, title='other-tech',
        )

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = _PUBLIC_HOST
        self.client.force_login(self.user)

    def test_hub_renders(self):
        resp = self.client.get('/ai-rooms/')
        self.assertEqual(resp.status_code, 200)

    def test_hub_counts_per_room_for_shop_audience(self):
        resp = self.client.get('/ai-rooms/')
        counts = {r['kind']: r['count'] for r in resp.context['rooms']}
        self.assertEqual(counts[RoomKind.REPAIR_ATLAS], 2)
        self.assertEqual(counts[RoomKind.AUTO_DIAGNOSTIC], 1)
        self.assertEqual(counts[RoomKind.DIAGNOSTIC_ROOM], 0)

    def test_hub_recent_excludes_other_users(self):
        resp = self.client.get('/ai-rooms/')
        titles = [c.title for c in resp.context['recent']]
        self.assertIn('ضفيرة', titles)
        self.assertNotIn('other-tech', titles)

    def test_hub_audience_session_flag_scopes_to_customer(self):
        """`is_customer_audience=True` flips the lens: only customer
        conversations appear, shop is hidden."""
        sess = self.client.session
        sess['is_customer_audience'] = True
        sess.save()

        resp = self.client.get('/ai-rooms/')
        titles = [c.title for c in resp.context['recent']]
        self.assertIn('customer-side', titles)
        self.assertNotIn('ضفيرة', titles)
        self.assertEqual(resp.context['audience'], Audience.CUSTOMER)

    def test_hub_requires_login(self):
        self.client.logout()
        resp = self.client.get('/ai-rooms/')
        self.assertIn(resp.status_code, (302, 403))


@override_settings(
    ALLOWED_HOSTS=['*'], ROOT_URLCONF='erp_core.urls',
    SECURE_SSL_REDIRECT=False,
)
class AIRoomsHistoryViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_public_tenant()
        cls.user = User.objects.create_user(
            username='hist', password='pw', email='h2@x.com',
        )
        AIRoomConversation.objects.create(
            user=cls.user, room=RoomKind.REPAIR_ATLAS,
            audience=Audience.SHOP, title='repair-one',
        )
        AIRoomConversation.objects.create(
            user=cls.user, room=RoomKind.AUTO_DIAGNOSTIC,
            audience=Audience.SHOP, title='diag-one',
        )

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = _PUBLIC_HOST
        self.client.force_login(self.user)

    def test_history_returns_all_rooms_without_filter(self):
        resp = self.client.get('/ai-rooms/history/')
        self.assertEqual(resp.status_code, 200)
        titles = [c.title for c in resp.context['conversations']]
        self.assertIn('repair-one', titles)
        self.assertIn('diag-one', titles)

    def test_history_filters_to_one_room(self):
        resp = self.client.get('/ai-rooms/history/?room=' + RoomKind.REPAIR_ATLAS)
        titles = [c.title for c in resp.context['conversations']]
        self.assertIn('repair-one', titles)
        self.assertNotIn('diag-one', titles)
        self.assertEqual(resp.context['room_filter'], RoomKind.REPAIR_ATLAS)

    def test_history_ignores_unknown_room_filter(self):
        """An invalid ?room=… is treated as "no filter" — survival mode
        so an old bookmark with a renamed room slug doesn't 500."""
        resp = self.client.get('/ai-rooms/history/?room=garbage')
        self.assertEqual(resp.status_code, 200)
        titles = [c.title for c in resp.context['conversations']]
        self.assertIn('repair-one', titles)
        self.assertIn('diag-one', titles)


@override_settings(
    ALLOWED_HOSTS=['*'], ROOT_URLCONF='erp_core.urls',
    SECURE_SSL_REDIRECT=False,
)
class AIRoomsConversationDetailViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _ensure_public_tenant()
        cls.user = User.objects.create_user(
            username='reader', password='pw', email='r@x.com',
        )
        cls.intruder = User.objects.create_user(
            username='intruder2', password='pw', email='i2@x.com',
        )
        cls.conv = AIRoomConversation.objects.create(
            user=cls.user, room=RoomKind.REPAIR_ATLAS,
            audience=Audience.SHOP, title='detail-test',
        )
        cls.conv.append_turn('user', 'إزاي أفك الدينمو؟')
        cls.conv.append_turn(
            'assistant', 'افصل البطارية الأول...',
            meta={'tier': 'high', 'confidence': 92},
        )

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = _PUBLIC_HOST

    def test_detail_renders_for_owner(self):
        self.client.force_login(self.user)
        resp = self.client.get(f'/ai-rooms/history/{self.conv.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['conv'].id, self.conv.id)
        self.assertIn('emoji', resp.context['meta'])

    def test_detail_blocks_other_user(self):
        """user B requesting user A's conversation by id must 404, not
        a redirect — i.e. it must not leak the existence of the row.
        Calling the view directly bypasses VisitorTracking + axes
        middleware noise that 302s a logged-in user on first request."""
        from django.http import Http404
        from django.test import RequestFactory
        from ai_rooms.views import conversation_detail
        rf = RequestFactory()
        req = rf.get(f'/ai-rooms/history/{self.conv.id}/')
        req.user = self.intruder
        with self.assertRaises(Http404):
            conversation_detail(req, conv_id=self.conv.id)

    def test_detail_404_on_unknown_id(self):
        from django.http import Http404
        from django.test import RequestFactory
        from ai_rooms.views import conversation_detail
        rf = RequestFactory()
        req = rf.get('/ai-rooms/history/99999/')
        req.user = self.user
        with self.assertRaises(Http404):
            conversation_detail(req, conv_id=99999)
