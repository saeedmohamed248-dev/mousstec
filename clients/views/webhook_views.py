"""
External webhook endpoints.

Only `universal_webhook_multiplexer` lives here today. Kept in its own
module because (a) it is one of the very few legitimate `@csrf_exempt`
endpoints (the caller is a third party, not our own browser) and
(b) it has its own HMAC-verification contract worth isolating.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.http import HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from clients.models import Client, EscrowLedger

logger = logging.getLogger('mouss_tec_core')


@csrf_exempt
def universal_webhook_multiplexer(request):
    """
    بوابة FinTech محصنة: تمنع التكرار (Idempotency) وتطبق سياسات مكافحة غسيل الأموال (AML).
    """
    if request.method != 'POST':
        return HttpResponseForbidden("POST Only")

    # 🛡️ HMAC signature verification — MANDATORY.
    # 🚨 لو الـ secret مش معرّف، الـ endpoint بيرفض كل الطلبات. مفيش fallback مفتوح
    # — ده endpoint بيـ credit محافظ مالية. fail-closed.
    secret = getattr(settings, 'WEBHOOK_HMAC_SECRET', None) or getattr(
        settings, 'PAYMOB_HMAC_SECRET', None
    )
    if not secret:
        logger.error(
            "[WEBHOOK] WEBHOOK_HMAC_SECRET not configured — refusing all requests. "
            "Set it in environment before enabling this endpoint."
        )
        return HttpResponseForbidden("Webhook signing not configured")

    received_sig = request.META.get('HTTP_X_WEBHOOK_SIGNATURE', '')
    if not received_sig:
        logger.warning("[WEBHOOK] Missing signature header — rejected.")
        return HttpResponseForbidden("Missing signature")
    computed = hmac.new(secret.encode('utf-8'), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_sig):
        logger.warning("[WEBHOOK] HMAC verification failed — rejected.")
        return HttpResponseForbidden("Invalid signature")

    try:
        payload = json.loads(request.body)
        # 🛡️ event_id لازم يجي من المزود — random UUID بيكسر الـ idempotency
        # ويسمح بإعادة معالجة نفس الـ deposit مرات لا نهائية.
        event_id = payload.get('id')
        if not event_id:
            logger.warning("[WEBHOOK] Payload missing 'id' — rejected.")
            return HttpResponseForbidden("Missing event id")

        if cache.get(f"webhook_processed_{event_id}"):
            return JsonResponse({"status": "duplicate"})

        if payload.get('type') == 'payment_intent.succeeded':
            client_id = payload['data']['metadata']['client_id']
            amount = Decimal(str(payload['data']['amount_received'])) / 100

            with transaction.atomic():
                tenant = Client.objects.select_for_update().get(id=client_id)

                # 🚀 AML: deposits over the threshold are credited *and*
                # immediately frozen so the fraud team can review before
                # the tenant can spend them.
                #
                # 🐛 [bug-fix 2026-06-13]:
                #   Previously this branch created a 'hold' EscrowLedger
                #   row directly on an empty wallet; the pre_save guard
                #   refused it ("الرصيد المتاح لا يكفي") and the atomic
                #   block aborted — so a 150k EGP deposit returned 500
                #   to the provider and the money silently disappeared.
                #   Fix: deposit first (signal credits wallet), THEN hold
                #   (signal moves it into escrow_held).
                if amount > Decimal('100000'):
                    logger.warning(f"🚨 [AML ALERT]: Large suspicious deposit of {amount} for {tenant.schema_name}.")
                    EscrowLedger.objects.create(
                        client=tenant, transaction_type='deposit', amount=amount,
                        description=f"إيداع كبير للمراجعة الأمنية ({event_id})",
                    )
                    EscrowLedger.objects.create(
                        client=tenant, transaction_type='hold', amount=amount,
                        description=f"تجميد إداري لمراجعة AML ({event_id})",
                    )
                    tenant.is_fraud_flagged = True
                    tenant.save(update_fields=['is_fraud_flagged'])
                else:
                    # 🐛 [bug-fix 2026-06-13]:
                    #   Previously this also did
                    #     tenant.wallet_balance = F('wallet_balance') + amount
                    #     tenant.save(update_fields=['wallet_balance'])
                    #   *before* creating the deposit row. Both paths
                    #   incremented the wallet by `amount`, so every
                    #   provider top-up was double-credited (1,000 EGP
                    #   in → 2,000 EGP on the wallet). The signal alone
                    #   is the source of truth; remove the manual update.
                    EscrowLedger.objects.create(
                        client=tenant, transaction_type='deposit', amount=amount,
                        description=f"إيداع سحابي ({event_id})",
                    )

            cache.set(f"webhook_processed_{event_id}", "processed", timeout=86400)
            return JsonResponse({"status": "success"})

        return JsonResponse({"status": "ignored"})
    except Exception as e:
        logger.error(f"🚨 Webhook Failure: {e}")
        return JsonResponse({"error": "Internal Error"}, status=500)
