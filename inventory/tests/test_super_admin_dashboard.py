"""
👑 لوحة المشرف الأعلى (/superadmin/) — كل زرار ورابط في الصفحة لازم يشتغل.

* الصفحة بتفتح، وكل رابط داخلي فيها (الأدوات + روابط التابات + 360° لكل شركة)
  بيرجع صفحة سليمة مش 404/500، وكل endpoint بيندَه من JavaScript موجود.
* إيصال فودافون كاش اللي العميل لسه مارفعش فيه حاجة مش بيتعد في «محتاج قرارك»
  (كان بيظهر بزرار «تأكيد» مقفول من غير سبب).
* 💸 سعر مودال «تفعيل اشتراك» = نفس إجمالي الفاتورة اللي بتتسجّل (كان 3 باقات سيارات
  بأسعار وخصومات مكتوبة في القالب، والفاتورة بتتحسب من الـ DB + الإضافات).
* بعد أي إجراء على شركة الصفحة بترجع على تاب الشركات (#tenants).
* «موافقة ونشر» قطعة غيار من اللوحة بيرجع JSON (AJAX) بدل ما يوديك لصفحة تانية.
"""
import json
import re
from decimal import Decimal
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.urls import Resolver404, resolve

from clients.models import Client, Domain, ManualPaymentReceipt, Plan, PlatformInvoice
from .base import ERPTenantTestCase

PLATFORM_HOST = 'platform-sa.test.com'

# روابط مقصود إنها مش صفحات عادية: الدخول على شركة بيحوّل لدومين الشركة.
_SKIP_PREFIXES = ('/superadmin/enter/',)
_PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
        b'\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


@override_settings(ALLOWED_HOSTS=['*'])
class SuperAdminDashboardTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_schema_to_public()
        pub = Client.objects.filter(schema_name='public').first()
        if pub is None:
            pub = Client(schema_name='public', name='Platform', owner_name='o', phone='0')
            pub.auto_create_schema = False
            pub.save()
        Domain.objects.get_or_create(domain=PLATFORM_HOST, defaults={'tenant': pub, 'is_primary': False})
        self.owner, _ = User.objects.get_or_create(
            username='sa_owner@test.com', defaults={'is_staff': True, 'is_superuser': True})
        self.c = HttpClient()
        self.c.force_login(self.owner)
        # نفس slugs الإنتاج (المهاجرات بتعملها) — مش قيم Client.plan القديمة silver/gold
        self.silver, _ = Plan.objects.get_or_create(
            slug='auto-silver', defaults={'name': 'Silver', 'industry': 'automotive',
                                          'monthly_price': Decimal('550.00'), 'is_active': True})
        self.gold, _ = Plan.objects.get_or_create(
            slug='auto-gold', defaults={'name': 'Gold', 'industry': 'automotive',
                                        'monthly_price': Decimal('850.00'), 'is_active': True})
        self.print_plan, _ = Plan.objects.get_or_create(
            slug='print-starter', defaults={'name': 'Print Starter', 'industry': 'printing',
                                            'monthly_price': Decimal('875.00'), 'is_active': True})
        Plan.objects.filter(pk__in=[self.silver.pk, self.gold.pk, self.print_plan.pk]).update(is_active=True)
        self.tenant.refresh_from_db()

    def tearDown(self):
        # ⚠️ TransactionTestCase بيفضّي جداول السكيمة الحالية — لازم نرجع لسكيمة الشركة
        connection.set_tenant(self.tenant)
        super().tearDown()

    def _get(self, url):
        return self.c.get(url, HTTP_HOST=PLATFORM_HOST)

    def _post(self, url, data=None, **extra):
        return self.c.post(url, data or {}, HTTP_HOST=PLATFORM_HOST, **extra)

    # ── الروابط والأزرار ─────────────────────────────────────────────
    def test_every_internal_link_on_the_dashboard_works(self):
        r = self._get('/superadmin/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        links = sorted({
            h for h in re.findall(r'href="(/[^"#{}\'+]*)"', html)
            if not h.startswith(('/static/', '/media/')) and not h.startswith(_SKIP_PREFIXES)
        })
        self.assertIn(f'/superadmin/tenant/{self.tenant.pk}/360/', links)
        self.assertIn('/superadmin/promotions/', links)      # أدوات كانت في البحث بس ومالهاش زرار
        broken = [(h, s) for h in links if (s := self._get(h).status_code) >= 400]
        self.assertEqual(broken, [], f'روابط بايظة في لوحة المشرف: {broken}')

    def test_every_javascript_endpoint_exists(self):
        html = self._get('/superadmin/').content.decode()
        urls = set(re.findall(r"fetch\(\s*[`'\"](/[^`'\"]+)[`'\"]", html))
        self.assertIn('/superadmin/parts/moderation/${id}/approve/', urls)
        missing = []
        for raw in urls:
            candidates = [urlsplit(re.sub(r'\$\{[^}]+\}', sub, raw)).path
                          for sub in ('1', '00000000-0000-0000-0000-000000000001')]
            for path in candidates:
                try:
                    resolve(path)
                    break
                except Resolver404:
                    continue
            else:
                missing.append(raw)
        self.assertEqual(missing, [], f'endpoints مش موجودة: {missing}')

    def test_tenant_action_returns_to_the_tenants_tab(self):
        r = self._post('/superadmin/', {'action': 'unflag_fraud', 'tenant_id': self.tenant.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], '/superadmin/#tenants')

    def test_trial_extension_label_matches_the_real_days(self):
        from clients.models.tenancy import TRIAL_DAYS
        Client.all_objects.filter(pk=self.tenant.pk).update(status='trial')
        html = self._get('/superadmin/').content.decode()
        self.assertIn(f'+{TRIAL_DAYS} يوم', html)
        self.assertNotIn('تمديد +7 أيام', html)

    # ── إيصالات فودافون كاش ─────────────────────────────────────────
    def test_receipts_without_upload_are_not_counted_as_waiting(self):
        uploaded = ManualPaymentReceipt(purchase_type='design', purchase_id=1, amount=Decimal('100'),
                                        sender_phone='01099998888', txn_reference='TXN12345')
        uploaded.receipt_image.save('r.png', ContentFile(_PNG), save=True)
        empty = ManualPaymentReceipt.objects.create(purchase_type='design', purchase_id=2, amount=Decimal('50'),
                                                    sender_phone='', txn_reference='')
        self.addCleanup(lambda: ManualPaymentReceipt.objects.filter(pk__in=[uploaded.pk, empty.pk]).delete())
        r = self._get('/superadmin/')
        ctx = r.context
        self.assertEqual(ctx['uploaded_receipts_count'], 1)
        self.assertEqual([x.pk for x in ctx['pending_manual_receipts']], [uploaded.pk])
        self.assertEqual([x.pk for x in ctx['unsubmitted_receipts']], [empty.pk])
        html = r.content.decode()
        self.assertIn('إيصالات دفع مستنية تأكيد', html)
        # زرار التأكيد مابقاش متعطّل لإيصال جاهز
        self.assertNotRegex(html, r"reviewReceipt\('[^']+', 'confirm'\)\"\s+disabled")

    # ── 💸 مودال تفعيل الاشتراك = الفاتورة ───────────────────────────
    def test_subscription_modal_price_equals_the_issued_invoice(self):
        Client.all_objects.filter(pk=self.tenant.pk).update(extra_users_purchased=2, plan='gold')
        self.tenant.refresh_from_db()
        r = self._get('/superadmin/')
        plans = {p['slug']: p for p in r.context['sub_plans']}
        self.assertIn('print-starter', plans)                 # باقات الطباعة بقت موجودة
        self.assertEqual(plans['print-starter']['industry'], 'printing')
        row = next(t for t in r.context['tenants'] if t['obj'].pk == self.tenant.pk)
        # نفس الحساب اللي الـ JS بيعمله: سعر الباقة للمدة + الإضافات × الشهور
        shown = Decimal(str(plans['auto-silver']['prices']['3'])) + row['addons_monthly'] * 3
        self.assertGreater(row['addons_monthly'], 0)
        self.assertEqual(Decimal(str(plans['auto-silver']['prices']['3'])), self.silver.price_for_period(3))
        self.assertEqual(row['plan_slug'], 'auto-gold')        # Client.plan='gold' ⇒ باقة الـ DB

        self._post('/superadmin/', {'action': 'activate_subscription', 'tenant_id': self.tenant.pk,
                                    'plan': 'auto-silver', 'billing_period': 'quarterly', 'payment_mode': 'paid'})
        inv = PlatformInvoice.objects.filter(tenant=self.tenant).order_by('-pk').first()
        self.assertEqual(inv.total, shown.quantize(Decimal('0.01')))
        self.assertEqual(inv.billing_cycle_months, 3)
        self.assertIn('data-plan="print-starter"', r.content.decode())

    def test_activate_and_renew_accept_the_legacy_plan_value(self):
        """زرار «اشتراك» القديم كان بيبعت 'silver' والباقة اسمها 'auto-silver' ⇒ عمره ما اشتغل."""
        from clients.models import TenantSubscription
        before = PlatformInvoice.objects.filter(tenant=self.tenant).count()
        self._post('/superadmin/', {'action': 'activate_subscription', 'tenant_id': self.tenant.pk,
                                    'plan': 'silver', 'billing_period': 'monthly', 'payment_mode': 'gift'})
        inv = PlatformInvoice.objects.filter(tenant=self.tenant).order_by('-pk').first()
        self.assertEqual(PlatformInvoice.objects.filter(tenant=self.tenant).count(), before + 1)
        self.assertEqual((inv.total, inv.plan_revision.plan_id), (Decimal('0.00'), self.silver.pk))
        # تجديد شركة اشتراكها مش مربوط بباقة — بيدوّر بـ Client.plan ('gold' ⇒ auto-gold)
        TenantSubscription.objects.filter(tenant=self.tenant).update(plan=None)
        Client.all_objects.filter(pk=self.tenant.pk).update(plan='gold')
        r = self._post('/superadmin/', {'action': 'renew_subscription', 'tenant_id': self.tenant.pk})
        self.assertEqual(r['Location'], '/superadmin/#tenants')
        inv = PlatformInvoice.objects.filter(tenant=self.tenant).order_by('-pk').first()
        self.assertEqual(inv.plan_revision.plan_id, self.gold.pk)
        self.assertEqual(inv.total, self.gold.price_for_period(1))

    # ── مراجعة قطع الغيار من اللوحة ─────────────────────────────────
    def test_part_listing_approve_answers_ajax_with_json(self):
        from clients.models import PartCarMake, PartListing
        make, _ = PartCarMake.objects.get_or_create(slug='sa-test-make', defaults={'name': 'SA Test Make'})
        listing = PartListing.objects.create(title='فانوس أمامي', description='سليم', car_make=make,
                                             price_egp=Decimal('900'), moderation_status='pending_approval',
                                             seller_tenant=self.tenant)
        # جداول المنصة (public) مابتتفضّاش بين الاختبارات — نمسح اللي عملناه
        self.addCleanup(lambda: (PartListing._base_manager.filter(car_make=make).delete(), make.delete()))
        html = self._get('/superadmin/').content.decode()
        self.assertIn(f'approvePartListing({listing.pk}, this)', html)
        r = self._post(f'/superadmin/parts/moderation/{listing.pk}/approve/', HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(r.status_code, 200)
        self.assertIn('message', r.json())
        listing.refresh_from_db()
        self.assertEqual(listing.moderation_status, 'approved')
        # الطلب العادي (من صفحة المراجعة الكاملة) لسه بيحوّل زي الأول
        listing2 = PartListing.objects.create(title='مراية', description='x', car_make=make,
                                              price_egp=Decimal('300'), moderation_status='pending_approval',
                                              seller_tenant=self.tenant)
        r = self._post(f'/superadmin/parts/moderation/{listing2.pk}/approve/')
        self.assertEqual(r.status_code, 302)

    def test_dashboard_page_is_valid_json_for_the_modal(self):
        html = self._get('/superadmin/').content.decode()
        m = re.search(r'<script id="sub-plans-data" type="application/json">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))
        self.assertTrue(all(set(p['prices']) == {'1', '3', '6', '12'} for p in data))
