"""📲 التواصل مع الشركات على واتساب (/superadmin/whatsapp/) — زرار لكل شركة، إرسال جماعي، متابعة."""
from datetime import timedelta
from unittest import mock
from urllib.parse import unquote

from django.contrib.auth import get_user_model
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.utils import timezone

from clients.models import Client, TenantOutreach
from clients.services import whatsapp_outreach as wa

_n = 0


def make_tenant(**kw):
    global _n
    _n += 1
    data = dict(schema_name=f'wa_t{_n}', name=f'شركة {_n}', owner_name=f'مالك {_n}',
                phone='01011112222', status='trial',
                trial_ends_at=timezone.localdate() + timedelta(days=5))
    data.update(kw)
    t = Client(**data)
    t.auto_create_schema = False
    t.save(verbosity=0)
    return t


class MessageAndLinkTests(TestCase):

    def test_placeholders_are_filled_per_company(self):
        t = make_tenant(name='ورشة النور', owner_name='أحمد', trial_ends_at=timezone.localdate() + timedelta(days=3))
        out = wa.render_message('أهلاً {owner} — «{company}» فاضلها {days_left} يوم {مش متغيّر}', t)
        self.assertEqual(out, 'أهلاً أحمد — «ورشة النور» فاضلها 3 يوم {مش متغيّر}')

    def test_expired_trial_days_never_negative_and_owner_falls_back_to_company(self):
        t = make_tenant(owner_name='', trial_ends_at=timezone.localdate() - timedelta(days=4))
        self.assertEqual(wa.tenant_state(t), ('trial_over', -4))
        self.assertEqual(wa.render_message('{owner}|{days_left}', t), f'{t.name}|0')

    def test_wa_link_uses_country_code_and_encodes_text(self):
        eg = make_tenant(phone='010 1111 2222')
        self.assertEqual(wa.wa_url(eg, 'أهلاً & سلام'), 'https://wa.me/201011112222?text=' +
                         '%D8%A3%D9%87%D9%84%D8%A7%D9%8B%20%26%20%D8%B3%D9%84%D8%A7%D9%85')
        ae = make_tenant(phone='0501234567', country='AE')
        self.assertTrue(wa.wa_url(ae).startswith('https://wa.me/971501234567'))
        self.assertIsNone(wa.wa_url(make_tenant(phone='123')))


class FollowUpTests(TestCase):

    def test_rows_put_due_first_then_never_contacted_then_oldest_contact(self):
        today = timezone.localdate()
        recent, old, never, due = (make_tenant() for _ in range(4))
        TenantOutreach.objects.create(tenant=recent, status='replied')
        o = TenantOutreach.objects.create(tenant=old, status='no_reply')
        TenantOutreach.objects.filter(pk=o.pk).update(created_at=timezone.now() - timedelta(days=9))
        TenantOutreach.objects.create(tenant=due, status='interested', next_follow_up=today)
        rows = wa.outreach_rows([recent, old, never, due])
        self.assertEqual([r['tenant'] for r in rows], [due, never, old, recent])
        self.assertTrue(rows[0]['due'])
        self.assertEqual([r['tenant'] for r in wa.outreach_rows([recent, old, never, due], due_only=True)], [due])

    def test_outcome_goes_on_last_contact_or_creates_one(self):
        t = make_tenant()
        first = TenantOutreach.objects.create(tenant=t, status='opened', message='م')
        nxt = timezone.localdate() + timedelta(days=3)
        o = wa.record_outcome(t, status='interested', notes='عايز عرض سعر', next_follow_up=nxt)
        self.assertEqual(o.pk, first.pk)
        first.refresh_from_db()
        self.assertEqual((first.status, first.notes, first.next_follow_up), ('interested', 'عايز عرض سعر', nxt))

        t2 = make_tenant()
        o2 = wa.record_outcome(t2, status='subscribed')   # كلّمه تليفون من غير واتساب
        self.assertEqual((o2.tenant, o2.channel, o2.status), (t2, 'phone', 'subscribed'))
        with self.assertRaises(ValueError):
            wa.record_outcome(t2, status='opened')


@override_settings(WHATSAPP_ACCESS_TOKEN='tok', WHATSAPP_PHONE_NUMBER_ID='pid')
class PlatformBulkSendTests(TestCase):

    def test_template_mode_sends_owner_and_company_and_records_results(self):
        good, boom = make_tenant(owner_name='سامي', name='مطبعة الأمل'), make_tenant(phone='01200000000')
        bad_phone = make_tenant(phone='12')
        batch, queued, bad = wa.queue_api_batch([good, boom, bad_phone], 'أهلاً {owner}')
        self.assertEqual((queued, bad), (2, 1))

        def fake(phone, **kw):
            if phone == wa.wa_phone(boom):
                raise ConnectionError('net down')
            return True
        with override_settings(WHATSAPP_OUTREACH_TEMPLATE='mt_outreach'), \
                mock.patch.object(wa, 'send_platform_whatsapp', side_effect=fake) as send:
            self.assertEqual(wa.send_api_batch(batch), {'sent': 1, 'failed': 1})
        kw = next(c.kwargs for c in send.call_args_list if c.args[0] == wa.wa_phone(good))
        self.assertEqual((kw['template'], tuple(kw['params']), kw['lang']), ('mt_outreach', ('سامي', 'مطبعة الأمل'), 'ar'))
        by = {o.tenant_id: o for o in TenantOutreach.objects.filter(batch=batch)}
        self.assertEqual(by[good.pk].status, 'sent')
        self.assertEqual(by[good.pk].message, 'أهلاً سامي')
        self.assertEqual((by[boom.pk].status, by[boom.pk].error), ('failed', 'net down'))
        self.assertEqual(by[bad_phone.pk].status, 'failed')
        self.assertIn('رقم', by[bad_phone.pk].error)
        # إعادة تشغيل نفس الدفعة مابتبعتش تاني
        with mock.patch.object(wa, 'send_platform_whatsapp') as again:
            wa.send_api_batch(batch)
        again.assert_not_called()

    def test_without_template_sends_the_rendered_text(self):
        t = make_tenant(owner_name='هاني')
        batch, _, _ = wa.queue_api_batch([t], 'إزيك يا {owner}')
        with override_settings(WHATSAPP_OUTREACH_TEMPLATE=''), \
                mock.patch.object(wa, 'send_platform_whatsapp', return_value=False) as send:
            self.assertEqual(wa.send_api_batch(batch), {'sent': 0, 'failed': 1})
        self.assertEqual(send.call_args.kwargs['text'], 'إزيك يا هاني')
        self.assertIn('Meta', TenantOutreach.objects.get(batch=batch).error)


class _PublicHostMixin:
    """نفس تجهيز اختبارات /superadmin/ التانية: tenant public ودومين testserver."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        cls._pub = Tenant.objects.filter(schema_name='public').first()
        cls._made_pub = cls._pub is None
        if cls._made_pub:
            cls._pub = Tenant(schema_name='public', name='Public', owner_name='o', phone='0')
            cls._pub.auto_create_schema = False
            cls._pub.save(verbosity=0)
        cls._dom = None
        if not Domain.objects.filter(domain='testserver').exists():
            cls._dom = Domain.objects.create(tenant=cls._pub, domain='testserver', is_primary=False)

    @classmethod
    def tearDownClass(cls):
        if cls._dom is not None:
            cls._dom.delete()
        if cls._made_pub:
            cls._pub.delete(force_drop=False)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user('wa_owner@test.com', password='x',
                                                          is_staff=True, is_superuser=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)


class OutreachPagesTests(_PublicHostMixin, TestCase):

    def test_list_page_has_whatsapp_button_per_company(self):
        t = make_tenant(name='ورشة الحرية', phone='01099998888')
        make_tenant(name='رقمها بايظ', phone='55')
        r = self.c.get('/superadmin/whatsapp/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'ورشة الحرية')
        self.assertContains(r, f'/superadmin/whatsapp/{t.pk}/open/')
        self.assertContains(r, '<span dir="ltr">01099998888</span>', html=False)
        self.assertContains(r, 'رقم مش صالح')

    def test_non_owner_is_blocked(self):
        u = get_user_model().objects.create_user('wa_nobody@test.com', password='x')
        c = HttpClient()
        c.force_login(u)
        self.assertEqual(c.get('/superadmin/whatsapp/').status_code, 302)
        self.assertEqual(c.post(f'/superadmin/whatsapp/{make_tenant().pk}/open/').status_code, 302)
        self.assertFalse(TenantOutreach.objects.exists())

    def test_open_logs_and_redirects_to_whatsapp_with_the_message(self):
        t = make_tenant(owner_name='كريم')
        self.assertEqual(self.c.get(f'/superadmin/whatsapp/{t.pk}/open/').status_code, 405)
        r = self.c.post(f'/superadmin/whatsapp/{t.pk}/open/', {'msg': 'أهلاً {owner}'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith('https://wa.me/201011112222?text='))
        self.assertEqual(unquote(r['Location'].split('text=')[1]), 'أهلاً كريم')
        o = TenantOutreach.objects.get(tenant=t)
        self.assertEqual((o.channel, o.status, o.message, o.created_by), ('phone', 'opened', 'أهلاً كريم', self.owner))

    def test_open_with_bad_phone_does_not_log(self):
        t = make_tenant(phone='9')
        r = self.c.post(f'/superadmin/whatsapp/{t.pk}/open/', {'msg': 'x'})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('wa.me', r['Location'])
        self.assertFalse(TenantOutreach.objects.filter(tenant=t).exists())

    def test_outcome_with_follow_up_in_days(self):
        t = make_tenant()
        r = self.c.post(f'/superadmin/whatsapp/{t.pk}/outcome/',
                        {'status': 'interested', 'notes': 'هيكلّم شريكه', 'in_days': '3'})
        self.assertEqual(r.status_code, 302)
        o = TenantOutreach.objects.get(tenant=t)
        self.assertEqual((o.status, o.notes, o.next_follow_up),
                         ('interested', 'هيكلّم شريكه', timezone.localdate() + timedelta(days=3)))
        self.c.post(f'/superadmin/whatsapp/{t.pk}/outcome/', {'status': 'bogus'})
        o.refresh_from_db()
        self.assertEqual(o.status, 'interested')

    def test_queue_walks_companies_one_by_one_skipping_bad_numbers(self):
        a = make_tenant(name='أولى')
        make_tenant(name='بايظة', phone='1')
        b = make_tenant(name='تانية')
        r = self.c.get('/superadmin/whatsapp/queue/', {'q': 'wa_t', 'msg': 'أهلاً {company}'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.context['current'], r.context['total']), (a, 2))
        self.assertContains(r, 'أهلاً أولى')
        self.assertIn('i=1', r.context['next_url'])
        r2 = self.c.get(r.context['next_url'])
        self.assertEqual(r2.context['current'], b)
        r3 = self.c.get(r2.context['next_url'])
        self.assertIsNone(r3.context['current'])
        self.assertContains(r3, 'خلصت كل الشركات')

    def test_bulk_platform_send_needs_confirmation_and_keys(self):
        t = make_tenant(name='جماعي')
        data = {'q': t.schema_name, 'msg': 'أهلاً {owner}'}
        with mock.patch('clients.tasks.send_whatsapp_outreach_batch.delay') as delay:
            self.c.post('/superadmin/whatsapp/send/', data)                       # من غير SEND
            with override_settings(WHATSAPP_ACCESS_TOKEN='', WHATSAPP_PHONE_NUMBER_ID=''):
                self.c.post('/superadmin/whatsapp/send/', {**data, 'confirm': 'SEND'})   # من غير مفاتيح
            self.assertFalse(TenantOutreach.objects.exists())
            delay.assert_not_called()
            with override_settings(WHATSAPP_ACCESS_TOKEN='tok', WHATSAPP_PHONE_NUMBER_ID='pid'):
                r = self.c.post('/superadmin/whatsapp/send/', {**data, 'confirm': 'send'})
        self.assertEqual(r.status_code, 302)
        o = TenantOutreach.objects.get(tenant=t)
        self.assertEqual((o.channel, o.status), ('api', 'queued'))
        delay.assert_called_once_with(o.batch)

    def test_bulk_send_marks_failed_when_queue_is_down(self):
        t = make_tenant()
        with override_settings(WHATSAPP_ACCESS_TOKEN='tok', WHATSAPP_PHONE_NUMBER_ID='pid'), \
                mock.patch('clients.tasks.send_whatsapp_outreach_batch.delay', side_effect=OSError('redis')):
            self.c.post('/superadmin/whatsapp/send/', {'q': t.schema_name, 'confirm': 'SEND'})
        self.assertEqual(TenantOutreach.objects.get(tenant=t).status, 'failed')

    def test_owner_dashboard_and_tenant_360_link_to_outreach(self):
        t = make_tenant()
        self.assertContains(self.c.get('/superadmin/'), '/superadmin/whatsapp/')
        r = self.c.get(f'/superadmin/tenant/{t.pk}/360/')
        self.assertContains(r, f'/superadmin/whatsapp/?q={t.schema_name}')
