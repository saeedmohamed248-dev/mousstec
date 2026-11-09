"""📢 البث للشركات: البانر لحملة اتبعتت إيميل بس + الحملة الواقفة على «جاري الإرسال»."""
import inspect
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.utils import timezone

from clients.models import BroadcastCampaign
from clients.views import saas_admin_views as v


def _req():
    r = RequestFactory().post('/x/')
    r.user = get_user_model().objects.create_user('bc_admin', password='x', is_superuser=True)
    r.session = {}
    r._messages = FallbackStorage(r)
    return r


class BroadcastFixesTests(TestCase):
    def _campaign(self, **kw):
        c = BroadcastCampaign.objects.create(subject='بوت يرد مكانك', body='عزيزي {tenant_name}',
                                             audience='all', send_email=True, show_in_app=False, **kw)
        return c

    def test_enable_banner_on_email_only_campaign(self):
        c = self._campaign(status='sent')
        inspect.unwrap(v.broadcast_enable_banner)(_req(), c.id)
        c.refresh_from_db()
        self.assertTrue(c.show_in_app)
        self.assertGreater(c.in_app_ends_at, timezone.now() + timedelta(days=13))

    def test_stuck_detection(self):
        c = self._campaign(status='sending')
        self.assertFalse(c.is_stuck)                       # لسه بادئة
        BroadcastCampaign.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        c.refresh_from_db()
        self.assertTrue(c.is_stuck)
        BroadcastCampaign.objects.filter(pk=c.pk).update(sent_count=3)
        c.refresh_from_db()
        self.assertFalse(c.is_stuck)                       # فيه تقدّم ⇒ مش واقفة

    def test_retry_of_stuck_campaign_sends_directly_not_via_celery(self):
        c = self._campaign(status='sending')
        BroadcastCampaign.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(minutes=30))

        class _SyncThread:
            def __init__(self, target, args=(), daemon=None):
                self.t, self.a = target, args

            def start(self):
                self.t(*self.a)

        with mock.patch('threading.Thread', _SyncThread), \
             mock.patch('clients.services.broadcast.send_campaign') as send, \
             mock.patch('clients.tasks.send_broadcast_campaign.delay') as delay, \
             mock.patch('django.db.connection.close'):
            inspect.unwrap(v.broadcast_retry)(_req(), c.id)
        send.assert_called_once()
        delay.assert_not_called()
