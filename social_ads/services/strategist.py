"""
The Social Studio strategist — the "يتعلم ويصحح" brain.

Three jobs, all pure Python over the tenant's own data (no external calls except
the LLM inside content_ai):

  • learn(config)      — read the last N published posts + their performance,
                         rank content angles / posting hours / hashtags, and
                         write the distilled playbook into StrategyMemory.
  • plan_week(config)  — generate the next batch of posts (respecting the learned
                         playbook + posts_per_week) and schedule them into the
                         best future slots. Honors the autopilot mode:
                         full → SCHEDULED, suggest → DRAFT (awaits approval).
  • optimize(config)   — shift ad budget toward the best-performing live campaigns
                         and pause the losers, within the tenant's guardrails.

Everything is defensive: a single tenant's bad data or a model hiccup logs and
returns, never raising into the Celery task.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from . import content_ai
from .content_ai import CONTENT_ANGLES

logger = logging.getLogger("mouss_tec_core")

_LEARN_WINDOW = 60  # posts to look back over
_MIN_POSTS_TO_LEARN = 4


# =====================================================================
# Memory bootstrap
# =====================================================================
def ensure_memory(config):
    from social_ads.models import StrategyMemory
    memory, _ = StrategyMemory.objects.get_or_create(
        config=config, defaults={"tenant": config.tenant}
    )
    return memory


# =====================================================================
# 1. LEARN
# =====================================================================
def learn(config) -> dict:
    """Analyze recent performance and update StrategyMemory. Returns a summary dict."""
    from social_ads.models import SocialPost

    memory = ensure_memory(config)
    posts = list(
        SocialPost.objects.filter(
            config=config, status=SocialPost.Status.PUBLISHED,
        ).order_by("-published_at")[:_LEARN_WINDOW]
    )
    measured = [p for p in posts if (p.reach or p.impressions)]
    if len(measured) < _MIN_POSTS_TO_LEARN:
        logger.info("social_ads: not enough measured posts to learn for %s (%d)",
                    config.tenant.schema_name, len(measured))
        return {"learned": False, "reason": "insufficient_data", "measured": len(measured)}

    # ── Score angles (mean engagement rate per angle) ─────────────────
    by_angle = defaultdict(list)
    by_hour = defaultdict(list)
    hashtag_perf = defaultdict(list)
    for p in measured:
        er = p.engagement_rate or 0.0
        angle = p.strategy_angle or "غير_مصنّف"
        by_angle[angle].append(er)
        if p.published_at:
            local = timezone.localtime(p.published_at)
            by_hour[local.hour].append(er)
        for tag in (p.hashtags or "").split():
            if tag.startswith("#"):
                hashtag_perf[tag].append(er)

    angle_scores = {a: round(sum(v) / len(v), 2) for a, v in by_angle.items() if v}
    hour_scores = {h: sum(v) / len(v) for h, v in by_hour.items() if v}
    best_hours = [f"{h:02d}:00" for h, _ in sorted(hour_scores.items(), key=lambda kv: kv[1], reverse=True)[:3]]
    top_hashtags = [t for t, _ in sorted(
        ((t, sum(v) / len(v)) for t, v in hashtag_perf.items() if len(v) >= 2),
        key=lambda kv: kv[1], reverse=True)[:10]]

    winners = sorted(measured, key=lambda p: p.engagement_rate or 0.0, reverse=True)[:3]
    winning_examples = [
        {"angle": p.strategy_angle, "caption": (p.caption or "")[:200],
         "engagement_rate": p.engagement_rate}
        for p in winners
    ]
    avg_er = round(sum(p.engagement_rate or 0.0 for p in measured) / len(measured), 2)

    # ── Build a stats summary and ask the LLM for a plain-language brief ─
    stats = _stats_summary(config, angle_scores, best_hours, top_hashtags, winning_examples, avg_er, len(measured))
    brief = content_ai.summarize_learnings(config, stats) or memory.learned_brief

    memory.angle_scores = angle_scores
    memory.best_hours = best_hours
    memory.top_hashtags = top_hashtags
    memory.winning_examples = winning_examples
    memory.avg_engagement_rate = avg_er
    memory.posts_analyzed = len(measured)
    memory.learned_brief = brief
    memory.last_learned_at = timezone.now()
    memory.save()

    logger.info("social_ads: learned for %s (posts=%d, avg_er=%.2f, top_angle=%s)",
                config.tenant.schema_name, len(measured), avg_er,
                (memory.best_angles(1) or ["-"])[0])
    return {"learned": True, "measured": len(measured), "avg_engagement_rate": avg_er,
            "best_angles": memory.best_angles(3)}


def _stats_summary(config, angle_scores, best_hours, top_hashtags, winners, avg_er, n) -> str:
    lines = [
        f"النشاط: {config.business_display_name or config.tenant.name} — {config.industry}",
        f"عدد المنشورات المُحلّلة: {n}",
        f"متوسط معدل التفاعل: {avg_er}%",
        "",
        "معدل التفاعل حسب زاوية المحتوى:",
    ]
    for angle, score in sorted(angle_scores.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"  - {angle}: {score}%")
    lines.append("")
    lines.append(f"أفضل ساعات النشر (حسب التفاعل): {', '.join(best_hours) or 'غير كافٍ'}")
    if top_hashtags:
        lines.append(f"أفضل الهاشتاجات: {' '.join(top_hashtags[:8])}")
    if winners:
        lines.append("")
        lines.append("أنجح المنشورات:")
        for w in winners:
            lines.append(f"  - ({w['angle']}, {w['engagement_rate']}%) {w['caption'][:80]}")
    return "\n".join(lines)


# =====================================================================
# 2. PLAN THE WEEK
# =====================================================================
def plan_week(config, *, force: bool = False) -> dict:
    """Generate + schedule the next batch of posts. Returns {created, scheduled_ids}.

    Skips generation if the tenant already has enough upcoming content, unless
    `force`. Content angles rotate, weighted toward the learned winners.
    """
    from social_ads.models import SocialPost

    if config.autopilot_mode == config.Autopilot.OFF and not force:
        return {"created": 0, "reason": "autopilot_off"}

    memory = ensure_memory(config)
    target = max(1, min(int(config.posts_per_week or 5), 21))

    # How many upcoming (scheduled or draft, not yet published) posts exist?
    upcoming = SocialPost.objects.filter(
        config=config,
        status__in=[SocialPost.Status.SCHEDULED, SocialPost.Status.DRAFT],
        scheduled_at__gte=timezone.now(),
    ).count()
    to_create = target - upcoming
    if to_create <= 0 and not force:
        return {"created": 0, "reason": "enough_upcoming", "upcoming": upcoming}
    to_create = max(to_create, 1 if force else to_create)

    slots = _next_slots(config, count=to_create)
    angles = _angle_rotation(memory, to_create)
    schedule_full = config.autopilot_mode == config.Autopilot.FULL

    created_ids = []
    for i in range(to_create):
        angle = angles[i]
        try:
            content = content_ai.generate_post(config, memory, angle=angle)
        except Exception as exc:  # never let one bad generation kill the batch
            logger.warning("social_ads: generate_post failed for %s: %s", config.tenant.schema_name, exc)
            continue
        image_url = _maybe_generate_image(config, content.get("image_prompt", ""))
        platform = _resolve_platform(config, has_image=bool(image_url))
        if platform is None:
            logger.info("social_ads: no publishable channel for %s — stopping batch", config.tenant.schema_name)
            break

        post = SocialPost.objects.create(
            config=config, tenant=config.tenant,
            platform=platform,
            status=SocialPost.Status.SCHEDULED if schedule_full else SocialPost.Status.DRAFT,
            source=SocialPost.Source.AUTOPILOT,
            caption=content["caption"],
            hashtags=content["hashtags"],
            image_prompt=content.get("image_prompt", ""),
            image_url=image_url,
            strategy_angle=content.get("strategy_angle", angle),
            ai_rationale=content.get("rationale", ""),
            scheduled_at=slots[i],
            approved_at=timezone.now() if schedule_full else None,
        )
        created_ids.append(post.id)

    logger.info("social_ads: planned %d posts for %s (mode=%s)",
                len(created_ids), config.tenant.schema_name, config.autopilot_mode)
    return {"created": len(created_ids), "scheduled_ids": created_ids,
            "mode": config.autopilot_mode}


def _angle_rotation(memory, count: int) -> list[str]:
    """Rotate angles, front-loading the learned winners so proven content repeats."""
    winners = memory.best_angles(3) if memory else []
    all_angles = [a for a, _ in CONTENT_ANGLES]
    # Weighted order: winners first, then the rest, then cycle.
    ordered = winners + [a for a in all_angles if a not in winners]
    if not ordered:
        ordered = all_angles
    return [ordered[i % len(ordered)] for i in range(count)]


def _next_slots(config, *, count: int) -> list[datetime]:
    """Compute the next `count` posting datetimes from preferred/learned hours."""
    hours = []
    try:
        memory = config.memory  # reverse OneToOne — raises if not yet created
    except Exception:
        memory = None
    if memory and memory.best_hours:
        hours = memory.best_hours
    if not hours:
        hours = config.preferred_times_list()
    times = _parse_times(hours) or [(11, 0), (19, 0)]

    now = timezone.localtime(timezone.now())
    slots: list[datetime] = []
    day_offset = 0
    while len(slots) < count:
        day = (now + timedelta(days=day_offset)).date()
        for (h, m) in times:
            candidate = timezone.make_aware(
                datetime(day.year, day.month, day.day, h, m),
                timezone.get_current_timezone(),
            )
            if candidate <= now + timedelta(minutes=30):
                continue
            slots.append(candidate)
            if len(slots) >= count:
                break
        day_offset += 1
        if day_offset > 60:  # safety guard
            break
    return slots


def _parse_times(items) -> list[tuple[int, int]]:
    out = []
    for it in items:
        it = str(it).strip()
        if not it:
            continue
        try:
            hh, mm = (it.split(":") + ["0"])[:2]
            out.append((int(hh) % 24, int(mm) % 60))
        except (ValueError, TypeError):
            continue
    return out


def _resolve_platform(config, *, has_image: bool):
    """Pick a platform the tenant can actually publish to for this post."""
    from social_ads.models import SocialPost
    fb = config.has_facebook()
    ig = config.has_instagram() and has_image  # IG requires an image
    if fb and ig:
        return SocialPost.Platform.BOTH
    if fb:
        return SocialPost.Platform.FACEBOOK
    if ig:
        return SocialPost.Platform.INSTAGRAM
    return None


def _maybe_generate_image(config, prompt: str) -> str:
    """Best-effort image generation.

    The platform's premium image pipeline (design_store / printing copilot) can
    be wired in here. Until an operator enables it, we return "" and the post is
    published as a text post on Facebook (Instagram is skipped when no image is
    available). Never raises.
    """
    if not (config.generate_images and prompt):
        return ""
    try:
        from django.conf import settings
        hook = getattr(settings, "SOCIAL_ADS_IMAGE_HOOK", None)
        if hook:
            from django.utils.module_loading import import_string
            gen = import_string(hook)
            return gen(config, prompt) or ""
    except Exception as exc:
        logger.info("social_ads: image hook failed for %s: %s", config.tenant.schema_name, exc)
    return ""


# =====================================================================
# 3. OPTIMIZE CAMPAIGNS (the "يصحح" for paid ads)
# =====================================================================
def optimize(config) -> dict:
    """Shift budget toward winning live campaigns, pause the losers. Returns a summary."""
    from social_ads.models import AdCampaign
    from . import meta_marketing

    if not (config.auto_optimize_ads and config.can_run_ads()):
        return {"optimized": 0, "reason": "disabled_or_no_ads"}

    token = config.page_access_token
    live = list(AdCampaign.objects.filter(config=config, status=AdCampaign.Status.ACTIVE))
    if len(live) < 2:
        return {"optimized": 0, "reason": "need_2plus_active", "active": len(live)}

    # Rank by cost-per-result (lower is better); campaigns with no results yet but
    # spend > 0 rank worst.
    def _score(c):
        if c.results:
            return float(c.cost_per_result or 0) or 0.001
        if float(c.spend or 0) > 0:
            return 1e9  # spent with nothing to show → worst
        return 1e6  # no data yet → neutral-ish

    ranked = sorted(live, key=_score)
    best, worst = ranked[0], ranked[-1]
    actions = []

    daily_cap = int(Decimal(str(config.max_daily_ad_budget or 0)) * 100)  # minor units

    try:
        # Reward the winner: bump its budget up to the daily cap.
        if best.meta_adset_id and best.daily_budget:
            new_minor = min(int(Decimal(str(best.daily_budget)) * 100 * Decimal("1.25")), daily_cap or 999999)
            meta_marketing.set_adset_budget(access_token=token, adset_id=best.meta_adset_id,
                                            daily_budget_minor=new_minor)
            best.daily_budget = Decimal(new_minor) / 100
            best.save(update_fields=["daily_budget", "updated_at"])
            actions.append(f"raised#{best.id}")

        # Pause a clear loser: spent money, produced nothing.
        if worst.id != best.id and float(worst.spend or 0) > 0 and not worst.results:
            if worst.meta_campaign_id:
                meta_marketing.set_campaign_status(access_token=token,
                                                   campaign_id=worst.meta_campaign_id, status="PAUSED")
            worst.status = AdCampaign.Status.PAUSED
            worst.save(update_fields=["status", "updated_at"])
            actions.append(f"paused#{worst.id}")
    except meta_marketing.MetaMarketingError as exc:
        logger.warning("social_ads: optimize failed for %s: %s", config.tenant.schema_name, exc)
        return {"optimized": len(actions), "actions": actions, "error": str(exc)}

    logger.info("social_ads: optimized %s → %s", config.tenant.schema_name, actions)
    return {"optimized": len(actions), "actions": actions}
