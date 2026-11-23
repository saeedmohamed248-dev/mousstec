"""
🗂️ صفحة طلبات أسعار الكتالوج بعد إعادة التصميم:
* التفاصيل اللي العميل كتبها بتتنظّم: الرقم التاني زرار اتصال/واتساب، لينك الفيسبوك زرار، والباقي رسالة نضيفة.
* الرقم المصري من غير الصفر (1099355771) بيطلع لينك واتساب صح.
* تحميل Excel (CSV بـ BOM) بيحترم الفلاتر، وأمر الشغل، وكارت الاتصال، والبحث، وتغيير الحالة بـ fetch.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client as HttpClient, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from clients.models import CatalogQuoteRequest
from clients.services import catalog_request_desk as desk
from clients.tests.test_printing_customer_flow import _TenantDomainMixin

FB_DETAILS = ('Another number for calling 01028433009\n'
              'Facebook page https://www.facebook.com/share/1HFVPeRgXx/')


class DeskParsingTests(SimpleTestCase):
    def test_extracts_extra_phone_and_social_link(self):
        c = desk.extract_contacts(FB_DETAILS, main_phone='01558383733')
        self.assertEqual(c['phones'], [{'local': '01028433009', 'wa': '201028433009'}])
        self.assertEqual(len(c['links']), 1)
        self.assertEqual(c['links'][0]['kind'], 'facebook')
        self.assertEqual(c['links'][0]['url'], 'https://www.facebook.com/share/1HFVPeRgXx/')
        self.assertNotIn('01028433009', c['note'])
        self.assertNotIn('facebook.com', c['note'])
        self.assertEqual(c['note'], '')   # «Facebook page» لوحدها من غير اللينك ملهاش لازمة
        long = desk.extract_contacts('عاوز الكروت تبقى مطفية وبحروف دهبي وكلمني 01028433009')
        self.assertIn('عاوز الكروت تبقى مطفية', long['note'])

    def test_main_phone_not_repeated_and_arabic_digits(self):
        c = desk.extract_contacts('كلمني على ٠١٥٥٨٣٨٣٧٣٣ أو 0100 123 4567', main_phone='01558383733')
        self.assertEqual([p['local'] for p in c['phones']], ['01001234567'])

    def test_wa_digits_forms(self):
        # 🐛 [FIX]: «1099355771» (من غير الصفر) كان بيعمل wa.me/1099355771 = رقم أمريكي
        for raw in ('1099355771', '01099355771', '+20 109 935 5771', '00201099355771', '٠١٠٩٩٣٥٥٧٧١'):
            self.assertEqual(desk.wa_digits(raw), '201099355771', raw)
        self.assertEqual(desk.local_phone('1099355771'), '01099355771')

    def test_age_levels_only_for_new(self):
        now = timezone.now()
        self.assertEqual(desk.age_info(now - timedelta(minutes=5), 'new', now)['level'], 'ok')
        self.assertEqual(desk.age_info(now - timedelta(hours=2), 'new', now)['level'], 'warn')
        self.assertEqual(desk.age_info(now - timedelta(hours=9), 'new', now)['level'], 'late')
        self.assertEqual(desk.age_info(now - timedelta(hours=9), 'done', now)['level'], '')

    def test_vcard_has_all_numbers(self):
        r = CatalogQuoteRequest(name='Ahmed Mahmoud', phone='01558383733', details=FB_DETAILS, item_title='كروت')
        card = desk.vcard(r)
        self.assertIn('TEL;TYPE=CELL:+201558383733', card)
        self.assertIn('TEL;TYPE=OTHER:+201028433009', card)
        self.assertIn('URL:https://www.facebook.com/share/1HFVPeRgXx/', card)


@override_settings(ALLOWED_HOSTS=['*'])
class DeskPageTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        self.q = CatalogQuoteRequest.objects.create(
            item_title='كروت بيزنس بطبعة سبوت يوفي', sector='printing', store_name='ديجيتال برنت الدقي',
            name='Ahmed Mahmoud', phone='01558383733', quantity='٢٥٠ كارت', details=FB_DETAILS,
            options={'size': '٥٫٥×٨٫٥ سم', 'color': 'أسود', 'color_hex': '#000'})
        self.other = CatalogQuoteRequest.objects.create(
            item_title='أجندة جلد', sector='printing', name='Kareem Hesham', phone='1099355771', status='contacted')
        owner = User.objects.create(username='desk_owner@test.com', is_staff=True, is_superuser=True)
        self.c = HttpClient(); self.c.force_login(owner)

    def test_page_organizes_details(self):
        html = self.c.get('/superadmin/catalog/requests/').content.decode()
        self.assertIn('href="tel:01028433009"', html)
        self.assertIn('https://wa.me/201028433009', html)
        self.assertIn('href="https://www.facebook.com/share/1HFVPeRgXx/"', html)
        self.assertIn('https://wa.me/201099355771', html)           # رقم من غير صفر اتصلّح
        self.assertIn('export=csv', html)
        self.assertIn(f'/superadmin/catalog/requests/{self.q.pk}/sheet/', html)

    def test_search_by_name_and_phone(self):
        page = self.c.get('/superadmin/catalog/requests/', {'q': 'Kareem'})
        self.assertEqual([r.pk for r in page.context['page'].object_list], [self.other.pk])
        page = self.c.get('/superadmin/catalog/requests/', {'q': '01028433009'})  # الرقم التاني في التفاصيل
        self.assertEqual([r.pk for r in page.context['page'].object_list], [self.q.pk])

    def test_csv_export_respects_filters(self):
        r = self.c.get('/superadmin/catalog/requests/', {'status': 'new', 'export': 'csv'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment;', r['Content-Disposition'])
        body = r.content.decode('utf-8')
        self.assertTrue(body.startswith('﻿'))
        self.assertIn('Ahmed Mahmoud', body)
        self.assertIn('01028433009', body)
        self.assertNotIn('Kareem', body)

    def test_sheet_for_printer_hides_customer(self):
        full = self.c.get(f'/superadmin/catalog/requests/{self.q.pk}/sheet/').content.decode()
        self.assertIn('01558383733', full)
        printer = self.c.get(f'/superadmin/catalog/requests/{self.q.pk}/sheet/?for=printer').content.decode()
        self.assertIn('كروت بيزنس', printer)
        self.assertNotIn('01558383733', printer)
        self.assertNotIn('Ahmed Mahmoud', printer)

    def test_vcard_and_owner_only(self):
        r = self.c.get(f'/superadmin/catalog/requests/{self.q.pk}/vcard/')
        self.assertEqual(r['Content-Type'], 'text/vcard; charset=utf-8')
        anon = HttpClient()
        for url in (f'/superadmin/catalog/requests/{self.q.pk}/vcard/',
                    f'/superadmin/catalog/requests/{self.q.pk}/sheet/',
                    '/superadmin/catalog/requests/?export=csv'):
            self.assertNotEqual(anon.get(url).status_code, 200, url)

    def test_quick_status_via_fetch(self):
        r = self.c.post(f'/superadmin/catalog/requests/{self.q.pk}/update/', {'status': 'done'},
                        HTTP_X_REQUESTED_WITH='fetch')
        j = r.json()
        self.assertEqual((j['ok'], j['status']), (True, 'done'))
        self.assertIn('/marketplace/catalog/review/', j['review_url'])
        self.q.refresh_from_db()
        self.assertEqual(self.q.status, 'done')
