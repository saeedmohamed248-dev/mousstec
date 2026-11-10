"""
🎥 تسجيل شاشة جلسات الشركات (rrweb):
* السكريبت بيتحقن في صفحات الشركة للموظف الداخل بس — مش للزائر، ولا لو الشركة مقفول لها،
  ولا لصاحب المنصة وهو داخل على الشركة (God Mode).
* الاستقبال: نفس الدومين بس، والدفعة بتتخزّن مضغوطة، والتكرار مابيتسجّلش مرتين،
  وجلسة موظف مايقدرش موظف تاني يكتب فيها.
* صاحب المنصة يقرا الأحداث (للايف بـ after=seq) ويقفل/يشغّل التسجيل؛ القديم بيتمسح.
"""
import json
import uuid
import zlib
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from clients.models import Client, Domain, ScreenChunk, ScreenSession
from clients.views.screen_recording_views import purge_old_sessions
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee

PLATFORM_HOST = 'platform-sr.test.com'
INGEST = '/screen-rec/ingest/'


@override_settings(ALLOWED_HOSTS=['*'])
class ScreenRecordingTests(ERPTenantTestCase):
    def setUp(self):
        Client.objects.filter(pk=self.tenant.pk).update(screen_recording_enabled=True)
        self.tenant.refresh_from_db()
        self.branch = make_branch()
        self.user, _ = make_employee('rec_cashier', role='admin', branch=self.branch)
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.first_name = 'منى'
        self.user.save()
        self.host = self.domain.domain
        self.c = HttpClient(HTTP_HOST=self.host)
        self.c.force_login(self.user)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _ingest(self, client=None, sid=None, seq=0, events=None, origin=True):
        extra = {'HTTP_ORIGIN': f'http://{self.host}'} if origin else {}
        body = {'sid': str(sid or self.sid), 'seq': seq, 'path': '/secure-portal/',
                'events': events or [{'type': 4, 'timestamp': 1, 'data': {'href': 'x'}}]}
        return (client or self.c).post(INGEST, json.dumps(body), content_type='application/json', **extra)

    # ── الحقن ─────────────────────────────────────────────
    def test_script_injected_for_logged_in_employee_only(self):
        html = self.c.get('/secure-portal/').content.decode()
        self.assertIn('rrweb-record-1.1.3.min.js', html)
        self.assertIn('js/screen-rec.js', html)
        anon = HttpClient(HTTP_HOST=self.host).get('/secure-portal/login/').content.decode()
        self.assertNotIn('rrweb-record', anon)

    def test_not_injected_when_disabled_or_platform_owner_entered(self):
        Client.objects.filter(pk=self.tenant.pk).update(screen_recording_enabled=False)
        self.assertNotIn('rrweb-record', self.c.get('/secure-portal/').content.decode())
        Client.objects.filter(pk=self.tenant.pk).update(screen_recording_enabled=True)
        s = self.c.session
        s['platform_owner_entered'] = True
        s.save()
        self.assertNotIn('rrweb-record', self.c.get('/secure-portal/').content.decode())

    # ── الاستقبال ─────────────────────────────────────────
    def test_ingest_stores_compressed_chunk_and_ignores_duplicates(self):
        self.sid = uuid.uuid4()
        ev = [{'type': 2, 'timestamp': 5, 'data': {'node': {}}}]
        r = self._ingest(seq=0, events=ev)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self._ingest(seq=0, events=ev).json().get('duplicate'), True)
        with schema_context('public'):
            sess = ScreenSession.objects.get(session_key=self.sid)
            self.assertEqual(sess.tenant_schema, self.tenant.schema_name)
            self.assertEqual(sess.username, 'rec_cashier')
            self.assertEqual(sess.user_display, 'منى')
            self.assertEqual(sess.chunks_count, 1)
            chunk = ScreenChunk.objects.get(session=sess)
            self.assertEqual(json.loads(zlib.decompress(bytes(chunk.data))), ev)

    def test_ingest_rejects_cross_origin_disabled_anonymous_and_other_user(self):
        self.sid = uuid.uuid4()
        self.assertEqual(self._ingest(origin=False).status_code, 403)
        anon = HttpClient(HTTP_HOST=self.host)
        self.assertEqual(self._ingest(client=anon).status_code, 403)
        self.assertEqual(self._ingest().status_code, 200)
        other, _ = make_employee('rec_other', role='admin', branch=self.branch)
        oc = HttpClient(HTTP_HOST=self.host); oc.force_login(other)
        self.assertEqual(self._ingest(client=oc, seq=1).status_code, 403)
        Client.objects.filter(pk=self.tenant.pk).update(screen_recording_enabled=False)
        self.assertEqual(self._ingest(seq=2).status_code, 403)
        self.assertEqual(self._ingest(events=[]).status_code, 403)

    def test_ingest_bad_payload_is_400(self):
        r = self.c.post(INGEST, 'not json', content_type='application/json',
                        HTTP_ORIGIN=f'http://{self.host}')
        self.assertEqual(r.status_code, 400)

    # ── المشاهدة (صاحب المنصة) ─────────────────────────────
    def _owner_client(self):
        connection.set_schema_to_public()
        pub = Client.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save()
        Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
        owner, _ = User.objects.get_or_create(
            username='sr_owner@test.com', defaults={'is_staff': True, 'is_superuser': True})
        c = HttpClient(HTTP_HOST=PLATFORM_HOST)
        c.force_login(owner)
        return c

    def test_owner_reads_events_incrementally_and_toggles(self):
        self.sid = uuid.uuid4()
        self._ingest(seq=0, events=[{'type': 4, 'timestamp': 1, 'data': {}}])
        self._ingest(seq=1, events=[{'type': 3, 'timestamp': 2, 'data': {}}])
        c = self._owner_client()
        sess = ScreenSession.objects.get(session_key=self.sid)
        d = c.get(f'/superadmin/screen/{sess.pk}/events/').json()
        self.assertEqual([e['timestamp'] for e in d['events']], [1, 2])
        self.assertEqual(d['last_seq'], 1)
        self.assertTrue(d['live'])
        self.assertEqual(c.get(f'/superadmin/screen/{sess.pk}/events/?after=1').json()['events'], [])
        page = c.get(f'/superadmin/screen/{sess.pk}/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('rrweb-1.1.3.min.js', page.content.decode())
        t360 = c.get(f'/superadmin/tenant/{self.tenant.pk}/360/').content.decode()
        self.assertIn(f'/superadmin/screen/{sess.pk}/', t360)
        c.post(f'/superadmin/tenant/{self.tenant.pk}/screen-rec/toggle/')
        self.assertFalse(Client.objects.get(pk=self.tenant.pk).screen_recording_enabled)

    def test_tenant_admin_cannot_watch(self):
        self.sid = uuid.uuid4()
        self._ingest()
        with schema_context('public'):
            pk = ScreenSession.objects.get(session_key=self.sid).pk
        r = self.c.get(f'/superadmin/screen/{pk}/events/')
        self.assertIn(r.status_code, (302, 403, 404))

    def test_purge_removes_old_sessions(self):
        self.sid = uuid.uuid4()
        self._ingest()
        with schema_context('public'):
            ScreenSession.objects.filter(session_key=self.sid).update(
                last_event_at=timezone.now() - timedelta(days=30))
        self.assertGreaterEqual(purge_old_sessions(), 1)
        with schema_context('public'):
            self.assertFalse(ScreenSession.objects.filter(session_key=self.sid).exists())


class FontCorsHeaderTests(ERPTenantTestCase):
    def test_fonts_get_cors_header_for_replay(self):
        from django.conf import settings
        h = {}
        settings.WHITENOISE_ADD_HEADERS_FUNCTION(h, '/x/fa-solid-900.woff2', '/static/fa-solid-900.woff2')
        self.assertEqual(h.get('Access-Control-Allow-Origin'), '*')
        h = {}
        settings.WHITENOISE_ADD_HEADERS_FUNCTION(h, '/x/app.js', '/static/app.js')
        self.assertNotIn('Access-Control-Allow-Origin', h)
