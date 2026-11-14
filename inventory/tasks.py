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

def _reconcile_treasuries(schema_name, target_date):
    """يقارن رصيد كل خزنة شغّالة بمجموع حركاتها لحد ``target_date`` (جوه الـ schema).
    بيرجّع الحالة {'date', 'discrepancies', 'clean'} وبيسجّلها في AgentEventBus."""
    from inventory.models import Treasury, FinancialTransaction

    with schema_context(schema_name):
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


RECON_LAST_KEY = 'fin_recon:last:{}'   # {treasury_id: الفرق} من آخر ليلة — التنبيه للجديد بس


def _reconcile_all_tenants(target_date):
    """🐛 [FIX — دورة ناقصة]: Beat بيشغّل المهمة بـ schema_name='public' والتعليق كان بيقول
    «الوكيل يتولى الـ tenants» — بس مفيش fan-out: الكود كان بيدوّر على الخزائن في public
    (مافيهوش جدول inventory_treasury) فيقع ProgrammingError كل ليلة، فمطابقة الخزائن الليلية
    ماشتغلتش لأي شركة أبداً، والفشل بيتكرر في الـ DLQ. دلوقتي بتلف على الشركات الشغّالة
    (شركة بايظة ماتوقفش الباقي)، ولو ظهر فرق جديد أو اتغيّر بيوصل إيميل لصاحب المنصة."""
    from django.core.cache import cache

    TenantModel = get_tenant_model()
    tenants = (TenantModel.objects.exclude(schema_name='public')
               .filter(is_active=True, status__in=('trial', 'active')).order_by('schema_name'))
    checked, failed, new_rows = 0, [], []
    for tenant in tenants:
        try:
            state = _reconcile_treasuries(tenant.schema_name, target_date)
        except Exception as exc:  # noqa: BLE001 — شركة واحدة لا توقف الباقي
            logger.error("[FINANCIAL RECONCILIATION] tenant %s failed: %s", tenant.schema_name, exc)
            failed.append(tenant.schema_name)
            continue
        checked += 1
        now = {str(d['treasury_id']): round(d['diff'], 2) for d in state['discrepancies']}
        key = RECON_LAST_KEY.format(tenant.schema_name)
        try:
            before = cache.get(key) or {}
            cache.set(key, now, 60 * 60 * 24 * 30)
        except Exception:  # noqa: BLE001 — الكاش واقع ⇒ نعتبر كل فرق جديد
            before = {}
        for d in state['discrepancies']:
            if before.get(str(d['treasury_id'])) != now[str(d['treasury_id'])]:
                new_rows.append((tenant, d))

    if new_rows:
        from clients.services.ops_alerts import send_ops_alert
        lines = [f"• {t.name} ({t.schema_name}) — خزنة «{d['treasury_name']}»: الرصيد {d['actual']:,.2f} "
                 f"بس حركاتها بتقول {d['expected']:,.2f} (فرق {d['diff']:,.2f})" for t, d in new_rows]
        send_ops_alert(
            f'⚠️ فرق في أرصدة الخزائن ({len(new_rows)})',
            'المطابقة الليلية لقت خزائن رصيدها مش مساوي لحركاتها:\n\n' + '\n'.join(lines)
            + '\n\nالتصحيح بعد المراجعة: python manage.py recompute_treasury --schema=<الشركة> --dry-run')

    AgentHealthMonitor.heartbeat('inventory_tasks_financial')
    return {'date': str(target_date), 'tenants': checked, 'failed': failed,
            'new_discrepancies': len(new_rows)}


@shared_task(bind=True, max_retries=2, default_retry_delay=60, name='inventory.tasks.process_financial_reconciliation')
def process_financial_reconciliation(self, schema_name: str = 'public', date_str: str = None):
    """
    Nightly reconciliation pass: audits treasury balances against transaction
    ledger totals and emits a pipeline event if a discrepancy is found.

    Triggered by Celery Beat (see CELERY_BEAT_SCHEDULE in settings.py) with
    schema_name='public' ⇒ fans out over every live tenant.
    """
    # 🐛 [FIX]: كانت timezone.now().date() — المهمة بتشتغل ١ بالليل بتوقيت المحل = تاريخ UTC
    #    بتاع امبارح، فحركات أول ساعة من اليوم كانت بتطلع برا المطابقة.
    target_date = (datetime.strptime(date_str, "%Y-%m-%d").date()
                   if date_str else timezone.localdate())
    if not schema_name or schema_name == 'public':
        return _reconcile_all_tenants(target_date)

    def _execute():
        return _reconcile_treasuries(schema_name, target_date)

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

def due_maintenance_customers(today, window_days=3):
    """جوه schema الشركة: العملاء اللي عربياتهم صيانتها مستحقة — تذكيرات «العملاء المتوقع رجوعهم»
    (متأخر/مستحق ولسه مااتبعتلهمش) + العربيات اللي «الزيارة الجاية» بتاعتها خلال ``window_days``.
    بيرجّع [(اسم العميل, العربية)] من غير تكرار، والمتأخر الأول."""
    from inventory.models import ServiceNudge, Vehicle

    rows, seen = [], set()
    nudges = (ServiceNudge.objects
              .filter(status=ServiceNudge.STATUS_PENDING,
                      urgency__in=(ServiceNudge.URGENCY_OVERDUE, ServiceNudge.URGENCY_DUE))
              .select_related('vehicle__customer').order_by('urgency', 'due_at'))
    visits = (Vehicle.objects
              .filter(estimated_next_visit__gte=today,
                      estimated_next_visit__lte=today + timedelta(days=window_days))
              .select_related('customer').order_by('estimated_next_visit'))
    for vehicle in [n.vehicle for n in nudges] + list(visits):
        customer = vehicle.customer
        if vehicle.pk in seen or customer is None or not customer.phone or customer.is_walk_in:
            continue
        seen.add(vehicle.pk)
        rows.append((customer.name, f"{vehicle.brand} {vehicle.model_name}".strip()))
    return rows


@shared_task(name='inventory.tasks.dispatch_maintenance_reminders')
def dispatch_maintenance_reminders():
    """🔧 كل يوم ٩ الصبح: صاحب الورشة بيعرف مين من عملائه صيانة عربيته مستحقة.

    🐛 [FIX — دورة ناقصة]: البوت كان «stub» — بيكتب «Would send» في اللوج بس، ولا عميل ولا صاحب
    ورشة وصله حاجة، وكمان كان بيستثني الشركات اللي في التجربة. رسالة من رقم الورشة لعميلها برّه
    نافذة الـ ٢٤ ساعة محتاجة قالب Meta متوافَق عليه لكل رقم، فالتذكير بيروح لصاحب الورشة (إيميل +
    واتساب من رقم المنصة) برابط شاشة «العملاء المتوقع رجوعهم» — فيها زرار واتساب لكل عميل بالرسالة
    جاهزة من رقم الورشة نفسها. مرة واحدة في اليوم لكل شركة.
    """
    from django.conf import settings
    from django.core.cache import cache
    from django.core.mail import send_mail

    from clients.services.whatsapp_notify import send_platform_whatsapp
    from clients.tasks import _tenant_link

    TenantModel = get_tenant_model()
    today = timezone.localdate()
    notified = 0
    tenants = (TenantModel.objects.exclude(schema_name='public')
               .filter(is_active=True, status__in=('trial', 'active'))
               .exclude(industry='printing').prefetch_related('domains'))
    for tenant in tenants:
        key = f'maint_reminder_owner:{tenant.schema_name}:{today}'
        try:
            if cache.get(key):
                continue
            with schema_context(tenant.schema_name):
                rows = due_maintenance_customers(today)
            if not rows:
                continue
            names = '، '.join(f'{n} ({car})' if car else n for n, car in rows[:5])
            more = f' و{len(rows) - 5} غيرهم' if len(rows) > 5 else ''
            line = (f'🔧 {len(rows)} عميل صيانة عربيته مستحقة: {names}{more}. '
                    f'ابعتلهم تذكير بضغطة من «العملاء المتوقع رجوعهم».')
            link = _tenant_link(tenant, '/system/crm/retention/')
            # المالك اللي قفل «ملخص اليوم» على قناة ما مابيوصلوش عليها برضه
            if tenant.daily_summary_email and tenant.email and '@' in tenant.email:
                send_mail(f'🔧 {len(rows)} عميل صيانته مستحقة — {tenant.name}',
                          f'أهلاً {tenant.owner_name or tenant.name}،\n\n{line}\n\n{link}\n\n— Mouss Tec',
                          settings.DEFAULT_FROM_EMAIL, [tenant.email], fail_silently=True)
            if tenant.daily_summary_whatsapp and tenant.phone:
                tpl = getattr(settings, 'WHATSAPP_MAINTENANCE_DIGEST_TEMPLATE', '')
                send_platform_whatsapp(tenant.phone, country=getattr(tenant, 'country', 'EG') or 'EG',
                                       template=tpl, params=(tenant.name, len(rows), link),
                                       text='' if tpl else f'{tenant.name}، {line}\n{link}\n— Mouss Tec')
            cache.set(key, 1, 60 * 60 * 26)
            notified += 1
            AgentHealthMonitor.heartbeat('maintenance_reminder_bot', schema=tenant.schema_name,
                                         metadata={'due': len(rows), 'date': str(today)})
        except Exception:  # noqa: BLE001 — شركة واحدة مابتوقفش الباقي
            logger.exception('[MAINTENANCE REMINDER BOT] tenant %s failed', tenant.schema_name)

    logger.info(f"📲 [MAINTENANCE REMINDER BOT] notified {notified} workshop owner(s).")
    return f"Workshops notified: {notified}"


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

# اسم الوكيل ← مسار مهمة Celery اللي بتتعاد
DLQ_TASK_MAP = {
    'b2b_marketplace_sync_task':   'clients.tasks.async_sync_b2b_marketplace_product',
    'b2b_marketplace_remove_task': 'clients.tasks.async_remove_b2b_marketplace_product',
    'inventory_tasks_ai_vision':   'inventory.tasks.process_ai_vision_invoice',
    'inventory_tasks_financial':   'inventory.tasks.process_financial_reconciliation',
    'welcome_bot':                 'clients.tasks.async_welcome_bot_task',
    'ai_bidding_award_agent':      'clients.tasks.process_ai_bidding_award',
}


def dlq_task_kwargs(agent_name, payload):
    """payload الـ DLQ ⇒ kwargs المهمة (أو None لو مايتعادش أوتوماتيك).

    🐛 [FIX]: الـ payload بيتسجّل بمفاتيح مختصرة ({'schema', 'date', 'invoice_id'}) والـ worker
    كان بيبعتها زي ما هي كـ kwargs ⇒ كل إعادة كانت بتقع فوراً بـ
    «unexpected keyword argument 'schema'» — يعني ولا مزامنة سوق B2B فاشلة اتعادت أبداً.
    """
    p = dict(payload or {})
    schema = p.pop('schema', None) or p.pop('schema_name', None)
    if agent_name == 'b2b_marketplace_sync_task':
        return {'schema_name': schema, 'product_id': p['product_id']} if schema and 'product_id' in p else None
    if agent_name == 'b2b_marketplace_remove_task':
        if not schema or 'part_number' not in p:
            return None
        return {'schema_name': schema, 'part_number': p['part_number'], 'condition': p.get('condition', '')}
    if agent_name == 'inventory_tasks_financial':
        return {'schema_name': schema or 'public', 'date_str': p.get('date') or p.get('date_str')}
    if agent_name == 'inventory_tasks_ai_vision':
        return None   # صورة الفاتورة مش محفوظة في الـ payload — مالهاش إعادة
    return dict(payload or {}) or None

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

        task_path = DLQ_TASK_MAP.get(agent_name)
        kwargs = dlq_task_kwargs(agent_name, payload) if task_path and payload else None
        if task_path and kwargs:
            # 🐛 [FIX]: عدد المحاولات كان بيرجع صفر مع كل فشل جديد (push بيعمل entry جديد)،
            #    فحد الـ ٣ محاولات عمره ما اشتغل والمهمة الفاشلة دايماً بتلف للأبد.
            dlq.mark_redispatched(agent_name, payload, entry['retry_count'])
            try:
                current_app.send_task(task_path, kwargs=kwargs)
                requeued += 1
                logger.info(f"♻️ [DLQ WORKER] Re-queued '{agent_name}' task (attempt {entry['retry_count']}).")
            except Exception as exc:
                logger.error(f"🔴 [DLQ WORKER] Re-dispatch failed for '{agent_name}': {exc}")
                dlq.push(agent_name, payload, error=str(exc), schema=schema)
        else:
            logger.error(
                f"💀 [DLQ WORKER] '{agent_name}' can't be replayed automatically "
                f"(no task mapping / incomplete payload). Payload: {payload}"
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


# ─────────────────────────────────────────────────────────────────────
# 🎨 Image Studio — استبدال خلفية الأصناف بالجملة (async, لا يعطّل الطلب)
# ─────────────────────────────────────────────────────────────────────
@shared_task(bind=True, name='inventory.tasks.bulk_replace_background')
def bulk_replace_background(self, schema_name: str, product_ids: list, preset_key: str = 'studio_white'):
    """يعالج خلفية صور عدد كبير من الأصناف في الخلفية (Celery) بدل ما يعلّق
    طلب الـ HTTP — كان تشغيله المتزامن على مئات الأصناف بيخلّي الصفحة تحمّل
    لساعات ثم تنقطع. بيعالج صنف صنف مع تسجيل تقدّم دوري.
    """
    from inventory.services import image_studio as _studio
    from inventory.models import Product

    done = skipped = failed = 0
    with schema_context(schema_name):
        total = len(product_ids)
        logger.info("[BG STUDIO] بدء معالجة %s صنف (schema=%s, preset=%s)",
                    total, schema_name, preset_key)
        for idx, pid in enumerate(product_ids, start=1):
            try:
                product = Product.objects.filter(pk=pid).first()
                if product is None or not product.image:
                    skipped += 1
                    continue
                gen = _studio.generate_preview(product, preset_key=preset_key)
                if not gen.get('ok'):
                    failed += 1
                    continue
                applied = _studio.apply_preview(product, gen['preview_path'], preset_key=preset_key)
                if applied.get('ok'):
                    done += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 — صنف واحد لا يوقف الدفعة
                failed += 1
                logger.exception("[BG STUDIO] فشل الصنف #%s: %s", pid, exc)
            if idx % 25 == 0 or idx == total:
                logger.info("[BG STUDIO] تقدّم %s/%s (تم=%s تخطّي=%s فشل=%s)",
                            idx, total, done, skipped, failed)

    logger.info("[BG STUDIO] انتهت: تم=%s تخطّي=%s فشل=%s", done, skipped, failed)
    return {'done': done, 'skipped': skipped, 'failed': failed}


# ─────────────────────────────────────────────────────────────────────
# 🗓️🧠 الملخّص الأسبوعي التلقائي للمستشار الذكي (Celery beat)
# ─────────────────────────────────────────────────────────────────────
def _email_advisor_brief(tenant, snap, insights, ai_text):
    """إرسال الملخّص بالإيميل best-effort لمدراء/محاسبي الشركة (لو الإيميل مُعدّ)."""
    from django.conf import settings as _st
    from django.core.mail import send_mail
    from django.contrib.auth.models import User

    if not (getattr(_st, 'EMAIL_HOST', '') or getattr(_st, 'BREVO_API_KEY', '')):
        return  # الإيميل مش مُعدّ — نتخطّى بهدوء

    recipients = list(
        User.objects.filter(is_active=True, is_staff=True)
        .exclude(email='').exclude(email__isnull=True)
        .values_list('email', flat=True)[:20]
    )
    if not recipients:
        return

    s = snap['sales']
    lines = [
        f"ملخّص أداء آخر {snap['period_days']} يوم — {snap['branch']}",
        "",
        f"• صافي المبيعات: {s['net_sales']:.0f} ج.م (تغيّر {s['change_vs_prev_pct']:+.0f}%)",
        f"• الربح: {s['profit']:.0f} ج.م (هامش {s['margin_pct']:.1f}%)",
        f"• قيمة المخزون بالتكلفة: {snap['inventory']['stock_value_cost']:.0f} ج.م",
        f"• تحت حد الأمان: {snap['inventory']['low_stock_items']} | راكد: {snap['inventory']['dead_stock_items']}",
        f"• آجل على العملاء: {snap['receivables_total']:.0f} ج.م",
        "",
        "أهم الملاحظات:",
    ]
    lines += [f"- {i['title']}: {i['text']}" for i in insights]
    if ai_text:
        lines += ["", "تحليل وخطة العمل:", ai_text]
    body = "\n".join(lines)

    try:
        send_mail(
            subject=f"📊 ملخّص Mouss Tec الأسبوعي — {tenant.schema_name}",
            message=body,
            from_email=getattr(_st, 'DEFAULT_FROM_EMAIL', 'noreply@mousstec.com'),
            recipient_list=recipients,
            fail_silently=True,
        )
    except Exception as exc:  # noqa: BLE001 — الإيميل لا يوقف المهمة
        logger.warning("[ADVISOR BRIEF] email failed for %s: %s", tenant.schema_name, exc)


@shared_task(name='inventory.tasks.generate_weekly_briefs')
def generate_weekly_briefs():
    """يتولّد أسبوعياً: يبني ملخّص المستشار لكل شركة، يخزّنه في الكاش (يظهر في
    صفحة المستشار)، ويبعته بالإيميل best-effort. Multi-tenant."""
    from django.core.cache import cache
    from inventory.services import business_advisor as adv

    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')
    count = 0
    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                snap = adv.build_snapshot(branch=None, days=7)
                insights = adv.rule_based_insights(snap)
                ai_text = adv.ai_analysis(snap) or ''
                brief = {
                    'generated_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                    'period': 'weekly', 'kpis': snap,
                    'insights': insights, 'ai_text': ai_text,
                }
                cache.set(f"{tenant.schema_name}:advisor_brief", brief, 60 * 60 * 24 * 14)
                _email_advisor_brief(tenant, snap, insights, ai_text)
                count += 1
        except Exception as exc:  # noqa: BLE001 — شركة واحدة لا توقف الباقي
            logger.error("[ADVISOR BRIEF] tenant %s failed: %s", tenant.schema_name, exc)
    logger.info("[ADVISOR BRIEF] generated %s weekly briefs", count)
    return {'briefs': count}


@shared_task(name='inventory.tasks.build_async_report')
def build_async_report(schema_name, task_id, user_id, report_type, branch_id=None, params=None):
    """📊 تقرير في الخلفية (request_async_report_api ?async=1) — النتيجة في الكاش ساعة."""
    from django.core.cache import cache
    from inventory.views.reports import ASYNC_REPORT_TTL, ReportParamError, build_report
    key = f"async_report:{schema_name}:{task_id}"
    try:
        with schema_context(schema_name):
            result = build_report(report_type, branch_id, params or {})
    except ReportParamError as exc:
        result = {"status": "failed", "error": str(exc)}
    except Exception:   # noqa: BLE001
        logger.exception("[ASYNC REPORT] %s failed on %s", report_type, schema_name)
        result = {"status": "failed", "error": "حدث خطأ أثناء توليد التقرير"}
    cache.set(key, {**result, "user_id": user_id}, ASYNC_REPORT_TTL)
    return result.get("status")
