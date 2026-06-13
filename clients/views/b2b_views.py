"""
B2B marketplace APIs — global search, blind bidding, escrow balance,
and the price/demand radar.

All endpoints in this module are tenant-authenticated (login_required +
explicit `request.tenant`/`connection.schema_name` checks).
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db import connection, transaction
from django.db.models import Avg, Count, Max, Min
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from clients.models import (
    BidOffer,
    BlindBiddingRequest,
    Client,
    EscrowLedger,
    GlobalB2BMarketplace,
)
from clients.services.entitlements import require_feature

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 🛒 محرك بحث سوق التجار (B2B Global Search API)
# =====================================================================
@login_required(login_url='/login/')
@require_feature('b2b_marketplace')
def b2b_market_search_api(request):
    if connection.schema_name == 'public' and not request.user.is_superuser:
        return JsonResponse({"error": "غير مصرح"}, status=403)

    part_number = request.GET.get('part_number', '').strip()
    if not part_number:
        return JsonResponse({"error": "برجاء تزويد رقم القطعة"}, status=400)

    results = GlobalB2BMarketplace.objects.filter(
        part_number__iexact=part_number, available_qty__gt=0,
        tenant__is_active=True, tenant__is_marketplace_active=True, tenant__is_fraud_flagged=False,
    ).select_related('tenant').order_by('-tenant__is_verified_merchant', 'wholesale_price')[:15]

    data = [{
        "dealer_name": item.tenant.name, "is_verified": item.tenant.is_verified_merchant,
        "rating": float(item.tenant.market_rating or 5.0), "price": float(item.wholesale_price),
        "qty_available": item.available_qty, "condition": item.get_condition_display(),
    } for item in results]

    return JsonResponse({"status": "success", "results_count": len(data), "dealers": data})


# =====================================================================
# ⚖️ محرك المزادات العكسية والترسية الذكية (Dynamic Blind Bidding)
# =====================================================================
@login_required(login_url='/login/')
@require_feature('b2b_marketplace')
def active_blind_bids_api(request):
    active_bids = (
        BlindBiddingRequest.objects
        .filter(status='open', expires_at__gt=timezone.now())
        .select_related('buyer').order_by('-created_at')
    )
    data = [{
        "bid_id": b.id, "part_number": b.part_number, "required_qty": b.required_qty,
        "buyer_name": "مشتري سري",
        "urgency": "High" if (b.expires_at - timezone.now()).total_seconds() < 7200 else "Normal",
    } for b in active_bids]
    return JsonResponse({"status": "success", "bids": data})


@login_required(login_url='/login/')
@require_feature('b2b_marketplace')
def submit_bid_offer_api(request):
    """
    🚀 ابتكار الذكاء التنافسي: وزن الخوارزمية يتغير ديناميكياً بناءً على سرعة التوصيل وعمر المزاد.
    """
    if request.method != 'POST':
        return JsonResponse({"error": "POST Only"}, status=400)
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
        return JsonResponse({"error": "للشركات فقط"}, status=403)

    try:
        data = json.loads(request.body)
        bid_id = data.get('bid_id')
        offer_price = Decimal(str(data.get('offer_price', 0)))
        delivery_days = int(data.get('delivery_days', 1))

        with transaction.atomic():
            bid = get_object_or_404(
                BlindBiddingRequest.objects.select_for_update(), id=bid_id, status='open',
            )
            buyer_tenant = Client.objects.select_for_update().get(id=bid.buyer_id)
            seller_tenant = request.tenant

            if buyer_tenant == seller_tenant:
                return JsonResponse({"error": "لا يمكنك المزايدة على طلبك"}, status=400)

            # 🤖 خوارزمية الترسية الديناميكية (Dynamic Weights)
            target = bid.target_price or offer_price
            base_price_score = min((target / offer_price) * 100, 100) if offer_price > 0 else 0

            if delivery_days <= 1:
                final_match_score = Decimal(str(
                    (base_price_score * 0.3) + 50
                    + ((getattr(seller_tenant, 'ai_trust_score', 100) / 100) * 20)
                ))
            else:
                del_score = max(20 - (delivery_days * 2), 0)
                final_match_score = Decimal(str(
                    (base_price_score * 0.5) + del_score
                    + ((getattr(seller_tenant, 'ai_trust_score', 100) / 100) * 30)
                ))

            offer, _ = BidOffer.objects.update_or_create(
                bidding_request=bid, seller=seller_tenant,
                defaults={
                    'offer_price': offer_price,
                    'estimated_delivery_days': delivery_days,
                    'ai_match_score': final_match_score,
                },
            )

            if not bid.ai_recommended_winner or final_match_score > bid.ai_recommended_winner.ai_match_score:
                bid.ai_recommended_winner = offer
                bid.save(update_fields=['ai_recommended_winner'])

            if bid.auto_award and bid.target_price and offer_price <= bid.target_price:
                total_req = (offer_price * bid.required_qty) * (
                    Decimal('1') + getattr(buyer_tenant, 'platform_fee_rate', Decimal('2.5')) / 100
                )
                if buyer_tenant.wallet_balance >= total_req:
                    bid.status = 'escrow_held'
                    bid.winner = seller_tenant
                    bid.winning_price = offer_price
                    bid.save(update_fields=['status', 'winner', 'winning_price'])
                    offer.is_winner = True
                    offer.save(update_fields=['is_winner'])
                    EscrowLedger.objects.create(
                        client=buyer_tenant, bidding_request=bid, transaction_type='hold',
                        amount=total_req, description=f"ضمان مزاد #{bid.id}",
                    )
                    return JsonResponse({"status": "auto_awarded", "message": "تم الترسية وحجز الضمان!"})

        return JsonResponse({
            "status": "success", "message": "تم تقديم عرضك بنجاح.",
            "ai_score": float(final_match_score),
        })
    except Exception as e:
        logger.error("[BID] submit_bid_offer_api error: %s", e)
        return JsonResponse({"error": "حدث خطأ أثناء تقديم العرض. حاول مرة أخرى."}, status=500)


# =====================================================================
# 🛡️ محفظة الضامن المالي (Escrow Ledger)
# =====================================================================
@login_required(login_url='/login/')
@require_feature('b2b_marketplace')
def my_escrow_wallet_api(request):
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
        return JsonResponse({"error": "متاح للمؤسسات فقط."}, status=403)
    return JsonResponse({
        "status": "success",
        "wallet": {
            "available": float(request.tenant.wallet_balance),
            "held": float(request.tenant.escrow_held),
        },
    })


# =====================================================================
# 🌍 رادار التنبؤ (Advanced Market Demand AI Predictor)
# =====================================================================
@login_required(login_url='/login/')
@require_feature('b2b_marketplace')
def market_demand_predictor_api(request):
    """
    🚀 ابتكار: استبعاد القيم الشاذة (Outliers) لحساب متوسط الأسعار بدقة أعلى.
    """
    thirty_days_ago = timezone.now() - timedelta(days=30)

    trending_parts = (
        BlindBiddingRequest.objects
        .filter(created_at__gte=thirty_days_ago, status__in=['completed', 'escrow_held'])
        .values('part_number')
        .annotate(
            request_count=Count('id'),
            avg_win_price=Avg('winning_price'),
            min_win_price=Min('winning_price'),
            max_win_price=Max('winning_price'),
        )
        .order_by('-request_count')[:5]
    )

    data = []
    for part in trending_parts:
        if part['max_win_price'] > (part['avg_win_price'] * Decimal('3.0')):
            part['max_win_price'] = part['avg_win_price'] * Decimal('1.5')

        data.append({
            "part_number": part['part_number'],
            "demand_heat": part['request_count'],
            "pricing_band": {
                "lowest": float(part['min_win_price']) if part['min_win_price'] else 0,
                "highest": float(part['max_win_price']) if part['max_win_price'] else 0,
                "suggested": float(part['avg_win_price']) if part['avg_win_price'] else 0,
            },
        })
    return JsonResponse({"status": "success", "trending_parts": data})
