"""Regression coverage + bug fix verification for
``inventory.signals.accrue_salesperson_commission``.

The signal hooks ``post_save`` on ``SaleInvoiceItem`` and credits the
salesperson's ``EmployeeProfile.commission_balance`` with their share
of the profit on the line. Reading the code, every save (not just
the create) added to ``commission_balance`` via an ``F() + commission``
update — a real double-pay risk every time anyone edited an existing
line. This file pins:

* Create credits the salesperson once.
* Edit (any subsequent save) does NOT credit again.
* No salesperson set → no accrual.
* Loss-making line (cost > price) → no accrual.
* Zero commission rate → no accrual.

``test_edit_does_not_re_accrue`` was a failing test that drove a
one-line fix in inventory/signals.py: ``if not created: return``.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User

from inventory.models import EmployeeProfile, SaleInvoiceItem
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import (
    make_branch,
    make_customer,
    make_inventory,
    make_product,
    make_sale_invoice,
    make_treasury,
)


class CommissionAccrualSignalTests(ERPTenantTestCase):
    """Each test reads the salesperson's balance from the DB to observe
    exactly what the signal committed — not the stale Python instance.
    """

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        self.treasury = make_treasury(self.branch, balance='5000.00')
        self.product = make_product(
            part_number='SP-001',
            retail_price='1000.00',
            average_cost='600.00',  # 400 profit per unit
        )
        make_inventory(self.product, self.branch, quantity=10)

        user = User.objects.create_user('salesperson_x', password='x')
        # A post_save signal on User auto-creates an EmployeeProfile,
        # so update the existing row instead of trying to create a
        # second one for the same user_id.
        self.sp, _ = EmployeeProfile.objects.update_or_create(
            user=user,
            defaults=dict(
                role='salesperson',
                branch=self.branch,
                commission_balance=Decimal('0.00'),
                commission_rate_pct=Decimal('10.00'),  # 10% of profit
            ),
        )

    def _add_line(self, *, invoice, qty=1, price='1000.00', salesperson=None):
        item = SaleInvoiceItem.objects.create(
            invoice=invoice,
            product=self.product,
            quantity=qty,
            unit_price=Decimal(price),
            cost_at_sale=self.product.average_cost,
            salesperson=salesperson,
        )
        return item

    def _balance(self):
        self.sp.refresh_from_db()
        return self.sp.commission_balance

    # ── Happy path ───────────────────────────────────────────────────
    def test_create_accrues_commission_once(self):
        """1 unit at 1000 with cost 600 = 400 profit. 10% = 40 EGP."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[],
        )
        self._add_line(invoice=si, qty=1, price='1000.00', salesperson=self.sp)
        self.assertEqual(self._balance(), Decimal('40.00'))

    def test_quantity_multiplies_profit(self):
        """5 units → 5 × 400 profit × 10% = 200 EGP."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[],
        )
        self._add_line(invoice=si, qty=5, price='1000.00', salesperson=self.sp)
        self.assertEqual(self._balance(), Decimal('200.00'))

    # ── Bug-fix verification ────────────────────────────────────────
    def test_edit_does_not_re_accrue(self):
        """Editing an already-saved line MUST NOT re-credit the
        salesperson. Without ``if not created: return`` the
        commission was added on every save call — every time an
        admin edited a description, fixed a typo, or status-flipped
        the parent invoice through a touchpoint that re-saved the
        item, the salesperson was paid the full commission again.
        """
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[],
        )
        item = self._add_line(
            invoice=si, qty=1, price='1000.00', salesperson=self.sp,
        )
        balance_after_create = self._balance()
        self.assertEqual(balance_after_create, Decimal('40.00'))

        # Now re-save the item — the way Django admin's "save changes"
        # button does, or the way SaleInvoice.update_total cascades
        # when something unrelated changes.
        item.refresh_from_db()
        item.save()

        # Balance must NOT have moved.
        self.assertEqual(
            self._balance(), balance_after_create,
            'Editing an item must not re-credit the salesperson — '
            'the signal needs ``if not created: return``.',
        )

    # ── Guard clauses ────────────────────────────────────────────────
    def test_no_salesperson_means_no_accrual(self):
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[],
        )
        self._add_line(invoice=si, qty=1, price='1000.00', salesperson=None)
        self.assertEqual(self._balance(), Decimal('0.00'))

    def test_loss_making_line_skips_accrual(self):
        """Sold below cost — there's no profit, so no commission. A
        salesperson getting paid commission on a loss would be a
        direct money leak."""
        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[],
        )
        self._add_line(
            invoice=si, qty=1, price='500.00', salesperson=self.sp,
        )
        self.assertEqual(self._balance(), Decimal('0.00'))

    def test_zero_rate_skips_accrual(self):
        """If the salesperson is on 0% commission, no accrual happens
        even on a profitable sale. (Defense against zero-rate ghost
        rows from old data.)"""
        self.sp.commission_rate_pct = Decimal('0')
        self.sp.save(update_fields=['commission_rate_pct'])

        si = make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[],
        )
        self._add_line(invoice=si, qty=1, price='1000.00', salesperson=self.sp)
        self.assertEqual(self._balance(), Decimal('0.00'))
