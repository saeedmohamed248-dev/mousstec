"""
📥📤 Purchases: upload a supplier invoice (Excel / CSV / PDF / photo) and
export the purchases list (Excel / PDF), like the inventory page.

* The reader finds the header row anywhere in the first rows (supplier files
  and our own exports have a title above the table), understands Arabic and
  English headers, skips total/discount rows, reads «1,250.50» / «١٢٥٠» /
  «14300,00», and treats a supplier's «سعر البيع» as our purchase cost.
* A text PDF is read without AI (Arabic comes out in display order and is
  put back); a scanned PDF goes to the AI reader as application/pdf.
* New items can carry a selling price; a part number flipped by an RTL PDF
  still matches the existing product.
* The list filters by date / payment state and exports summary or lines.
"""
import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone

from inventory.models import Product, PurchaseInvoice
from inventory.report_export import _reportlab_pdf
from inventory.services.purchase_import import (
    ImportFileError, extract_purchase_items, parse_number, rows_to_items, template_xlsx,
)
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_inventory, make_product, make_purchase_invoice, make_treasury, make_vendor,
)

D = lambda v: Decimal(str(v))  # noqa: E731


def _supplier_pdf(rows, columns=('رقم القطعة', 'اسم الصنف', 'الكمية', 'سعر الوحدة', 'الإجمالي')):
    return _reportlab_pdf('فاتورة مورد', 'شركة النور لقطع الغيار', [{
        'columns': list(columns), 'rows': rows, 'total': ['', 'الإجمالي', '', '', D('1')]}])


class ReaderTests(SimpleTestCase):
    def test_numbers_in_every_shape(self):
        for raw, want in [('1,250.50', '1250.50'), ('14300,00', '14300.00'), ('١٢٥٠', '1250'),
                          ('850 ج.م', '850'), ('1.250,50', '1250.50'), ('12,155', '12155'),
                          ('٣٤٠٠٫٥', '3400.5'), (4.0, '4.0')]:
            self.assertEqual(parse_number(raw), D(want), raw)
        self.assertIsNone(parse_number('ـــ'))

    def test_header_below_a_title_and_summary_rows_skipped(self):
        rows = [['فاتورة بيع رقم 77', None, None, None], ['العميل: محل موس'], [],
                ['البيان', 'العدد', 'سعر البيع', 'القيمة'],
                ['بلف عادم', '2', '150 ج.م', '300'],
                ['قسم الكهرباء', None, None, None],          # عنوان قسم
                ['Taxi light', '1', '20', '20'],             # مش «tax»
                ['خصم 5%', None, None, '-16'], ['الإجمالي', None, None, '304']]
        self.assertEqual(rows_to_items(rows), [
            {'part_number': '', 'name': 'بلف عادم', 'qty': 2, 'cost': 150.0, 'sell': 0},
            {'part_number': '', 'name': 'Taxi light', 'qty': 1, 'cost': 20.0, 'sell': 0},
        ])

    def test_unit_cost_from_line_total_and_sell_column(self):
        rows = [['Item Code', 'Description', 'Qty', 'Amount', 'Retail'],
                ['A-1', 'Brake pad', 4, 1000, 300]]
        self.assertEqual(rows_to_items(rows),
                         [{'part_number': 'A-1', 'name': 'Brake pad', 'qty': 4, 'cost': 250.0, 'sell': 300.0}])

    def test_text_pdf_is_read_without_ai(self):
        pdf = _supplier_pdf([['04465-0D130', 'تيل فرامل أمامي تويوتا', 4, D('850'), D('3400')],
                             ['', 'فلتر زيت لاند كروزر 2020', 10, D('125.50'), D('1255')]])
        vendor, items = extract_purchase_items(SimpleUploadedFile('inv.pdf', pdf))
        self.assertEqual([(i['part_number'], i['name'], i['qty'], i['cost']) for i in items], [
            ('04465-0D130', 'تيل فرامل أمامي تويوتا', 4, 850.0),
            ('', 'فلتر زيت لاند كروزر 2020', 10, 125.5)])

    def test_scanned_pdf_goes_to_ai_as_pdf(self):
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        canvas.Canvas(buf).save()   # PDF من غير نص (زي الممسوح)
        up = SimpleUploadedFile('scan.pdf', buf.getvalue())
        with override_settings(ENABLE_AI_PREDICTIONS=False):
            with self.assertRaisesMessage(ImportFileError, 'ممسوحة'):
                extract_purchase_items(up)
        up.seek(0)
        ai = {'vendor_name': 'شركة النور', 'items': [{'part_number': 'X1', 'name': 'كاوتش', 'qty': '2', 'cost': '1,500'}]}
        with override_settings(ENABLE_AI_PREDICTIONS=True, AI_VISION_API_KEY='k'), \
                mock.patch('inventory.ai_services.scan_invoice_image_ai', return_value=ai) as scan:
            vendor, items = extract_purchase_items(up)
        self.assertEqual(scan.call_args.kwargs['mime_type'], 'application/pdf')
        self.assertEqual((vendor, items[0]['qty'], items[0]['cost']), ('شركة النور', 2, 1500.0))

    def test_gemini_gets_the_real_mime_type(self):
        from inventory import ai_services
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {'candidates': [{'content': {'parts': [{'text': '{"items": []}'}]}}]}
        with override_settings(ENABLE_AI_PREDICTIONS=True, AI_VISION_API_KEY='k'), \
                mock.patch.object(ai_services.requests, 'post', return_value=resp) as post:
            ai_services.scan_invoice_image_ai('QUJD', mime_type='application/pdf')
        part = post.call_args.kwargs['json']['contents'][0]['parts'][1]['inline_data']
        self.assertEqual(part, {'mime_type': 'application/pdf', 'data': 'QUJD'})

    def test_photo_without_ai_and_bad_files_explain_themselves(self):
        with override_settings(ENABLE_AI_PREDICTIONS=False):
            with self.assertRaisesMessage(ImportFileError, 'مش مفعّلة'):
                extract_purchase_items(SimpleUploadedFile('a.jpg', b'x'))
        with self.assertRaisesMessage(ImportFileError, 'مش سليم'):
            extract_purchase_items(SimpleUploadedFile('a.xlsx', b'not a workbook'))
        with self.assertRaisesMessage(ImportFileError, 'مالقيناش'):
            extract_purchase_items(SimpleUploadedFile('a.csv', 'a,b\n1,2\n'.encode()))

    def test_template_round_trip_and_csv_semicolons(self):
        _v, items = extract_purchase_items(SimpleUploadedFile('t.xlsx', template_xlsx()))
        self.assertEqual(items[0], {'part_number': '04465-0D130', 'name': 'تيل فرامل أمامي',
                                    'qty': 4, 'cost': 850.0, 'sell': 1100.0})
        csv = 'كود;اسم الصنف;كمية;سعر\nP-9;مساعد;2;1.250,50\n'.encode('cp1256')
        _v, items = extract_purchase_items(SimpleUploadedFile('a.csv', csv))
        self.assertEqual((items[0]['part_number'], items[0]['name'], items[0]['cost']), ('P-9', 'مساعد', 1250.5))


@override_settings(ALLOWED_HOSTS=['*'])
class PurchaseUploadTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.vendor = make_vendor(name='شركة النور')
        self.known = make_product(part_number='90915-YZZE1', name='فلتر زيت', purchase_price='100')
        make_inventory(self.known, self.branch, quantity=0)
        self.c = Client()
        self.c.force_login(User.objects.create_user('buyer', password='x', is_staff=True, is_superuser=True))

    def _extract(self, name, data):
        r = self.c.post('/system/purchases/import/extract/', {'file': SimpleUploadedFile(name, data)},
                        HTTP_HOST=self.host)
        return r.status_code, r.json()

    def test_pdf_upload_matches_a_flipped_code_and_saves_with_sell_price(self):
        pdf = _supplier_pdf([['YZZE1-90915', 'فلتر زيت', 10, D('120'), D('1200')],
                             ['NEW-77', 'طقم دبرياج', 1, D('2500'), D('2500')]])
        code, body = self._extract('inv.pdf', pdf)
        self.assertEqual(code, 200, body)
        known, new = body['items']
        self.assertEqual((known['product_id'], known['part_number']), (self.known.pk, '90915-YZZE1'))
        self.assertIsNone(new['product_id'])
        new['sell'] = 3200
        r = self.c.post('/system/purchases/import/save/', json.dumps({
            'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk, 'items': body['items']}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        inv = PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertEqual((inv.status, inv.total_amount), ('posted', D('3700.00')))
        clutch = Product.objects.get(part_number='NEW-77')
        self.assertEqual((clutch.retail_price, clutch.purchase_price), (D('3200.00'), D('2500.00')))

    def test_unreadable_upload_returns_the_reason(self):
        code, body = self._extract('notes.csv', 'hello,world\n'.encode())
        self.assertEqual(code, 400)
        self.assertIn('مالقيناش', body['error'])
        r = self.c.get('/system/purchases/import/template.xlsx', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, template_xlsx())


@override_settings(ALLOWED_HOSTS=['*'])
class PurchaseExportTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        branch = make_branch()
        treasury = make_treasury(branch, balance='50000')
        p1 = make_product(part_number='EX-1', name='رادياتير')
        p2 = make_product(part_number='EX-2', name='طرمبة مياه')
        self.old = make_purchase_invoice(make_vendor(name='مورد قديم'), branch,
                                         items=[(p1, 2, '1000.00')])
        PurchaseInvoice.objects.filter(pk=self.old.pk).update(date_created=timezone.now() - timedelta(days=40))
        self.paid = make_purchase_invoice(make_vendor(name='مورد مدفوع', phone='0122'), branch, treasury=treasury,
                                          items=[(p2, 3, '200.00')], paid_amount='600.00')
        self.due = make_purchase_invoice(make_vendor(name='مورد آجل', phone='0133'), branch,
                                         items=[(p1, 1, '900.00'), (p2, 5, '210.00')])
        self.c = Client()
        self.c.force_login(User.objects.create_user('acc', password='x', is_staff=True, is_superuser=True))

    def _sheet(self, **params):
        import openpyxl
        r = self.c.get('/system/purchases/export/', params, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content[:300])
        ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
        rows = [list(x) for x in ws.iter_rows(values_only=True) if any(v not in (None, '') for v in x)]
        return rows[2], rows[3:-1], rows[-1]   # العنوان + الوصف ثم الجدول

    def test_summary_respects_filters_and_totals(self):
        head, rows, total = self._sheet(all='1', pay='due')
        self.assertEqual(head[:3], ['#', 'التاريخ', 'المورد'])
        self.assertEqual({r[0] for r in rows}, {self.old.pk, self.due.pk})
        self.assertEqual(total[8], 2000 + 1950)   # المتبقّي
        since = (timezone.localdate() - timedelta(days=7)).isoformat()
        _h, rows, _t = self._sheet(all='1', **{'from': since})
        self.assertEqual({r[0] for r in rows}, {self.paid.pk, self.due.pk})
        _h, rows, _t = self._sheet(ids=f'{self.paid.pk}')
        self.assertEqual([(r[0], r[6], r[10]) for r in rows], [(self.paid.pk, 600, 'خزنة اختبار')])

    def test_lines_export_and_pdf(self):
        head, rows, total = self._sheet(ids=f'{self.due.pk}', mode='lines')
        self.assertEqual(head[3:6], ['رقم القطعة', 'الصنف', 'الكمية'])
        self.assertEqual([(r[3], r[5], r[8]) for r in rows], [('EX-1', 1, 900), ('EX-2', 5, 1050)])
        self.assertEqual((total[5], total[8]), (6, 1950))
        r = self.c.get('/system/purchases/export/', {'all': '1', 'fmt': 'pdf'}, HTTP_HOST=self.host)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_list_page_filters_and_buttons(self):
        r = self.c.get('/system/purchases/', {'pay': 'paid'}, HTTP_HOST=self.host)
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn('مورد مدفوع', html)
        self.assertNotIn('مورد آجل', html)
        self.assertIn('/system/purchases/export/', html)
        self.assertIn('رفع فاتورة (Excel / PDF', html)
        r = self.c.get('/system/purchases/', {'from': '2026-13-45'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
