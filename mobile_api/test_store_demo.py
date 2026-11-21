"""📱 حسابات مراجعين المتاجر (store_review_demo): الشركتين بيتعملوا والمراجع يدخل من التطبيق."""
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from clients.models import Client, Domain
from inventory.tests.base import delete_test_tenant

API = '/api/mobile/v1'


@override_settings(ALLOWED_HOSTS=['*'])
class StoreReviewDemoTests(TransactionTestCase):
    base = 'storedemo.test'

    def tearDown(self):
        connection.set_schema_to_public()
        for sub in ('appreview', 'appreviewprint'):
            tenant = Client.objects.filter(schema_name=sub).first()
            if tenant:
                delete_test_tenant(tenant, Domain.objects.filter(tenant=tenant).first())
        Domain.objects.filter(domain=self.base).delete()
        super().tearDown()

    def _login(self, sub):
        c = APIClient()
        r = c.post(f'{API}/auth/login/', {'username': 'reviewer', 'password': 'Review123!'},
                   format='json', HTTP_HOST=f'{sub}.{self.base}')
        self.assertEqual(r.status_code, 200, r.content)
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        return c, r.data

    def test_creates_both_companies_and_reviewer_can_use_the_app(self):
        call_command('store_review_demo', base_domain=self.base, password='Review123!', stdout=StringIO())
        # تشغيل تاني آمن (idempotent)
        call_command('store_review_demo', base_domain=self.base, password='Review123!', stdout=StringIO())
        connection.set_schema_to_public()

        printing = Client.objects.get(schema_name='appreviewprint')
        self.assertEqual(printing.industry, 'printing')
        self.assertIsNone(printing.email_verify_deadline)
        self.assertEqual(Client.objects.get(schema_name='appreview').industry, 'automotive')

        c, data = self._login('appreviewprint')
        self.assertEqual(data['tenant']['industry'], 'printing')
        d = c.get(f'{API}/print/dashboard/', HTTP_HOST=f'appreviewprint.{self.base}').data
        self.assertEqual(d['pending_proofs'], 1)
        self.assertGreaterEqual(d['low_stock_materials'], 1)
        self.assertGreaterEqual(d['open_orders'], 4)
        jobs = c.get(f'{API}/print/jobs/', HTTP_HOST=f'appreviewprint.{self.base}').data
        rows = jobs['results'] if 'results' in jobs else jobs
        self.assertEqual(len(rows), 4)   # المسودة مش في لوحة الإنتاج

        c, data = self._login('appreview')
        self.assertEqual(data['tenant']['industry'], 'automotive')
        self.assertEqual(c.get(f'{API}/dashboard/', HTTP_HOST=f'appreview.{self.base}').status_code, 200)
