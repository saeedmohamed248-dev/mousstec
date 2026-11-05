"""💬 صفحة المحادثة في لوحة الرد الآلي.

🐛 قبل كده:
  • الـ view كان باعت رسايل المحادثة باسم "messages" — نفس اسم تنبيهات Django — فالقالب الأساسي
    كان بيعرض كل رسالة كمان كصندوق تنبيه أزرق فوق («201001234567 → replied [whatsapp]»)،
    والتنبيهات الحقيقية (زي «اكتب رسالة أولاً») ماكانتش بتظهر.
  • على الموبايل السايدبار (256px) كان ثابت جنب المحتوى والمحادثة متعصرة في عمود ضيق.
"""
import uuid
from unittest import mock

from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from clients.models import Client
from omnichannel import console_views
from omnichannel.models import ChannelMessageLog, TenantChannelConfig

_conversation = console_views.console_conversation.__wrapped__   # من غير حارس الاشتراك/الدخول


class ConsoleConversationPageTests(TestCase):

    def setUp(self):
        patcher = mock.patch.object(Client, 'auto_create_schema', False)
        patcher.start()
        self.addCleanup(patcher.stop)
        schema = f'tst_{uuid.uuid4().hex[:8]}'
        Client.objects.bulk_create([Client(schema_name=schema, name='مركز', owner_name='م', phone='01000000000')])
        self.tenant = Client.objects.get(schema_name=schema)
        self.config = TenantChannelConfig.objects.create(tenant=self.tenant, is_subscription_active=True)
        self.owner = User.objects.create_user(f'own_{schema}', password='x', is_superuser=True, is_staff=True)
        for q, a in (('عندكم تيل فرامل؟', 'أيوه متوفر بـ 1,450 ج.م'), ('تمام احجزلي', 'تم الحجز بكره الساعة 11')):
            ChannelMessageLog.objects.create(tenant=self.tenant, channel='whatsapp', sender_id='201001234567',
                                             contact_name='أحمد علي', inbound_text=q, outbound_text=a, status='replied')

    def render(self, flash=None):
        request = RequestFactory().get('/omnichannel/console/c/whatsapp/201001234567/')
        request.user = self.owner
        request.session = {}
        request._messages = FallbackStorage(request)
        if flash:
            messages.error(request, flash)
        request.omni_tenant = self.tenant
        request.omni_config = self.config
        response = _conversation(request, 'whatsapp', '201001234567')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_chat_messages_are_not_repeated_as_alert_boxes(self):
        html = self.render()
        self.assertIn('عندكم تيل فرامل؟', html)
        self.assertIn('تم الحجز بكره الساعة 11', html)
        self.assertEqual(html.count('أيوه متوفر بـ 1,450 ج.م'), 1)
        self.assertNotIn('201001234567 → replied', html)   # str(ChannelMessageLog) كتنبيه

    def test_real_alerts_show_on_the_conversation_page(self):
        html = self.render(flash='اكتب رسالة أولاً.')
        self.assertIn('اكتب رسالة أولاً.', html)

    def test_layout_stacks_on_mobile(self):
        html = self.render()
        self.assertIn('flex flex-col md:flex-row min-h-screen', html)
        self.assertIn('w-full md:w-64', html)
        self.assertIn('href="/system/dashboard/" class="side-link md:hidden"', html)   # طريق الرجوع ظاهر على الموبايل
