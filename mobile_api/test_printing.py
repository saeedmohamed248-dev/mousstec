"""
🎨 تطبيق الموبايل للمطابع + القطاع + أجهزة الإشعارات.

    python manage.py test mobile_api.test_printing
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from inventory.tests import factories as f
from inventory.tests.base import ERPTenantTestCase
from mobile_api.models import MobileDevice
from printing.models import (
    PrintCustomer, PrintJob, PrintMaterial, PrintOrder, PrintProof, PrintTransaction, PrintTreasury,
)

API = '/api/mobile/v1'


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingApiBase(ERPTenantTestCase):
    industry = 'printing'

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry=self.industry)
        self.tenant.industry = self.industry
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.client = APIClient()
        self.owner = f.make_user(username='owner', password='pass12345', is_superuser=True, is_staff=True)

    def login(self, username='owner', password='pass12345'):
        self.client.credentials()
        r = self.client.post(f'{API}/auth/login/', {'username': username, 'password': password},
                             format='json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        return r

    def get(self, url, **kw):
        return self.client.get(f'{API}{url}', HTTP_HOST=self.host, **kw)

    def post(self, url, data=None):
        return self.client.post(f'{API}{url}', data or {}, format='json', HTTP_HOST=self.host)

    def patch(self, url, data):
        return self.client.patch(f'{API}{url}', data, format='json', HTTP_HOST=self.host)


class PrintingTenantInfoTests(PrintingApiBase):
    def test_login_and_me_report_printing_industry(self):
        r = self.login()
        self.assertEqual(r.data['tenant']['industry'], 'printing')
        me = self.get('/auth/me/')
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data['tenant']['industry'], 'printing')
        self.assertIn('view_printorder', me.data['tenant']['permissions'])

    def test_workshop_endpoints_closed_for_print_shop(self):
        self.login()
        for url in ('/dashboard/', '/work-orders/', '/vehicles/', '/products/'):
            self.assertEqual(self.get(url).status_code, 403, url)

    def test_print_dashboard(self):
        self.login()
        cust = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        order = PrintOrder.objects.create(customer=cust, status='confirmed')
        PrintJob.objects.create(order=order, description='كروت بزنس', quantity=1, unit_price=Decimal('100'))
        PrintMaterial.objects.create(name='ورق كوشيه', quantity=Decimal('1'), min_stock=Decimal('5'))
        r = self.get('/print/dashboard/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['open_orders'], 1)
        self.assertEqual(r.data['jobs_by_stage']['design'], 1)
        self.assertEqual(r.data['low_stock_materials'], 1)
        self.assertEqual(Decimal(r.data['sales_today']), Decimal('100.00'))


class PrintOrderFlowTests(PrintingApiBase):
    def test_create_order_from_mobile_uses_quick_invoice(self):
        self.login()
        r = self.post('/print/orders/', {
            'customer_name': 'أحمد', 'customer_phone': '01099998888',
            'lines': [{'description': '500 كارت', 'quantity': 2, 'unit_price': '150'}],
        })
        self.assertEqual(r.status_code, 201, r.content)
        order = PrintOrder.objects.get(pk=r.data['id'])
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(order.total_amount, Decimal('300.00'))
        self.assertEqual(order.jobs.count(), 1)
        self.assertIn('deliver', r.data['allowed_actions'])
        self.assertTrue(PrintCustomer.objects.filter(phone='01099998888').exists())

    def test_paid_order_requires_treasury_and_records_payment(self):
        self.login()
        treasury = PrintTreasury.objects.create(name='الكاشير', kind='cash')
        body = {'customer_name': 'منى', 'lines': [{'description': 'بنر', 'quantity': 1, 'unit_price': '200'}],
                'paid': '50', 'delivered': True}
        self.assertEqual(self.post('/print/orders/', body).status_code, 400)
        body['treasury'] = treasury.pk
        r = self.post('/print/orders/', body)
        self.assertEqual(r.status_code, 201, r.content)
        order = PrintOrder.objects.get(pk=r.data['id'])
        self.assertEqual(order.status, 'delivered')
        self.assertEqual(order.paid_amount, Decimal('50.00'))
        self.assertEqual(PrintTransaction.objects.filter(order=order, transaction_type='in').count(), 1)

    def test_list_filter_and_detail(self):
        self.login()
        cust = PrintCustomer.objects.create(name='سارة')
        o1 = PrintOrder.objects.create(customer=cust, status='confirmed')
        PrintOrder.objects.create(customer=cust, status='delivered')
        r = self.get('/print/orders/', data={'status': 'open'})
        self.assertEqual([row['id'] for row in r.data['results']] if 'results' in r.data
                         else [row['id'] for row in r.data], [o1.pk])
        d = self.get(f'/print/orders/{o1.pk}/')
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.data['customer_name'], 'سارة')

    def test_confirm_deliver_and_cancel_actions(self):
        self.login()
        cust = PrintCustomer.objects.create(name='كريم')
        draft = PrintOrder.objects.create(customer=cust, status='draft')
        PrintJob.objects.create(order=draft, description='فلاير', quantity=1, unit_price=Decimal('80'),
                                proof_waived=True)
        r = self.post(f'/print/orders/{draft.pk}/action/', {'action': 'confirm'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['status'], 'confirmed')
        r = self.post(f'/print/orders/{draft.pk}/action/', {'action': 'deliver'})
        self.assertEqual(r.status_code, 200, r.content)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'delivered')
        self.assertTrue(draft.jobs.get().is_complete)
        # الطلب اتسلّم ⇒ مفيش إلغاء من الموبايل
        r = self.post(f'/print/orders/{draft.pk}/action/', {'action': 'cancel'})
        self.assertEqual(r.status_code, 409)

    def test_cancel_refused_when_customer_paid(self):
        self.login()
        treasury = PrintTreasury.objects.create(name='خزنة', kind='cash')
        cust = PrintCustomer.objects.create(name='هبة')
        order = PrintOrder.objects.create(customer=cust, status='confirmed')
        PrintJob.objects.create(order=order, description='ستيكر', quantity=1, unit_price=Decimal('100'))
        PrintTransaction.objects.create(treasury=treasury, transaction_type='in', category='customer',
                                        amount=Decimal('40'), order=order, customer=cust,
                                        description='دفعة')
        r = self.post(f'/print/orders/{order.pk}/action/', {'action': 'cancel'})
        self.assertEqual(r.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')

    def test_deliver_blocked_until_proof_approved(self):
        self.login()
        cust = PrintCustomer.objects.create(name='نادر')
        order = PrintOrder.objects.create(customer=cust, status='confirmed')
        PrintJob.objects.create(order=order, description='علب', quantity=1, unit_price=Decimal('500'))
        r = self.post(f'/print/orders/{order.pk}/action/', {'action': 'deliver'})
        self.assertEqual(r.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')


class PrintJobBoardTests(PrintingApiBase):
    def setUp(self):
        super().setUp()
        cust = PrintCustomer.objects.create(name='شركة الندى')
        self.order = PrintOrder.objects.create(customer=cust, status='confirmed')
        self.job = PrintJob.objects.create(order=self.order, description='كروت', quantity=1,
                                           unit_price=Decimal('100'))

    def test_advance_respects_proof_approval(self):
        self.login()
        r = self.post(f'/print/jobs/{self.job.pk}/advance/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data['stage'], 'proof')
        self.assertTrue(r.data['blocked'])
        r = self.post(f'/print/jobs/{self.job.pk}/advance/')
        self.assertEqual(r.status_code, 409)
        self.job.refresh_from_db()
        self.assertEqual(self.job.stage, 'proof')

    def test_board_lists_active_jobs_only(self):
        self.login()
        r = self.get('/print/jobs/')
        rows = r.data['results'] if 'results' in r.data else r.data
        self.assertEqual([j['id'] for j in rows], [self.job.pk])
        PrintOrder.objects.filter(pk=self.order.pk).update(status='draft')
        r = self.get('/print/jobs/')
        rows = r.data['results'] if 'results' in r.data else r.data
        self.assertEqual(rows, [])


class PrintPermissionTests(PrintingApiBase):
    def test_staff_without_permission_is_forbidden(self):
        f.make_user(username='designer', password='pass12345', is_staff=True)
        self.login('designer')
        self.assertEqual(self.get('/print/orders/').status_code, 403)
        self.assertEqual(self.get('/print/dashboard/').status_code, 403)

    def test_view_only_staff_cannot_advance_or_create(self):
        u = f.make_user(username='viewer1', password='pass12345', is_staff=True)
        u.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='printing', codename__in=('view_printorder', 'view_printjob')))
        cust = PrintCustomer.objects.create(name='x')
        order = PrintOrder.objects.create(customer=cust, status='confirmed')
        job = PrintJob.objects.create(order=order, description='y', quantity=1, unit_price=Decimal('1'))
        self.login('viewer1')
        self.assertEqual(self.get('/print/orders/').status_code, 200)
        self.assertEqual(self.post(f'/print/jobs/{job.pk}/advance/').status_code, 403)
        self.assertEqual(self.post('/print/orders/', {'customer_name': 'z', 'lines': [
            {'description': 'a', 'quantity': 1, 'unit_price': '5'}]}).status_code, 403)
        # الداشبورد مش بيبيّن مبيعات النهارده لمن مايشوفش الخزنة
        r = self.get('/print/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data['sales_today'])

    def test_customer_write_cannot_touch_balance_fields(self):
        self.login()
        r = self.post('/print/customers/', {'name': 'عميل', 'phone': '0100', 'opening_balance': '9999'})
        self.assertEqual(r.status_code, 201, r.content)
        cust = PrintCustomer.objects.get(pk=r.data['id'])
        self.assertEqual(cust.opening_balance, Decimal('0'))
        r = self.patch(f'/print/customers/{cust.pk}/', {'company': 'النور', 'credit_limit': '5'})
        self.assertEqual(r.status_code, 200, r.content)
        cust.refresh_from_db()
        self.assertEqual(cust.company, 'النور')
        self.assertEqual(cust.credit_limit, Decimal('0'))
        self.assertEqual(self.client.delete(f'{API}/print/customers/{cust.pk}/', HTTP_HOST=self.host).status_code, 405)


@override_settings(ALLOWED_HOSTS=['*'])
class AutomotiveCannotReachPrintingTests(PrintingApiBase):
    industry = 'automotive'

    def test_print_endpoints_closed_for_workshop(self):
        r = self.login()
        self.assertEqual(r.data['tenant']['industry'], 'automotive')
        self.assertNotIn('permissions', r.data['tenant'])
        self.assertEqual(self.get('/print/orders/').status_code, 403)
        self.assertEqual(self.get('/print/dashboard/').status_code, 403)
        self.assertEqual(self.get('/dashboard/').status_code, 200)


class DeviceAndPushTests(PrintingApiBase):
    def test_register_reassign_and_unregister_device(self):
        self.login()
        r = self.post('/devices/', {'token': 'tok-1', 'platform': 'ios', 'language': 'en'})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(r.data['push_enabled'])
        self.assertEqual(self.post('/devices/', {'token': 'tok-1'}).status_code, 200)
        self.assertEqual(MobileDevice.objects.count(), 1)
        other = f.make_user(username='other', password='pass12345')
        self.login('other')
        self.post('/devices/', {'token': 'tok-1'})
        self.assertEqual(MobileDevice.objects.get().user_id, other.pk)
        r = self.client.delete(f'{API}/devices/?token=tok-1', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 204)
        self.assertFalse(MobileDevice.objects.exists())

    def test_proof_response_notifies_production_staff(self):
        MobileDevice.objects.create(user=self.owner, token='owner-tok')
        cust = PrintCustomer.objects.create(name='عميل', phone='0101')
        order = PrintOrder.objects.create(customer=cust, status='confirmed')
        job = PrintJob.objects.create(order=order, description='كروت', quantity=1, unit_price=Decimal('10'))
        proof = PrintProof.objects.create(job=job, file='print_proofs/x.png')
        with mock.patch('mobile_api.push.send_to_tokens', return_value=[]) as send, \
                mock.patch('mobile_api.tasks.send_push.delay', side_effect=RuntimeError('no broker')):
            proof.respond('approve')
        send.assert_called_once()
        tokens, title = send.call_args[0][0], send.call_args[0][1]
        self.assertEqual(tokens, ['owner-tok'])
        self.assertIn('وافق', title)

    def test_dead_tokens_are_deactivated(self):
        from mobile_api.push import deliver
        MobileDevice.objects.create(user=self.owner, token='dead')
        with mock.patch('mobile_api.push.send_to_tokens', return_value=['dead']):
            deliver(self.tenant.schema_name, [self.owner.pk], 't', 'b')
        self.assertFalse(MobileDevice.objects.get(token='dead').is_active)


class FcmSenderTests(PrintingApiBase):
    """push.send_to_tokens: التوكن الميت بس هو اللي يتقفل — والإرسال من غير إعداد مايعملش حاجة."""

    def test_without_config_nothing_is_sent(self):
        from mobile_api import push
        with mock.patch.dict('os.environ', {'FCM_SERVICE_ACCOUNT_JSON': '', 'FCM_SERVICE_ACCOUNT_FILE': ''}), \
                mock.patch('requests.post') as post:
            self.assertEqual(push.send_to_tokens(['t'], 'x', 'y'), [])
        post.assert_not_called()

    def test_only_unregistered_tokens_are_dead(self):
        from mobile_api import push
        sa = {'project_id': 'p', 'client_email': 'e', 'private_key': 'k'}

        def resp(code, text):
            return mock.Mock(status_code=code, text=text)

        replies = {'dead': resp(404, '{"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}'),
                   'bad': resp(400, '{"error": {"message": "The registration token is not a valid FCM registration token"}}'),
                   'ok': resp(200, '{}'),
                   'payload': resp(400, '{"error": {"status": "INVALID_ARGUMENT", "message": "Invalid JSON payload"}}')}
        with mock.patch.object(push, '_service_account', return_value=sa), \
                mock.patch.object(push, '_access_token', return_value='AT'), \
                mock.patch('requests.post', side_effect=lambda url, json, **kw: replies[json['message']['token']]):
            dead = push.send_to_tokens(['dead', 'bad', 'ok', 'payload'], 'x', 'y')
        self.assertEqual(sorted(dead), ['bad', 'dead'])
