"""
Dispute service.

Chokepoint for opening, resolving, and cancelling DisputeTickets. Like
the escrow service, this is the only place views are allowed to mutate
DisputeTicket state — that keeps the order/escrow state machine sane.

State transitions:

    (none) --open--> open
    open --start_review--> under_review
    open|under_review --resolve_refund-->  resolved_refund   (escrow.refund_to_buyer)
    open|under_review --resolve_release--> resolved_release  (escrow.release_to_seller)
    open|under_review --resolve_split-->   resolved_split    (escrow.split_settlement)
    open|under_review --cancel-->          cancelled         (no escrow action)

When a dispute moves to a `resolved_*` state the PartOrder status is
synced to its terminal state (released/refunded). When cancelled, the
order is moved back to whatever it was before the dispute opened.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.utils import timezone

from clients.services import escrow as escrow_svc


# ── Opening ─────────────────────────────────────────────────────────
@transaction.atomic
def open_dispute(
    *,
    order,
    opener,            # MarketplaceCustomer or Client
    opener_role: str,  # 'buyer' or 'seller'
    category: str,
    description: str,
):
    """
    Open a new dispute. Validates the opener has standing on the order
    and that the 3-day window is still open.
    """
    from clients.models import DisputeTicket

    if opener_role not in ('buyer', 'seller'):
        raise ValidationError(f"opener_role must be 'buyer' or 'seller', got '{opener_role}'.")
    if not category or category not in dict(DisputeTicket.CATEGORY_CHOICES):
        raise ValidationError(f"Unknown dispute category: {category!r}.")
    if not (description or '').strip():
        raise ValidationError("Description is required.")

    # Standing check — opener must actually be the buyer or seller.
    is_customer = hasattr(opener, 'sector')  # MarketplaceCustomer has .sector
    if opener_role == 'buyer':
        ok = (
            (is_customer and order.buyer_customer_id == opener.pk)
            or (not is_customer and order.buyer_tenant_id == opener.pk)
        )
    else:  # seller
        listing = order.listing
        ok = (
            (is_customer and listing.seller_customer_id == opener.pk)
            or (not is_customer and listing.seller_tenant_id == opener.pk)
        )
    if not ok:
        raise PermissionDenied("You don't have standing to open a dispute on this order.")

    # 3-day window
    if not DisputeTicket.is_within_window(order):
        raise ValidationError(
            f"The {DisputeTicket.DISPUTE_WINDOW_DAYS}-day inspection window has "
            f"closed for this order (status='{order.status}')."
        )

    # Don't allow a second open dispute on the same order — funnel everything
    # through the existing one.
    existing = DisputeTicket.objects.filter(
        order=order, status__in=('open', 'under_review'),
    ).first()
    if existing is not None:
        return existing

    ticket = DisputeTicket.objects.create(
        order=order,
        opened_by_role=opener_role,
        opened_by_customer=opener if is_customer else None,
        opened_by_tenant=None if is_customer else opener,
        category=category,
        description=description.strip(),
        status='open',
        order_status_at_open=order.status,
    )

    # Freeze the order — auto_release_expired_warranties skips 'disputed'.
    if order.status != 'disputed':
        order.status = 'disputed'
        order.save(update_fields=['status'])

    return ticket


# ── Resolution paths ────────────────────────────────────────────────
def _ensure_resolvable(ticket):
    if ticket.status not in ('open', 'under_review'):
        raise ValidationError(f"Cannot resolve a dispute in status '{ticket.status}'.")


@transaction.atomic
def resolve_with_refund(ticket, *, return_reason, by_user=None, notes=''):
    """Full refund to buyer. Hands off to escrow.refund_to_buyer."""
    _ensure_resolvable(ticket)
    order = ticket.order
    escrow_svc.refund_to_buyer(order, return_reason=return_reason, by_user=by_user)
    order.status = 'refunded'
    order.refunded_at = timezone.now()
    order.save(update_fields=['status', 'refunded_at'])

    ticket.status = 'resolved_refund'
    ticket.resolution_notes = (notes or '')[:5000]
    ticket.resolved_by = by_user if (by_user and getattr(by_user, 'is_authenticated', False)) else None
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=['status', 'resolution_notes', 'resolved_by', 'resolved_at'])
    return ticket


@transaction.atomic
def resolve_with_release(ticket, *, by_user=None, notes=''):
    """Release funds to seller. Restores order to delivered so escrow.release_to_seller accepts it."""
    _ensure_resolvable(ticket)
    order = ticket.order
    # escrow.release_to_seller only checks EscrowHold.status — not the
    # order's warranty_ends_at — so we just need a hold in 'held' state,
    # which is guaranteed for any order that reached 'disputed' through
    # the normal flow (paid_held → delivered → disputed).
    escrow_svc.release_to_seller(order, by_user=by_user, reason='admin dispute resolution')
    order.status = 'released'
    order.released_at = timezone.now()
    order.save(update_fields=['status', 'released_at'])

    ticket.status = 'resolved_release'
    ticket.resolution_notes = (notes or '')[:5000]
    ticket.resolved_by = by_user if (by_user and getattr(by_user, 'is_authenticated', False)) else None
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=['status', 'resolution_notes', 'resolved_by', 'resolved_at'])
    return ticket


@transaction.atomic
def resolve_with_split(ticket, *, refund_amount: Decimal, return_reason,
                       by_user=None, notes=''):
    """Partial refund — calls escrow.split_settlement."""
    _ensure_resolvable(ticket)
    order = ticket.order
    escrow_svc.split_settlement(
        order, refund_amount=refund_amount, return_reason=return_reason, by_user=by_user,
    )
    order.status = 'released'  # split is a terminal-released state for the order
    order.released_at = timezone.now()
    order.save(update_fields=['status', 'released_at'])

    ticket.status = 'resolved_split'
    ticket.resolution_notes = (notes or '')[:5000]
    ticket.resolved_by = by_user if (by_user and getattr(by_user, 'is_authenticated', False)) else None
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=['status', 'resolution_notes', 'resolved_by', 'resolved_at'])
    return ticket


@transaction.atomic
def cancel_dispute(ticket, *, by_role: str, notes=''):
    """Opener retracts. Order returns to its pre-dispute status."""
    _ensure_resolvable(ticket)
    if by_role != ticket.opened_by_role:
        raise PermissionDenied("Only the opener can cancel a dispute.")
    order = ticket.order
    if order.status == 'disputed' and ticket.order_status_at_open:
        order.status = ticket.order_status_at_open
        order.save(update_fields=['status'])
    ticket.status = 'cancelled'
    ticket.resolution_notes = (notes or '')[:5000]
    ticket.resolved_at = timezone.now()
    ticket.save(update_fields=['status', 'resolution_notes', 'resolved_at'])
    return ticket
