"""
👥 استيراد العملاء والموردين بأرصدتهم الافتتاحية من Excel (صفحة المالك).

قبل كده كان من لوحة Django الفنية بس. القواعد اللي لازم تفضل صح:
* الرصيد في الملف = الرصيد النهائي، والفرق بس هو اللي يتقيّد (١١٠٠ / ٢١٠٠ قصاد ٣٠٠٥).
* رفع نفس الملف مرتين ⇒ مفيش قيد مكرر.
* «عميل نقدي» المشترك ممنوع عليه آجل.
* خانة الرصيد الفاضية ⇒ الرصيد مايتلمسش.
* أرقام Excel اللي فقدت الصفر (1012345678.0) بترجع موبايل صح.
"""
from decimal import Decimal

import tablib
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import Sum
from django.test import Client, SimpleTestCase, override_settings

from inventory.models import AccountingEntry, Customer, Vendor
from inventory.models.customers import WALK_IN_PHONE
from inventory.services.party_import import clean_phone, import_parties, parse_amount, resolve_columns
from .base import ERPTenantTestCase

D = lambda v: Decimal(str(v))  # noqa: E731


def acct(code):
    agg = AccountingEntry.objects.filter(account__code=code).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or D(0)) - (agg['c'] or D(0))


def ds(headers, rows):
    d = tablib.Dataset(headers=headers)
    for r in rows:
        d.append(r)
    return d


@override_settings(ALLOWED_HOSTS=['*'])
class PartyImportTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        AccountingEntry.objects.all().delete()
        Customer.objects.all().delete()
        Vendor.objects.all().delete()

    def test_customers_with_arabic_headers_post_opening_journal(self):
        r = import_parties('customer', ds(['اسم العميل', 'الموبايل', 'المديونية'], [
            ['أحمد علي', '1012345678.0', '1500'],
            ['شركة النور', '01198765432', '٢٬٠٠٠'],
        ]))
        self.assertEqual((r['created'], r['balances'], r['error_count']), (2, 2, 0))
        self.assertEqual(Customer.find_by_phone('01012345678').balance, D('1500'))
        self.assertEqual(acct('1100'), D('3500'))          # المدينون
        self.assertEqual(acct('3005'), D('-3500'))         # رصيد افتتاحي (دائن)
        self.assertEqual(sum(Customer.objects.values_list('balance', flat=True)), acct('1100'))

    def test_reimport_same_file_does_not_double_post_and_change_posts_delta(self):
        f = ds(['name', 'phone', 'balance'], [['أحمد', '01012345678', '1500']])
        import_parties('customer', f)
        r = import_parties('customer', f)
        self.assertEqual((r['created'], r['updated'], r['balances']), (0, 1, 0))
        self.assertEqual(acct('1100'), D('1500'))
        import_parties('customer', ds(['name', 'phone', 'balance'], [['أحمد', '01012345678', '1000']]))
        self.assertEqual(Customer.objects.get().balance, D('1000'))
        self.assertEqual(acct('1100'), D('1000'))

    def test_empty_balance_keeps_existing_balance(self):
        import_parties('customer', ds(['name', 'phone', 'balance'], [['أحمد', '01012345678', '700']]))
        import_parties('customer', ds(['name', 'phone', 'balance'], [['أحمد علي', '01012345678', '']]))
        c = Customer.objects.get()
        self.assertEqual((c.name, c.balance), ('أحمد علي', D('700')))
        self.assertEqual(acct('1100'), D('700'))

    def test_walk_in_and_missing_phone_are_rejected_rest_of_file_continues(self):
        r = import_parties('customer', ds(['name', 'phone', 'balance'], [
            ['عميل نقدي', WALK_IN_PHONE, '500'],
            ['من غير رقم', '', '300'],
            ['سليم', '01055556666', '200'],
        ]))
        self.assertEqual((r['created'], r['error_count']), (1, 2))
        self.assertIn('عميل نقدي', r['errors'][0])
        self.assertEqual(acct('1100'), D('200'))
        self.assertFalse(Customer.objects.filter(balance=D('500')).exists())

    def test_bad_amount_is_row_error(self):
        r = import_parties('customer', ds(['name', 'phone', 'balance'], [['س', '01011112222', 'كتير']]))
        self.assertEqual((r['created'], r['error_count']), (0, 1))
        self.assertEqual(acct('1100'), D('0'))

    def test_vendors_by_name_post_to_payables(self):
        r = import_parties('vendor', ds(['المورد', 'رصيد'], [['توكيل البافارية', '25000'], ['مخازن الأمل', '8000']]))
        self.assertEqual((r['created'], r['balances']), (2, 2))
        self.assertEqual(acct('2100'), D('-33000'))        # الدائنون (دائن)
        self.assertEqual(acct('3005'), D('33000'))
        import_parties('vendor', ds(['المورد', 'رصيد'], [['توكيل البافارية', '20000']]))
        self.assertEqual(Vendor.objects.count(), 2)
        self.assertEqual(acct('2100'), D('-28000'))

    def test_page_template_and_upload(self):
        u = User.objects.create_user('party_import_admin', password='x', is_superuser=True, is_staff=True)
        c = Client(HTTP_HOST=self.domain.domain)
        c.force_login(u)
        r = c.get('/system/parties/import/?kind=vendor&template=1')
        self.assertEqual(r.status_code, 200)
        self.assertIn('name,phone,balance,tax_id', r.content.decode('utf-8-sig'))
        up = SimpleUploadedFile('c.csv', 'الاسم,الموبايل,الرصيد\nأحمد,01012345678,900\n'.encode('utf-8'))
        r = c.post('/system/parties/import/', {'kind': 'customer', 'file': up})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'تم الاستيراد')
        self.assertEqual(Customer.objects.get().balance, D('900'))


class PartyImportHelpersTests(SimpleTestCase):

    def test_helpers(self):
        self.assertEqual(clean_phone('1012345678.0'), '01012345678')
        self.assertEqual(clean_phone('010-1234-5678'), '01012345678')
        self.assertEqual(parse_amount('(1,250.5)'), D('-1250.50'))
        self.assertEqual(parse_amount('١٥٠٠'), D('1500.00'))
        self.assertIsNone(parse_amount(''))
        self.assertEqual(resolve_columns(['اسم العميل', 'رقم الموبايل', 'المديونية', 'الرقم الضريبي']),
                         {'name': 0, 'phone': 1, 'balance': 2, 'tax_id': 3})
