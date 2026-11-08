"""🏪 «الزبون مايخرجش من المحل»: تسجيل أقصر، تنبيه فوري بالتسجيل، «ارفعولي بضاعتي»، وعرض آخر التجربة."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client as HttpClient, override_settings
from django.utils import timezone

from clients.models import Client, Coupon, ImportAssistRequest
from clients.services.trial_offer import offer_code, offer_for
from clients.tasks import send_trial_end_offers
from inventory.tests.base import ERPTenantTestCase

SIGNUP = {'company_name': 'مركز القمة', 'full_name': 'سعيد', 'email': 'owner@qemma.test',
          'phone': '01055551111', 'password': 'Str0ng#Pass2026', 'website_url_honeypot': '',
          'industry': 'automotive'}


@override_settings(ALLOWED_HOSTS=['*'], SUPPORT_INBOX_EMAIL='boss@mousstec.test')
class SignupFunnelTests(ERPTenantTestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.delete('signup_ip:127.0.0.1')
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain

    def owner(self):
        c = HttpClient()
        c.force_login(User.objects.create_user(f'fun_owner_{User.objects.count()}', password='x',
                                               is_staff=True, is_superuser=True))
        return c

    def test_signup_without_link_or_business_type_and_alert_email(self):
        r = HttpClient().post('/connect/signup/', SIGNUP, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content[:400])
        connection.set_schema_to_public()
        t = Client.objects.get(email='owner@qemma.test')
        self.assertEqual((t.business_type, t.business_type_confirmed), ('both', False))
        alerts = [m for m in mail.outbox if m.to == ['boss@mousstec.test']]
        self.assertEqual(len(alerts), 1)
        self.assertIn('مركز القمة', alerts[0].subject)
        self.assertIn('https://wa.me/201055551111', alerts[0].body)
        connection.set_tenant(self.tenant)

    def test_business_type_card_and_choice(self):
        Client.objects.filter(pk=self.tenant.pk).update(industry='automotive', business_type='both',
                                                        business_type_confirmed=False)
        c = self.owner()
        r = c.get('/secure-portal/', HTTP_HOST=self.host)
        self.assertContains(r, 'businessTypePrompt')
        r = c.post('/account/workspace/business-type/', {'business_type': 'parts_dealer', 'next': '/secure-portal/'},
                   HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        t = Client.objects.get(pk=self.tenant.pk)
        self.assertEqual((t.business_type, t.business_type_confirmed), ('parts_dealer', True))
        r = c.post('/account/workspace/business-type/', {'business_type': 'print_shop'}, HTTP_HOST=self.host)
        self.assertEqual(Client.objects.get(pk=self.tenant.pk).business_type, 'parts_dealer')   # من مجال تاني

    def test_import_assist_request_is_saved_and_alerts_the_owner(self):
        c = self.owner()
        f = SimpleUploadedFile('stock.xlsx', b'PK\x03\x04fake', content_type='application/vnd.ms-excel')
        r = c.post('/account/workspace/import-request/', {'file': f, 'note': 'حوالي 2000 صنف'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        connection.set_schema_to_public()
        req = ImportAssistRequest.objects.get(tenant_id=self.tenant.pk)
        self.assertEqual((req.original_name, req.note, req.status), ('stock.xlsx', 'حوالي 2000 صنف', 'new'))
        self.assertTrue([m for m in mail.outbox if m.to == ['boss@mousstec.test'] and 'رفع بضاعة' in m.subject])
        req.file.delete(save=False)
        connection.set_tenant(self.tenant)
        bad = SimpleUploadedFile('virus.exe', b'MZ', content_type='application/octet-stream')
        c.post('/account/workspace/import-request/', {'file': bad}, HTTP_HOST=self.host)
        connection.set_schema_to_public()
        self.assertEqual(ImportAssistRequest.objects.filter(tenant_id=self.tenant.pk).count(), 1)
        connection.set_tenant(self.tenant)

    def test_trial_end_offer_banner_coupon_and_email(self):
        today = timezone.localdate()
        Client.objects.filter(pk=self.tenant.pk).update(status='trial', trial_ends_at=today + timedelta(days=3),
                                                        email='owner@trial.test')
        self.tenant = Client.objects.get(pk=self.tenant.pk)
        self.assertIsNone(offer_for(self.tenant))                       # فاضل 3 أيام — لسه
        Client.objects.filter(pk=self.tenant.pk).update(trial_ends_at=today + timedelta(days=2))
        self.tenant = Client.objects.get(pk=self.tenant.pk)
        offer = offer_for(self.tenant)
        self.assertEqual((offer['days_left'], offer['percent']), (2, 20))
        self.assertIn(f"coupon={offer_code(self.tenant)}", offer['url'])
        connection.set_schema_to_public()
        coupon = Coupon.objects.get(code=offer_code(self.tenant))
        self.assertEqual((coupon.max_redemptions, coupon.new_customers_only), (1, True))
        self.assertEqual(timezone.localtime(coupon.ends_at).date(), today + timedelta(days=3))
        send_trial_end_offers()
        ours = [m for m in mail.outbox if m.to == ['owner@trial.test']]
        self.assertEqual(len(ours), 1)
        self.assertIn(coupon.code, ours[0].body)
        connection.set_tenant(self.tenant)
        r = self.owner().get('/system/dashboard/', HTTP_HOST=self.host)
        self.assertContains(r, 'trialOfferBanner')
        self.assertContains(r, 'بخصم 20%')
