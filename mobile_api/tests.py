"""
📱 Mobile API — Tests

اختبارات شاملة تعمل داخل tenant حقيقي (django-tenants) وتستخدم مصانع بيانات
inventory القائمة. تغطّي: المصادقة، لوحة المعلومات، أوامر الشغل، المخزون،
تنبيهات النقص، والعملاء — بالإضافة إلى حماية المسارات وعزل الحقول الحسّاسة.

التشغيل:
    python manage.py test mobile_api
"""
from decimal import Decimal

from django.db import connection
from rest_framework.test import APIClient

from inventory.tests.base import ERPTenantTestCase
from inventory.tests import factories as f
from inventory.models import StockAlert, Vehicle


class MobileApiTestBase(ERPTenantTestCase):
    """يهيّئ مستخدماً + بيانات أساسية ويصادق عبر JWT داخل الـ tenant."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        # نمرّر HTTP_HOST بنطاق الـ tenant حتى يوجّه django-tenants للـ schema الصحيح.
        self._host = self.domain.domain
        self.user = f.make_user(username='tech1', password='pass12345')
        self.branch = f.make_branch(name='الفرع الرئيسي')

    def authenticate(self):
        resp = self.client.post(
            '/api/mobile/v1/auth/login/',
            {'username': 'tech1', 'password': 'pass12345'},
            format='json', HTTP_HOST=self._host,
        )
        assert resp.status_code == 200, resp.content
        token = resp.data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return resp

    def get(self, url, **kwargs):
        return self.client.get(url, HTTP_HOST=self._host, **kwargs)

    def post(self, url, data=None, **kwargs):
        return self.client.post(url, data or {}, format='json', HTTP_HOST=self._host, **kwargs)


class AuthTests(MobileApiTestBase):
    def test_login_returns_tokens_and_user(self):
        resp = self.post(
            '/api/mobile/v1/auth/login/',
            {'username': 'tech1', 'password': 'pass12345'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('access', resp.data)
        self.assertIn('refresh', resp.data)
        self.assertEqual(resp.data['user']['username'], 'tech1')

    def test_login_wrong_password_rejected(self):
        resp = self.post(
            '/api/mobile/v1/auth/login/',
            {'username': 'tech1', 'password': 'WRONG'},
        )
        self.assertEqual(resp.status_code, 401)

    def test_me_requires_auth(self):
        resp = self.get('/api/mobile/v1/auth/me/')
        self.assertEqual(resp.status_code, 401)

    def test_me_returns_current_user(self):
        self.authenticate()
        resp = self.get('/api/mobile/v1/auth/me/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['username'], 'tech1')


class DashboardTests(MobileApiTestBase):
    def test_dashboard_counts(self):
        customer = f.make_customer()
        product = f.make_product()
        f.make_inventory(product, self.branch, quantity=1)
        f.make_sale_invoice(
            customer, self.branch, items=[(product, 1, '100.00')],
            status='in_progress', invoice_type='maintenance',
        )
        StockAlert.objects.create(
            product=product, branch=self.branch, alert_type='low_stock',
            current_quantity=1, min_stock_level=2,
        )
        self.authenticate()
        resp = self.get('/api/mobile/v1/dashboard/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['open_work_orders'], 1)
        self.assertEqual(resp.data['in_progress'], 1)
        self.assertEqual(resp.data['low_stock_alerts'], 1)
        self.assertEqual(resp.data['total_customers'], 1)


class WorkOrderTests(MobileApiTestBase):
    def _make_order(self, status='quotation'):
        customer = f.make_customer()
        product = f.make_product()
        f.make_inventory(product, self.branch, quantity=5)
        return f.make_sale_invoice(
            customer, self.branch, items=[(product, 1, '100.00')],
            status=status, invoice_type='maintenance',
        )

    def test_list_only_maintenance_orders(self):
        self._make_order()
        # فاتورة بيع عادية يجب ألا تظهر في أوامر الشغل.
        customer = f.make_customer(name='بيع', phone='01099999999')
        product = f.make_product(part_number='SALE-1')
        f.make_inventory(product, self.branch, quantity=5)
        f.make_sale_invoice(
            customer, self.branch, items=[(product, 1, '50.00')],
            status='quotation', invoice_type='sale',
        )
        self.authenticate()
        resp = self.get('/api/mobile/v1/work-orders/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 1)

    def test_filter_open_excludes_posted(self):
        self._make_order(status='posted')
        self._make_order(status='in_progress')
        self.authenticate()
        resp = self.get('/api/mobile/v1/work-orders/?status=open')
        self.assertEqual(resp.data['count'], 1)

    def test_update_status(self):
        order = self._make_order(status='in_progress')
        self.authenticate()
        resp = self.post(f'/api/mobile/v1/work-orders/{order.id}/status/', {'status': 'ready'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'ready')
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

    def test_update_status_rejects_invalid(self):
        order = self._make_order()
        self.authenticate()
        resp = self.post(f'/api/mobile/v1/work-orders/{order.id}/status/', {'status': 'flying'})
        self.assertEqual(resp.status_code, 400)

    def test_detail_hides_cost_fields(self):
        order = self._make_order()
        self.authenticate()
        resp = self.get(f'/api/mobile/v1/work-orders/{order.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('total_cost', resp.data)
        self.assertNotIn('net_profit', resp.data)


class InventoryTests(MobileApiTestBase):
    def test_list_and_total_quantity(self):
        product = f.make_product(name='فلتر زيت')
        f.make_inventory(product, self.branch, quantity=7)
        self.authenticate()
        resp = self.get('/api/mobile/v1/products/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(resp.data['results'][0]['total_quantity'], 7)

    def test_search(self):
        f.make_inventory(f.make_product(name='طلمبة مياه', part_number='WP-1'), self.branch, 3)
        f.make_inventory(f.make_product(name='دبرياج', part_number='CL-1'), self.branch, 3)
        self.authenticate()
        resp = self.get('/api/mobile/v1/products/?search=طلمبة')
        self.assertEqual(resp.data['count'], 1)

    def test_low_stock_endpoint(self):
        low = f.make_product(name='قليل', part_number='LOW-1', min_stock_level=5)
        f.make_inventory(low, self.branch, quantity=1)
        ok = f.make_product(name='كافي', part_number='OK-1', min_stock_level=2)
        f.make_inventory(ok, self.branch, quantity=50)
        self.authenticate()
        resp = self.get('/api/mobile/v1/products/low-stock/')
        self.assertEqual(resp.status_code, 200)
        names = [p['name'] for p in resp.data['results']]
        self.assertIn('قليل', names)
        self.assertNotIn('كافي', names)


class CustomerTests(MobileApiTestBase):
    def test_customer_detail_includes_vehicles(self):
        customer = f.make_customer(name='أحمد', phone='01212121212')
        Vehicle.objects.create(
            customer=customer, chassis_number='WBА12345678901234'[:17],
            car_plate='ن ص ر 123', brand='BMW', model_name='320i',
        )
        self.authenticate()
        resp = self.get(f'/api/mobile/v1/customers/{customer.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['vehicles']), 1)


class CreateFlowTests(MobileApiTestBase):
    def test_create_customer(self):
        self.authenticate()
        resp = self.post('/api/mobile/v1/customers/', {
            'name': 'عميل جديد', 'phone': '01234567890', 'is_b2b_company': False,
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['name'], 'عميل جديد')

    def test_create_work_order_defaults_to_maintenance(self):
        customer = f.make_customer()
        self.authenticate()
        resp = self.post('/api/mobile/v1/work-orders/', {
            'customer': customer.id, 'branch': self.branch.id, 'notes': 'صوت غريب بالموتور',
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        # يظهر في قائمة أوامر الصيانة.
        listed = self.get('/api/mobile/v1/work-orders/')
        self.assertEqual(listed.data['count'], 1)

    def test_record_cash_transaction(self):
        treasury = f.make_treasury(self.branch)
        self.authenticate()
        resp = self.post('/api/mobile/v1/transactions/', {
            'treasury': treasury.id, 'transaction_type': 'in',
            'amount': '250.00', 'description': 'دفعة عميل',
        })
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_reject_non_positive_amount(self):
        treasury = f.make_treasury(self.branch)
        self.authenticate()
        resp = self.post('/api/mobile/v1/transactions/', {
            'treasury': treasury.id, 'transaction_type': 'in', 'amount': '0',
        })
        self.assertEqual(resp.status_code, 400)


class ModuleListingTests(MobileApiTestBase):
    """كل الموديولات الجديدة تُرجع 200 وقائمة مُرقّمة للمستخدم المصادَق."""

    def test_all_module_endpoints_ok(self):
        f.make_vendor()
        f.make_treasury(self.branch)
        self.authenticate()
        for url in [
            '/api/mobile/v1/vendors/',
            '/api/mobile/v1/purchase-invoices/',
            '/api/mobile/v1/services/',
            '/api/mobile/v1/treasuries/',
            '/api/mobile/v1/transactions/',
            '/api/mobile/v1/branches/',
            '/api/mobile/v1/vehicles/',
            '/api/mobile/v1/employees/',
            '/api/mobile/v1/attendance/',
            '/api/mobile/v1/leave-requests/',
            '/api/mobile/v1/advances/',
            '/api/mobile/v1/payroll-runs/',
            '/api/mobile/v1/fault-logs/',
            '/api/mobile/v1/diag-scans/',
            '/api/mobile/v1/stock-transfers/',
            '/api/mobile/v1/inventory-movements/',
        ]:
            resp = self.get(url)
            self.assertEqual(resp.status_code, 200, f'{url} -> {resp.status_code}: {resp.content[:200]}')
            self.assertIn('results', resp.data)


class AnalyticsTests(MobileApiTestBase):
    def test_analytics_shape(self):
        customer = f.make_customer()
        product = f.make_product()
        f.make_inventory(product, self.branch, quantity=10)
        f.make_sale_invoice(
            customer, self.branch, items=[(product, 2, '100.00')],
            status='posted', invoice_type='sale',
        )
        self.authenticate()
        resp = self.get('/api/mobile/v1/analytics/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data['revenue_last_7_days']), 7)
        self.assertIn('work_order_status', resp.data)
        self.assertIn('top_products', resp.data)


class HrWorkflowTests(MobileApiTestBase):
    def _employee(self):
        user = f.make_user(username='emp_hr', password='x')
        from hr.models import Employee
        return Employee.objects.create(user=user, employee_id='E-100', hire_date='2024-01-01')

    def test_approve_leave_request(self):
        emp = self._employee()
        from hr.models import LeaveRequest
        leave = LeaveRequest.objects.create(
            employee=emp, leave_type='annual', from_date='2026-09-10', to_date='2026-09-12',
        )
        self.authenticate()
        resp = self.post(f'/api/mobile/v1/leave-requests/{leave.id}/approve/', {})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['status'], 'approved')

    def test_reject_advance_with_reason(self):
        emp = self._employee()
        from hr.models import Advance
        adv = Advance.objects.create(employee=emp, amount='500.00')
        self.authenticate()
        resp = self.post(f'/api/mobile/v1/advances/{adv.id}/reject/', {'notes': 'الرصيد لا يسمح'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['status'], 'rejected')


class SecurityTests(MobileApiTestBase):
    def test_all_resource_endpoints_require_auth(self):
        for url in [
            '/api/mobile/v1/dashboard/',
            '/api/mobile/v1/work-orders/',
            '/api/mobile/v1/products/',
            '/api/mobile/v1/stock-alerts/',
            '/api/mobile/v1/customers/',
            '/api/mobile/v1/treasuries/',
            '/api/mobile/v1/employees/',
            '/api/mobile/v1/vendors/',
        ]:
            resp = self.get(url)
            self.assertEqual(resp.status_code, 401, f'{url} should require auth')
