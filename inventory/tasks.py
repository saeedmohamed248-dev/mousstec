"""
Inventory Async Agents — Celery Tasks
======================================
All tasks here run in background workers and are fully protected by the
Mouss Tec MAS Orchestrator (CircuitBreaker + DLQ + HealthMonitor).

Queue routing (defined in settings.py):
  heavy_ai_tasks        → process_ai_vision_*
  urgent_fintech_tasks  → process_financial_*
  default               → everything else
"""
import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.db import transaction, connection
from django.db.models import Sum, F, Q
from django.utils import timezone
from django_tenants.utils import schema_context, get_tenant_model

from erp_core.orchestrator import run_agent_safely, dlq, AgentEventBus, AgentHealthMonitor

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 1. AI Vision — فاتورة الصورة (Vision Procurement Bot — async wrapper)
# ─────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=2, default_retry_delay=30, name='inventory.tasks.process_ai_vision_invoice')
def process_ai_vision_invoice(self, schema_name: str, purchase_invoice_id: int, image_base64: str):
    """
    Receives a base64 invoice image from the POS frontend, runs it through the
    Vision Procurement Bot, and auto-fills the PurchaseInvoice items.

    Pipeline position: triggered from the Purchase Invoice create view when a
    user uploads a supplier invoice photo instead of entering data manually.
    """
    def _execute():
        from inventory.ai_services import scan_invoice_image_ai
        from inventory.models import PurchaseInvoice, PurchaseInvoiceItem, Product

        with schema_context(schema_name):
            invoice = PurchaseInvoice.objects.select_related('branch').get(pk=purchase_invoice_id)

            if invoice.status != 'draft':
                logger.info(f"[AI VISION] Invoice #{purchase_invoice_id} is not draft — skipping.")
                return {"skipped": True}

            ai_result = scan_invoice_image_ai(image_base64)
            items_data = ai_result.get('items', [])
            filled_count = 0

            with transaction.atomic():
                for item in items_data:
                    part_number = (item.get('part_number') or '').strip()
                    if not part_number:
                        continue
                    product = Product.objects.filter(part_number__iexact=part_number).first()
                    if not product:
                        continue

                    PurchaseInvoiceItem.objects.get_or_create(
                        invoice=invoice,
                        product=product,
                        defaults={
                            'quantity':   int(item.get('qty', 1)),
                            'cost_price': float(item.get('cost', product.purchase_price or 0)),
                        }
                    )
                    filled_count += 1

                if ai_result.get('vendor_name'):
                    from inventory.models import Vendor
                    vendor = Vendor.objects.filter(name__icontains=ai_result['vendor_name']).first()
                    if vendor:
                        invoice.vendor = vendor
                        invoice.save(update_fields=['vendor'])

            AgentEventBus.set_agent_state(
                'inventory_tasks_ai_vision', schema=schema_name,
                state={'last_invoice_id': purchase_invoice_id, 'items_filled': filled_count}
            )
            logger.info(
                f"👁️ [AI VISION BOT] Invoice #{purchase_invoice_id}: "
                f"filled {filled_count} items from image. Schema: {schema_name}"
            )
            return {"filled": filled_count}

    try:
        return run_agent_safely(
            agent_name='inventory_tasks_ai_vision',
            func=_execute,
            payload={'schema': schema_name, 'invoice_id': purchase_invoice_id},
            schema=schema_name,
            failure_threshold=3,
            reraise=True,
        )
    except Exception as exc:
        logger.error(f"🔴 [AI VISION BOT] Retrying ({self.request.retries}/2)… {exc}")
        raise self.retry(exc=exc)


# ─────────────────────────────────────────────────────────────────────
# 2. Financial Reconciliation Agent
# ─────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=2, default_retry_delay=60, name='inventory.tasks.process_financial_reconciliation')
def process_financial_reconciliation(self, schema_name: str, date_str: str = None):
    """
    Nightly reconciliation pass: audits treasury balances against transaction
    ledger totals and emits a pipeline event if a discrepancy is found.

    Triggered by Celery Beat (see CELERY_BEAT_SCHEDULE in settings.py).
    """
    def _execute():
        from inventory.models import Treasury, FinancialTransaction

        with schema_context(schema_name):
            target_date = (
                datetime.strptime(date_str, "%Y-%m-%d").date()
                if date_str else timezone.now().date()
            )
            discrepancies = []

            for treasury in Treasury.objects.filter(is_active=True):
                total_in = FinancialTransaction.objects.filter(
                    treasury=treasury,
                    transaction_type='in',
                    date__date__lte=target_date,
                ).aggregate(s=Sum('amount'))['s'] or 0

                total_out = FinancialTransaction.objects.filter(
                    treasury=treasury,
                    transaction_type='out',
                    date__date__lte=target_date,
                ).aggregate(s=Sum('amount'))['s'] or 0

                expected_balance = total_in - total_out
                actual_balance   = float(treasury.balance)
                diff             = abs(actual_balance - float(expected_balance))

                if diff > 0.01:
                    discrepancies.append({
                        'treasury_id':   treasury.pk,
                        'treasury_name': treasury.name,
                        'expected':      float(expected_balance),
                        'actual':        actual_balance,
                        'diff':          diff,
                    })
                    logger.warning(
                        f"⚠️ [FINANCIAL RECONCILIATION] Treasury '{treasury.name}' "
                        f"discrepancy: expected={expected_balance:.2f}, actual={actual_balance:.2f}"
                    )

            state = {
                'date':          str(target_date),
                'discrepancies': discrepancies,
                'clean':         len(discrepancies) == 0,
            }
            AgentEventBus.set_agent_state(
                'inventory_tasks_financial', schema=schema_name, state=state
            )

            if discrepancies:
                AgentEventBus.push_pipeline_event(
                    'financial_discrepancy_detected',
                    data={'schema': schema_name, 'discrepancies': discrepancies},
                    schema=schema_name,
                )

            logger.info(
                f"💰 [FINANCIAL RECONCILIATION] Schema '{schema_name}': "
                f"{len(discrepancies)} discrepanc(ies) on {target_date}."
            )
            return state

    try:
        return run_agent_safely(
            agent_name='inventory_tasks_financial',
            func=_execute,
            payload={'schema': schema_name, 'date': date_str},
            schema=schema_name,
            failure_threshold=3,
            reraise=True,
        )
    except Exception as exc:
        raise self.retry(exc=exc)


# ─────────────────────────────────────────────────────────────────────
# 3. Proactive Maintenance Reminder Bot
# ─────────────────────────────────────────────────────────────────────

@shared_task(name='inventory.tasks.dispatch_maintenance_reminders')
def dispatch_maintenance_reminders():
    """
    Scans all tenant schemas for vehicles whose `estimated_next_visit` falls
    within the next 3 days and dispatches WhatsApp/SMS reminders.

    Triggered by Celery Beat daily at 09:00.
    Pipeline: Outbound Orchestrator sets `estimated_next_visit` → this bot reads it.
    """
    TenantModel = get_tenant_model()
    today = timezone.now().date()
    reminder_window = today + timedelta(days=3)
    total_sent = 0

    for tenant in TenantModel.objects.exclude(schema_name='public').filter(status='active'):
        def _execute(t=tenant):
            from inventory.models import Vehicle

            with schema_context(t.schema_name):
                due_vehicles = Vehicle.objects.filter(
                    estimated_next_visit__lte=reminder_window,
                    estimated_next_visit__gte=today,
                ).select_related('customer')

                sent = 0
                for vehicle in due_vehicles:
                    customer = vehicle.customer
                    if not customer or not customer.phone:
                        continue

                    msg = (
                        f"تذكير صيانة 🔧\n"
                        f"عزيزنا {customer.name}، موعد صيانة سيارتك "
                        f"{vehicle.brand} {vehicle.model_name} "
                        f"اقترب ({vehicle.estimated_next_visit}).\n"
                        f"احجز موعدك الآن مع {t.name}!"
                    )
                    # 💡 [هنا يتم ربط WhatsApp API / Twilio / UltraMsg]
                    logger.info(
                        f"📲 [MAINTENANCE REMINDER BOT] Would send to "
                        f"{customer.phone}: {msg[:60]}…"
                    )
                    sent += 1

                AgentHealthMonitor.heartbeat(
                    'maintenance_reminder_bot', schema=t.schema_name,
                    metadata={'sent': sent, 'date': str(today)}
                )
                return sent

        result = run_agent_safely(
            agent_name='maintenance_reminder_bot',
            func=_execute,
            payload={'schema': tenant.schema_name, 'window': str(reminder_window)},
            schema=tenant.schema_name,
            failure_threshold=5,
        )
        total_sent += (result or 0)

    logger.info(f"📲 [MAINTENANCE REMINDER BOT] Dispatched {total_sent} reminders across all tenants.")
    return f"Reminders dispatched: {total_sent}"


# ─────────────────────────────────────────────────────────────────────
# 4. Elastic Pricing Sync Agent
# ─────────────────────────────────────────────────────────────────────

@shared_task(name='inventory.tasks.sync_elastic_pricing')
def sync_elastic_pricing(schema_name: str):
    """
    Runs the Elastic Pricing Bot over all active products in a tenant schema
    and updates `sale_price` if the AI-suggested retail differs by more than 5%.

    Triggered post-purchase (Inbound Orchestrator → on_commit) and by Beat weekly.
    """
    def _execute():
        from inventory.models import Product
        from inventory.ai_services import predict_market_price_elasticity

        with schema_context(schema_name):
            products = Product.objects.filter(
                is_active=True, average_cost__gt=0
            ).only('id', 'name', 'condition', 'average_cost', 'retail_price')

            updated = 0
            for product in products:
                try:
                    result = predict_market_price_elasticity(
                        product.name, product.condition, float(product.average_cost)
                    )
                    suggested = float(result.get('suggested_retail', 0))
                    if suggested <= 0:
                        continue

                    current = float(product.retail_price or 0)
                    deviation = abs(suggested - current) / max(current, 1)

                    if deviation > 0.05:  # update only if > 5% difference
                        Product.objects.filter(pk=product.pk).update(retail_price=suggested)
                        updated += 1
                except Exception as item_exc:
                    logger.warning(
                        f"⚠️ [ELASTIC PRICING] Skipped product {product.pk}: {item_exc}"
                    )
                    continue

            AgentEventBus.set_agent_state(
                'elastic_pricing_bot', schema=schema_name,
                state={'updated': updated, 'schema': schema_name}
            )
            logger.info(
                f"💸 [ELASTIC PRICING BOT] Schema '{schema_name}': "
                f"updated {updated} product prices."
            )
            return updated

    run_agent_safely(
        agent_name='elastic_pricing_bot',
        func=_execute,
        payload={'schema': schema_name},
        schema=schema_name,
        failure_threshold=3,
    )


# ─────────────────────────────────────────────────────────────────────
# 5. DLQ Retry Worker — يعيد تشغيل المهام الفاشلة
# ─────────────────────────────────────────────────────────────────────

@shared_task(name='inventory.tasks.drain_dlq_and_retry')
def drain_dlq_and_retry(max_entries: int = 20):
    """
    Processes up to `max_entries` entries from the Dead Letter Queue and
    attempts to re-dispatch each one as a Celery task.

    Triggered by Celery Beat every hour.
    Only entries with retry_count < 3 are re-tried; older ones are logged
    and left for manual review (preserved in the DLQ list).
    """
    from celery import current_app

    processed = 0
    requeued  = 0

    for _ in range(max_entries):
        entry = dlq.pop()
        if not entry:
            break

        processed += 1
        retry_count = entry.get('retry_count', 0)
        agent_name  = entry.get('agent', 'unknown')
        payload     = entry.get('payload', {})
        schema      = entry.get('schema', 'public')

        if retry_count >= 3:
            logger.error(
                f"💀 [DLQ WORKER] Entry from '{agent_name}' exceeded max retries. "
                f"Payload: {payload}"
            )
            continue

        # Increment retry count and push back if re-dispatch fails
        entry['retry_count'] = retry_count + 1

        # Map agent names to their Celery task paths
        TASK_MAP = {
            'b2b_marketplace_sync_task':   'clients.tasks.async_sync_b2b_marketplace_product',
            'b2b_marketplace_remove_task': 'clients.tasks.async_remove_b2b_marketplace_product',
            'inventory_tasks_ai_vision':   'inventory.tasks.process_ai_vision_invoice',
            'inventory_tasks_financial':   'inventory.tasks.process_financial_reconciliation',
            'welcome_bot':                 'clients.tasks.async_welcome_bot_task',
            'ai_bidding_award_agent':      'clients.tasks.process_ai_bidding_award',
        }

        task_path = TASK_MAP.get(agent_name)
        if task_path and payload:
            try:
                current_app.send_task(task_path, kwargs=payload)
                requeued += 1
                logger.info(f"♻️ [DLQ WORKER] Re-queued '{agent_name}' task (attempt {entry['retry_count']}).")
            except Exception as exc:
                logger.error(f"🔴 [DLQ WORKER] Re-dispatch failed for '{agent_name}': {exc}")
                dlq.push(agent_name, payload, error=str(exc), schema=schema)
        else:
            logger.warning(
                f"⚠️ [DLQ WORKER] No task mapping for agent '{agent_name}'. "
                f"Entry preserved for manual review."
            )

    AgentHealthMonitor.heartbeat('dlq_retry_worker')
    logger.info(f"♻️ [DLQ WORKER] Cycle complete: processed={processed}, requeued={requeued}.")
    return f"DLQ: processed={processed}, requeued={requeued}"


# ─────────────────────────────────────────────────────────────────────
# 🔮 Daily predictive-nudge sweep — populates ServiceNudge rows
# ─────────────────────────────────────────────────────────────────────
from celery import shared_task as _shared_task


@_shared_task(name='inventory.tasks.refresh_service_nudges')
def refresh_service_nudges(schema_name=None, limit=2000):
    """Daily sweep — recomputes every vehicle's ServiceNudge rows so the
    CRM Retention Dashboard has fresh data to surface.

    Schedule via Beat (see erp_core/settings.py CELERY_BEAT_SCHEDULE).
    """
    import logging
    log = logging.getLogger('mouss_tec_core')

    from django_tenants.utils import schema_context, get_tenant_model
    from inventory.predictive_engine import refresh_all_nudges

    if schema_name:
        targets = [schema_name]
    else:
        Tenant = get_tenant_model()
        targets = list(
            Tenant.objects.exclude(schema_name='public')
                          .filter(status__in=['active', 'trial'])
                          .values_list('schema_name', flat=True)
        )

    summary = {'tenants': 0, 'scanned': 0, 'nudged': 0, 'errors': 0}
    for schema in targets:
        try:
            with schema_context(schema):
                result = refresh_all_nudges(limit=limit)
            summary['tenants'] += 1
            summary['scanned'] += result.get('vehicles_scanned', 0)
            summary['nudged'] += result.get('vehicles_nudged', 0)
        except Exception as exc:
            summary['errors'] += 1
            log.exception(
                "[refresh_service_nudges] schema=%s failed: %s", schema, exc,
            )

    log.info("🔮 [Service Nudges Sweep] %s", summary)
    return summary
