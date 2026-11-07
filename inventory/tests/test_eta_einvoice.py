"""🧾 تجهيز الفاتورة الإلكترونية (ETA): الحساب يطابق الفاتورة، والنواقص بتظهر كأخطاء."""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import ETASettings, SaleInvoice
from inventory.services.eta_einvoice import build_document
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_inventory, make_product, make_sale_invoice


@override_settings(ALLOWED_HOSTS=['*'])
class ETAEInvoiceTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.settings = ETASettings.load()
        for k, v in dict(taxpayer_rin='123456789', taxpayer_name='موس تك', activity_code='4530',
                         governate='القاهرة', region_city='مدينة نصر', street='عباس العقاد',
                         building_number='10', service_item_code='EG-123456789-LABOR').items():
            setattr(self.settings, k, v)
        self.settings.save()
        self.oil = make_product(part_number='ETA-OIL', name='زيت', eta_item_code='EG-123456789-OIL')
        self.flt = make_product(part_number='ETA-FLT', name='فلتر', eta_item_code='6221234567890')
        make_inventory(self.oil, self.branch, 50)
        make_inventory(self.flt, self.branch, 50)
        self.cust = make_customer('شركة الأمل', phone='01022223333', tax_id='987654321',
                                  eta_governate='الجيزة', eta_region_city='الدقي', eta_street='التحرير',
                                  eta_building_number='5')

    def invoice(self, customer=None, discount='0', tax='14', items=None):
        inv = make_sale_invoice(customer or self.cust, self.branch,
                                items=items or [(self.oil, 3, '100.00'), (self.flt, 1, '50.00')])
        inv.discount = Decimal(discount)
        inv.tax_percentage = Decimal(tax)
        inv.save(update_fields=['discount', 'tax_percentage'])
        inv.update_total()
        SaleInvoice.objects.filter(pk=inv.pk).update(status='posted')
        inv.refresh_from_db()
        return inv

    def test_complete_invoice_is_ready_and_totals_match(self):
        inv = self.invoice(discount='35', tax='14')    # (300+50−35)=315 + 14% = 359.10
        doc, errors = build_document(inv)
        self.assertEqual(errors, [])
        self.assertEqual(Decimal(str(doc['totalAmount'])), inv.total_amount)
        self.assertEqual(Decimal(str(doc['totalAmount'])), Decimal('359.10'))
        self.assertEqual(Decimal(str(doc['totalDiscountAmount'])), Decimal('35.00'))
        self.assertEqual(doc['receiver']['type'], 'B')
        self.assertEqual(doc['documentType'], 'i')
        self.assertEqual([l['itemType'] for l in doc['invoiceLines']], ['EGS', 'GS1'])
        self.assertEqual(doc['taxTotals'][0]['taxType'], 'T1')
        self.assertTrue(doc['dateTimeIssued'].endswith('Z'))

    def test_missing_item_code_company_address_and_settings_are_reported(self):
        self.flt.eta_item_code = ''
        self.flt.save(update_fields=['eta_item_code'])
        self.cust.eta_street = ''
        self.cust.save(update_fields=['eta_street'])
        self.settings.activity_code = ''
        self.settings.save()
        _doc, errors = build_document(self.invoice())
        joined = ' | '.join(errors)
        self.assertIn('ETA-FLT', joined)
        self.assertIn('شركة الأمل', joined)
        self.assertIn('كود النشاط', joined)

    def test_person_over_50k_needs_national_id(self):
        person = make_customer('أحمد', phone='01033334444')
        inv = self.invoice(customer=person, tax='0', items=[(self.oil, 1, '60000.00')])
        _doc, errors = build_document(inv)
        self.assertTrue(any('الرقم القومي' in e for e in errors))
        person.national_id = '29001011234567'
        person.save(update_fields=['national_id'])
        doc, errors = build_document(SaleInvoice.objects.get(pk=inv.pk))
        self.assertEqual(errors, [])
        self.assertEqual(doc['receiver'], {**doc['receiver'], 'type': 'P', 'id': '29001011234567'})

    def test_draft_invoice_is_not_ready(self):
        inv = self.invoice()
        SaleInvoice.objects.filter(pk=inv.pk).update(status='quotation')
        inv.refresh_from_db()
        _doc, errors = build_document(inv)
        self.assertTrue(any('معتمدة' in e for e in errors))

    def test_overview_page_and_downloads(self):
        ready = self.invoice(items=[(self.oil, 2, '100.00')])
        self.flt.eta_item_code = ''
        self.flt.save(update_fields=['eta_item_code'])
        broken = self.invoice(items=[(self.flt, 1, '50.00')])
        boss = User.objects.create_user('eta_boss', password='x', is_staff=True, is_superuser=True)
        c = Client()
        c.force_login(boss)
        r = c.get(reverse('inventory:eta_overview'), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['ready'], 1)
        r = c.get(reverse('inventory:eta_overview'), {'download': '1'}, HTTP_HOST=self.host)
        docs = json.loads(r.content)['documents']
        self.assertEqual([d['internalID'] for d in docs], [str(ready.pk)])
        r = c.get(reverse('inventory:eta_invoice_json', args=[broken.pk]), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()['errors'])
        r = c.get(reverse('inventory:eta_invoice_json', args=[ready.pk]), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r['Content-Disposition'])
