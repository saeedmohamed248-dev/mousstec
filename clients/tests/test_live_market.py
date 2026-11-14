"""
🟢 لايف السوق (/superadmin/live/):
* بيعرض الموجودين دلوقتي بالاسم: عميل السوق (بالقطاع)، الشركة المشتركة، حساب سوق المطابع، والزائر المجهول.
* اللي عدّى عليه أكتر من ٥ دقايق مش أونلاين.
* «جاي منين»: فيسبوك من علامة الإعلان (fbclid) أو الـ referer — والـ middleware بيحفظ العلامة.
* أرقام النهارده لكل سوق، وتنبيه الطلبات المستنية موافقة.
* للسوبر أدمن بس.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from clients.models import Client, MarketplaceCustomer, ServiceRequest, VisitorLog
from clients.services.live_market import area_of, snapshot, source_of
from print_market.models import MarketAccount
from clients.tests.test_printing_customer_flow import _TenantDomainMixin


@override_settings(ALLOWED_HOSTS=['*'])
class LiveMarketTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        VisitorLog.objects.all().delete()
        now = timezone.now()
        self.cust = MarketplaceCustomer.objects.create(phone='01077700011', full_name='هشام',
                                                       company_name='شركة الأفق', sector='printing',
                                                       is_verified=True)
        self.tenant = Client(schema_name='live_printco', name='مطبعة النور الحديثة', owner_name='x',
                             phone='01000000099', industry='printing', status='active', is_active=True)
        self.tenant.auto_create_schema = False
        self.tenant.save()
        log = VisitorLog.objects.create
        log(ip_address='41.1.1.1', path='/marketplace/', marketplace_customer=self.cust, device_type='mobile',
            referer='https://m.facebook.com/')
        log(ip_address='41.1.1.1', path='/marketplace/request/abc/', marketplace_customer=self.cust,
            device_type='mobile')
        log(ip_address='10.0.0.5', path='/secure-portal/', tenant_schema='live_printco', device_type='desktop')
        log(ip_address='197.2.2.2', path='/marketplace/catalog/automotive/?fbclid', device_type='mobile')
        log(ip_address='197.3.3.3', path='/marketplace/print-market/dashboard/', device_type='mobile')
        old = log(ip_address='8.8.8.8', path='/marketplace/catalog/printing/', device_type='desktop')
        VisitorLog.objects.filter(pk=old.pk).update(timestamp=now - timedelta(minutes=20))
        from print_market.models import MarketAccount
        self.pm = MarketAccount(role='shop', name='مطبعة الإبداع', email='live@pm.test')
        self.pm.set_password('x-Strong-2026-x')
        self.pm.save()
        cache.set('pm_online_ip:197.3.3.3', self.pm.pk, 600)

    def test_helpers(self):
        self.assertEqual(area_of('/marketplace/', 'public', 'automotive'), 'automotive')
        self.assertEqual(area_of('/marketplace/print-market/x/'), 'print_market')
        self.assertEqual(area_of('/anything/', 'shop1'), 'companies')
        self.assertEqual(source_of('', '/x/?fbclid'), 'facebook')
        self.assertEqual(source_of('https://www.google.com/', '/x/'), 'google')
        self.assertEqual(source_of('', '/x/'), 'direct')

    def test_snapshot_identifies_everyone_online(self):
        snap = snapshot()
        self.assertEqual(snap['online_total'], 4)   # اللي من ٢٠ دقيقة مش محسوب
        rows = {r['who'] or r['ip_masked']: r for r in snap['online']}
        self.assertEqual(rows['شركة الأفق']['area'], 'printing')        # صفحة عامة ⇒ قطاع العميل
        self.assertEqual(rows['شركة الأفق']['pages'], 2)
        self.assertEqual(rows['شركة الأفق']['source'], 'facebook')
        self.assertEqual(rows['مطبعة النور الحديثة']['area'], 'companies')
        self.assertEqual(rows['مطبعة الإبداع']['kind'], 'pm_shop')
        self.assertEqual(rows['197.2.*.*']['area'], 'automotive')
        self.assertEqual(rows['197.2.*.*']['source'], 'facebook')
        self.assertEqual(snap['online_by_area']['printing'], 1)
        self.assertEqual(snap['today']['areas']['printing']['signups'], 1)
        self.assertEqual(snap['today']['areas']['companies']['visitors'], 1)   # الشركة بس، مش الزوار
        self.assertEqual(snap['today']['areas']['automotive']['visitors'], 1)

    def test_page_superadmin_only(self):
        ServiceRequest.objects.create(customer=self.cust, sector='printing', title='5000 كارت',
                                      description='تفاصيل الطلب', status='pending_approval',
                                      expires_at=timezone.now() + timedelta(days=3))
        from print_market.models import MarketOrder
        MarketOrder.objects.create(customer=MarketAccount.objects.create(role='customer', name='ع', email='c@live.t'),
                                   shop=self.pm, title='كروت', details='x', status='disputed')
        c = HttpClient()
        self.assertEqual(c.get('/superadmin/live/').status_code, 302)
        c.force_login(User.objects.create(username='staff_nobody', is_staff=True))
        self.assertNotEqual(c.get('/superadmin/live/').status_code, 200)
        c.force_login(User.objects.create(username='owner_live', is_staff=True, is_superuser=True))
        r = c.get('/superadmin/live/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for s in ('شركة الأفق', 'مطبعة النور الحديثة', 'مطبعة الإبداع', '197.2.*.*', 'مستني موافقتك', 'فيسبوك',
                  'سوق المطابع مستنيك', '1 مشكلة مفتوحة', 'marketorder/?status__exact=disputed'):
            self.assertIn(s, html)
        self.assertNotIn('197.2.2.2', html)   # الـ IP الكامل مابيظهرش
        self.assertNotIn('/superadmin/live/</span>', html)   # صاحب المنصة مش بيظهر كزائر
        p = c.get('/superadmin/live/?partial=1')
        self.assertNotIn('<html', p.content.decode())
        self.assertIn('شركة الأفق', p.content.decode())

    def test_background_requests_are_not_pages(self):
        VisitorLog.objects.create(ip_address='41.1.1.1', path='/marketplace/notifications/',
                                  marketplace_customer=self.cust, device_type='mobile')
        VisitorLog.objects.create(ip_address='41.1.1.1', path='/marketplace/request/create/', method='POST',
                                  marketplace_customer=self.cust, device_type='mobile')
        row = next(r for r in snapshot()['online'] if r['who'] == 'شركة الأفق')
        self.assertEqual(row['pages'], 2)                          # الصفحتين بس، مش الـ polling ولا الـ POST
        self.assertEqual(row['path'], '/marketplace/request/abc/')  # آخر صفحة فعلية

    def test_middleware_keeps_ad_marker(self):
        VisitorLog.objects.all().delete()
        HttpClient().get('/marketplace/?fbclid=abc123&x=1')
        HttpClient().get('/marketplace/?utm_source=facebook&utm_campaign=b2b')
        paths = set(VisitorLog.objects.values_list('path', flat=True))
        self.assertIn('/marketplace/?fbclid', paths)
        self.assertIn('/marketplace/?utm_source=facebook', paths)


@override_settings(ALLOWED_HOSTS=['*'])
class VisitorJourneyTests(_TenantDomainMixin, TestCase):
    """🧭 رحلة أي زائر: دخل منين وإمتى، كل صفحة والساعة كام وقعد قد إيه، عمل إيه، ووقف فين."""

    def setUp(self):
        cache.clear()
        VisitorLog.objects.all().delete()
        # ساعة ثابتة نص النهار عشان «النهارده» مايتقسمش لو الاختبار اشتغل بعد نص الليل بدقايق
        now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        patcher = mock.patch('django.utils.timezone.now', return_value=now)
        patcher.start()
        self.addCleanup(patcher.stop)
        ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit Version/17 Mobile Safari/604.1'

        def log(minutes_ago, path, method='GET', status=200, ref=''):
            o = VisitorLog.objects.create(ip_address='156.164.9.9', path=path, method=method, status_code=status,
                                          device_type='mobile', user_agent=ua, referer=ref)
            VisitorLog.objects.filter(pk=o.pk).update(timestamp=now - timedelta(minutes=minutes_ago))

        log(20, '/marketplace/catalog/printing/?fbclid', ref='https://m.facebook.com/')
        log(18, '/marketplace/notifications/')          # خلفية
        log(15, '/marketplace/register/')
        log(14, '/marketplace/register/', method='POST')
        log(12, '/marketplace/missing/', status=404)
        log(3, '/marketplace/printing/')
        self.owner = User.objects.create(username='owner_journey', is_staff=True, is_superuser=True)

    def _row(self):
        rows = snapshot(tab='today')['visitors_today']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_today_list_has_entry_exit_actions(self):
        r = self._row()
        self.assertTrue(r['is_online'])
        self.assertEqual(r['pages'], 4)
        self.assertEqual(r['actions'], 1)
        self.assertEqual(r['errors'], 1)
        self.assertEqual(r['entry_path'], '/marketplace/catalog/printing/?fbclid')
        self.assertEqual(r['path'], '/marketplace/printing/')
        self.assertEqual((r['browser'], r['os']), ('Safari', 'iOS'))
        self.assertEqual(r['source'], 'facebook')

    def test_journey_steps_and_durations(self):
        from clients.services.live_market import visitor_journey
        j = visitor_journey(self._row()['token'])
        self.assertEqual(j['totals']['sessions'], 1)
        s = j['sessions'][0]
        kinds = [st['kind'] for st in s['steps']]
        self.assertNotIn('background', kinds)               # مستخبية افتراضياً
        self.assertEqual(kinds, ['page', 'page', 'action', 'page', 'page'])
        self.assertTrue(s['steps'][0]['is_entry'])
        self.assertTrue(s['steps'][-1]['is_exit'])
        self.assertEqual(s['steps'][0]['seconds'], 5 * 60)  # الكتالوج لحد صفحة التسجيل (الخلفية مابتقطعش)
        self.assertEqual(str(s['steps'][2]['label']), 'عمل حساب جديد')
        self.assertTrue(s['steps'][3]['is_error'])
        bg = visitor_journey(self._row()['token'], show_background=True)
        self.assertIn('background', [st['kind'] for st in bg['sessions'][0]['steps']])

    def test_session_split_after_30_minutes(self):
        from clients.services.live_market import visitor_journey
        o = VisitorLog.objects.create(ip_address='156.164.9.9', path='/marketplace/', device_type='mobile',
                                      user_agent=VisitorLog.objects.first().user_agent)
        VisitorLog.objects.filter(pk=o.pk).update(timestamp=timezone.now() - timedelta(minutes=90))
        j = visitor_journey(self._row()['token'], days=2)
        self.assertEqual(j['totals']['sessions'], 2)

    def test_pages_bilingual_and_private(self):
        c = HttpClient()
        token = self._row()['token']
        self.assertEqual(c.get(f'/superadmin/live/v/{token}/').status_code, 302)   # مش مسجّل
        c.force_login(self.owner)
        r = c.get('/superadmin/live/?tab=today')
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'/superadmin/live/v/{token}/', html)
        self.assertNotIn('156.164.9.9', html)
        r = c.get(f'/superadmin/live/v/{token}/')
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        for s in ('وقف هنا', 'أول صفحة', 'عمل حساب جديد', 'خطأ 404', 'iOS'):
            self.assertIn(s, html)
        self.assertNotIn('156.164.9.9', html)
        self.assertNotIn('<html', c.get(f'/superadmin/live/v/{token}/?partial=1').content.decode())
        self.assertEqual(c.get('/superadmin/live/v/notatoken/').status_code, 404)
        c.post('/i18n/setlang/', {'language': 'en', 'next': '/'})
        html = c.get(f'/superadmin/live/v/{token}/', HTTP_ACCEPT_LANGUAGE='en').content.decode()
        for s in ('Stopped here', 'Created a new account', 'Journey step by step'):
            self.assertIn(s, html)
        html = c.get('/superadmin/live/?tab=stats', HTTP_ACCEPT_LANGUAGE='en').content.decode()
        self.assertIn('Today', html)

    def test_search_and_area_filter(self):
        self.assertEqual(len(snapshot(tab='today', area='automotive')['visitors_today']), 0)
        self.assertEqual(len(snapshot(tab='today', area='printing')['visitors_today']), 1)
        self.assertEqual(len(snapshot(tab='today', q='printing')['visitors_today']), 1)
        self.assertEqual(len(snapshot(tab='today', q='zzz')['visitors_today']), 0)
