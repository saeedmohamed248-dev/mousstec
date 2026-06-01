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
import uuid
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

    # 🛡️ HMAC signature verification for webhook security
    secret = getattr(settings, 'WEBHOOK_HMAC_SECRET', None)
    if secret:
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
        event_id = payload.get('id', f'evt_{uuid.uuid4().hex[:12]}')

        if cache.get(f"webhook_processed_{event_id}"):
            return JsonResponse({"status": "duplicate"})

        if payload.get('type') == 'payment_intent.succeeded':
            client_id = payload['data']['metadata']['client_id']
            amount = Decimal(str(payload['data']['amount_received'])) / 100

            with transaction.atomic():
                tenant = Client.objects.select_for_update().get(id=client_id)

                # 🚀 ابتكار AML: إذا كان المبلغ ضخماً جداً، يتم تعليقه لحين المراجعة اليدوية
                if amount > Decimal('100000'):
                    logger.warning(f"🚨 [AML ALERT]: Large suspicious deposit of {amount} for {tenant.schema_name}.")
                    EscrowLedger.objects.create(
                        client=tenant, transaction_type='hold', amount=amount,
                        description=f"إيداع معلق للمراجعة الأمنية ({event_id})",
                    )
                    tenant.is_fraud_flagged = True
                    tenant.save(update_fields=['is_fraud_flagged'])
                else:
                    tenant.wallet_balance = F('wallet_balance') + amount
                    tenant.save(update_fields=['wallet_balance'])
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
