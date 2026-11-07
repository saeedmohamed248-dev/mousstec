"""🏭 لوحة الإنتاج: المهام في أعمدة مراحلها، وزرار «المرحلة الجاية» بيحترم شرط البروفة."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.tests.base import ERPTenantTestCase
from printing.models import PrintCustomer, PrintJob, PrintOrder

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class ProductionBoardTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.user = User.objects.create_superuser('board_boss', 'b@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)
        cust = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        self.order = PrintOrder.objects.create(order_number='B-1', customer=cust, status='confirmed')
        self.job = PrintJob.objects.create(order=self.order, description='كروت بزنس', quantity=1,
                                           unit_price=Decimal('100'))

    def get(self):
        return self.c.get(reverse('printing:production_board'), HTTP_HOST=self.host)

    def advance(self, job):
        return self.c.post(reverse('printing:production_job_advance', args=[job.pk]), HTTP_HOST=self.host)

    def test_board_shows_job_in_its_stage_column(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        design = dict((c, jobs) for c, _l, jobs in r.context['columns'])['design']
        self.assertEqual([j.pk for j in design], [self.job.pk])
        self.assertContains(r, 'كروت بزنس')

    def test_draft_cancelled_and_delivered_orders_are_hidden(self):
        for status in ('draft', 'cancelled', 'delivered'):
            PrintOrder.objects.filter(pk=self.order.pk).update(status=status)
            jobs = [j for _c, _l, js in self.get().context['columns'] for j in js]
            self.assertEqual(jobs, [], status)

    def test_advance_moves_to_proof_then_blocks_printing_until_approved(self):
        self.advance(self.job)
        self.job.refresh_from_db()
        self.assertEqual(self.job.stage, 'proof')
        r = self.advance(self.job)                      # من غير موافقة على البروفة
        self.assertEqual(r.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.stage, 'proof')
        board_job = dict((c, js) for c, _l, js in self.get().context['columns'])['proof'][0]
        self.assertTrue(board_job.board_blocked)

    def test_waived_proof_goes_straight_to_printing_and_finishes(self):
        PrintJob.objects.filter(pk=self.job.pk).update(proof_waived=True)
        self.job.refresh_from_db()
        for expected in ('printing', 'finishing', 'done'):
            self.advance(self.job)
            self.job.refresh_from_db()
            self.assertEqual(self.job.stage, expected)
        self.assertTrue(self.job.is_complete)

    def test_user_without_permission_is_forbidden(self):
        clerk = User.objects.create_user('clerk', 'c@x.com', 'x')
        c = Client()
        c.force_login(clerk)
        r = c.post(reverse('printing:production_job_advance', args=[self.job.pk]), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        self.job.refresh_from_db()
        self.assertEqual(self.job.stage, 'design')

    def test_get_on_advance_is_not_allowed(self):
        r = self.c.get(reverse('printing:production_job_advance', args=[self.job.pk]), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 405)
