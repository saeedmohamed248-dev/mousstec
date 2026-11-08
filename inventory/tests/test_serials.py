"""🔢 أرقام السيريال: تسجيل وقت الاستلام (والمتكرر بيتعدّى)، ربط بفاتورة البيع والضمان منها، والبحث."""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import SaleInvoiceItem, SerialNumber
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_product, make_sale_invoice


@override_settings(ALLOWED_HOSTS=['*'])
class SerialNumberTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.ecu = make_product(part_number='ECU-77', name='كمبيوتر محرك', warranty_months=6)
        self.other = make_product(part_number='FLT-1', name='فلتر')
        self.c = Client()
        self.c.force_login(User.objects.create_user('sn_boss', password='x', is_staff=True, is_superuser=True))

    def post(self, name, data, *args):
        return self.c.post(reverse(f'inventory:{name}', args=args), data, HTTP_HOST=self.host)

    def test_register_sell_and_search(self):
        r = self.post('serials_register', {'product': 'ECU-77', 'branch': self.branch.pk,
                                           'serials': 'SN-001\nSN-002\nSN-001'})
        self.assertIn('n=2', r['Location'])
        r = self.post('serials_register', {'product': 'ECU-77', 'branch': self.branch.pk, 'serials': 'SN-002'})
        self.assertIn('n=0', r['Location'])
        self.assertIn('dup=SN-002', r['Location'])
        self.assertEqual(SerialNumber.objects.count(), 2)

        sn = SerialNumber.objects.get(serial='SN-001')
        wrong = make_sale_invoice(make_customer(), self.branch, items=[(self.other, 1, '50')])
        r = self.post('serial_sell', {'invoice': wrong.pk}, sn.pk)
        self.assertIn('err=not_in_invoice', r['Location'])

        inv = make_sale_invoice(make_customer('عميل', phone='01033332222'), self.branch,
                                items=[(self.ecu, 1, '5000')])
        r = self.post('serial_sell', {'invoice': f'#{inv.pk}'}, sn.pk)
        self.assertIn('ok=sold', r['Location'])
        sn.refresh_from_db()
        self.assertEqual((sn.status, sn.sale_invoice_id), ('sold', inv.pk))
        item = SaleInvoiceItem.objects.get(invoice=inv)
        self.assertEqual(sn.warranty_end, item.warranty_end_date)
        self.assertTrue(sn.warranty_active)

        r = self.c.get(reverse('inventory:serials'), {'q': 'SN-001'}, HTTP_HOST=self.host)
        self.assertContains(r, 'الضمان ساري لحد')
        self.assertContains(r, f'#{inv.pk}')

        r = self.post('serial_status', {'status': 'in_stock'}, sn.pk)
        sn.refresh_from_db()
        self.assertEqual((sn.status, sn.sale_invoice_id, sn.warranty_end), ('in_stock', None, None))

    def test_unknown_product_and_cashier_cannot_register(self):
        r = self.post('serials_register', {'product': 'NOPE', 'serials': 'X'})
        self.assertIn('err=product', r['Location'])
        from .factories import make_employee
        cashier, _p = make_employee('sn_cashier', role='cashier', branch=self.branch)
        c = Client()
        c.force_login(cashier)
        r = c.post(reverse('inventory:serials_register'), {'product': 'ECU-77', 'serials': 'Z'}, HTTP_HOST=self.host)
        self.assertNotIn('ok=', r.get('Location', ''))
        self.assertFalse(SerialNumber.objects.filter(serial='Z').exists())
