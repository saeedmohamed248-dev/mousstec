"""
Smart matching for the parts market — connects supply and demand.

* A listing goes live      → buyers whose open "Part Wanted" request fits it
                              get a notification with a direct link.
* A wanted request is posted → sellers who already have a fitting live
                              listing get told a buyer is looking for it,
                              and the buyer sees the matches right away.
* Price guide              → what similar parts actually sold for / are
                              listed at, so sellers price realistically.

Fitment rule (same spirit as fitment.py): same make is mandatory; model
must agree when both sides specify one; the requested year must fall inside
the listing's year range when the listing has one; and the part itself must
match — identical OEM number, or at least one meaningful word in common
between the part names.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from statistics import median

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

MAX_ALERTS_PER_EVENT = 30

# Words too generic to identify a part on their own.
_STOPWORDS = {
    'قطعة', 'قطعه', 'قطع', 'غيار', 'جديد', 'جديدة', 'مستعمل', 'مستعملة', 'اصلي', 'أصلي',
    'اصلية', 'أصلية', 'عربية', 'عربيه', 'سيارة', 'سياره', 'محتاج', 'مطلوب', 'كامل', 'كاملة',
    'حالة', 'ممتاز', 'ممتازة', 'نظيف', 'نظيفة', 'the', 'and', 'for', 'new', 'used', 'original',
    'part', 'parts', 'car', 'oem',
}
_TOKEN_RE = re.compile(r'[\w؀-ۿ]+', re.UNICODE)


def _normalize(word: str) -> str:
    w = word.lower().strip()
    # Light Arabic normalisation so "مرايه/مراية" or "أمامي/امامي" match.
    w = (w.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
          .replace('ة', 'ه').replace('ى', 'ي'))
    if w.startswith('ال') and len(w) > 4:
        w = w[2:]
    return w


def tokens(text: str) -> set[str]:
    out = set()
    for raw in _TOKEN_RE.findall(text or ''):
        w = _normalize(raw)
        if len(w) >= 3 and w not in _STOPWORDS and not w.isdigit():
            out.add(w)
    return out


def _oem(value: str) -> str:
    return re.sub(r'[\s\-\.]', '', (value or '')).upper()


def part_matches(*, name_a, oem_a, name_b, oem_b) -> bool:
    a, b = _oem(oem_a), _oem(oem_b)
    if a and b:
        return a == b
    return bool(tokens(name_a) & tokens(name_b))


def _model_q(prefix: str, model: str):
    """Model agrees when either side leaves it blank or the names overlap."""
    model = (model or '').strip()
    if not model:
        return Q()
    return (Q(**{f'{prefix}car_model': ''}) | Q(**{f'{prefix}car_model__iexact': model})
            | Q(**{f'{prefix}car_model__icontains': model}))


def _public_listings():
    from clients.models import PartListing
    return PartListing.objects.filter(
        status='active', moderation_status='approved', is_deleted=False,
        reserved_for__isnull=True,
    )


# ── Supply ← demand ──────────────────────────────────────────────────
def listings_matching_request(req, limit=5):
    """Live public listings that fit an open wanted request."""
    qs = (
        _public_listings()
        .filter(car_make_id=req.car_make_id)
        .filter(Q(car_year_from__isnull=True) | Q(car_year_from__lte=req.car_year))
        .filter(Q(car_year_to__isnull=True) | Q(car_year_to__gte=req.car_year))
        .select_related('seller_customer')
        .order_by('price_egp')
    )
    if req.buyer_customer_id:
        qs = qs.exclude(seller_customer_id=req.buyer_customer_id)
    model = (req.car_model or '').strip()
    out = []
    for listing in qs[:300]:
        lm = (listing.car_model or '').strip().lower()
        if model and lm and lm != model.lower() and model.lower() not in lm and lm not in model.lower():
            continue
        if part_matches(name_a=listing.title, oem_a=listing.part_number,
                        name_b=req.part_name, oem_b=req.part_number_oem):
            out.append(listing)
            if len(out) >= limit:
                break
    return out


def requests_matching_listing(listing, limit=MAX_ALERTS_PER_EVENT):
    """Open wanted requests that a (public) listing can satisfy."""
    from clients.models import PartWantedRequest
    qs = PartWantedRequest.objects.filter(
        is_deleted=False, status='open', expires_at__gt=timezone.now(),
        car_make_id=listing.car_make_id,
    ).filter(_model_q('', listing.car_model)).select_related('buyer_customer')
    if listing.car_year_from:
        qs = qs.filter(car_year__gte=listing.car_year_from)
    if listing.car_year_to:
        qs = qs.filter(car_year__lte=listing.car_year_to)
    if listing.seller_customer_id:
        qs = qs.exclude(buyer_customer_id=listing.seller_customer_id)
    out = []
    for req in qs.order_by('-created_at')[:500]:
        if part_matches(name_a=listing.title, oem_a=listing.part_number,
                        name_b=req.part_name, oem_b=req.part_number_oem):
            out.append(req)
            if len(out) >= limit:
                break
    return out


def notify_buyers_of_new_listing(listing) -> int:
    """Called when a listing becomes publicly visible."""
    if not listing.is_publicly_visible:
        return 0
    from clients.models import CustomerNotification
    sent, seen = 0, set()
    for req in requests_matching_listing(listing):
        buyer = req.buyer_customer
        if buyer is None or buyer.pk in seen:
            continue
        seen.add(buyer.pk)
        CustomerNotification.objects.create(
            customer=buyer,
            title=f'🎯 لقينا قطعة ممكن تكون اللي بتدور عليها',
            body=(f'«{listing.title[:80]}» — {listing.car_make.name} {listing.car_model} '
                  f'بسعر {listing.price_egp} — تطابق طلبك «{req.part_name[:60]}».'),
            level='info', icon='fa-bullseye',
            action_url=f'/marketplace/parts/{listing.listing_code}/', action_label='شوف القطعة',
        )
        sent += 1
    if sent:
        logger.info("[MATCH] listing %s → alerted %d buyer(s)", listing.pk, sent)
    return sent


def notify_sellers_of_new_request(req) -> int:
    """Called when a wanted request is posted: tell sellers who have it."""
    from clients.models import CustomerNotification
    sent, seen = 0, set()
    for listing in listings_matching_request(req, limit=MAX_ALERTS_PER_EVENT):
        seller = listing.seller_customer
        if seller is None or seller.pk in seen:
            continue
        seen.add(seller.pk)
        CustomerNotification.objects.create(
            customer=seller,
            title='🔔 مشتري بيدور على قطعة عندك',
            body=(f'فيه طلب «{req.part_name[:60]}» لـ {req.car_make.name} {req.car_model} {req.car_year} '
                  f'بيطابق قطعتك «{listing.title[:60]}». قدّم عرض سعر قبل غيرك.'),
            level='info', icon='fa-bell',
            action_url='/marketplace/parts/wanted/sellers/', action_label='قدّم عرض',
        )
        sent += 1
    return sent


# ── Price guide ──────────────────────────────────────────────────────
def price_guide(*, car_make_id, title='', condition='', part_number=''):
    """
    Realistic price range for a part: what similar parts sold for (paid
    orders) and what live listings ask. Returns None when there isn't
    enough data to say anything useful.
    """
    from clients.models import PartOrder
    if not car_make_id or not (tokens(title) or _oem(part_number)):
        return None

    def _similar(listing):
        return part_matches(name_a=listing.title, oem_a=listing.part_number,
                            name_b=title, oem_b=part_number)

    sold = []
    for o in (PartOrder.objects
              .filter(listing__car_make_id=car_make_id,
                      status__in=('paid_held', 'shipped', 'delivered', 'released'))
              .select_related('listing').order_by('-paid_at')[:400]):
        if (not condition or o.listing.condition == condition) and _similar(o.listing):
            sold.append(o.amount_paid)
    live = [
        l.price_egp for l in _public_listings().filter(car_make_id=car_make_id)[:400]
        if (not condition or l.condition == condition) and _similar(l)
    ]
    prices = sold + live
    if len(prices) < 2:
        return None
    q = Decimal('1')
    return {
        'count': len(prices),
        'sold_count': len(sold),
        'live_count': len(live),
        'min': min(prices).quantize(q),
        'max': max(prices).quantize(q),
        'median': Decimal(median(prices)).quantize(q),
    }
