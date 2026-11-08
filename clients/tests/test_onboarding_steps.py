"""🧭 «أول خطوات» للشركة الجديدة: بتتشطب لوحدها من البيانات، وبتختفي لما تخلص، وللمالك بس."""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from clients.services.onboarding_steps import onboarding_steps
from inventory.models import Customer, Product, SaleInvoice, Treasury
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_customer, make_employee, make_product, make_treasury


@override_settings(ALLOWED_HOSTS=['*'])
class OnboardingStepsTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='automotive')
        self.tenant.industry = 'automotive'
        for m in (SaleInvoice, Product, Treasury):
            m.objects.all().delete()
        Customer.objects.all().delete()
        self.host = self.domain.domain

    def test_steps_tick_themselves_and_disappear_when_done(self):
        ob = onboarding_steps(self.tenant)
        self.assertEqual((ob['done'], ob['total']), (0, 5))
        self.assertTrue(all(s['url'] != '#' for s in ob['steps']))
        branch = make_branch()
        make_product()
        make_treasury(branch)
        self.assertEqual(onboarding_steps(self.tenant)['done'], 2)
        cust = make_customer('عميل حقيقي', phone='01055554444')
        SaleInvoice.objects.create(customer=cust, branch=branch, invoice_type='sale')
        make_employee('ob_cashier', role='cashier', branch=branch)
        self.assertIsNone(onboarding_steps(self.tenant))

    def test_card_shows_for_the_owner_only(self):
        owner = User.objects.create_user('ob_owner', password='x', is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(owner)
        r = c.get('/system/dashboard/', HTTP_HOST=self.host)
        self.assertContains(r, 'onboardingSteps')
        self.assertContains(r, 'ضيف أصنافك')
        cashier, _p = make_employee('ob_cash2', role='cashier', branch=make_branch('فرع 2'))
        c2 = Client()
        c2.force_login(cashier)
        r = c2.get('/system/dashboard/', HTTP_HOST=self.host)
        self.assertNotContains(r, 'onboardingSteps')
