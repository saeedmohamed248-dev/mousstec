"""🧠 مستشار العميل (/marketplace/advisor/chat/) لما مزوّد الذكاء الاصطناعي واقع.

🐛 كان بيرجّع 503 — العميل بيشوف «⚠️ تعذر الحصول على رد» ولوحة الأخطاء بتعلّمه «Code failure».
"""
from __future__ import annotations

import json
from unittest import mock

from django.test import TestCase, override_settings

from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin, _customer, _login

URL = '/marketplace/advisor/chat/'


class CustomerAdvisorTests(_PublicDomainMixin, TestCase):
    def setUp(self):
        self.customer = _customer('Advisor')
        self.c = _login(self.customer)

    def ask(self, text='عربيتي بتسخن'):
        return self.c.post(URL, data=json.dumps({'message': text}), content_type='application/json')

    def test_reply_goes_into_history(self):
        with mock.patch('inventory.ai_services.call_llm_layer', return_value='غيّر الترموستات'):
            r = self.ask()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['reply'], 'غيّر الترموستات')
        self.assertNotIn('degraded', r.json())

    def test_provider_down_gives_a_helpful_200_not_503(self):
        with mock.patch('inventory.ai_services.call_llm_layer', return_value=None):
            r = self.ask()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['degraded'], 'ai_empty')
        self.assertIn('/marketplace/catalog/automotive/', body['reply'])
        # الرد الاحتياطي مايدخلش الهيستوري (السؤال الجاي يتبعت للـ AI نضيف)
        with mock.patch('inventory.ai_services.call_llm_layer', return_value='تمام') as m:
            self.ask('وبعدين؟')
        sent = m.call_args[0][0]
        self.assertNotIn('مش متاح', json.dumps(sent, ensure_ascii=False))

    def test_pipeline_exception_is_logged_and_still_answers(self):
        with mock.patch('inventory.ai_services.call_llm_layer', side_effect=RuntimeError('boom')), \
                self.assertLogs('mouss_tec_core', level='ERROR'):
            r = self.ask()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['degraded'], 'ai_unavailable')

    def test_printing_customer_gets_printing_links(self):
        type(self.customer).objects.filter(pk=self.customer.pk).update(sector='printing')
        with mock.patch('inventory.ai_services.call_llm_layer', return_value=''):
            r = self.ask('عايز كروت')
        self.assertIn('/marketplace/catalog/printing/', r.json()['reply'])

    def test_anonymous_still_401(self):
        from django.test import Client
        r = Client().post(URL, data=json.dumps({'message': 'x'}), content_type='application/json')
        self.assertEqual(r.status_code, 401)


@override_settings(ENABLE_AI_PREDICTIONS=True, TOGETHER_API_KEY='t', AI_VISION_API_KEY='g', GEMINI_API_KEY='')
class TogetherFallsBackToGeminiTests(TestCase):
    def test_together_error_tries_gemini(self):
        from inventory import ai_services
        bad = mock.Mock(status_code=402, text='credits exhausted')
        with mock.patch.object(ai_services.requests, 'post', return_value=bad), \
                mock.patch.object(ai_services, '_call_gemini_vision', return_value='رد من Gemini') as gem:
            out = ai_services._call_together_text([{'role': 'user', 'content': 'x'}], json_mode=False, max_retries=1)
        self.assertEqual(out, 'رد من Gemini')
        gem.assert_called_once()

    @override_settings(AI_VISION_API_KEY='')
    def test_no_gemini_key_returns_none(self):
        from inventory import ai_services
        bad = mock.Mock(status_code=500, text='down')
        with mock.patch.object(ai_services.requests, 'post', return_value=bad), \
                mock.patch.object(ai_services, '_call_gemini_vision') as gem:
            self.assertIsNone(ai_services._call_together_text([{'role': 'user', 'content': 'x'}],
                                                              json_mode=False, max_retries=1))
        gem.assert_not_called()
