"""
💰 العمولات من الواجهة (كانت بتتسجّل من لوحة المالك بس — فالرصيد دايماً صفر):
  • الكاشير بيختار «البائع» في نقطة البيع وأمر الشغل ⇒ عمولة المبيعات على ربح السطر.
  • المدير بيحدد الفني لكل خدمة في مراجعة البطاقة ⇒ عمولة الخدمة عند التسليم، مرة واحدة.
  • نسبة العمولة بتتعدّل من شاشة الموظف (الأدمن/المدير) — ومحدش يغيّر نسبة نفسه.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import (
    EmployeeProfile, SaleInvoice, SaleInvoiceItem, SaleInvoiceServiceItem, ServiceCatalog,
)
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_inventory, make_product, make_treasury


@override_settings(ALLOWED_HOSTS=['*'])
class CommissionFromUITests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='5000.00')
        self.part = make_product(part_number='CM-1', retail_price='1000.00', average_cost='600.00')
        make_inventory(self.part, self.branch, quantity=20)
        self.boss = User.objects.create_user('cm_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)
        self.seller, _ = make_employee('cm_seller', role='sales', branch=self.branch)
        EmployeeProfile.objects.filter(user=self.seller).update(commission_rate_pct=Decimal('10'))
        self.seller_prof = EmployeeProfile.objects.get(user=self.seller)

    def _post(self, url, payload, client=None):
        return (client or self.c).post(url, data=json.dumps(payload),
                                       content_type='application/json', HTTP_HOST=self.host)

    def _balance(self, prof):
        return EmployeeProfile.objects.get(pk=prof.pk).commission_balance

    # ── البائع في نقطة البيع ──────────────────────────────────────────
    def _pos(self, **extra):
        return self._post('/system/lightning-pos/checkout/', {
            'branch_id': self.branch.pk,
            'payments': [{'treasury_id': self.cash.pk, 'amount': 2000}],
            'items': [{'product_id': self.part.pk, 'qty': 2, 'price': 1000}], **extra})

    def test_pos_sale_credits_chosen_salesperson(self):
        r = self._pos(salesperson_id=self.seller_prof.pk)
        self.assertEqual(r.status_code, 200, r.content)
        item = SaleInvoiceItem.objects.get(invoice_id=r.json()['invoice_id'])
        self.assertEqual(item.salesperson_id, self.seller_prof.pk)
        # ربح السطر (1000−600)×2 = 800 ⇒ 10% = 80
        self.assertEqual(self._balance(self.seller_prof), Decimal('80.00'))

    def test_pos_sale_without_salesperson_credits_nobody(self):
        r = self._pos()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(SaleInvoiceItem.objects.get(invoice_id=r.json()['invoice_id']).salesperson_id)
        self.assertEqual(self._balance(self.seller_prof), Decimal('0.00'))

    def test_pos_rejects_unknown_or_inactive_salesperson(self):
        before = SaleInvoice.objects.count()
        self.assertEqual(self._pos(salesperson_id=999999).status_code, 400)
        User.objects.filter(pk=self.seller.pk).update(is_active=False)
        self.assertEqual(self._pos(salesperson_id=self.seller_prof.pk).status_code, 400)
        self.assertEqual(SaleInvoice.objects.count(), before)
        self.assertEqual(self._balance(self.seller_prof), Decimal('0.00'))

    def test_pos_screen_defaults_to_current_employee(self):
        c = Client()
        c.force_login(self.seller)
        r = c.get('/system/lightning-pos/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'<option value="{self.seller_prof.pk}" selected>')

    # ── البائع في أمر الشغل ──────────────────────────────────────────
    def test_job_card_parts_credit_chosen_salesperson(self):
        r = self._post('/system/job-card/save/', {
            'branch_id': self.branch.pk, 'invoice_type': 'sale',
            'customer_name': 'عميل عمولة', 'customer_phone': '01000000111',
            'items': [{'product_id': self.part.pk, 'qty': 1, 'price': 1000}],
            'services': [], 'salesperson_id': self.seller_prof.pk})
        self.assertEqual(r.status_code, 200, r.content)
        item = SaleInvoiceItem.objects.get(invoice_id=r.json()['invoice_id'])
        self.assertEqual(item.salesperson_id, self.seller_prof.pk)
        self.assertEqual(self._balance(self.seller_prof), Decimal('40.00'))   # 400 ربح × 10%

    # ── نسبة العمولة من شاشة الموظف ──────────────────────────────────
    def _edit(self, client, user, **over):
        prof = EmployeeProfile.objects.get(user=user)
        data = {'full_name': 'موظف عمولة', 'email': f'{user.username}@t.test', 'role': prof.role,
                'branch': str(self.branch.pk), 'is_active': 'on', 'max_discount_pct': '0'}
        data.update(over)
        return client.post(f'/system/staff/{user.pk}/edit/', data, HTTP_HOST=self.host)

    def test_admin_sets_commission_rate_from_staff_screen(self):
        self._edit(self.c, self.seller, commission_rate_pct='12.5')
        self.assertEqual(EmployeeProfile.objects.get(user=self.seller).commission_rate_pct, Decimal('12.50'))
        # قيمة غلط ⇒ النسبة القديمة تفضل
        self._edit(self.c, self.seller, commission_rate_pct='150')
        self.assertEqual(EmployeeProfile.objects.get(user=self.seller).commission_rate_pct, Decimal('12.50'))

    def test_manager_cannot_raise_own_commission_rate(self):
        manager, _ = make_employee('cm_manager', role='manager', branch=self.branch)
        mc = Client()
        mc.force_login(manager)
        self._edit(mc, manager, commission_rate_pct='50')
        self.assertEqual(EmployeeProfile.objects.get(user=manager).commission_rate_pct, Decimal('0.00'))
        # لكن يقدر يحدد نسبة موظف تاني
        self._edit(mc, self.seller, commission_rate_pct='7')
        self.assertEqual(EmployeeProfile.objects.get(user=self.seller).commission_rate_pct, Decimal('7.00'))

    # ── الفني في مراجعة البطاقة ──────────────────────────────────────
    def _maintenance_card(self):
        self.service = ServiceCatalog.objects.create(
            name='تغيير فرامل', labor_price=Decimal('500.00'), tech_commission_percent=Decimal('20'))
        r = self._post('/system/job-card/save/', {
            'branch_id': self.branch.pk, 'invoice_type': 'maintenance',
            'customer_name': 'عميل صيانة', 'customer_phone': '01000000222',
            'items': [], 'services': [{'service_id': self.service.pk, 'price': 500}]})
        self.assertEqual(r.status_code, 200, r.content)
        inv = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        return inv, SaleInvoiceServiceItem.objects.get(invoice=inv)

    def _review(self, client, inv, svc, tech_id):
        return client.post(f'/system/invoice/{inv.pk}/review/', {
            f'svc_{svc.pk}_billable': 'on', f'svc_{svc.pk}_note': '',
            f'svc_{svc.pk}_tech': str(tech_id) if tech_id else ''}, HTTP_HOST=self.host)

    def test_manager_assigns_technician_and_commission_accrues_once_on_delivery(self):
        tech, _ = make_employee('cm_tech', role='tech', branch=self.branch)
        tech_prof = EmployeeProfile.objects.get(user=tech)
        other, _ = make_employee('cm_tech2', role='tech', branch=self.branch)
        other_prof = EmployeeProfile.objects.get(user=other)
        inv, svc = self._maintenance_card()

        self.assertEqual(self._review(self.c, inv, svc, tech_prof.pk).status_code, 302)
        svc.refresh_from_db()
        self.assertEqual(svc.technician_id, tech_prof.pk)
        self.assertEqual(self._balance(tech_prof), Decimal('0.00'))   # لسه ماتسلّمتش

        inv = SaleInvoice.objects.get(pk=inv.pk)
        inv.status = 'posted'
        inv.save()
        self.assertEqual(self._balance(tech_prof), Decimal('100.00'))   # 500 × 20%

        # بعد ما العمولة اتحسبت الفني مايتغيّرش — وإلا تتدفع مرتين
        self._review(self.c, inv, svc, other_prof.pk)
        svc.refresh_from_db()
        self.assertEqual(svc.technician_id, tech_prof.pk)
        self.assertEqual(self._balance(tech_prof), Decimal('100.00'))
        self.assertEqual(self._balance(other_prof), Decimal('0.00'))

    def test_assigning_technician_after_delivery_accrues_immediately(self):
        tech, _ = make_employee('cm_tech3', role='tech', branch=self.branch)
        tech_prof = EmployeeProfile.objects.get(user=tech)
        inv, svc = self._maintenance_card()
        inv.status = 'posted'
        inv.save()
        self.assertEqual(self._balance(tech_prof), Decimal('0.00'))
        self._review(self.c, inv, svc, tech_prof.pk)
        self.assertEqual(self._balance(tech_prof), Decimal('100.00'))
        self._review(self.c, inv, svc, tech_prof.pk)   # حفظ تاني مايزوّدش
        self.assertEqual(self._balance(tech_prof), Decimal('100.00'))

    def test_accountant_review_cannot_assign_technician(self):
        tech, _ = make_employee('cm_tech4', role='tech', branch=self.branch)
        tech_prof = EmployeeProfile.objects.get(user=tech)
        acc, _ = make_employee('cm_acc', role='accountant', branch=self.branch)
        ac = Client()
        ac.force_login(acc)
        inv, svc = self._maintenance_card()
        self._review(ac, inv, svc, tech_prof.pk)
        svc.refresh_from_db()
        self.assertIsNone(svc.technician_id)
