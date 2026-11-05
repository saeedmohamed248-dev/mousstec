"""
⚠️ أيام السماح بعد انتهاء الاشتراك: لوحة الشركة كانت بتقول «باقي 0 أيام على انتهاء اشتراكك»
ومفيش أي تنبيه إن الدخول هيقف بعد ٣ أيام — دلوقتي «اشتراكك خلص — باقي N يوم سماح».
"""
from datetime import timedelta

from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.utils import get_tenant_model

from .base import ERPTenantTestCase
from .factories import make_branch, make_employee


@override_settings(ALLOWED_HOSTS=['*'])
class DashboardGraceBannerTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.user, _ = make_employee('grace_admin', role='admin', branch=self.branch)
        self.c = Client(HTTP_HOST=self.domain.domain)
        self.c.force_login(self.user)

    def _dashboard(self, end_offset):
        get_tenant_model().objects.filter(pk=self.tenant.pk).update(
            status='active', is_active=True,
            subscription_end_date=timezone.localdate() + timedelta(days=end_offset))
        r = self.c.get('/system/dashboard/')
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_expired_yesterday_shows_grace_days_left(self):
        html = self._dashboard(-1)
        self.assertIn('اشتراكك خلص', html)
        self.assertIn('باقي 2 يوم سماح', html)
        self.assertNotIn('باقي 0 أيام على انتهاء اشتراكك', html)

    def test_last_grace_day(self):
        self.assertIn('النهارده آخر يوم سماح', self._dashboard(-3))

    def test_ends_today(self):
        html = self._dashboard(0)
        self.assertIn('اشتراكك بينتهي النهارده', html)
        self.assertNotIn('اشتراكك خلص', html)

    def test_still_running(self):
        html = self._dashboard(5)
        self.assertIn('باقي 5 أيام على انتهاء اشتراكك', html)
