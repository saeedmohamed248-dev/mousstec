"""
🧠 صفحة «المستشار الذكي» زي ما العميل بيشوفها:
* مفيش أسماء متغيرات سيرفر (ENABLE_AI_PREDICTIONS / AI_VISION_API_KEY) قدام العميل.
* من غير مبيعات: الرسوم مكانها رسالة («أول فاتورة بيع هتظهر هنا») بدل مربع أبيض فاضي.
* أول فاتورة بيع معتمدة بتظهر في بيانات الرسمين (اتجاه 12 شهر + أعلى المنتجات)؛ عرض السعر لأ.
* 💸 الشات والـ AI مطفي: السؤال مايتخصمش من حصة/رصيد الشركة، والرد بييجي من بياناتها.
"""
import json

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_inventory, make_product, make_sale_invoice


def _script_json(html, el_id):
    import re
    m = re.search(rf'<script id="{el_id}" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


@override_settings(ALLOWED_HOSTS=['*'], ENABLE_AI_PREDICTIONS=False)
class BusinessAdvisorPageTests(ERPTenantTestCase):

    def test_customer_view_empty_then_first_sale_and_free_offline_chat(self):
        connection.set_tenant(self.tenant)
        host = self.domain.domain
        branch = make_branch()
        product = make_product(part_number='ADV-1', name='طرمبة بنزين', retail_price='1500.00',
                               purchase_price='900.00', average_cost='900.00')
        make_inventory(product, branch, quantity=5)
        owner = User.objects.create_user('adv_boss', password='x', is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(owner)

        # 1) شركة جديدة من غير مبيعات
        r = c.get('/system/reports/advisor/', HTTP_HOST=host)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn('ENABLE_AI_PREDICTIONS', html)
        self.assertNotIn('AI_VISION_API_KEY', html)
        self.assertIn('id="trend-empty"', html)
        self.assertIn('id="top-empty"', html)
        self.assertNotIn('id="trendChart"', html)

        # 2) عرض سعر بس ⇒ لسه مفيش مبيعات
        cust = make_customer()
        make_sale_invoice(cust, branch, items=[(product, 1, '1500')], status='quotation')
        html = c.get('/system/reports/advisor/', HTTP_HOST=host).content.decode()
        self.assertIn('id="trend-empty"', html)

        # 3) أول فاتورة بيع معتمدة ⇒ بتظهر في الرسمين
        make_sale_invoice(cust, branch, items=[(product, 1, '1500')], status='posted')
        html = c.get('/system/reports/advisor/', HTTP_HOST=host).content.decode()
        self.assertIn('id="trendChart"', html)
        self.assertIn('id="topChart"', html)
        trend = _script_json(html, 'trend-data')
        self.assertEqual(sum(m['sales'] for m in trend), 1500.0)
        top = _script_json(html, 'top-data')
        self.assertEqual(top[0]['name'], 'طرمبة بنزين')
        self.assertEqual(top[0]['revenue'], 1500.0)

        # 4) 💸 الشات والـ AI مطفي: رد من البيانات، ومفيش خصم من حصة الأسئلة
        from clients.models import AILimitTracker
        from django_tenants.utils import schema_context
        with schema_context('public'):
            before = AILimitTracker.objects.filter(tenant_id=self.tenant.pk, action_type='copilot_question').count()
        r = c.post('/system/reports/advisor/ask/', data=json.dumps({'question': 'المبيعات كام؟'}),
                   content_type='application/json', HTTP_HOST=host)
        body = r.json()
        self.assertTrue(body.get('answer'), body)
        self.assertNotIn('ENABLE_AI_PREDICTIONS', json.dumps(body, ensure_ascii=False))
        with schema_context('public'):
            after = AILimitTracker.objects.filter(tenant_id=self.tenant.pk, action_type='copilot_question').count()
        self.assertEqual(after, before)
