"""
Trust & Privacy service.

Two responsibilities:

1. **Mask** buyer/seller contact data so the public marketplace surfaces
   only what's safe to show (initials + last 4 phone digits + trust badge).
   Mirrors the eBay/Amazon pre-purchase disclosure model.

2. **Reveal** the full contact details only when a paying relationship
   exists — i.e. an escrow-funded PartOrder where the requesting party is
   either the buyer or the seller. Anyone else stays masked.

This module is import-light: no view, no Django request dependency, so it
can be called from views, templates (via templatetags), DRF serializers,
and tests without ceremony.
"""
from __future__ import annotations

from typing import Optional


# ── REVEAL_STATUSES ──────────────────────────────────────────────────
# Once an order is in any of these statuses, buyer ↔ seller may see each
# other's real contact info. Earlier statuses (pending_payment) MUST stay
# masked: payment hasn't actually cleared yet.
REVEAL_STATUSES = frozenset({
    'paid_held', 'shipped', 'delivered', 'released',
    'refund_requested', 'refunded', 'disputed',
})


# ── Masking primitives ───────────────────────────────────────────────
def mask_name(name: str | None) -> str:
    """'Saied Mohamed Ali' → 'S*** M*** A***'. Empty → '—'."""
    if not name:
        return '—'
    parts = [p for p in str(name).strip().split() if p]
    if not parts:
        return '—'
    return ' '.join(f'{p[0]}{"*" * max(len(p) - 1, 2)}' for p in parts)


def mask_phone(phone: str | None) -> str:
    """'+201234567890' → '+201******7890'. Keeps country code + last 4."""
    if not phone:
        return '—'
    s = str(phone).strip()
    if len(s) <= 8:
        return '*' * len(s)
    head = s[:4]
    tail = s[-4:]
    return f'{head}{"*" * (len(s) - 8)}{tail}'


def mask_email(email: str | None) -> str:
    """'foo.bar@example.com' → 'f******@example.com'."""
    if not email or '@' not in str(email):
        return '—'
    local, _, domain = str(email).partition('@')
    if not local:
        return '—'
    return f'{local[0]}{"*" * max(len(local) - 1, 2)}@{domain}'


# ── Trust badge metadata for templates ───────────────────────────────
TIER_BADGE = {
    'new':      {'icon': '👤', 'label_ar': 'جديد',            'color': '#94a3b8'},
    'basic':    {'icon': '📱', 'label_ar': 'موبايل موثق',     'color': '#0ea5e9'},
    'email':    {'icon': '✉️', 'label_ar': 'بريد موثق',       'color': '#22c55e'},
    'id':       {'icon': '🪪', 'label_ar': 'هوية موثقة',      'color': '#a855f7'},
    'business': {'icon': '🏢', 'label_ar': 'نشاط تجاري موثق', 'color': '#facc15'},
}


def get_trust_badge(customer) -> dict:
    """Return the badge dict for a MarketplaceCustomer. Safe if no verification row exists yet."""
    verif = getattr(customer, 'verification', None)
    tier = getattr(verif, 'trust_tier', None) or 'new'
    score = getattr(verif, 'trust_score', 0) or 0
    badge = dict(TIER_BADGE.get(tier, TIER_BADGE['new']))
    badge['tier'] = tier
    badge['score'] = score
    return badge


# ── Reveal policy ────────────────────────────────────────────────────
def can_reveal_contact(viewer_customer, target_customer, order=None) -> bool:
    """
    Returns True iff `viewer_customer` is allowed to see `target_customer`'s
    real contact details (full name / phone / email).

    Rules:
      * Owner viewing themselves → always True.
      * If an `order` is supplied AND its status is in REVEAL_STATUSES AND
        the viewer is the buyer or seller on that order AND the target is
        the counterparty → True.
      * Otherwise False (mask).

    Note: tenant-side accounts (seller_tenant / buyer_tenant) are handled
    by the caller — this helper deals with MarketplaceCustomer pairs.
    """
    if viewer_customer is None or target_customer is None:
        return False
    if viewer_customer.pk == target_customer.pk:
        return True
    if order is None:
        return False
    if order.status not in REVEAL_STATUSES:
        return False
    buyer_id  = order.buyer_customer_id
    seller_id = order.listing.seller_customer_id if order.listing_id else None
    pair = {buyer_id, seller_id}
    if viewer_customer.pk in pair and target_customer.pk in pair:
        return True
    return False


def contact_view(customer, *, viewer=None, order=None) -> dict:
    """
    Returns a dict ready for templates / JSON responses:
        {
          'display_name': str,   # masked or real
          'phone':        str,   # masked or real
          'email':        str,
          'is_revealed':  bool,
          'badge':        dict,
        }
    """
    if customer is None:
        return {
            'display_name': '—', 'phone': '—', 'email': '—',
            'is_revealed': False, 'badge': TIER_BADGE['new'],
        }
    reveal = can_reveal_contact(viewer, customer, order=order)
    badge = get_trust_badge(customer)
    if reveal:
        return {
            'display_name': customer.company_name or customer.full_name or '—',
            'phone': customer.phone or '—',
            'email': customer.email or '—',
            'is_revealed': True,
            'badge': badge,
        }
    return {
        'display_name': mask_name(customer.company_name or customer.full_name),
        'phone': mask_phone(customer.phone),
        'email': mask_email(customer.email),
        'is_revealed': False,
        'badge': badge,
    }
