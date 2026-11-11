"""
🟢 لايف السوق (/superadmin/live/):
* بيعرض الموجودين دلوقتي بالاسم: عميل السوق (بالقطاع)، الشركة المشتركة، حساب سوق المطابع، والزائر المجهول.
* اللي عدّى عليه أكتر من ٥ دقايق مش أونلاين.
* «جاي منين»: فيسبوك من علامة الإعلان (fbclid) أو الـ referer — والـ middleware بيحفظ العلامة.
* أرقام النهارده لكل سوق، وتنبيه الطلبات المستنية موافقة.
* للسوبر أدمن بس.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from clients.models import Client, MarketplaceCustomer, ServiceRequest, VisitorLog
from clients.services.live_market import area_of, snapshot, source_of
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
        c = HttpClient()
        self.assertEqual(c.get('/superadmin/live/').status_code, 302)
        c.force_login(User.objects.create(username='staff_nobody', is_staff=True))
        self.assertNotEqual(c.get('/superadmin/live/').status_code, 200)
        c.force_login(User.objects.create(username='owner_live', is_staff=True, is_superuser=True))
        r = c.get('/superadmin/live/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for s in ('شركة الأفق', 'مطبعة النور الحديثة', 'مطبعة الإبداع', '197.2.*.*', 'مستني موافقتك', 'فيسبوك'):
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
