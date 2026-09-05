"""
Celery tasks for Social Studio.

Beat-driven (see erp_core.settings.CELERY_BEAT_SCHEDULE):
  • social_ads.publish_due_posts   — every 5 min: publish scheduled posts now due.
  • social_ads.sync_all_insights   — hourly: refresh post + campaign performance.
  • social_ads.run_autopilot       — daily: generate + schedule the next batch.
  • social_ads.run_learning        — daily: distill performance into StrategyMemory.
  • social_ads.optimize_all_ads    — every 6h: rebalance live campaign budgets.

Per-object workers (dispatched by the sweepers or the dashboard):
  • social_ads.publish_post        — publish one SocialPost to FB/IG.
  • social_ads.launch_campaign     — create + start one AdCampaign on Meta.

All models live in the PUBLIC schema, so tasks query configs directly without a
schema context. Every task is contained: one tenant's failure logs and moves on,
never crashing the worker or blocking the others.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("mouss_tec_core")


# =====================================================================
# Sweepers (beat)
# =====================================================================
@shared_task(name="social_ads.publish_due_posts")
def publish_due_posts():
    """Find SCHEDULED posts whose time has come and dispatch a publish per post."""
    from .models import SocialPost

    due = SocialPost.objects.filter(
        status=SocialPost.Status.SCHEDULED,
        scheduled_at__lte=timezone.now(),
    ).values_list("id", flat=True)[:200]
    n = 0
    for post_id in due:
        publish_post.delay(post_id)
        n += 1
    if n:
        logger.info("social_ads: dispatched %d due posts", n)
    return {"dispatched": n}


@shared_task(name="social_ads.sync_all_insights")
def sync_all_insights():
    """Refresh performance for recently published posts + live campaigns."""
    from datetime import timedelta

    from .models import AdCampaign, SocialAdsConfig, SocialPost

    since = timezone.now() - timedelta(days=30)
    posts = SocialPost.objects.filter(
        status=SocialPost.Status.PUBLISHED, published_at__gte=since,
    ).exclude(fb_post_id="").values_list("id", flat=True)[:500]
    for pid in posts:
        sync_post_insights.delay(pid)

    camps = AdCampaign.objects.filter(
        status__in=[AdCampaign.Status.ACTIVE, AdCampaign.Status.PAUSED],
    ).exclude(meta_campaign_id="").values_list("id", flat=True)[:500]
    for cid in camps:
        sync_campaign_insights.delay(cid)

    return {"posts": len(posts), "campaigns": len(camps)}


@shared_task(name="social_ads.run_autopilot")
def run_autopilot():
    """Generate + schedule the next content batch for every operational tenant."""
    from .models import SocialAdsConfig
    from .services import strategist

    count = 0
    for config in _operational_configs():
        if config.autopilot_mode == SocialAdsConfig.Autopilot.OFF:
            continue
        try:
            res = strategist.plan_week(config)
            count += res.get("created", 0)
        except Exception:
            logger.exception("social_ads: autopilot failed for %s", config.tenant.schema_name)
    logger.info("social_ads: autopilot created %d posts across tenants", count)
    return {"created": count}


@shared_task(name="social_ads.run_learning")
def run_learning():
    """Update every operational tenant's StrategyMemory from recent performance."""
    from .services import strategist

    learned = 0
    for config in _operational_configs():
        try:
            res = strategist.learn(config)
            if res.get("learned"):
                learned += 1
        except Exception:
            logger.exception("social_ads: learning failed for %s", config.tenant.schema_name)
    return {"tenants_learned": learned}


@shared_task(name="social_ads.optimize_all_ads")
def optimize_all_ads():
    """Rebalance live ad budgets for every tenant that enabled auto-optimize."""
    from .services import strategist

    optimized = 0
    for config in _operational_configs():
        try:
            res = strategist.optimize(config)
            optimized += res.get("optimized", 0)
        except Exception:
            logger.exception("social_ads: optimize failed for %s", config.tenant.schema_name)
    return {"actions": optimized}


# =====================================================================
# Per-object workers
# =====================================================================
@shared_task(
    name="social_ads.publish_post", bind=True, max_retries=2,
    default_retry_delay=30, acks_late=True,
)
def publish_post(self, post_id: int):
    """Publish one SocialPost to Facebook and/or Instagram."""
    from .models import SocialPost
    from .services import meta_marketing

    try:
        post = SocialPost.objects.select_related("config", "tenant").get(pk=post_id)
    except SocialPost.DoesNotExist:
        logger.warning("social_ads: post %s vanished before publish", post_id)
        return

    config = post.config
    # Gate: only publish SCHEDULED (or a manual re-try of FAILED) while subscribed.
    if post.status not in (SocialPost.Status.SCHEDULED, SocialPost.Status.FAILED):
        logger.info("social_ads: post %s not publishable (status=%s)", post_id, post.status)
        return
    if not config.is_operational:
        post.status = SocialPost.Status.FAILED
        post.error = "الاشتراك غير فعّال أو بيانات الربط ناقصة."
        post.save(update_fields=["status", "error", "updated_at"])
        return

    post.status = SocialPost.Status.PUBLISHING
    post.publish_attempts = (post.publish_attempts or 0) + 1
    post.save(update_fields=["status", "publish_attempts", "updated_at"])

    # Generate the image LAZILY now (fresh, publicly-fetchable URL for Meta) if the
    # post has a visual brief but no image yet and the tenant wants images.
    if config.generate_images and post.image_prompt and not post.image_url:
        from .services.strategist import _maybe_generate_image
        img_url = _maybe_generate_image(config, post.image_prompt)
        if img_url:
            post.image_url = img_url
            post.save(update_fields=["image_url", "updated_at"])

    token = config.page_access_token
    want_fb = post.platform in (SocialPost.Platform.FACEBOOK, SocialPost.Platform.BOTH) and config.has_facebook()
    want_ig = post.platform in (SocialPost.Platform.INSTAGRAM, SocialPost.Platform.BOTH) and config.has_instagram()

    published_any = False
    errors = []

    if want_fb:
        try:
            res = meta_marketing.publish_facebook_post(
                access_token=token, page_id=config.facebook_page_id,
                message=post.full_text, image_url=post.image_url,
                link=config.website_url or "",
            )
            post.fb_post_id = res.get("post_id") or res.get("id") or ""
            if post.fb_post_id:
                post.permalink = meta_marketing.get_post_permalink(
                    access_token=token, post_id=post.fb_post_id) or post.permalink
            published_any = True
        except meta_marketing.MetaMarketingError as exc:
            errors.append(f"FB: {exc}")

    if want_ig:
        if not post.image_url:
            errors.append("IG: يتطلب إنستجرام صورة — تم تخطّي النشر على إنستجرام.")
        else:
            try:
                res = meta_marketing.publish_instagram_post(
                    access_token=token, ig_user_id=config.instagram_account_id,
                    image_url=post.image_url, caption=post.full_text,
                )
                post.ig_media_id = res.get("id") or ""
                published_any = True
            except meta_marketing.MetaMarketingError as exc:
                errors.append(f"IG: {exc}")

    if published_any:
        post.status = SocialPost.Status.PUBLISHED
        post.published_at = timezone.now()
        post.error = "؛ ".join(errors)  # partial-failure note, if any
        post.save(update_fields=["status", "published_at", "fb_post_id", "ig_media_id",
                                 "permalink", "error", "updated_at"])
        logger.info("social_ads: published post %s (fb=%s ig=%s)", post_id, post.fb_post_id, post.ig_media_id)
        if config.notify_on_publish:
            _notify(config, f"تم نشر بوست جديد ✅\n\n{post.caption[:200]}")
        return

    # Nothing published.
    post.status = SocialPost.Status.FAILED
    post.error = "؛ ".join(errors) or "فشل النشر لسبب غير معروف."
    post.save(update_fields=["status", "error", "updated_at"])
    logger.error("social_ads: publish failed for post %s: %s", post_id, post.error)
    if (post.publish_attempts or 0) < 3:
        raise self.retry(exc=meta_marketing.MetaMarketingError(post.error))


@shared_task(name="social_ads.sync_post_insights")
def sync_post_insights(post_id: int):
    """Fetch + persist performance for one published post."""
    from .models import PerformanceSnapshot, SocialPost
    from .services import meta_marketing

    try:
        post = SocialPost.objects.select_related("config").get(pk=post_id)
    except SocialPost.DoesNotExist:
        return
    if not post.fb_post_id or not post.config.page_access_token:
        return

    data = meta_marketing.fetch_post_insights(
        access_token=post.config.page_access_token, post_id=post.fb_post_id)
    post.reach = data["reach"]
    post.impressions = data["impressions"]
    post.likes = data["likes"]
    post.comments = data["comments"]
    post.shares = data["shares"]
    post.clicks = data["clicks"]
    post.recompute_engagement()
    post.insights_synced_at = timezone.now()
    post.save(update_fields=["reach", "impressions", "likes", "comments", "shares",
                             "clicks", "engagement_rate", "insights_synced_at", "updated_at"])

    PerformanceSnapshot.objects.create(
        tenant=post.tenant, kind=PerformanceSnapshot.Kind.POST, post=post,
        reach=post.reach, impressions=post.impressions,
        interactions=post.likes + post.comments + post.shares,
        clicks=post.clicks, engagement_rate=post.engagement_rate,
    )


@shared_task(name="social_ads.sync_campaign_insights")
def sync_campaign_insights(campaign_id: int):
    """Fetch + persist performance for one ad campaign."""
    from decimal import Decimal

    from .models import AdCampaign, PerformanceSnapshot
    from .services import meta_marketing

    try:
        camp = AdCampaign.objects.select_related("config").get(pk=campaign_id)
    except AdCampaign.DoesNotExist:
        return
    if not camp.meta_campaign_id or not camp.config.page_access_token:
        return

    data = meta_marketing.fetch_campaign_insights(
        access_token=camp.config.page_access_token, campaign_id=camp.meta_campaign_id)
    camp.spend = Decimal(str(data["spend"]))
    camp.reach = data["reach"]
    camp.impressions = data["impressions"]
    camp.clicks = data["clicks"]
    camp.results = data["results"]
    camp.recompute_ctr()
    camp.insights_synced_at = timezone.now()
    camp.save(update_fields=["spend", "reach", "impressions", "clicks", "results",
                             "ctr", "cost_per_result", "insights_synced_at", "updated_at"])

    PerformanceSnapshot.objects.create(
        tenant=camp.tenant, kind=PerformanceSnapshot.Kind.CAMPAIGN, campaign=camp,
        reach=camp.reach, impressions=camp.impressions, clicks=camp.clicks,
        spend=camp.spend, results=camp.results,
    )


@shared_task(name="social_ads.launch_campaign", bind=True, max_retries=1, default_retry_delay=30)
def launch_campaign(self, campaign_id: int, *, activate: bool = False):
    """Create a campaign → ad set → ad on Meta. Starts PAUSED unless `activate`.

    Requires a promotable published post (post.fb_post_id) so the ad boosts real
    organic content. Guards on the tenant's monthly budget cap.
    """
    from decimal import Decimal

    from .models import AdCampaign
    from .services import meta_marketing

    try:
        camp = AdCampaign.objects.select_related("config", "post", "tenant").get(pk=campaign_id)
    except AdCampaign.DoesNotExist:
        return

    config = camp.config
    if not config.can_run_ads():
        camp.status = AdCampaign.Status.FAILED
        camp.error = "الإعلانات غير مفعّلة أو حساب الإعلانات غير مربوط."
        camp.save(update_fields=["status", "error", "updated_at"])
        return

    # Budget guardrail.
    if config.remaining_ad_budget() < camp.daily_budget:
        camp.status = AdCampaign.Status.FAILED
        camp.error = "تجاوز السقف الشهري للإنفاق الإعلاني."
        camp.save(update_fields=["status", "error", "updated_at"])
        _notify(config, "تعذّر إطلاق حملة: تجاوز سقف الإنفاق الإعلاني الشهري.")
        return

    token = config.page_access_token
    daily_minor = int(Decimal(str(camp.daily_budget)) * 100)
    targeting = _build_targeting(config, camp.audience_spec)
    optimization = _objective_to_optimization(camp.objective)

    try:
        camp.meta_campaign_id = meta_marketing.create_campaign(
            access_token=token, ad_account_id=config.ad_account_id,
            name=camp.name, objective=camp.objective, status="PAUSED",
        )
        camp.meta_adset_id = meta_marketing.create_adset(
            access_token=token, ad_account_id=config.ad_account_id,
            campaign_id=camp.meta_campaign_id, name=f"{camp.name} — Ad Set",
            daily_budget_minor=daily_minor, targeting=targeting,
            optimization_goal=optimization,
        )
        if camp.post and camp.post.fb_post_id:
            camp.meta_ad_id = meta_marketing.create_ad_from_post(
                access_token=token, ad_account_id=config.ad_account_id,
                adset_id=camp.meta_adset_id, page_id=config.facebook_page_id,
                post_id=camp.post.fb_post_id, name=f"{camp.name} — Ad",
            )
        if activate:
            meta_marketing.set_campaign_status(
                access_token=token, campaign_id=camp.meta_campaign_id, status="ACTIVE")
            camp.status = AdCampaign.Status.ACTIVE
            camp.started_at = timezone.now()
        else:
            camp.status = AdCampaign.Status.PAUSED
        camp.error = ""
        camp.save()
        logger.info("social_ads: launched campaign %s (meta=%s, active=%s)",
                    campaign_id, camp.meta_campaign_id, activate)
    except meta_marketing.MetaMarketingError as exc:
        camp.status = AdCampaign.Status.FAILED
        camp.error = str(exc)
        camp.save(update_fields=["status", "error", "meta_campaign_id",
                                 "meta_adset_id", "updated_at"])
        logger.error("social_ads: launch_campaign failed %s: %s", campaign_id, exc)


# =====================================================================
# Helpers
# =====================================================================
def _operational_configs():
    """Yield every config with a live subscription (public schema, cheap query)."""
    from .models import SocialAdsConfig

    qs = SocialAdsConfig.objects.filter(is_subscription_active=True).select_related("tenant")
    for config in qs.iterator():
        if config.subscription_is_valid:
            yield config


def _build_targeting(config, audience_spec: dict) -> dict:
    """Turn the AI's loose audience_spec into a Meta targeting object.

    Conservative defaults (Egypt, broad age) when the spec is thin, so a campaign
    is always launchable even if the LLM returned little.
    """
    spec = audience_spec or {}
    geo = spec.get("geo")
    countries = ["EG"]
    if (getattr(config.tenant, "country", "EG") or "EG").upper() == "AE":
        countries = ["AE"]
    targeting = {
        "geo_locations": {"countries": countries},
        "age_min": int(spec.get("age_min", 21) or 21),
        "age_max": int(spec.get("age_max", 55) or 55),
        "publisher_platforms": ["facebook", "instagram"],
    }
    genders = spec.get("genders")
    if genders == "male":
        targeting["genders"] = [1]
    elif genders == "female":
        targeting["genders"] = [2]
    return targeting


def _objective_to_optimization(objective: str) -> str:
    return {
        "OUTCOME_AWARENESS": "REACH",
        "OUTCOME_TRAFFIC": "LINK_CLICKS",
        "OUTCOME_ENGAGEMENT": "POST_ENGAGEMENT",
        "OUTCOME_LEADS": "LEAD_GENERATION",
        "OUTCOME_SALES": "OFFSITE_CONVERSIONS",
    }.get(objective, "LINK_CLICKS")


def _notify(config, body: str):
    """Best-effort email alert. Never raises."""
    to_email = (config.notify_email or getattr(config.tenant, "email", "") or "").strip()
    if not to_email:
        return
    try:
        from django.core.mail import send_mail
        send_mail(f"[{config.tenant.name}] استوديو التسويق", body, None, [to_email], fail_silently=True)
    except Exception:
        logger.warning("social_ads: notify email failed (SMTP?)", exc_info=True)
