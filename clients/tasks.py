from __future__ import annotations

from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db import transaction
from django.db.models import F, Min, Sum
from clients.models import Client
import logging

from erp_core.orchestrator import run_agent_safely, AgentEventBus, AgentHealthMonitor, dlq

# تهيئة رادار المراقبة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 📨 1. وكيل الترحيب الذكي (Zero-Trust Omni-Channel Dispatcher)
# =====================================================================
@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def async_welcome_bot_task(self, client_name, client_phone, business_type, full_login_url, admin_user, secret_credential=None):
    """
    يتلقى الأوامر من الـ Provisioning Orchestrator (signals.py).
    🚀 ابتكار: مهيأ للتعامل مع الروابط السحرية (Magic Links) المطبقة في معمارية Zero-Trust.
    """
    try:
        type_str = "مركز الصيانة" if business_type in ['service_center', 'both'] else "تجارة قطع الغيار"
        
        # صياغة الرسالة بنظام الـ Magic Link أو Credentials
        msg = (
            f"مرحباً بك في Mouss Tec Ecosystem 🚀\n\n"
            f"تم تأسيس نظام {type_str} الخاص بك ({client_name}) بنجاح.\n\n"
            f"🌐 ادخل لغرفة العمليات الآن: {full_login_url}\n"
            f"👤 الدخول مخصص للمسؤول: {admin_user}\n"
        )
        
        # دعم رجعي (Backward Compatibility) في حال تم إرسال باسورد بدلاً من التوكن
        if secret_credential and "token=" not in full_login_url:
            msg += f"🔑 كلمة المرور المؤقتة: {secret_credential}\n"
            
        msg += "\nنتمنى لك أرباحاً هائلة والتفوق على منافسيك!"
        
        # 💡 [مكان دمج بوابة WhatsApp API (مثل Twilio/Ultramsg) أو البريد الإلكتروني]
        # example: twilio_client.messages.create(body=msg, from_='+123', to=client_phone)
        
        logger.info(f"📨 [AI WELCOME BOT]: Secure onboarding message dispatched to {client_name} ({client_phone}).")
        # print(msg) # مفيد للعرض أثناء الـ Local Development
        
    except Exception as exc:
        logger.warning(f"⚠️ [AI WELCOME BOT]: Delivery failed for {client_name}. Retrying... ({self.request.retries}/3)")
        raise self.retry(exc=exc)

# =====================================================================
# 📧 Helper: إرسال تحذير تجديد للعميل (email — مع stub لـ WhatsApp)
# =====================================================================
def _send_subscription_warning(tenant, *, days_left: int, is_trial: bool):
    """
    يبعت email للـ tenant بإن اشتراكه/تجربته قربت تنتهي. لو فيه phone بنـ log
    رسالة WhatsApp جاهزة للإرسال (الـ provider integration نقطة توسعة لاحقة).
    Idempotency: cache key per (tenant, days_left) يمنع إعادة الإرسال نفس اليوم.
    """
    from django.core.cache import cache
    from django.conf import settings
    from django.core.mail import send_mail
    import os

    cache_key = f"dunning_sent:{tenant.schema_name}:{days_left}:{timezone.now().date()}"
    if cache.get(cache_key):
        return  # اتبعت دلوقتي — متبعتش تاني

    base_domain = os.getenv('BASE_DOMAIN', getattr(settings, 'BASE_DOMAIN', 'mousstec.com'))
    pricing_url = f"https://{base_domain}/pricing/?shop={tenant.schema_name}"

    if is_trial:
        period_word = "الفترة التجريبية"
    else:
        period_word = "اشتراك Mouss Tec"

    if days_left == 0:
        subject = f"⚠️ {period_word} ينتهي اليوم — Mouss Tec"
        body_intro = f"عزيزي {tenant.name}،\n\n{period_word} الخاصة بشركتك تنتهي اليوم."
    elif days_left == 1:
        subject = f"⏰ {period_word} ينتهي بكرة — Mouss Tec"
        body_intro = f"عزيزي {tenant.name}،\n\n{period_word} الخاصة بشركتك تنتهي خلال يوم واحد."
    else:
        subject = f"🔔 {period_word} ينتهي خلال {days_left} أيام — Mouss Tec"
        body_intro = f"عزيزي {tenant.name}،\n\n{period_word} الخاصة بشركتك تنتهي خلال {days_left} أيام."

    body = (
        f"{body_intro}\n\n"
        f"لضمان عدم انقطاع الخدمة، يرجى التجديد من خلال:\n{pricing_url}\n\n"
        f"بعد انتهاء الاشتراك بـ 3 أيام، يدخل النظام في وضع 'القراءة فقط'، "
        f"وبعدها يتم تعليق الوصول حتى التجديد.\n\n"
        f"شكراً لثقتك في Mouss Tec 🚀"
    )

    # Email: لو SMTP متضبط
    recipient = (tenant.email or '').strip()
    if recipient and getattr(settings, 'EMAIL_HOST', ''):
        send_mail(
            subject=subject,
            message=body,
            from_email=None,  # DEFAULT_FROM_EMAIL
            recipient_list=[recipient],
            fail_silently=True,
        )

    # WhatsApp: stub — لما الـ provider يتربط، اللي بعد الـ log هيتبعت فعلاً
    if tenant.phone:
        logger.info(
            f"📱 [DUNNING/WhatsApp queued] {tenant.name} ({tenant.phone}): "
            f"{period_word} ينتهي خلال {days_left} يوم — {pricing_url}"
        )

    cache.set(cache_key, True, timeout=86400)  # 24 hours


# =====================================================================
# 📢 Broadcast campaign sender — runs in background to avoid HTTP timeout
# =====================================================================
@shared_task(bind=True, name='clients.tasks.send_broadcast_campaign')
def send_broadcast_campaign(self, campaign_id):
    from clients.models import BroadcastCampaign
    from clients.services.broadcast import send_campaign
    try:
        campaign = BroadcastCampaign.objects.get(pk=campaign_id)
    except BroadcastCampaign.DoesNotExist:
        logger.warning("send_broadcast_campaign: campaign #%s not found", campaign_id)
        return
    try:
        send_campaign(campaign)
    except Exception as e:
        logger.exception("send_broadcast_campaign #%s failed", campaign_id)
        campaign.status = 'failed'
        campaign.error_log = f"{type(e).__name__}: {e}"
        campaign.save(update_fields=['status', 'error_log'])


# =====================================================================
# 💸 2. وكيل إدارة المطالبات والتعليق (Dunning & Grace Period Enforcer)
# =====================================================================
@shared_task
def orchestrate_billing_and_suspensions():
    """
    🚀 ابتكار Dunning Management:
    لا يغلق الحسابات فجأة! يرسل تحذيرات قبل الانتهاء بـ 3 أيام، ويحترم فترة السماح (Grace Period)
    ويطبق الـ Bulk Update لتفادي الـ N+1 Queries.
    """
    today = timezone.now().date()
    # نبعت تحذيرات في أكتر من نقطة عشان العميل ميتفاجئش — 3 أيام، يوم واحد، يوم
    # الانتهاء نفسه. الـ orchestrate task بيتشغّل يومياً، فكل tenant بياخد على
    # الأكتر notification واحد لكل warning window.
    warning_windows = [3, 1, 0]  # أيام متبقية للتحذير
    grace_period_deadline = today - timedelta(days=3)

    try:
        # -------------------------------------------------------------
        # أ. مرحلة التحذير الاستباقي (Early Warning - Retention)
        # -------------------------------------------------------------
        warned_count = 0
        for days_ahead in warning_windows:
            warning_date = today + timedelta(days=days_ahead)
            expiring_trials = Client.objects.filter(
                status='trial', trial_ends_at=warning_date, is_active=True
            ).exclude(schema_name='public')
            expiring_active = Client.objects.filter(
                status='active', subscription_end_date=warning_date, is_active=True
            ).exclude(schema_name='public')

            for tenant in list(expiring_trials) + list(expiring_active):
                try:
                    _send_subscription_warning(
                        tenant,
                        days_left=days_ahead,
                        is_trial=(tenant.status == 'trial'),
                    )
                    warned_count += 1
                except Exception as send_err:
                    logger.warning(
                        f"⚠️ [DUNNING]: notification failed for {tenant.schema_name} "
                        f"({days_ahead}d left) — {send_err}"
                    )

        if warned_count > 0:
            logger.info(f"🔔 [DUNNING SYSTEM]: {warned_count} renewal warnings dispatched.")

        # -------------------------------------------------------------
        # ب. مرحلة الإغلاق الناعم (Soft/Hard Suspension) - Bulk Update
        # -------------------------------------------------------------
        # 1. إيقاف الفترات التجريبية المنتهية (التريال ليس له فترة سماح)
        expired_trials = Client.objects.filter(status='trial', trial_ends_at__lt=today, is_active=True).exclude(schema_name='public')
        trials_suspended = expired_trials.update(status='suspended')

        # 2. إيقاف الاشتراكات المدفوعة التي استنفدت فترة السماح (Grace Period Ended)
        expired_active = Client.objects.filter(status='active', subscription_end_date__lt=grace_period_deadline, is_active=True).exclude(schema_name='public')
        active_suspended = expired_active.update(status='suspended')
        
        total_suspended = trials_suspended + active_suspended
        if total_suspended > 0:
            logger.info(f"🔒 [CRON GUARD]: Successfully suspended {trials_suspended} trials and {active_suspended} expired subscriptions.")
            
        return f"Billing Cron Orchestrated: {warned_count} Warned | {total_suspended} Suspended Safely."
        
    except Exception as e:
        logger.error(f"🔴 [CRON GUARD FATAL ERROR]: Billing orchestration crashed - {e}")
        return f"Failed: {e}"

# =====================================================================
# 🤖 3. وكيل الثقة ومكافحة الاحتيال (AI Time-Decay Trust Orchestrator)
# =====================================================================
@shared_task
def update_market_trust_scores():
    """
    محرك تقييم التجار بالذكاء الاصطناعي:
    🚀 ابتكار: يستخدم الـ Bulk Update لتحديث 100,000 تاجر في ثوانٍ.
    🚀 ابتكار: خوارزمية التآكل الزمني (Time-Decay) لمعاقبة الخمول.
    """
    # جلب التجار النشطين فقط
    active_merchants = Client.objects.filter(is_active=True, is_marketplace_active=True)
    
    updates_to_save = []
    auto_banned_count = 0
    today = timezone.now().date()
    
    for tenant in active_merchants:
        try:
            base_score = 100
            
            # 1. عقوبة النزاعات (Dispute Penalty - Harsh)
            if tenant.dispute_rate > 0:
                base_score -= int(tenant.dispute_rate * 3) # تغليظ العقوبة لردع المحتالين
            
            # 2. مكافأة الإنجاز وخوارزمية التآكل (Velocity & Decay Algorithm)
            # نحسب عمر حساب التاجر بالشهور
            account_age_days = (today - tenant.created_on).days if tenant.created_on else 1
            account_age_months = max(account_age_days / 30.0, 1)
            
            # سرعة الصفقات (Deals per month)
            velocity = getattr(tenant, 'successful_deals', 0) / account_age_months
            
            if velocity > 5: # تاجر نشط جداً
                base_score += 10
            elif velocity < 0.5 and account_age_months > 3: # تاجر خامل لأكثر من 3 شهور
                base_score -= 5 # عقوبة التآكل الزمني

            # ضبط النقاط النهائية
            new_trust_score = max(min(base_score, 100), 1)
            
            # تحديث الكائن في الذاكرة (Memory)
            tenant.ai_trust_score = new_trust_score
            
            # 3. الحظر الآلي لحماية السوق (Auto-Ban Mechanism)
            if tenant.ai_trust_score < 40 and not getattr(tenant, 'is_fraud_flagged', False):
                tenant.is_fraud_flagged = True
                auto_banned_count += 1
                logger.critical(f"🚨 [AI SHIELD]: Tenant '{tenant.schema_name}' AUTO-BANNED (Score: {tenant.ai_trust_score}).")
                
            updates_to_save.append(tenant)
            
        except Exception as e:
            logger.error(f"🔴 [AI SHIELD ERROR]: Calculation failed for '{tenant.schema_name}' - {e}")
            continue
            
    # ⚡ تحديث مجمع (Bulk Update) لدفع البيانات دفعة واحدة بدلاً من استعلامات N+1 الخانقة
    if updates_to_save:
        try:
            # تقسيم التحديثات لكتل (Batches) لتفادي اختناق الذاكرة في قواعد البيانات الضخمة
            Client.objects.bulk_update(updates_to_save, ['ai_trust_score', 'is_fraud_flagged'], batch_size=1000)
            logger.info(f"🛡️ [AI SHIELD]: Trust scores synchronized for {len(updates_to_save)} merchants.")
        except Exception as e:
            logger.error(f"🔴 [AI SHIELD FATAL]: Bulk update crashed - {e}")
            
    return f"AI Trust Orchestrator: Synced {len(updates_to_save)} merchants. Banned {auto_banned_count} fraudsters."


# =====================================================================
# 🌐 4. وكيل مزامنة سوق B2B (B2B Marketplace Sync Task)
# =====================================================================

@shared_task(bind=True, max_retries=3, default_retry_delay=30, name='clients.tasks.async_sync_b2b_marketplace_product')
def async_sync_b2b_marketplace_product(self, schema_name: str, product_id: int):
    """
    يتلقى الإشارة من وكيل B2B Sync في inventory/signals.py.
    يُحدِّث أو يُنشئ إدخالاً في GlobalB2BMarketplace بالسوق المركزي العام (public schema).

    السلسلة: تغيير المخزون → B2B Sync Agent (signal) → هذه المهمة → GlobalB2BMarketplace
    """
    def _execute():
        from django_tenants.utils import schema_context
        from clients.models import GlobalB2BMarketplace, Client
        from django.apps import apps

        # 1. قراءة بيانات المنتج من سكيما الورشة
        with schema_context(schema_name):
            Product   = apps.get_model('inventory', 'Product')
            Inventory = apps.get_model('inventory', 'Inventory')

            product = Product.objects.filter(pk=product_id).first()
            if not product:
                logger.warning(f"[B2B SYNC] Product {product_id} not found in schema '{schema_name}'.")
                return

            total_qty = Inventory.objects.filter(
                product=product
            ).aggregate(s=Sum('quantity'))['s'] or 0

            # إذا المخزون صفر أو سالب نحذف من السوق بدلاً من المزامنة
            if total_qty <= 0:
                with schema_context('public'):
                    GlobalB2BMarketplace.objects.filter(
                        tenant__schema_name=schema_name,
                        part_number=product.part_number,
                        condition=product.condition,
                    ).delete()
                logger.info(f"🛑 [B2B SYNC] Removed zero-stock product P/N {product.part_number} from market.")
                return

            listing_data = {
                'seller_schema':  schema_name,
                'part_number':    product.part_number,
                'name':           product.name,
                'brand':          getattr(product, 'brand', ''),
                'condition':      product.condition,
                'available_qty':  total_qty,
                'asking_price':   float(product.sale_price or product.average_cost or 0),
                'average_cost':   float(product.average_cost or 0),
            }

        # 2. الكتابة في سكيما السوق المركزي (public)
        with schema_context('public'):
            tenant = Client.objects.filter(schema_name=schema_name).first()
            if not tenant:
                return

            with transaction.atomic():
                listing, created = GlobalB2BMarketplace.objects.update_or_create(
                    tenant=tenant,
                    part_number=listing_data['part_number'],
                    condition=listing_data['condition'],
                    defaults={
                        'product_name':   listing_data['name'],
                        'brand':          listing_data['brand'],
                        'available_qty':  listing_data['available_qty'],
                        'wholesale_price': listing_data['asking_price'],
                    }
                )

        action = "created" if created else "updated"
        AgentEventBus.set_agent_state(
            'b2b_marketplace_sync_task', schema=schema_name,
            state={'last_product_id': product_id, 'action': action}
        )
        logger.info(
            f"🌐 [B2B SYNC TASK] P/N {listing_data['part_number']} "
            f"{action} in global market. Schema: {schema_name}"
        )

    try:
        return run_agent_safely(
            agent_name='b2b_marketplace_sync_task',
            func=_execute,
            payload={'schema': schema_name, 'product_id': product_id},
            schema=schema_name,
            failure_threshold=3,
            reraise=True,
        )
    except Exception as exc:
        logger.warning(f"⚠️ [B2B SYNC TASK] Retrying ({self.request.retries}/3)… {exc}")
        raise self.retry(exc=exc)


# =====================================================================
# 🗑️ 5. وكيل حذف المنتج من سوق B2B (B2B Marketplace Remove Task)
# =====================================================================

@shared_task(bind=True, max_retries=3, default_retry_delay=30, name='clients.tasks.async_remove_b2b_marketplace_product')
def async_remove_b2b_marketplace_product(self, schema_name: str, part_number: str, condition: str):
    """
    يُزيل منتجاً من GlobalB2BMarketplace عند حذف سجل المخزون كلياً.
    يُستدعى من post_delete signal في inventory/signals.py.
    """
    def _execute():
        from django_tenants.utils import schema_context
        from clients.models import GlobalB2BMarketplace

        with schema_context('public'):
            deleted_count, _ = GlobalB2BMarketplace.objects.filter(
                tenant__schema_name=schema_name,
                part_number=part_number,
                condition=condition,
            ).delete()

        AgentEventBus.set_agent_state(
            'b2b_marketplace_remove_task', schema=schema_name,
            state={'part_number': part_number, 'deleted': deleted_count}
        )
        logger.info(
            f"🛑 [B2B REMOVE TASK] Removed {deleted_count} listing(s) for "
            f"P/N '{part_number}' from schema '{schema_name}'."
        )

    try:
        return run_agent_safely(
            agent_name='b2b_marketplace_remove_task',
            func=_execute,
            payload={'schema': schema_name, 'part_number': part_number, 'condition': condition},
            schema=schema_name,
            failure_threshold=3,
            reraise=True,
        )
    except Exception as exc:
        raise self.retry(exc=exc)


# =====================================================================
# 🤖 6. وكيل الترسية الذكية للمزادات (AI Bidding Award Agent)
# =====================================================================

@shared_task(bind=True, max_retries=2, default_retry_delay=60, name='clients.tasks.process_ai_bidding_award')
def process_ai_bidding_award(self, bid_id: int):
    """
    يُشغِّله الـ Ecosystem Watchdog عند انتهاء وقت مزاد مفتوح.
    يطبق خوارزمية ترسية AI:
        1. يُرتِّب العروض حسب السعر + درجة الثقة (ai_trust_score)
        2. يُرسي المزاد على أفضل عرض
        3. يُحرِّك Escrow ويُرسل إشعارات

    Pipeline: Watchdog → هذه المهمة → trigger_release_to_seller → إشعار B2B
    """
    def _execute():
        from django_tenants.utils import schema_context
        from clients.models import BlindBiddingRequest, BidOffer, Client

        with schema_context('public'):
            bid = BlindBiddingRequest.objects.select_related('buyer').get(pk=bid_id)

            if bid.status not in ('awarding', 'open'):
                logger.info(f"[AI BIDDING AWARD] Bid #{bid_id} already processed (status={bid.status}).")
                return

            offers = BidOffer.objects.filter(
                bidding_request=bid
            ).select_related('seller').order_by('offer_price')

            if not offers.exists():
                bid.status = 'cancelled'
                bid.save(update_fields=['status'])
                logger.info(f"[AI BIDDING AWARD] Bid #{bid_id}: no offers → cancelled.")
                return

            # ── Scoring Algorithm ──────────────────────────────────
            # Score = 100 - price_rank_score + trust_bonus
            # Lower price = better rank; higher trust = bonus points
            scored_offers = []
            min_price = float(offers.first().offer_price)

            for offer in offers:
                price_score  = (float(offer.offer_price) / max(min_price, 1)) * 50
                trust_score  = getattr(offer.seller, 'ai_trust_score', 50) * 0.5
                final_score  = trust_score - price_score + 100
                scored_offers.append((final_score, offer))

            scored_offers.sort(key=lambda x: x[0], reverse=True)
            winning_score, winning_offer = scored_offers[0]

            with transaction.atomic():
                bid.status        = 'completed'
                bid.winner        = winning_offer.seller
                bid.winning_price = winning_offer.offer_price
                bid.save(update_fields=['status', 'winner', 'winning_price'])

                # حرِّك Escrow للبائع الفائز
                bid.trigger_release_to_seller()

            # إشعار البائع الفائز (عبر Celery)
            from celery import current_app
            try:
                current_app.send_task(
                    'clients.tasks.async_welcome_bot_task',
                    args=[
                        winning_offer.seller.name,
                        getattr(winning_offer.seller, 'phone', ''),
                        'parts_trader',
                        f"تهانينا! فزت بمزاد #{bid_id} بسعر {winning_offer.offer_price} جنيه.",
                        '',
                    ],
                )
            except Exception as notif_exc:
                logger.warning(f"⚠️ [AI BIDDING AWARD] Notification failed: {notif_exc}")

            AgentEventBus.set_agent_state(
                'ai_bidding_award_agent', schema='public',
                state={
                    'bid_id':        bid_id,
                    'winner_schema': winning_offer.seller.schema_name,
                    'price':         float(winning_offer.offer_price),
                    'score':         winning_score,
                }
            )
            AgentEventBus.push_pipeline_event(
                'bid_awarded',
                data={'bid_id': bid_id, 'winner': winning_offer.seller.schema_name},
                schema='public',
            )
            logger.info(
                f"⚖️ [AI BIDDING AWARD] Bid #{bid_id} awarded to "
                f"'{winning_offer.seller.schema_name}' at {winning_offer.offer_price} EGP "
                f"(AI score: {winning_score:.1f})."
            )

    try:
        return run_agent_safely(
            agent_name='ai_bidding_award_agent',
            func=_execute,
            payload={'bid_id': bid_id},
            schema='public',
            failure_threshold=3,
            reraise=True,
        )
    except Exception as exc:
        logger.error(f"🔴 [AI BIDDING AWARD] Retrying ({self.request.retries}/2)… {exc}")
        raise self.retry(exc=exc)


# =====================================================================
# 💬 4. تنظيف محادثات التصميم المهجورة (Stale Design-Chat Sweeper)
# =====================================================================
# Periodic — schedule via celery beat (e.g. every 15 min). Mass-UPDATEs
# idle planning/generated/refining conversations to 'abandoned' so the
# resume banner doesn't surface dead shells and the lock column stays
# clean. The actual logic lives in clients.services.design_chat —
# this wrapper exists only so beat can call it without importing Django
# management commands.
# =====================================================================
@shared_task(name='clients.tasks.cleanup_stale_design_conversations')
def cleanup_stale_design_conversations(idle_minutes: int | None = None):
    """Sweep stale design-chat sessions → 'abandoned'.

    Args:
        idle_minutes: override settings.DESIGN_CHAT_IDLE_MINUTES if you want
                      a different cutoff for this run (rarely useful — leave
                      None for the default 60-minute window).

    Returns the service's audit dict so beat logs / Flower see the counts:
      {'inspected', 'abandoned', 'cutoff', 'dry_run', 'by_stage'}.

    Safe under feature flag: if DESIGN_CHAT_ENABLED is False the query
    still runs but finds nothing (table either empty or already swept).
    No behavioural side effect — kept as a no-op rather than a hard skip
    so re-enabling the flag doesn't leave a backlog to clear.
    """
    from django.conf import settings as _settings
    from clients.services.design_chat import prune_stale_conversations

    try:
        result = prune_stale_conversations(
            idle_minutes=idle_minutes,
            dry_run=False,
        )
    except Exception as exc:
        logger.error(f"🔴 [DESIGN CHAT SWEEP] failed: {exc}", exc_info=True)
        return {'error': str(exc), 'abandoned': 0}

    if result.get('abandoned'):
        logger.info(
            f"🧹 [DESIGN CHAT SWEEP] abandoned={result['abandoned']} "
            f"by_stage={result['by_stage']} cutoff={result['cutoff']}"
        )
    return result

# ─────────────────────────────────────────────────────────────────────
# 🔐 OBD device security — replay-protection nonce cleanup
# ─────────────────────────────────────────────────────────────────────
@shared_task(name='clients.tasks.purge_obd_device_nonces')
def purge_obd_device_nonces(retention_seconds: int = 900):
    """Delete OBDDeviceNonce rows older than `retention_seconds`.

    The nonce table only needs to remember entries inside the largest active
    replay window (default 300s). We keep 3× that as a safety margin so a
    misconfigured device with a wider window can't accidentally accept a
    replay. Runs every 5 minutes via Celery Beat — see CELERY_BEAT_SCHEDULE.
    """
    from clients.obd_device_models import OBDDeviceNonce

    try:
        deleted = OBDDeviceNonce.purge_older_than(seconds=retention_seconds)
    except Exception as exc:
        logger.error(f"🔴 [OBD NONCE PURGE] failed: {exc}", exc_info=True)
        return {'error': str(exc), 'deleted': 0}

    if deleted:
        logger.info(f"🧹 [OBD NONCE PURGE] deleted={deleted} "
                    f"retention={retention_seconds}s")
    return {'deleted': deleted, 'retention_seconds': retention_seconds}


# ─────────────────────────────────────────────────────────────────────
# 🚗 P2P Parts Marketplace — auto-release escrow after warranty expires
# ─────────────────────────────────────────────────────────────────────
@shared_task(name='clients.tasks.release_expired_parts_escrow')
def release_expired_parts_escrow():
    """Release escrow for delivered part orders whose warranty has expired.

    Iterates ``PartOrder`` rows with ``status='delivered'`` and
    ``warranty_ends_at`` in the past, moves them to ``released`` and notifies
    the seller. Runs hourly via Celery Beat — see CELERY_BEAT_SCHEDULE.
    """
    try:
        from clients.views.parts_marketplace_views import auto_release_expired_warranties
        n = auto_release_expired_warranties()
    except Exception as exc:
        logger.error(f"🔴 [PARTS ESCROW RELEASE] failed: {exc}", exc_info=True)
        return {'error': str(exc), 'released': 0}
    if n:
        logger.info(f"💰 [PARTS ESCROW RELEASE] released={n} orders")
    return {'released': n}


# ─────────────────────────────────────────────────────────────────────
# 🎨 Design persistence — async fallback when inline persist fails
# ─────────────────────────────────────────────────────────────────────
@shared_task(
    bind=True, max_retries=5,
    default_retry_delay=60,           # 1m, then 2m, 4m, 8m, 16m via autoretry
    name='clients.tasks.persist_design_image_async',
)
def persist_design_image_async(self, design_pk: int):
    """Async fallback: re-persist a CustomerDesign whose inline persist failed.

    The synchronous path in design_chat_views catches a RuntimeError and
    enqueues this task with the original (still-temporarily-valid) provider
    URL stored on the row. We download, generate WebP variants, and update
    the row in place.

    Idempotent: if `image_persisted_at` is already set, returns early — beat
    schedulers and manual reruns won't double-persist.
    """
    from clients.models import CustomerDesign
    from clients.services.design_persistence import persist_image_with_variants

    try:
        design = CustomerDesign.objects.get(pk=design_pk)
    except CustomerDesign.DoesNotExist:
        logger.warning(f"[ASYNC PERSIST] design #{design_pk} vanished — nothing to do")
        return {'status': 'gone'}

    if design.image_persisted_at:
        return {'status': 'already_persisted'}

    try:
        result = persist_image_with_variants(
            request=None,
            customer=design.customer,
            provider_image_url=design.image_url,
            prefix='design_chat',
        )
    except RuntimeError as exc:
        # Provider URL probably expired between sync-failure and this task
        # firing. Retry with backoff; Celery surfaces final failure to DLQ.
        logger.warning(f"[ASYNC PERSIST] #{design_pk} fetch failed: {exc}")
        raise self.retry(exc=exc)

    design.image_url = result['image_url'][:600]
    design.image_thumb_url = (result['thumb_url'] or '')[:600]
    design.image_preview_url = (result['preview_url'] or '')[:600]
    design.image_persisted_at = timezone.now()
    if result['size_bytes'] is not None:
        design.image_size_bytes = result['size_bytes']
    design.save(update_fields=[
        'image_url', 'image_thumb_url', 'image_preview_url',
        'image_persisted_at', 'image_size_bytes',
    ])
    logger.info(f"[ASYNC PERSIST] #{design_pk} recovered via async retry")
    return {'status': 'persisted', 'design_pk': design_pk}


# ─────────────────────────────────────────────────────────────────────
# 🧹 Design storage — daily self-healing audit
# ─────────────────────────────────────────────────────────────────────
@shared_task(name='clients.tasks.audit_design_storage_daily')
def audit_design_storage_daily():
    """Run the storage audit as a periodic safety net.

    Catches anything the sync + async paths missed: ephemeral URLs that
    slipped through (e.g. Celery worker was down), or variant backfills
    for legacy rows. Idempotent — already-persisted rows are skipped by
    the underlying service.

    Bounded by `limit=200` per job so a backlog can't melt the worker.
    Returns the call's audit counts for beat logs / Flower visibility.
    """
    from io import StringIO
    from django.core.management import call_command

    out = {'repair': '', 'backfill': '', 'error': None}
    try:
        buf = StringIO()
        call_command(
            'audit_design_storage', repair=True, flag_broken=True,
            limit=200, stdout=buf,
        )
        out['repair'] = buf.getvalue()[-500:]

        buf = StringIO()
        call_command(
            'audit_design_storage', backfill_variants=True,
            limit=200, stdout=buf,
        )
        out['backfill'] = buf.getvalue()[-500:]
    except Exception as exc:
        logger.error(f"🔴 [DESIGN STORAGE AUDIT] failed: {exc}", exc_info=True)
        out['error'] = str(exc)

    return out
