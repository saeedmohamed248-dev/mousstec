"""
🔍 Every path that changes stock outside purchases/sales keeps the books together.

Found in the inventory review:
* «رفع مخزون كامل» overwrote the quantity with no movement, no journal and no
  average-cost update, and blanked prices whose column wasn't in the file;
* bulk «إضافة بالجملة» posted no opening-stock entry;
* the product edit page re-applied the quantity it showed when opened (a sale
  in between came back), and archived products that still had stock;
* «تحميل بالتصوير» added stock to existing parts without re-averaging the
  cost, and booked it at the file's cost (or 0);
* the stock card's «مصاريف شراء» applied the last receipt's expense to every
  unit on hand, and «على الفترة» kept counting invoices sold out long ago;
* scrap-car yields multiplied each part's cost by its quantity twice.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Inventory, InventoryMovement, Product, PurchaseInvoice
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_treasury, make_vendor
from .test_purchase_lifecycle_audit import books_problems

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class InventoryPathsTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='100000.00')
        self.opening = {self.cash.pk: D('100000.00')}
        self.vendor = make_vendor(name='FIXIT UAE')
        self.c = Client()
        self.c.force_login(User.objects.create_user('ip_boss', password='x', is_staff=True, is_superuser=True))

    def ok(self, step):
        self.assertEqual(books_problems(self.opening), [], f"after: {step}")

    def stock(self, p):
        return sum(Inventory.objects.filter(product=p).values_list('quantity', flat=True))

    def buy(self, product, qty, cost, shipping=0):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk, 'treasury_id': None, 'paid_amount': 0,
                'items': [{'product_id': product.pk, 'qty': qty, 'cost': cost}],
                'extra_costs': ([{'kind': 'shipping', 'behavior': 'landed', 'amount': shipping}] if shipping else [])}
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        return PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])

    def upload(self, csv_text):
        f = SimpleUploadedFile('stock.csv', csv_text.encode('utf-8'), content_type='text/csv')
        r = self.c.post('/system/products/import/', {'file': f, 'branch': self.branch.pk}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        return r

    def test_full_stock_upload_records_movements_cost_and_ledger(self):
        p = make_product(part_number='IP-1', name='طرمبة', retail_price='950', purchase_price='0', average_cost='0')
        self.buy(p, 10, 100, shipping=200)                     # متوسط 120
        self.upload("part_number,name,quantity\nIP-1,طرمبة,4\n")
        p.refresh_from_db()
        self.assertEqual((self.stock(p), p.average_cost, p.retail_price), (4, D('120.00'), D('950.00')))
        self.assertTrue(InventoryMovement.objects.filter(product=p, quantity_change=-6).exists())
        self.ok('upload lowers stock')
        self.upload("part_number,name,purchase_price,quantity\nIP-1,طرمبة,100,6\n")
        p.refresh_from_db()
        self.assertEqual((self.stock(p), p.average_cost), (6, D('113.33')))   # (4×120 + 2×100) / 6
        self.ok('upload raises stock')
        self.upload("part_number,name,purchase_price,retail_price,quantity\nIP-NEW,فلتر,80,150,5\n")
        new = Product.objects.get(part_number='IP-NEW')
        self.assertEqual((new.average_cost, self.stock(new)), (D('80.00'), 5))
        self.ok('upload creates a product')

    def test_bulk_add_capitalises_opening_stock(self):
        r = self.c.post('/system/bulk-product/create/', json.dumps({
            'branch_id': self.branch.pk,
            'items': [{'part_number': 'IP-B1', 'name': 'تيل', 'purchase_price': '200', 'retail_price': '300',
                       'starting_qty': 50}]}), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.stock(Product.objects.get(part_number='IP-B1')), 50)
        self.ok('bulk add')

    def test_product_edit_does_not_bring_back_sold_stock(self):
        p = make_product(part_number='IP-2', name='فلتر', retail_price='250', purchase_price='0', average_cost='0')
        self.buy(p, 10, 100)
        page = self.c.get(f'/system/products/{p.pk}/edit/', HTTP_HOST=self.host).content.decode()
        self.assertIn('name="orig_qty" value="10"', page)
        # اتباع 2 من الكاشير وصفحة التعديل لسه مفتوحة على 10
        r = self.c.post('/system/lightning-pos/checkout/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_name': 'عميل', 'customer_phone': '01011112222',
            'payments': [{'treasury_id': self.cash.pk, 'amount': 500}],
            'items': [{'product_id': p.pk, 'qty': 2, 'price': 250, 'discount': 0}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        form = {'part_number': 'IP-2', 'name': 'فلتر زيت', 'quantity': '10', 'orig_qty': '10', 'is_active': 'on'}
        self.c.post(f'/system/products/{p.pk}/edit/', form, HTTP_HOST=self.host)
        self.assertEqual(self.stock(p), 8)
        self.ok('edit name only')
        self.c.post(f'/system/products/{p.pk}/edit/', dict(form, quantity='9', orig_qty='8'), HTTP_HOST=self.host)
        self.assertEqual(self.stock(p), 9)
        self.ok('edit quantity')
        r = self.c.post(f'/system/products/{p.pk}/edit/', {k: v for k, v in form.items() if k != 'is_active'},
                        HTTP_HOST=self.host)
        self.assertIn('err=hasstock', r['Location'])
        self.assertTrue(Product.objects.get(pk=p.pk).is_active)

    def test_photo_import_averages_the_cost(self):
        p = make_product(part_number='IP-3', name='كويل', retail_price='400', purchase_price='0', average_cost='0')
        self.buy(p, 10, 150)
        r = self.c.post('/system/inventory/import/save/', json.dumps({
            'branch_id': self.branch.pk,
            'items': [{'product_id': p.pk, 'qty': 5, 'purchase_price': 100, 'retail_price': 400}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        p.refresh_from_db()
        self.assertEqual((self.stock(p), p.average_cost), (15, D('133.33')))
        self.ok('photo import')

    def test_stock_card_expense_split_follows_what_is_on_hand(self):
        p = make_product(part_number='IP-4', name='جوان', retail_price='200', purchase_price='0', average_cost='0')
        self.upload("part_number,name,purchase_price,quantity\nIP-4,جوان,100,100\n")   # 100 قطعة من غير مصاريف
        self.buy(p, 1, 100, shipping=500)
        html = self.c.get('/system/products/', HTTP_HOST=self.host).content.decode()
        self.assertRegex(html, r'مصاريف شراء <b[^>]*>500</b>')      # مش 10,599.95

    def test_scrap_yield_costs_each_part_once(self):
        from inventory.models import ScrapDismantlingJob, ScrapDismantlingYield
        from inventory.services.inventory_service import InventoryService
        a = make_product(part_number='IP-S1', name='باب', retail_price='1000', average_cost='0', condition='used')
        b = make_product(part_number='IP-S2', name='موتور', retail_price='6000', average_cost='0', condition='used')
        job = ScrapDismantlingJob.objects.create(car_model='E90', branch=self.branch, total_purchase_cost=D('10000'))
        ScrapDismantlingYield.objects.create(job=job, product=a, quantity=4, estimated_cost_allocation=0)
        ScrapDismantlingYield.objects.create(job=job, product=b, quantity=1, estimated_cost_allocation=0)
        InventoryService.distribute_scrap_cost(job)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((self.stock(a), a.average_cost, self.stock(b), b.average_cost),
                         (4, D('1000.00'), 1, D('6000.00')))   # 10,000 مش 22,000
        self.ok('scrap yield')
