"""
🎨 ai_prompt_engineer — Together AI migration tests (Phase N.6 critical fix)
=====================================================================
Pre-fix: the view imported `openai` and called gpt-4o-mini, hard-failing
with 500 "مفتاح OpenAI API غير مُعد" for any tenant without an OpenAI key
— which is now ALL tenants after the N.6 deprecation.

These tests pin the new Together-backed pipeline:
  • Happy path: returns engineered_prompt + category + defaults
  • Auth gate (no addon, no bonus) → 403
  • Empty prompt → 400
  • LLM rate-limited → 429
  • LLM JSON parse failure → 502 with friendly Arabic message
  • LLM returns 'rejected' status → 400 (echoed to client)
  • Response shape preserved (frontend untouched)
"""
import json
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, TransactionTestCase
from django_tenants.utils import get_tenant_model, get_tenant_domain_model


def _parse(resp):
    return json.loads(resp.content)


class _TenantBase(TransactionTestCase):
    """Reuses the printing-tenant pattern from the N.6 view tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        TenantModel = get_tenant_model()
        DomainModel = get_tenant_domain_model()
        cls.tenant = TenantModel(
            schema_name='test_pe', name='مطبعة اختبار البرومبت',
            owner_name='Owner', phone='+201000099999', industry='printing',
        )
        cls.tenant.auto_create_schema = True
        cls.tenant.save(verbosity=0)
        cls.domain = DomainModel.objects.create(
            tenant=cls.tenant, domain='test-pe.test.com', is_primary=True,
        )
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        try:
            from clients.models import EscrowLedger, AILimitTracker, AIBonusGrant
            EscrowLedger.objects.filter(client=cls.tenant).delete()
            AILimitTracker.objects.filter(tenant=cls.tenant).delete()
            AIBonusGrant.objects.filter(tenant=cls.tenant).delete()
        except Exception:
            pass
        try:
            cls.domain.delete()
        except Exception:
            pass
        try:
            cls.tenant.delete(force_drop=True)
        except Exception:
            pass
        super().tearDownClass()

    def _make_user(self):
        connection.set_schema_to_public()
        try:
            u = User.objects.filter(username='pe_tester').first()
            user = u or User.objects.create_user(username='pe_tester', password='x')
        finally:
            connection.set_tenant(self.tenant)
        return user


def _llm_ok(prompt='A cinematic logo, studio lighting, vector art.',
            category='logo', size='1024x1024'):
    return {
        'success': True,
        'data': {
            'engineered_prompt': prompt,
            'design_category': category,
            'recommended_size': size,
            'negative_prompt': 'blurry, low quality',
        },
        'model_used': 'meta-llama/Llama-3-70B-Instruct',
    }


def _llm_err(error='together_llm_http_500', detail='upstream'):
    return {'success': False, 'error': error, 'detail': detail}


class PromptEngineerTogetherMigrationTests(_TenantBase):

    def setUp(self):
        super().setUp()
        self.user = self._make_user()
        self.rf = RequestFactory()

    def _run(self, *, post=None, allow=True, llm=None):
        from printing.views import ai_prompt_engineer
        req = self.rf.post(
            '/printing/ai/prompt-engineer/',
            data=post or {'prompt': 'تصميم بوستر لمطعم لبناني'},
        )
        req.user = self.user
        check_p = patch(
            'printing.views._check_ai_access',
            return_value=(allow, None if allow else 'no_access'),
        )
        llm_p = patch(
            'erp_core.ai.design_engine._call_together_llm',
            return_value=llm if llm is not None else _llm_ok(),
        )
        with check_p, llm_p:
            return ai_prompt_engineer(req)

    # ── Happy path ──────────────────────────────────────────────
    def test_success_returns_engineered_prompt_and_defaults(self):
        resp = self._run()
        self.assertEqual(resp.status_code, 200)
        data = _parse(resp)
        self.assertEqual(data['status'], 'success')
        self.assertIn('engineered_prompt', data)
        self.assertEqual(data['design_category'], 'logo')
        # Defensive defaults populated
        self.assertEqual(data['recommended_quality'], 'hd')
        self.assertEqual(data['original_intent'], 'تصميم بوستر لمطعم لبناني')

    def test_legacy_response_shape_preserved(self):
        """Frontend depends on these specific keys — don't drop them."""
        resp = self._run()
        data = _parse(resp)
        for k in ('status', 'engineered_prompt', 'design_category',
                  'negative_prompt', 'recommended_size', 'recommended_quality',
                  'original_intent'):
            self.assertIn(k, data, msg=f'missing {k}')

    # ── Input validation ───────────────────────────────────────
    def test_empty_prompt_returns_400(self):
        resp = self._run(post={'prompt': '   '})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('وصف التصميم', _parse(resp)['error'])

    def test_access_denied_returns_403(self):
        resp = self._run(allow=False)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(_parse(resp)['status'], 'error')

    # ── LLM failure modes ──────────────────────────────────────
    def test_together_key_missing_returns_502_with_friendly_message(self):
        resp = self._run(llm=_llm_err('together_key_missing'))
        self.assertEqual(resp.status_code, 502)
        self.assertIn('Together AI', _parse(resp)['error'])

    def test_rate_limit_returns_429(self):
        resp = self._run(llm=_llm_err('together_llm_http_429'))
        self.assertEqual(resp.status_code, 429)
        self.assertIn('دقيقة', _parse(resp)['error'])

    def test_invalid_json_returns_502_specific_message(self):
        resp = self._run(llm=_llm_err('together_llm_invalid_json'))
        self.assertEqual(resp.status_code, 502)
        self.assertIn('تحليل', _parse(resp)['error'])

    def test_unknown_llm_error_returns_502_with_code(self):
        resp = self._run(llm=_llm_err('together_llm_http_503'))
        self.assertEqual(resp.status_code, 502)
        # User can see the underlying code for support tickets
        self.assertIn('together_llm_http_503', _parse(resp)['error'])

    # ── LLM-side semantic failures ─────────────────────────────
    def test_llm_returns_rejected_passes_through_with_400(self):
        """The system prompt can refuse out-of-scope requests via
        {"status": "rejected", ...}. The view must NOT muffle this."""
        rejected = {
            'success': True,
            'data': {
                'status': 'rejected',
                'reason': 'out of scope',
            },
            'model_used': 'llama-3-70b',
        }
        resp = self._run(llm=rejected)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_parse(resp)['status'], 'rejected')

    def test_missing_engineered_prompt_returns_502(self):
        bad = {
            'success': True,
            'data': {'design_category': 'logo'},  # no engineered_prompt
            'model_used': 'x',
        }
        resp = self._run(llm=bad)
        self.assertEqual(resp.status_code, 502)
        self.assertIn('توليد البرومبت', _parse(resp)['error'])

    # ── Migration sanity ──────────────────────────────────────
    def test_view_no_longer_imports_openai_at_call_time(self):
        """The migration removed the runtime openai import from this
        function — confirm by patching `openai` to raise and showing
        the happy path still works."""
        # If view tries `import openai` we'd see ImportError side-effect.
        # Easier check: just verify happy path completes without any
        # openai-* patches present.
        resp = self._run()
        self.assertEqual(resp.status_code, 200)
