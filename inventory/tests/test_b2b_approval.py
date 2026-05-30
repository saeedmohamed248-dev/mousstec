"""
B2B Listing Approval Tests — Ensure products need approval before marketplace listing.
"""
from decimal import Decimal
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_inventory, make_user

from inventory.models import B2BListingRequest


class B2BListingApprovalTests(ERPTenantTestCase):
    """B2B listing request lifecycle: creation, approval, rejection."""

    def setUp(self):
        self.branch = make_branch()
        self.product = make_product(
            part_number='B2B-001',
            retail_price='500.00',
            b2b_wholesale_price=Decimal('400.00'),
        )
        make_inventory(self.product, self.branch, quantity=10)
        self.user = make_user(username='approver')

    def test_publishing_creates_pending_request(self):
        """Setting is_b2b_published=True should create a pending request."""
        self.product.is_b2b_published = True
        self.product.save()

        req = B2BListingRequest.objects.filter(product=self.product).first()
        self.assertIsNotNone(req)
        self.assertEqual(req.status, 'pending')
        self.assertEqual(req.requested_price, Decimal('400.00'))

    def test_no_duplicate_pending_requests(self):
        """Saving product again should NOT create duplicate pending requests."""
        self.product.is_b2b_published = True
        self.product.save()
        self.product.save()  # Second save

        count = B2BListingRequest.objects.filter(
            product=self.product, status='pending',
        ).count()
        self.assertEqual(count, 1)

    def test_unpublishing_does_not_create_request(self):
        """Setting is_b2b_published=False should NOT create a request."""
        self.product.is_b2b_published = False
        self.product.save()

        count = B2BListingRequest.objects.filter(product=self.product).count()
        self.assertEqual(count, 0)

    def test_approve_listing(self):
        """Approving a listing should update status and set reviewer."""
        from inventory.services.inventory_service import InventoryService

        req = B2BListingRequest.objects.create(
            product=self.product,
            requested_price=Decimal('400.00'),
        )
        InventoryService.approve_b2b_listing(req, Decimal('380.00'), self.user)

        req.refresh_from_db()
        self.assertEqual(req.status, 'approved')
        self.assertEqual(req.approved_price, Decimal('380.00'))
        self.assertEqual(req.reviewed_by, self.user)
        self.assertIsNotNone(req.reviewed_at)

    def test_reject_listing(self):
        """Rejecting should set status to rejected."""
        req = B2BListingRequest.objects.create(
            product=self.product,
            requested_price=Decimal('400.00'),
        )
        req.status = 'rejected'
        req.reviewed_by = self.user
        req.save()

        req.refresh_from_db()
        self.assertEqual(req.status, 'rejected')

    def test_default_price_uses_retail_when_no_b2b(self):
        """If b2b_wholesale_price is 0, should use retail_price."""
        self.product.b2b_wholesale_price = Decimal('0.00')
        self.product.is_b2b_published = True
        self.product.save()

        req = B2BListingRequest.objects.filter(product=self.product).first()
        self.assertIsNotNone(req)
        self.assertEqual(req.requested_price, Decimal('500.00'))
