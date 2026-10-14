"""
🔢 Numbers read like global systems: ``1,848,400.00`` — not ``1848400,00``.

The Arabic locale had no thousands grouping and a comma as the decimal
mark, so every amount on screen read «1848400,00». Display filters
(``|amount`` / ``|num`` / ``|compact``) are builtins; input values keep the
plain machine format.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.template import Context, Template
from django.test import Client, SimpleTestCase, override_settings

from .base import ERPTenantTestCase
from .factories import make_branch, make_inventory, make_product, make_purchase_invoice, make_vendor


class FilterTests(SimpleTestCase):
    def render(self, src, **ctx):
        return Template(src).render(Context(ctx))

    def test_amount_num_compact(self):
        self.assertEqual(self.render('{{ v|amount }}', v=Decimal('1848400')), '1,848,400.00')
        self.assertEqual(self.render('{{ v|amount }}', v=-1234.5), '-1,234.50')
        self.assertEqual(self.render('{{ v|amount }}', v=Decimal('-0.001')), '0.00')
        self.assertEqual(self.render('{{ v|num }}', v=2844075.4), '2,844,075')
        self.assertEqual(self.render('{{ v|num:1 }}', v=12.345), '12.3')
        self.assertEqual(self.render('{{ v|num:"-2" }}', v=Decimal('12.00')), '12')
        self.assertEqual(self.render('{{ v|num:"-2" }}', v=Decimal('12.5')), '12.50')
        self.assertEqual(self.render('{{ v|compact }}', v=1848400), '1.85 مليون')
        self.assertEqual(self.render('{{ v|compact }}', v=2400000000), '2.4 مليار')
        self.assertEqual(self.render('{{ v|compact }}', v=185000), '185 ألف')
        self.assertEqual(self.render('{{ v|compact }}', v=950), '950')
        for empty in (None, '', 'abc'):
            self.assertEqual(self.render('[{{ v|amount }}{{ v|num }}{{ v|compact }}]', v=empty), '[]')


@override_settings(ALLOWED_HOSTS=['*'])
class PageNumbersTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        branch = make_branch()
        engine = make_product(part_number='ND-1', name='موتور', retail_price='65000', purchase_price='44000',
                              average_cost='44000')
        make_inventory(engine, branch, quantity=42)
        make_purchase_invoice(make_vendor(name='FIXIT UAE'), branch, items=[(engine, 42, '44009.52')])
        self.c = Client()
        self.c.force_login(User.objects.create_user('nd_boss', password='x', is_staff=True, is_superuser=True))

    def test_purchases_and_products_show_grouped_numbers(self):
        html = self.c.get('/system/purchases/', HTTP_HOST=self.host).content.decode()
        self.assertIn('1,848,399.84', html)
        self.assertNotIn('1848399,84', html)
        html = self.c.get('/system/products/', HTTP_HOST=self.host).content.decode()
        self.assertIn('1,848,000', html)        # قيمة المخزون بالتكلفة 42 × 44,000
        self.assertIn('1.85 مليون', html)
        self.assertIn('صنف خلص', html)


@override_settings(ALLOWED_HOSTS=['*'])
class StockCostBreakdownTests(ERPTenantTestCase):
    """«إجمالي تكلفة المخزون» = بضاعة + مصاريف شراء محمّلة، والمصاريف «على الفترة» ظاهرة لوحدها."""

    def test_purchase_expenses_show_in_stock_cost(self):
        from inventory.models import PurchaseInvoiceExtraCost
        connection.set_tenant(self.tenant)
        branch = make_branch()
        pads = make_product(part_number='SC-1', name='تيل', average_cost='0', purchase_price='0')
        make_inventory(pads, branch, quantity=0)
        inv = make_purchase_invoice(make_vendor(name='مورد'), branch, items=[(pads, 10, '100.00')])
        PurchaseInvoiceExtraCost.objects.create(invoice=inv, kind='shipping', amount=Decimal('200'))
        PurchaseInvoiceExtraCost.objects.create(invoice=inv, kind='travel', amount=Decimal('50'))
        inv.status = 'posted'
        inv.save()
        c = Client()
        c.force_login(User.objects.create_user('sc_boss', password='x', is_staff=True, is_superuser=True))
        html = c.get('/system/products/', HTTP_HOST=self.domain.domain).content.decode()
        self.assertIn('إجمالي تكلفة المخزون', html)
        self.assertRegex(html, r'بضاعة <b class="tabular-nums">1,000</b> \+ مصاريف شراء <b[^>]*>200</b>')
        self.assertIn('شراء 100.00 · مصاريف 20.00', html)
        self.assertIn('+ 50 مصاريف شراء «على الفترة»', html)
