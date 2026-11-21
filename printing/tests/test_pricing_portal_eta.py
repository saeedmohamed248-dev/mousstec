"""
💲 حاسبة الأسعار + 🔗 بوابة العميل + 🛒 المتجر الأونلاين + 🧾 الفاتورة الإلكترونية للمطابع.
"""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import ETASettings
from inventory.tests.base import ERPTenantTestCase
from printing.models import (
    PriceQuotation, PrintCustomer, PrintOrder, PrintPriceRule, PrintPriceTier, PrintProof, PrintTreasury,
    ProductType,
)
from printing.quick_invoice import create_quick_invoice

User = get_user_model()
D = Decimal


@override_settings(ALLOWED_HOSTS=['*'])
class _Base(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        cache.clear()
        self.host = self.domain.domain
        self.boss = User.objects.create_superuser('price_boss', 'p@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.boss)
        self.anon = Client()
        self.cards = PrintPriceRule.objects.create(name='كارت بزنس', unit_label='كارت', setup_fee=D('50'),
                                                   min_quantity=100, is_public=True)
        for q, p in ((100, '1.5'), (500, '1'), (1000, '0.7')):
            PrintPriceTier.objects.create(rule=self.cards, min_qty=q, unit_price=D(p))


class PriceCalculatorTests(_Base):
    def test_tiers_setup_fee_and_min_total(self):
        self.assertEqual(self.cards.quote(100)['total'], D('200.00'))     # 100×1.5 + 50
        self.assertEqual(self.cards.quote(499)['unit_price'], D('1.60'))  # (748.5+50)/499
        q = self.cards.quote(1000)
        self.assertEqual((q['unit_price'], q['total']), (D('0.75'), D('750.00')))   # 700 + 50
        self.cards.min_total = D('300')
        self.assertEqual(self.cards.quote(100)['total'], D('300.00'))
        self.assertTrue(self.cards.quote(100)['min_applied'])

    def test_below_min_quantity_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.cards.quote(50)

    def test_quick_invoice_offers_the_price_list(self):
        r = self.c.get(reverse('printing:quick_invoice'), HTTP_HOST=self.host)
        self.assertContains(r, '<option value="كارت بزنس">')
        self.assertContains(r, 'qiProducts')

    def test_price_list_points_to_the_shop(self):
        r = self.c.get(reverse('admin:printing_printpricerule_changelist'), HTTP_HOST=self.host)
        self.assertContains(r, reverse('printing:print_shop'))
        self.assertContains(r, '1000+ = 0.7')

    def test_admin_shows_examples(self):
        r = self.c.get(reverse('admin:printing_printpricerule_change', args=[self.cards.pk]), HTTP_HOST=self.host)
        self.assertContains(r, '750.00')


class CustomerPortalTests(_Base):
    def setUp(self):
        super().setUp()
        self.treasury = PrintTreasury.objects.create(name='الخزنة')
        self.nada = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        self.order, _w = create_quick_invoice(user=self.boss, lines=[('500 كارت', '1', '300')], customer_id=self.nada.pk,
                                              paid='100', treasury=self.treasury)
        self.other = PrintCustomer.objects.create(name='عميل تاني', phone='01099990000')
        self.other_order, _w = create_quick_invoice(user=self.boss, lines=[('بنر', '1', '900')], customer_id=self.other.pk)

    def test_link_is_created_from_customer_page_and_can_be_rotated(self):
        url = reverse('printing:customer_portal_link', args=[self.nada.pk])
        self.c.post(url, HTTP_HOST=self.host)
        self.nada.refresh_from_db()
        first = self.nada.portal_token
        self.assertIsNotNone(first)
        page = self.c.get(reverse('admin:printing_printcustomer_change', args=[self.nada.pk]), HTTP_HOST=self.host)
        self.assertContains(page, str(first))
        self.c.post(url, {'regenerate': '1'}, HTTP_HOST=self.host)
        self.nada.refresh_from_db()
        self.assertNotEqual(self.nada.portal_token, first)
        old = self.anon.get(reverse('printing:customer_portal', args=[first]), HTTP_HOST=self.host)
        self.assertIn(old.status_code, (302, 404))   # 404 العام بيحوّل للرئيسية

    def test_portal_shows_only_his_orders_balance_and_invoice(self):
        token = self.nada.get_portal_token()
        r = self.anon.get(reverse('printing:customer_portal', args=[token]), HTTP_HOST=self.host)
        self.assertContains(r, self.order.order_number)
        self.assertNotContains(r, self.other_order.order_number)
        self.assertContains(r, '200.00')                                     # باقي عليه
        self.assertEqual(r['X-Robots-Tag'], 'noindex, nofollow')
        inv = self.anon.get(reverse('printing:customer_portal_order', args=[token, self.order.pk]), HTTP_HOST=self.host)
        self.assertContains(inv, self.order.order_number)
        self.assertNotContains(inv, reverse('admin:printing_printorder_change', args=[self.order.pk]))   # من غير أزرار الموظف
        theirs = self.anon.get(reverse('printing:customer_portal_order', args=[token, self.other_order.pk]),
                               HTTP_HOST=self.host)
        self.assertIn(theirs.status_code, (302, 404))

    def test_pending_proof_shows_on_portal(self):
        from django.core.files.base import ContentFile
        job = self.order.jobs.first()
        proof = PrintProof(job=job)
        proof.file.save('p.png', ContentFile(b'x'), save=False)
        proof.save()
        r = self.anon.get(reverse('printing:customer_portal', args=[self.nada.get_portal_token()]), HTTP_HOST=self.host)
        self.assertContains(r, str(proof.share_token))

    def test_unknown_token_is_404(self):
        import uuid
        r = self.anon.get(reverse('printing:customer_portal', args=[uuid.uuid4()]), HTTP_HOST=self.host)
        self.assertIn(r.status_code, (302, 404))


class PrintShopTests(_Base):
    def test_shop_lists_public_products_with_live_prices(self):
        PrintPriceRule.objects.create(name='منتج داخلي', is_public=False)
        r = self.anon.get(reverse('printing:print_shop'), HTTP_HOST=self.host)
        self.assertContains(r, 'كارت بزنس')
        self.assertNotContains(r, 'منتج داخلي')

    def test_no_public_products_is_404(self):
        PrintPriceRule.objects.update(is_public=False)
        self.assertIn(self.anon.get(reverse('printing:print_shop'), HTTP_HOST=self.host).status_code, (302, 404))

    def test_order_becomes_a_sent_quotation_priced_by_the_server(self):
        r = self.anon.post(reverse('printing:print_shop'), {
            'rule': [str(self.cards.pk)], 'qty': ['1000'], 'name': 'أحمد', 'phone': '01012345678',
            'notes': 'لوجو دهبي'}, HTTP_HOST=self.host)
        quote = PriceQuotation.objects.get()
        self.assertRedirects(r, reverse('printing:quotation_public_view', args=[quote.share_token]) + '?new=1',
                             fetch_redirect_response=False)
        self.assertEqual((quote.status, quote.customer_phone, quote.total), ('sent', '01012345678', D('750.00')))
        self.assertIn('لوجو دهبي', quote.notes)
        self.assertEqual(quote.lines.get().unit_price, D('0.75'))

    def test_portal_customer_orders_without_typing_his_data(self):
        nada = PrintCustomer.objects.create(name='شركة الندى', phone='01011112222')
        token = nada.get_portal_token()
        self.anon.post(reverse('printing:print_shop'), {'c': str(token), 'rule': [str(self.cards.pk)], 'qty': ['100']},
                       HTTP_HOST=self.host)
        self.assertEqual(PriceQuotation.objects.get().customer, nada)

    def test_bad_input_and_bots_save_nothing(self):
        url = reverse('printing:print_shop')
        r = self.anon.post(url, {'rule': [str(self.cards.pk)], 'qty': ['10'], 'name': 'أحمد', 'phone': '01012345678'},
                           HTTP_HOST=self.host)
        self.assertContains(r, 'أقل كمية')
        r = self.anon.post(url, {'rule': [str(self.cards.pk)], 'qty': ['100'], 'name': '', 'phone': ''}, HTTP_HOST=self.host)
        self.assertContains(r, 'اكتب اسمك')
        self.anon.post(url, {'rule': [str(self.cards.pk)], 'qty': ['100'], 'name': 'بوت', 'phone': '01012345678',
                             'website': 'spam'}, HTTP_HOST=self.host)
        self.assertFalse(PriceQuotation.objects.exists())


class PrintETATests(_Base):
    def setUp(self):
        super().setUp()
        self.treasury = PrintTreasury.objects.create(name='الخزنة')
        s = ETASettings.load()
        for k, v in dict(taxpayer_rin='123456789', taxpayer_name='مطبعة', activity_code='1811', governate='القاهرة',
                         region_city='مدينة نصر', street='عباس العقاد', building_number='5',
                         service_item_code='EG-123456789-PRINT').items():
            setattr(s, k, v)
        s.save()
        self.company = PrintCustomer.objects.create(
            name='أحمد', company='شركة الندى', phone='01011112222', tax_id='987654321', eta_governate='الجيزة',
            eta_region_city='الدقي', eta_street='التحرير', eta_building_number='10')

    def _order(self, customer, tax='0', discount='0'):
        order, _w = create_quick_invoice(user=self.boss, customer_id=customer.pk, discount=discount,
                                         lines=[('500 كارت', '500', '1'), ('بنر', '2', '250')])
        if tax != '0':
            PrintOrder.objects.filter(pk=order.pk).update(tax_amount=D(tax))
            order.refresh_from_db()
        return order

    def test_company_invoice_with_14pct_vat_is_ready_and_totals_match(self):
        pt = ProductType.objects.create(name='كروت', eta_item_code='EG-123456789-CARDS')
        order = self._order(self.company, tax='126', discount='100')     # (1000−100)×14٪
        order.jobs.filter(description='500 كارت').update(product_type=pt)
        from printing.eta import build_document
        doc, errors = build_document(order)
        self.assertEqual(errors, [])
        self.assertEqual(doc['receiver']['type'], 'B')
        self.assertEqual(doc['receiver']['name'], 'شركة الندى')
        self.assertEqual(doc['totalAmount'], 1026.0)
        self.assertEqual(doc['taxTotals'][0]['amount'], 126.0)
        self.assertEqual(doc['totalDiscountAmount'], 100.0)
        self.assertEqual({l['itemCode'] for l in doc['invoiceLines']}, {'EG-123456789-CARDS', 'EG-123456789-PRINT'})
        r = self.c.get(reverse('printing:eta_order_json', args=[order.pk]), HTTP_HOST=self.host)
        self.assertEqual(json.loads(r.content)['documents'][0]['internalID'], order.order_number)

    def test_missing_data_is_listed_not_exported(self):
        person = PrintCustomer.objects.create(name='فرد', phone='01000000000')
        order, _w = create_quick_invoice(user=self.boss, customer_id=person.pk, lines=[('لوحة', '1', '60000')])
        from printing.eta import build_document
        _doc, errors = build_document(order)
        self.assertTrue(any('الرقم القومي' in e for e in errors))
        r = self.c.get(reverse('printing:eta_order_json', args=[order.pk]), HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 400)

    def test_overview_page_and_settings_form(self):
        self._order(self.company)
        r = self.c.get(reverse('printing:eta_overview'), HTTP_HOST=self.host)
        self.assertContains(r, 'جاهزة')
        self.c.post(reverse('printing:eta_overview'), {
            'taxpayer_rin': '111', 'taxpayer_name': 'x', 'activity_code': '1811', 'branch_id': '0',
            'governate': 'a', 'region_city': 'b', 'street': 'c', 'building_number': '1', 'service_item_code': 'EG-1'},
            HTTP_HOST=self.host)
        self.assertEqual(ETASettings.load().taxpayer_rin, '111')
        dl = self.c.get(reverse('printing:eta_overview') + '?download=1', HTTP_HOST=self.host)
        self.assertEqual(len(json.loads(dl.content)['documents']), 1)

    def test_not_delivered_is_not_ready(self):
        order, _w = create_quick_invoice(user=self.boss, customer_id=self.company.pk, delivered=False,
                                         lines=[('بنر', '1', '100')])
        from printing.eta import build_document
        self.assertTrue(any('ماتسلّمتش' in e for e in build_document(order)[1]))
