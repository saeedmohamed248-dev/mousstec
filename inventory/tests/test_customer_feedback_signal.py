"""
CustomerFeedback Auto-Create Signal — DMS Backlog #1
=====================================================
Locks down the post_save signal `create_customer_feedback_on_post`:

  • Fires exactly once when invoice transitions to status='posted'
  • Does NOT fire for non-posted statuses (quotation, in_progress, etc.)
  • Is idempotent — saving a posted invoice again does not duplicate the
    feedback row or rotate its public_token.

Without these tests, a refactor that drops the `get_or_create` (or that adds
a second `save()` in the cashier flow) would silently break the rating link
the customer received via SMS — they'd land on a 404.
"""
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_treasury, make_customer, make_product,
    make_inventory, make_sale_invoice,
)
from inventory.models import CustomerFeedback


class CustomerFeedbackSignalTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='10000.00')
        self.customer = make_customer()
        self.product = make_product(
            part_number='FB-001', retail_price='200.00', average_cost='100.00',
        )
        make_inventory(self.product, self.branch, quantity=10)

    def _make_quotation(self):
        return make_sale_invoice(
            customer=self.customer, branch=self.branch, treasury=self.treasury,
            items=[(self.product, 2, '200.00')],
            paid_amount='400.00', status='quotation',
        )

    # ── happy path ────────────────────────────────────────────────────
    def test_feedback_created_on_post(self):
        inv = self._make_quotation()
        self.assertFalse(CustomerFeedback.objects.filter(sale_invoice=inv).exists())

        inv.status = 'posted'
        inv.save()

        fb = CustomerFeedback.objects.filter(sale_invoice=inv).first()
        self.assertIsNotNone(fb, 'Feedback row must be auto-created on post')
        self.assertIsNotNone(fb.public_token, 'public_token must be set for the SMS link')

    # ── negative paths ────────────────────────────────────────────────
    def test_no_feedback_for_quotation_status(self):
        inv = self._make_quotation()  # stays in quotation
        self.assertFalse(CustomerFeedback.objects.filter(sale_invoice=inv).exists())

    def test_no_feedback_for_in_progress_status(self):
        inv = self._make_quotation()
        inv.status = 'in_progress'
        inv.save()
        self.assertFalse(CustomerFeedback.objects.filter(sale_invoice=inv).exists())

    # ── idempotency ───────────────────────────────────────────────────
    def test_resaving_posted_invoice_does_not_duplicate_feedback(self):
        """If the cashier edits the posted invoice (e.g. updates notes),
        we must NOT mint a new feedback row — the old public_token in the
        customer's SMS would 404."""
        inv = self._make_quotation()
        inv.status = 'posted'
        inv.save()
        original_fb = CustomerFeedback.objects.get(sale_invoice=inv)
        original_token = original_fb.public_token

        # Re-save (simulates cashier editing notes, etc.)
        inv.notes = 'updated by cashier'
        inv.save()

        fbs = CustomerFeedback.objects.filter(sale_invoice=inv)
        self.assertEqual(fbs.count(), 1, 'No duplicate feedback row')
        self.assertEqual(fbs.first().public_token, original_token,
                         'Token must NOT rotate on re-save (SMS link stays valid)')
