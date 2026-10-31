"""🆘 زرار «مشكلة؟ كلّمنا» جوه السيستم — بلاغ بالكلام + صور/فيديو/تسجيل صوتي يوصل لوحة المالك.

قبل كده: فورم الدعم كان في صفحات الموقع العامة بس، نص من غير أي مرفقات، والعميل لازم يكتب
اسمه وإيميله تاني. مفيش طريقة يصوّر المشكلة أو يقولها بصوته، ولوحة المالك ماكانتش بتنبّه.
"""
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import SupportAttachment, SupportTicket
from inventory.tests.base import ERPTenantTestCase
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

User = get_user_model()
_MEDIA = tempfile.mkdtemp(prefix='mt_support_')


def _png():
    return SimpleUploadedFile('err.png', b'\x89PNG\r\n\x1a\n' + b'0' * 64, content_type='image/png')


def _voice():
    return SimpleUploadedFile('voice-note.webm', b'\x1aE\xdf\xa3' + b'0' * 64, content_type='audio/webm;codecs=opus')


@override_settings(ALLOWED_HOSTS=['*'], MEDIA_ROOT=_MEDIA, SUPPORT_INBOX_EMAIL='owner@plat.test',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class InAppSupportReportTests(ERPTenantTestCase):

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('cashier1', password='x', email='cashier@shop.test',
                                             first_name='أحمد')
        self.c = HttpClient()
        self.c.force_login(self.user)

    def tearDown(self):
        connection.set_schema_to_public()
        SupportTicket.objects.filter(tenant=self.tenant).delete()
        connection.set_tenant(self.tenant)
        super().tearDown()

    def post(self, data):
        return self.c.post('/support/report/', data, HTTP_HOST=self.domain.domain)

    def test_button_shows_only_for_logged_in_company_users(self):
        r = self.c.get('/support/report/', HTTP_HOST=self.domain.domain)
        self.assertTrue(r.json()['enabled'])
        self.assertTrue(r.json()['csrf'])
        r = HttpClient().get('/support/report/', HTTP_HOST=self.domain.domain)
        self.assertFalse(r.json()['enabled'])
        r = HttpClient().post('/support/report/', {'message': 'x'}, HTTP_HOST=self.domain.domain)
        self.assertEqual(r.status_code, 403)

    def test_report_with_photo_and_voice_reaches_the_owner_inbox(self):
        r = self.post({'message': 'الفاتورة مش بتطبع\nبيطلع خطأ', 'urgent': '1',
                       'page_url': 'https://shop.mousstec.com/system/sales/', 'files': [_png()], 'voice': [_voice()]})
        self.assertEqual(r.status_code, 200, r.content)
        ticket = SupportTicket.objects.get(pk=r.json()['ticket_id'])
        self.assertEqual((ticket.tenant_id, ticket.source, ticket.priority), (self.tenant.pk, 'in_app', 'high'))
        self.assertEqual((ticket.reporter, ticket.name, ticket.email), ('cashier1', 'أحمد', 'cashier@shop.test'))
        self.assertEqual(ticket.subject, '🆘 الفاتورة مش بتطبع')
        self.assertEqual(ticket.page_url, 'https://shop.mousstec.com/system/sales/')
        kinds = sorted(SupportAttachment.objects.filter(ticket=ticket).values_list('kind', 'content_type'))
        self.assertEqual(kinds, [('audio', 'audio/webm'), ('image', 'image/png')])
        self.assertEqual(len(mail.outbox), 1)                          # إشعار فوري لصاحب المنصة
        self.assertIn(f'/superadmin/support/{ticket.id}/', mail.outbox[0].body)

    def test_voice_only_report_is_fine(self):
        r = self.post({'message': '', 'voice': [_voice()]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(SupportTicket.objects.get(pk=r.json()['ticket_id']).message, '(البلاغ في المرفقات)')

    def test_rejects_empty_wrong_type_and_too_many_files(self):
        self.assertEqual(self.post({'message': ''}).status_code, 400)
        exe = SimpleUploadedFile('virus.exe', b'MZ' + b'0' * 32, content_type='application/x-msdownload')
        self.assertEqual(self.post({'message': 'x', 'files': [exe]}).status_code, 400)
        self.assertEqual(self.post({'message': 'x', 'files': [_png() for _ in range(7)]}).status_code, 400)
        self.assertFalse(SupportTicket.objects.filter(tenant=self.tenant).exists())

    def test_customer_follows_ticket_sees_staff_reply_and_answers_back(self):
        from clients.models import SupportReply
        tid = self.post({'message': 'الطباعة واقفة'}).json()['ticket_id']
        ticket = SupportTicket.objects.get(pk=tid)
        SupportTicket.objects.filter(pk=tid).update(seen_at=ticket.created_at, status='waiting', customer_unread=True)
        SupportReply.objects.create(ticket=ticket, author_kind='staff', author_name='الدعم', body='جرّب تحدّث الصفحة')
        host = {'HTTP_HOST': self.domain.domain}
        self.assertEqual(self.c.get('/support/report/', **host).json()['unread'], 1)     # شارة على الزرار
        self.assertContains(self.c.get('/support/tickets/', **host), 'رد جديد')
        r = self.c.get(f'/support/tickets/{tid}/', **host)
        self.assertContains(r, 'جرّب تحدّث الصفحة')
        self.assertEqual(self.c.get('/support/report/', **host).json()['unread'], 0)     # اتقرت
        r = self.c.post(f'/support/tickets/{tid}/', {'action': 'reply', 'body': 'لسه واقفة', 'voice': [_voice()]}, **host)
        self.assertEqual(r.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual((ticket.status, ticket.seen_at), ('open', None))                  # يرجع «جديد» عند المالك
        reply = ticket.replies.get(author_kind='customer')
        self.assertEqual((reply.body, reply.attachments.count()), ('لسه واقفة', 1))
        self.assertEqual(len(mail.outbox), 2)                                              # بلاغ + رد
        self.c.post(f'/support/tickets/{tid}/', {'action': 'close'}, **host)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'closed')

    def test_employee_sees_only_own_tickets_and_attachments(self):
        tid = self.post({'message': 'سر', 'files': [_png()]}).json()['ticket_id']
        att = SupportAttachment.objects.get(ticket_id=tid)
        other = User.objects.create_user('cashier2', password='x')
        c2 = HttpClient()
        c2.force_login(other)
        host = {'HTTP_HOST': self.domain.domain}
        # (الـ 404 جوه الشركة بيتحوّل لتحويل على الداشبورد — المهم إن التذكرة/الملف مايظهروش)
        self.assertIn(c2.get(f'/support/tickets/{tid}/', **host).status_code, (302, 404))
        self.assertIn(c2.get(f'/support/attachment/{att.id}/', **host).status_code, (302, 404))
        self.assertNotContains(c2.get('/support/tickets/', **host), 'سر')
        self.assertEqual(self.c.get(f'/support/attachment/{att.id}/', **host).status_code, 200)
        boss = User.objects.create_user('owner1', password='x', is_superuser=True, is_staff=True)
        c3 = HttpClient()
        c3.force_login(boss)
        self.assertEqual(c3.get(f'/support/tickets/{tid}/', **host).status_code, 200)   # مدير الشركة يشوف الكل

    def test_expired_company_can_still_report(self):
        connection.set_schema_to_public()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(status='suspended')
        connection.set_tenant(self.tenant)
        try:
            from django.core.cache import caches
            caches['local_tier'].clear()
            r = self.post({'message': 'الاشتراك وقف ومش عارف أدفع'})
            self.assertEqual(r.status_code, 200, r.content)
        finally:
            connection.set_schema_to_public()
            type(self.tenant).objects.filter(pk=self.tenant.pk).update(status='active')
            connection.set_tenant(self.tenant)


@override_settings(ALLOWED_HOSTS=['*'], MEDIA_ROOT=_MEDIA)
class OwnerSeesReportsTests(_PublicDomainMixin, TestCase):

    def setUp(self):
        self.owner = User.objects.create_user('owner@plat.test', password='x', is_superuser=True, is_staff=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)
        self.ticket = SupportTicket.objects.create(name='أحمد', email='a@b.test', subject='🆘 مشكلة', message='x',
                                                   source='in_app', reporter='cashier1', phone='01012345678')
        self.att = SupportAttachment.objects.create(ticket=self.ticket, kind='audio', file=_voice(),
                                                    content_type='audio/webm', size=68, original_name='voice.webm')

    def test_new_report_alert_then_detail_marks_it_seen_and_plays_attachments(self):
        data = self.c.get('/superadmin/api/support/new/').json()
        self.assertEqual(data['unseen'], 1)
        self.assertEqual((data['latest'][0]['id'], data['latest'][0]['attachments']), (self.ticket.id, 1))
        r = self.c.get(f'/superadmin/support/{self.ticket.id}/')
        self.assertContains(r, f'/superadmin/support/attachment/{self.att.id}/')
        self.assertContains(r, '<audio')
        self.assertContains(r, 'https://wa.me/201012345678')
        self.assertEqual(self.c.get('/superadmin/api/support/new/').json()['unseen'], 0)
        f = self.c.get(f'/superadmin/support/attachment/{self.att.id}/')
        self.assertEqual((f.status_code, f['Content-Type']), (200, 'audio/webm'))
        self.assertTrue(b''.join(f.streaming_content).startswith(b'\x1aE\xdf\xa3'))

    def test_owner_reply_lands_in_the_customer_thread_even_without_email(self):
        r = self.c.post(f'/superadmin/support/{self.ticket.id}/',
                        {'action': 'reply', 'reply': 'اتصلّحت، جرّب تاني', 'files': [_png()]})
        self.assertEqual(r.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertTrue(self.ticket.customer_unread)
        reply = self.ticket.replies.get()
        self.assertEqual((reply.author_kind, reply.body, reply.attachments.count()), ('staff', 'اتصلّحت، جرّب تاني', 1))
        self.assertContains(self.c.get(f'/superadmin/support/{self.ticket.id}/'), 'اتصلّحت، جرّب تاني')

    def test_attachments_are_not_public(self):
        r = HttpClient().get(f'/superadmin/support/attachment/{self.att.id}/')
        self.assertEqual(r.status_code, 302)
        r = HttpClient().get('/superadmin/api/support/new/')
        self.assertEqual(r.status_code, 302)

    def test_inbox_marks_new_reports_and_attachments(self):
        r = self.c.get('/superadmin/support/')
        self.assertContains(r, 'جديد')
        self.assertContains(r, '📎1')
