"""📅 تعديل تاريخ فاتورة معتمدة (سيارات).

🐛 قبل كده: الأدمن بيغيّر تاريخ الفاتورة لشهر فات، والفاتورة تتنقل — بس قيدها في
الدفتر يفضل بالتاريخ القديم ⇒ تقرير المبيعات (بتاريخ الفاتورة) وقائمة الدخل (بتاريخ
القيد) مابيتطابقوش، ومفيش أي فحص للفترات المقفولة.

دلوقتي: قيد البيع/الشراء بيتنقل مع التاريخ، والتحصيل/السداد بيفضل بيوم ما الفلوس
اتحركت، والتعديل بيترفض لو التاريخ القديم أو الجديد في فترة مقفولة.
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone

from inventory.models import FiscalYear, AccountingPeriod, AccountingEntry, JournalEntry, SaleInvoice
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_purchase_invoice,
    make_sale_invoice, make_treasury, make_vendor,
)


class InvoiceRedateTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='5000.00')
        self.part = make_product(part_number='RD-1', retail_price='100.00', average_cost='60.00')
        make_inventory(self.part, self.branch, 10)
        self.cust = make_customer(phone='01011112299')
        self.back = timezone.now() - timedelta(days=40)

    def _period(self, name, start, end, closed):
        fy, _ = FiscalYear.objects.get_or_create(
            code='RD', defaults={'name': 'RD', 'start_date': start.replace(month=1, day=1),
                                 'end_date': start.replace(month=12, day=31)})
        return AccountingPeriod.objects.create(fiscal_year=fy, name=name, start_date=start,
                                               end_date=end, is_closed=closed)

    def _sale(self):
        inv = make_sale_invoice(self.cust, self.branch, treasury=self.till,
                                items=[(self.part, 1, '100.00')], paid_amount='100.00', status='posted')
        inv.refresh_from_db()
        self.assertTrue(inv.is_applied)
        return inv

    def test_sale_entry_moves_with_the_invoice_date_but_the_payment_stays(self):
        inv = self._sale()
        today = timezone.localdate()
        sale_je = JournalEntry.objects.get(reference=f'SINV-{inv.pk}')
        pay_je = JournalEntry.objects.filter(reference__startswith='FT-', sale_invoice=inv).first() \
            or JournalEntry.objects.filter(reference__startswith='FT-').latest('pk')
        self.assertEqual(sale_je.date, today)

        inv.date_created = self.back
        inv.save()

        sale_je.refresh_from_db()
        new_day = timezone.localtime(self.back).date()
        self.assertEqual(sale_je.date, new_day)
        self.assertEqual(sale_je.number[:9], f'JE-{new_day:%Y%m}')
        self.assertTrue(all(e.entry_date.date() == new_day or timezone.localtime(e.entry_date).date() == new_day
                            for e in AccountingEntry.objects.filter(journal_entry=sale_je)))
        pay_je.refresh_from_db()
        self.assertEqual(pay_je.date, today)        # الفلوس دخلت النهارده فعلاً

    def test_purchase_entry_moves_too(self):
        pinv = make_purchase_invoice(make_vendor(), self.branch, treasury=self.till,
                                     items=[(self.part, 2, '60.00')], status='posted')
        pinv.refresh_from_db()
        je = JournalEntry.objects.get(reference=f'PINV-{pinv.pk}')
        pinv.date_created = self.back
        pinv.save()
        je.refresh_from_db()
        self.assertEqual(je.date, timezone.localtime(self.back).date())

    def test_cannot_move_an_invoice_into_a_closed_period(self):
        inv = self._sale()
        d = timezone.localtime(self.back).date()
        self._period('مقفولة', d.replace(day=1),
                     (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1), True)
        inv.date_created = self.back
        with self.assertRaises(ValidationError):
            inv.save()
        # الفاتورة ماتحفظتش بالتاريخ الجديد، وقيدها لسه بتاريخه
        self.assertNotEqual(timezone.localtime(SaleInvoice.objects.get(pk=inv.pk).date_created).date(), d)
        self.assertEqual(JournalEntry.objects.get(reference=f'SINV-{inv.pk}').date, timezone.localdate())

    def test_cannot_move_an_invoice_out_of_a_closed_period(self):
        inv = self._sale()
        today = timezone.localdate()
        self._period('الشهر ده', today.replace(day=1), today + timedelta(days=1), False)
        JournalEntry.objects.filter(reference=f'SINV-{inv.pk}').update(
            period=AccountingPeriod.objects.get(name='الشهر ده'))
        AccountingPeriod.objects.filter(name='الشهر ده').update(is_closed=True)
        inv.date_created = self.back
        with self.assertRaises(ValidationError):
            inv.save()

    def test_partial_saves_do_not_touch_entries(self):
        inv = self._sale()
        inv.notes = 'ملاحظة'
        inv.save(update_fields=['notes'])
        self.assertEqual(JournalEntry.objects.get(reference=f'SINV-{inv.pk}').date, timezone.localdate())
