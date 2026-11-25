"""
👤 «فيه موظف مسجّل بنفس الإيميل ده بالفعل» كانت بتقف هنا من غير ما تقول مين ولا تعمل إيه.
دلوقتي الصفحة بتعرض الموظف الموجود + حالته + زرار يبعتله الدعوة تاني / يفتح بياناته.
"""
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from .base import ERPTenantTestCase
from .factories import make_branch, make_employee


@override_settings(ALLOWED_HOSTS=['*'])
class AddEmployeeExistingEmailTests(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(max_users=0)
        self.tenant.max_users = 0
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.main = make_branch('رئيسي')
        self.boss, _ = make_employee('boss_x', role='admin', branch=self.main, email='boss@shop.test')
        self.c = Client()
        self.c.force_login(self.boss)

    def _add(self, email):
        r = self.c.post('/system/staff/add/', {'full_name': 'Hossam Maged', 'email': email,
                                                'role': 'cashier'}, HTTP_HOST=self.host)
        connection.set_tenant(self.tenant)
        return r

    def test_pending_employee_shows_resend_and_edit(self):
        self.assertTrue(User.objects.filter(email='hossam@yahoo.com').exists() is False)
        self._add('hossam@yahoo.com')                       # أول مرة ⇒ اتضاف ولسه مافعّلش
        r = self._add('Hossam@Yahoo.com')                    # تاني مرة ⇒ موجود
        html = r.content.decode()
        self.assertEqual(User.objects.filter(email__iexact='hossam@yahoo.com').count(), 1)
        emp = User.objects.get(email='hossam@yahoo.com')
        self.assertEqual(r.context['existing']['tone'], 'pending')
        self.assertIn('data-testid="existing-employee"', html)
        self.assertIn(f'/system/staff/{emp.pk}/edit/', html)
        self.assertIn('value="resend"', html)

    def test_deactivated_and_self(self):
        old, _ = make_employee('old_x', role='cashier', branch=self.main, email='old@shop.test')
        old.is_active = False
        old.save(update_fields=['is_active'])
        r = self._add('old@shop.test')
        self.assertEqual(r.context['existing']['tone'], 'off')
        self.assertNotIn('value="resend"', r.content.decode())
        r = self._add('boss@shop.test')
        self.assertEqual(r.context['existing']['tone'], 'self')
        self.assertFalse(r.context['existing']['can_manage'])
