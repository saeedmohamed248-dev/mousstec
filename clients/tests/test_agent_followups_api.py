"""
🤖 API وكيل المتابعة + دقة بيانات التجارب والـ churn.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.db import connection
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from clients.services import tenant_health, trial_watch
from clients.views import agent_api
from inventory.tests.base import ERPTenantTestCase


class TrustScoreTests(SimpleTestCase):
    def test_zero_trust_stays_zero(self):
        # 🐛 كان `or 100` بيحوّل 0 لـ 100
        t = SimpleNamespace(is_fraud_flagged=False, ai_trust_score=0, dispute_rate=0)
        self.assertEqual(tenant_health._trust_score(t)[0], 0)

    def test_missing_trust_defaults_to_100(self):
        t = SimpleNamespace(is_fraud_flagged=False, ai_trust_score=None, dispute_rate=0)
        self.assertEqual(tenant_health._trust_score(t)[0], 100)


@override_settings(ALLOWED_HOSTS=['*'])
class TrialRowsDefaultQueryTests(ERPTenantTestCase):
    """الـ cron بيعمل suspend للتجربة المنتهية — لازم تفضل ظاهرة كـ expired."""

    def setUp(self):
        super().setUp()
        connection.set_schema_to_public()

    def tearDown(self):
        connection.set_schema_to_public()
        super().tearDown()

    def _set(self, **fields):
        type(self.tenant).all_objects.filter(pk=self.tenant.pk).update(**fields)

    def _ids(self):
        return [r['tenant'].id for r in trial_watch.trial_rows()]

    def test_auto_suspended_expired_trial_is_listed_as_expired(self):
        self._set(status='suspended', is_active=True, is_deleted=False, is_fraud_flagged=False,
                  subscription_end_date=None, trial_ends_at=timezone.localdate() - timedelta(days=2))
        rows = [r for r in trial_watch.trial_rows() if r['tenant'].id == self.tenant.id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['verdict'], 'expired')

    def test_manually_suspended_or_ex_subscriber_is_not_a_trial(self):
        old = timezone.localdate() - timedelta(days=2)
        self._set(status='suspended', is_active=False, subscription_end_date=None, trial_ends_at=old)
        self.assertNotIn(self.tenant.id, self._ids())
        self._set(status='suspended', is_active=True, subscription_end_date=old, trial_ends_at=old)
        self.assertNotIn(self.tenant.id, self._ids())

    def test_old_expired_trial_is_dropped(self):
        self._set(status='suspended', is_active=True, subscription_end_date=None,
                  trial_ends_at=timezone.localdate() - timedelta(days=trial_watch.RECENTLY_EXPIRED_DAYS + 1))
        self.assertNotIn(self.tenant.id, self._ids())


class FollowupsApiTests(SimpleTestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def _call(self, token=None):
        req = self.rf.get('/api/agent/followups/',
                          **({'HTTP_AUTHORIZATION': f'Bearer {token}'} if token else {}))
        with mock.patch.object(agent_api, 'connection', SimpleNamespace(schema_name='public')):
            return agent_api.followups(req)

    @override_settings(AGENT_API_TOKEN='')
    def test_closed_when_token_not_configured(self):
        with mock.patch.dict('os.environ', {'AGENT_API_TOKEN': ''}):
            with self.assertRaises(Http404):
                self._call('anything')

    @override_settings(AGENT_API_TOKEN='s3cret')
    def test_wrong_or_missing_token_is_401(self):
        self.assertEqual(self._call('nope').status_code, 401)
        self.assertEqual(self._call().status_code, 401)

    @override_settings(AGENT_API_TOKEN='s3cret')
    def test_returns_followups_matching_admin_pages(self):
        today = timezone.localdate()
        t_cold = SimpleNamespace(id=1, name='ورشة أ', schema_name='a', industry='automotive',
                                 status='trial', phone='01012345678')
        t_ok = SimpleNamespace(id=2, name='ورشة ب', schema_name='b', industry='automotive',
                               status='trial', phone='')
        rows = [
            {'tenant': t_cold, 'verdict': 'cold', 'verdict_label': 'x', 'days_left': 5,
             'last_login': None, 'last_doc': None, 'docs_7d': 0, 'docs_total': 0, 'error': ''},
            {'tenant': t_ok, 'verdict': 'active', 'verdict_label': 'y', 'days_left': 5,
             'last_login': None, 'last_doc': None, 'docs_7d': 3, 'docs_total': 9, 'error': ''},
        ]
        payer_bad = SimpleNamespace(id=3, name='مطبعة', schema_name='c', industry='printing',
                                    status='active', phone='', subscription_end_date=today)
        payer_good = SimpleNamespace(id=4, name='مطبعة ٢', schema_name='d', industry='printing',
                                     status='active', phone='', subscription_end_date=None)
        healths = {3: {'score': 22, 'grade': 'D', 'risk': 'critical', 'reasons': ['r']},
                   4: {'score': 90, 'grade': 'A', 'risk': 'low', 'reasons': []}}
        qs = mock.MagicMock()
        qs.exclude.return_value.filter.return_value = [payer_bad, payer_good]
        with mock.patch('clients.services.trial_watch.trial_rows', return_value=rows), \
             mock.patch('clients.services.tenant_health.bulk_tenant_health', return_value=healths), \
             mock.patch('clients.models.Client.objects', qs):
            resp = self._call('s3cret')
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data['counts'], {'trials': 1, 'at_risk': 1})
        self.assertEqual(data['trials'][0]['wa_phone'], '201012345678')
        self.assertEqual(data['trials'][0]['verdict'], 'cold')
        self.assertEqual(data['at_risk'][0]['id'], 3)
        self.assertNotIn('email', json.dumps(data))
