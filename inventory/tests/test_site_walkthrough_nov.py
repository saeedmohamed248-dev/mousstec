"""🧭 تصليحات جولة «جرّب الموقع كأنك عميل» (نوفمبر):

* أمر الشراء: أمر فاضي + تعديل أصناف المسودة (إضافة/كمية/سعر/شيل) + مايتبعتش فاضي.
* كتالوج الخدمات والمصنعيات من البورتال (كان من لوحة Django بس).
* إلغاء طلب تسعير (RFQ) من برج التحكم.
* «شراء X · مصاريف Y» في قايمة الأصناف بقى من مصاريف الاستلامات اللي لسه في الرصيد.
* بوت تذكير الصيانة وبوت الترحيب بقوا بيبعتوا فعلاً (كانوا بيكتبوا في اللوج بس).
* هدية التسجيل اتمنحت مرتين (١٣ تصميم بدل ١٠) + صفحة /login/ للي داخل بالفعل.
* صنف/عربية من غير ماركة كانت بتتسجّل BMW.
* رابط البروفة في واتساب كان من غير دومين.
"""
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import (
    RFQ, Product, PurchaseOrder, PurchaseOrderItem, SaleInvoice, SaleInvoiceServiceItem,
    ServiceCatalog, Vehicle,
)
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_employee, make_product, make_treasury, make_vendor,
)


@override_settings(ALLOWED_HOSTS=['*'])
class WalkthroughFixesTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='50000.00')
        self.vendor = make_vendor()
        self.plug = make_product(part_number='WK-PLUG', name='بوجيه', retail_price='210.00',
                                 average_cost='120.00', purchase_price='120.00')
        self.oil = make_product(part_number='WK-OIL', name='زيت', retail_price='300.00',
                                average_cost='0', purchase_price='0')
        Product.objects.filter(pk=self.oil.pk).update(barcode='6221234567890')
        self.boss = User.objects.create_user('wk_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)

    def get(self, url, client=None, **kw):
        return (client or self.c).get(url, HTTP_HOST=self.host, **kw)

    def post(self, url, data=None, client=None, **kw):
        return (client or self.c).post(url, data or {}, HTTP_HOST=self.host, **kw)

    def as_role(self, role, username):
        user, _profile = make_employee(username=username, role=role, branch=self.branch)
        c = Client()
        c.force_login(user)
        return c

    # ── 📝 أمر الشراء ────────────────────────────────────────────────────
    def _empty_po(self):
        r = self.post(reverse('inventory:purchase_order_create'),
                      {'vendor': self.vendor.pk, 'branch': self.branch.pk, 'empty': '1'})
        po = PurchaseOrder.objects.latest('pk')
        self.assertEqual(r['Location'], reverse('inventory:purchase_order_detail', args=[po.pk]))
        return po

    def test_empty_po_then_add_edit_and_remove_lines(self):
        po = self._empty_po()
        self.assertEqual((po.status, po.items.count()), ('draft', 0))
        lines = reverse('inventory:purchase_order_lines', args=[po.pk])

        # إضافة برقم القطعة (من غير سعر ⇒ آخر سعر شراء) وبالباركود (بسعر)
        self.post(lines, {'new_code': 'wk-plug', 'new_qty': '5'})
        self.post(lines, {'new_code': '6221234567890', 'new_qty': '3', 'new_cost': '150'})
        # نفس الصنف تاني ⇒ الكمية بتزيد مش سطر مكرر
        self.post(lines, {'new_code': 'WK-PLUG', 'new_qty': '2'})
        plug = po.items.get(product=self.plug)
        oil = po.items.get(product=self.oil)
        self.assertEqual((plug.quantity, plug.unit_cost), (7, Decimal('120.00')))
        self.assertEqual((oil.quantity, oil.unit_cost), (3, Decimal('150.00')))
        self.assertEqual(po.items.count(), 2)

        # تعديل الكمية والسعر
        self.post(lines, {f'qty_{plug.pk}': '8', f'cost_{plug.pk}': '118.50',
                          f'qty_{oil.pk}': '3', f'cost_{oil.pk}': '150'})
        plug.refresh_from_db()
        self.assertEqual((plug.quantity, plug.unit_cost), (8, Decimal('118.50')))
        self.assertEqual(po.total, Decimal('8') * Decimal('118.50') + Decimal('450'))

        # شيل سطر
        self.post(lines, {'remove': str(oil.pk)})
        self.assertEqual(list(po.items.values_list('product_id', flat=True)), [self.plug.pk])

        # الصفحة بتعرض فورم التعديل للمسودة
        r = self.get(reverse('inventory:purchase_order_detail', args=[po.pk]))
        self.assertContains(r, f'name="qty_{plug.pk}"')
        self.assertContains(r, 'name="new_code"')

    def test_po_lines_reject_bad_input_without_saving(self):
        po = self._empty_po()
        item = PurchaseOrderItem.objects.create(order=po, product=self.plug, quantity=2, unit_cost=Decimal('100'))
        lines = reverse('inventory:purchase_order_lines', args=[po.pk])
        for data, err in (({'new_code': 'NOPE'}, 'err=product'),
                          ({f'qty_{item.pk}': '0'}, 'err=number'),
                          ({f'cost_{item.pk}': '-1'}, 'err=number'),
                          ({f'cost_{item.pk}': 'NaN'}, 'err=number'),
                          ({f'cost_{item.pk}': '1e20'}, 'err=number'),      # كان بيوقّع الحفظ 500
                          ({'new_code': 'WK-OIL', 'new_qty': '5000000'}, 'err=number')):
            r = self.post(lines, data)
            self.assertIn(err, r['Location'], data)
        item.refresh_from_db()
        self.assertEqual((item.quantity, item.unit_cost), (2, Decimal('100.00')))
        self.assertEqual(po.items.count(), 1)

        # سطر سليم + سطر غلط في نفس الحفظ ⇒ ولا حاجة تتحفظ (مش نص الأمر)
        other = PurchaseOrderItem.objects.create(order=po, product=self.oil, quantity=1, unit_cost=Decimal('50'))
        r = self.post(lines, {f'qty_{item.pk}': '9', f'qty_{other.pk}': '-3'})
        self.assertIn('err=number', r['Location'])
        item.refresh_from_db()
        self.assertEqual(item.quantity, 2)
        # صنف متأرشف مايتضافش
        Product.objects.filter(pk=self.plug.pk).update(is_active=False)
        other.delete()
        r = self.post(lines, {'new_code': 'WK-PLUG'})
        self.assertIn('err=product', r['Location'])
        Product.objects.filter(pk=self.plug.pk).update(is_active=True)
        # تعديل كمية سطر + إضافة نفس الصنف في نفس الحفظ ⇒ الاتنين يتجمعوا
        self.post(lines, {f'qty_{item.pk}': '5', 'new_code': 'WK-PLUG', 'new_qty': '2', 'new_cost': '100'})
        item.refresh_from_db()
        self.assertEqual(item.quantity, 7)

    def test_empty_po_cannot_be_sent_and_sent_po_lines_are_locked(self):
        po = self._empty_po()
        action = reverse('inventory:purchase_order_action', args=[po.pk, 'send'])
        r = self.post(action)
        self.assertIn('err=empty', r['Location'])
        po.refresh_from_db()
        self.assertEqual(po.status, 'draft')

        PurchaseOrderItem.objects.create(order=po, product=self.plug, quantity=2, unit_cost=Decimal('100'))
        self.post(action)
        po.refresh_from_db()
        self.assertEqual(po.status, 'sent')
        r = self.post(reverse('inventory:purchase_order_lines', args=[po.pk]), {'new_code': 'WK-OIL'})
        self.assertIn('err=state', r['Location'])
        self.assertEqual(po.items.count(), 1)
        r = self.get(reverse('inventory:purchase_order_detail', args=[po.pk]))
        self.assertNotContains(r, 'name="new_code"')

    # ── 🛠️ كتالوج الخدمات ───────────────────────────────────────────────
    def test_services_catalog_add_edit_delete(self):
        url = reverse('inventory:services_catalog')
        r = self.post(url, {'action': 'add', 'name': 'وزن أمامي', 'labor_price': '350',
                            'estimated_hours': '1.5', 'tech_commission_percent': '20'})
        self.assertIn('ok=added', r['Location'])
        svc = ServiceCatalog.objects.get(name='وزن أمامي')
        self.assertEqual((svc.labor_price, svc.estimated_hours, svc.tech_commission_percent),
                         (Decimal('350.00'), Decimal('1.5'), Decimal('20.00')))

        r = self.post(url, {'action': 'edit', 'pk': svc.pk, 'name': 'وزن وترصيص أمامي', 'labor_price': '400',
                            'estimated_hours': '2', 'tech_commission_percent': '15'})
        self.assertIn('ok=edited', r['Location'])
        svc.refresh_from_db()
        self.assertEqual((svc.name, svc.labor_price), ('وزن وترصيص أمامي', Decimal('400.00')))

        # تظهر في أمر الشغل + لينك «إدارة الخدمات» للمدير
        r = self.get(reverse('inventory:job_card_create'))
        self.assertContains(r, 'وزن وترصيص أمامي')
        self.assertContains(r, url)
        self.assertNotContains(r, 'value="BMW"')          # الماركة مابقتش متعبّية BMW

        r = self.post(url, {'action': 'delete', 'pk': svc.pk})
        self.assertIn('ok=deleted', r['Location'])
        self.assertFalse(ServiceCatalog.objects.filter(pk=svc.pk).exists())

    def test_services_catalog_rejects_bad_numbers_and_keeps_sold_services(self):
        url = reverse('inventory:services_catalog')
        before = ServiceCatalog.objects.count()
        for data in ({'name': '', 'labor_price': '100'},
                     {'name': 'x', 'labor_price': '-1'},
                     {'name': 'x', 'labor_price': '100', 'tech_commission_percent': '150'},
                     {'name': 'x', 'labor_price': 'abc'},
                     {'name': 'x', 'labor_price': '1e12'}):
            r = self.post(url, dict(data, action='add'))
            self.assertIn('err=', r['Location'], data)
        self.assertEqual(ServiceCatalog.objects.count(), before)

        svc = ServiceCatalog.objects.create(name='فحص', labor_price=Decimal('200'))
        inv = SaleInvoice.objects.create(branch=self.branch, customer=make_customer())
        SaleInvoiceServiceItem.objects.create(invoice=inv, service=svc)
        r = self.post(url, {'action': 'delete', 'pk': svc.pk})
        self.assertIn('err=used', r['Location'])
        self.assertTrue(ServiceCatalog.objects.filter(pk=svc.pk).exists())

    def test_cashier_cannot_manage_services_and_sees_role_names(self):
        cashier = self.as_role('cashier', 'wk_cashier')
        r = self.get(reverse('inventory:services_catalog'), client=cashier)
        self.assertEqual(r.status_code, 403)
        body = r.content.decode()
        self.assertIn('كاشير', body)                      # اسم الدور مش كود «cashier»
        self.assertNotIn('دورك الحالي: cashier', body)
        r = self.post(reverse('inventory:services_catalog'), {'action': 'add', 'name': 'x', 'labor_price': '1'},
                      client=cashier)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(ServiceCatalog.objects.filter(name='x').exists())
        r = self.get(reverse('inventory:job_card_create'), client=cashier)
        self.assertNotContains(r, reverse('inventory:services_catalog') + '"')

    # ── 🗼 إلغاء طلب تسعير ───────────────────────────────────────────────
    def test_rfq_cancel(self):
        rfq = RFQ.objects.create(branch=self.branch, part_number_requested='X-1')
        url = reverse('inventory:rfq_cancel', args=[rfq.pk])
        cashier = self.as_role('cashier', 'wk_rfq_cashier')
        self.assertEqual(self.post(url, client=cashier).status_code, 403)
        self.assertEqual(self.get(url).status_code, 405)
        r = self.post(url)
        self.assertEqual(r.status_code, 200, r.content)
        rfq.refresh_from_db()
        self.assertEqual(rfq.status, RFQ.STATUS_CANCELLED)
        self.assertEqual(self.post(url).status_code, 400)        # مايتلغيش مرتين

    # ── 📦 «شراء · مصاريف» في قايمة الأصناف ─────────────────────────────
    def _receive(self, qty, cost, shipping=0):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk, 'treasury_id': self.till.pk,
                'paid_amount': 0, 'cost_allocation': 'value',
                'items': [{'product_id': self.oil.pk, 'qty': qty, 'cost': cost}],
                'extra_costs': [{'kind': 'shipping', 'behavior': 'landed', 'amount': shipping}] if shipping else []}
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)

    def test_product_list_expense_share_uses_receipts_still_in_stock(self):
        self._receive(20, 150)                 # ٢٠ قديم من غير مصاريف
        self._receive(10, 140, shipping=60)    # ١٠ جديد بشحن ٦٠ ⇒ ٦ للقطعة
        r = self.get(reverse('inventory:product_list'), data={'branch': self.branch.pk})
        row = next(x for x in r.context['rows'] if x['product'].pk == self.oil.pk)
        self.assertEqual(row['stock'], 30)
        # المصاريف اللي في الرصيد ٦٠ ÷ ٣٠ قطعة = ٢ (مش ٦ نصيب آخر استلام مضروب في كل الرصيد)
        self.assertEqual(row['expense_share'], Decimal('2.00'))
        self.assertEqual(row['goods_cost'] + row['expense_share'], row['unit_cost'])

    # ── 🏷️ صنف من غير ماركة ─────────────────────────────────────────────
    def test_quick_product_without_brand_is_not_bmw(self):
        r = self.post(reverse('inventory:quick_product_create'), {'part_number': 'WK-NEW', 'name': 'كاوتش',
                                                                 'retail_price': '100'})
        self.assertLess(r.status_code, 400, r.content)
        self.assertEqual(Product.objects.get(part_number='WK-NEW').brand, '—')

    # ── 🔧 تذكير الصيانة ─────────────────────────────────────────────────
    def test_due_maintenance_customers_and_owner_digest(self):
        from inventory.tasks import dispatch_maintenance_reminders, due_maintenance_customers
        today = timezone.localdate()
        ali = make_customer('علي', phone='01011112222')
        Vehicle.objects.create(customer=ali, chassis_number='WKVIN0000000001', brand='تويوتا',
                               model_name='كورولا', estimated_next_visit=today + timedelta(days=2))
        later = make_customer('بعيد', phone='01033334444')
        Vehicle.objects.create(customer=later, chassis_number='WKVIN0000000002',
                               estimated_next_visit=today + timedelta(days=20))
        no_phone = make_customer('من غير رقم', phone='')
        Vehicle.objects.create(customer=no_phone, chassis_number='WKVIN0000000003',
                               estimated_next_visit=today + timedelta(days=1))
        self.assertEqual(due_maintenance_customers(today), [('علي', 'تويوتا كورولا')])

        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            email='owner@example.com', status='trial', is_active=True,
            daily_summary_email=True, daily_summary_whatsapp=True)
        cache.delete(f'maint_reminder_owner:{self.tenant.schema_name}:{today}')
        with mock.patch('django.core.mail.send_mail') as mail, \
                mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa:
            dispatch_maintenance_reminders()
            dispatch_maintenance_reminders()        # مرة واحدة في اليوم
        connection.set_tenant(self.tenant)
        self.assertEqual(mail.call_count, 1)
        self.assertIn('علي', mail.call_args.args[1])
        self.assertEqual(mail.call_args.args[3], ['owner@example.com'])
        self.assertEqual(wa.call_count, 1)
        self.assertEqual(wa.call_args.args[0], '01000000000')
        cache.delete(f'maint_reminder_owner:{self.tenant.schema_name}:{today}')

    # ── 👋 الترحيب والهدية والدخول ───────────────────────────────────────
    def test_welcome_bot_really_sends(self):
        from clients.tasks import async_welcome_bot_task
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa, \
                override_settings(WHATSAPP_WELCOME_TEMPLATE='welcome_v1'):
            async_welcome_bot_task.run('مركز النور', '01012345678', 'automotive',
                                       'https://nour.mousstec.com/login/', 'owner')
        wa.assert_called_once()
        self.assertEqual(wa.call_args.args[0], '01012345678')
        self.assertEqual(wa.call_args.kwargs['template'], 'welcome_v1')
        self.assertEqual(tuple(wa.call_args.kwargs['params']), ('مركز النور', 'https://nour.mousstec.com/login/'))

    def test_new_company_gets_one_welcome_gift_only(self):
        from clients.models import AIBonusGrant
        connection.set_schema_to_public()
        grants = AIBonusGrant.objects.filter(tenant=self.tenant)
        self.assertEqual(grants.count(), 1)
        self.assertEqual(grants.get().granted_designs, 10)
        connection.set_tenant(self.tenant)

    def test_logged_in_user_opening_login_goes_to_his_panel(self):
        r = self.get('/login/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('smart_post_login_redirect'))
        self.assertEqual(Client().get('/login/', HTTP_HOST=self.host).status_code, 200)

    # ── 🖨️ رابط البروفة ─────────────────────────────────────────────────
    def test_proof_whatsapp_link_has_the_shop_domain(self):
        from urllib.parse import unquote
        from printing.admin import _proof_links
        proof = SimpleNamespace(share_token='abc123', job=SimpleNamespace(
            description='كروت شخصية', order=SimpleNamespace(customer=SimpleNamespace(whatsapp='', phone='01012345678'))))
        html = unquote(str(_proof_links(proof)))
        self.assertIn(f'://{self.domain.domain}/printing/proof/abc123/', html)
        self.assertIn('wa.me/201012345678', html)
