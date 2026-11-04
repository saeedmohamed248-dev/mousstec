"""🔑 استرجاع كلمة سر عميل السوق بكود على الموبايل + قفل ثغرة الحسابات القديمة من غير باسورد."""
from __future__ import annotations

import json
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from clients.models import MarketplaceCustomer
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin

SEND = 'clients.views.marketplace_password_reset_views._send_otp_via_channel'


def _phone() -> str:
    return f'+2010{uuid.uuid4().int % 100_000_000:08d}'


@override_settings(OTP_DELIVERY_PROVIDER="console")  # الإيميل بس — مستقل عن .env
class MarketplacePasswordResetTests(_PublicDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.phone = _phone()
        self.customer = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='ريست', phone=self.phone,
            sector='automotive', is_verified=True, email='reset@shop.test',
        )
        self.customer.set_password('Old-pass-123')
        self.customer.save()
        self.sent = []

    def _post(self, url, body, client=None, **extra):
        return (client or Client()).post(url, data=json.dumps(body), content_type='application/json', **extra)

    def _send(self, phone=None, **extra):
        def fake(phone_, code, **kw):
            self.sent.append((phone_, code, kw.get('provider'), kw.get('email')))
            return True
        with mock.patch(SEND, side_effect=fake):
            return self._post('/marketplace/password/forgot/send/', {'phone': phone or self.phone}, **extra)

    def _confirm(self, code, password='Brand-new-pass-9', client=None, ident=None):
        return self._post('/marketplace/password/forgot/confirm/',
                          {'phone': ident or self.phone, 'code': code, 'password': password}, client=client)

    def test_page_renders(self):
        r = Client().get('/marketplace/password/forgot/?phone=01012345678')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="sendBtn"')
        self.assertContains(r, 'value="01012345678"')

    def test_full_flow_changes_password_logs_in_and_kicks_old_sessions(self):
        old_token = self.customer.session_token
        self.assertEqual(self._send().status_code, 200)
        self.assertEqual(len(self.sent), 1)
        code = self.sent[0][1]
        self.assertRegex(code, r'^\d{6}$')

        c = Client()
        r = self._confirm(code, client=c)
        self.assertEqual(r.status_code, 200, r.content)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.check_password('Brand-new-pass-9'))
        self.assertFalse(self.customer.check_password('Old-pass-123'))
        self.assertNotEqual(self.customer.session_token, old_token)
        self.assertEqual(r.cookies['mp_session'].value, str(self.customer.session_token))
        # الكود مايتستخدمش تاني
        self.assertEqual(self._confirm(code, 'Another-pass-77').status_code, 400)

    def test_code_goes_to_the_account_email(self):
        self._send()
        self.assertEqual(self.sent[0][2:], ('email', 'reset@shop.test'))

    def test_reset_by_typing_the_email(self):
        self._send(phone='RESET@shop.test')
        self.assertEqual(len(self.sent), 1)
        r = self._confirm(self.sent[0][1], ident='reset@shop.test')
        self.assertEqual(r.status_code, 200, r.content)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.check_password('Brand-new-pass-9'))

    def test_phone_channel_used_too_when_gateway_configured(self):
        with self.settings(OTP_DELIVERY_PROVIDER='whatsapp_meta'):
            self._send()
        self.assertEqual([x[2] for x in self.sent], ['email', None])

    def test_duplicate_email_is_not_guessed(self):
        MarketplaceCustomer.objects.create(customer_type='individual', full_name='تاني', phone=_phone(),
                                           sector='automotive', is_verified=True, email='reset@shop.test')
        self._send(phone='reset@shop.test')
        self.assertEqual(self.sent, [])

    def test_local_phone_format_is_normalized(self):
        self._send(phone='0' + self.phone[3:])   # 010xxxxxxxx
        self.assertEqual(self.sent[0][0], self.phone)

    def test_unknown_phone_gets_same_reply_and_nothing_is_sent(self):
        known = self._send().json()
        unknown = self._send(phone=_phone()).json()
        self.assertEqual(known, unknown)
        self.assertEqual(len(self.sent), 1)

    def test_five_wrong_codes_burn_the_code(self):
        self._send()
        code = self.sent[0][1]
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self._confirm(wrong).status_code, 400)
        self.assertEqual(self._confirm(code).status_code, 400)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.check_password('Old-pass-123'))

    def test_weak_password_rejected_but_code_still_usable(self):
        self._send()
        code = self.sent[0][1]
        self.assertEqual(self._confirm(code, '123').status_code, 400)
        self.assertEqual(self._confirm(code).status_code, 200)

    def test_send_limited_per_phone(self):
        for i in range(5):
            self._send(REMOTE_ADDR=f'10.1.0.{i + 1}')
        self.assertEqual(len(self.sent), 3)

    def test_csrf_is_enforced(self):
        r = self._post('/marketplace/password/forgot/send/', {'phone': self.phone},
                       client=Client(enforce_csrf_checks=True))
        self.assertEqual(r.status_code, 403)

    def test_blocked_customer_cannot_reset(self):
        self.customer.is_blocked = True
        self.customer.save()
        self._send()
        self.assertEqual(self.sent, [])


class LegacyPasswordlessLoginTests(_PublicDomainMixin, TestCase):
    """🐛 قبل كده: حساب من غير باسورد ⇒ أي حد يعرف الرقم يدخل ويحط باسورد من عنده."""

    def test_passwordless_account_cannot_be_claimed_from_login(self):
        cache.clear()
        phone = _phone()
        customer = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='قديم', phone=phone,
            sector='automotive', is_verified=True,
        )
        r = Client().post('/marketplace/login/', data=json.dumps({'phone': phone, 'password': 'attacker-pass-1'}),
                          content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()['reset_required'])
        customer.refresh_from_db()
        self.assertFalse(customer.has_usable_password())
        self.assertNotIn('mp_session', r.cookies)


@override_settings(OTP_DELIVERY_PROVIDER='console',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PasswordResetRealEmailTests(_PublicDomainMixin, TestCase):
    """📧 من غير mock: الإيميل بيتبعت فعلاً بنظام الإيميل وفيه الكود اللي بيغيّر الباسورد."""

    def test_email_contains_working_code(self):
        import re
        from django.core import mail
        cache.clear()
        phone = _phone()
        c = MarketplaceCustomer.objects.create(customer_type='individual', full_name='ميل', phone=phone,
                                               sector='automotive', is_verified=True, email='mail@shop.test')
        Client().post('/marketplace/password/forgot/send/', data=json.dumps({'phone': phone}),
                      content_type='application/json')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['mail@shop.test'])
        code = re.search(r'\b(\d{6})\b', mail.outbox[0].body).group(1)
        r = Client().post('/marketplace/password/forgot/confirm/', content_type='application/json',
                          data=json.dumps({'phone': phone, 'code': code, 'password': 'Fresh-pass-321'}))
        self.assertEqual(r.status_code, 200, r.content)
        c.refresh_from_db()
        self.assertTrue(c.check_password('Fresh-pass-321'))
