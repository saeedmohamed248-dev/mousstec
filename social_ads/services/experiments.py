"""
A/B testing for Social Studio.

Create two variants (A and B) of the same idea with deliberately different hooks,
publish both, then — once both have published and gathered engagement — pick the
winner by real performance and teach the winning style back to the strategy
memory. This is how the bot keeps improving like a growth team: hypothesis →
experiment → measure → double down.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.utils import timezone

from . import content_ai, strategist

logger = logging.getLogger("mouss_tec_core")

# Two distinct creative directions so the test is meaningful (not two near-copies).
_VARIANT_HINTS = {
    "A": "الافتتاحية: ابدأ بسؤال أو جملة صادمة تثير الفضول. أسلوب عاطفي/قصصي.",
    "B": "الافتتاحية: ابدأ بالعرض/السعر/الفائدة مباشرة. أسلوب مباشر ومختصر.",
}

# How long to wait after both variants publish before judging a winner.
EVALUATION_DELAY_HOURS = 48


def create_experiment(config, *, angle: str = "", occasion: str = "") -> dict:
    """Generate an A/B pair sharing one experiment_id. Returns {experiment_id, created}."""
    from social_ads.models import SocialPost

    if not config.is_operational:
        return {"created": 0, "reason": "not_operational"}

    memory = strategist.ensure_memory(config)
    angle = angle or (memory.best_angles(1) or ["عرض_سعري"])[0]
    exp_id = uuid.uuid4().hex[:16]
    # Space the two variants a few hours apart so they don't cannibalise reach.
    slots = strategist._next_slots(config, count=2) or [
        timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=5)]
    full_auto = config.autopilot_mode == config.Autopilot.FULL

    created = 0
    for i, variant in enumerate(("A", "B")):
        try:
            content = content_ai.generate_post(
                config, memory, angle=angle, occasion=occasion,
                extra_hint=_VARIANT_HINTS[variant])
        except Exception:
            logger.exception("social_ads: A/B variant %s generation failed", variant)
            continue
        will_have_image = bool(config.generate_images and content.get("image_prompt"))
        platform = strategist._resolve_platform(config, has_image=will_have_image) \
            or SocialPost.Platform.FACEBOOK
        SocialPost.objects.create(
            config=config, tenant=config.tenant, platform=platform,
            status=SocialPost.Status.SCHEDULED if full_auto else SocialPost.Status.DRAFT,
            source=SocialPost.Source.AUTOPILOT,
            title=f"تجربة A/B — نسخة {variant}",
            caption=content["caption"], hashtags=content["hashtags"],
            image_prompt=content.get("image_prompt", ""),
            strategy_angle=content.get("strategy_angle", angle),
            ai_rationale=content.get("rationale", ""),
            experiment_id=exp_id, variant=variant,
            scheduled_at=slots[i] if i < len(slots) else None,
            approved_at=timezone.now() if full_auto else None,
        )
        created += 1

    logger.info("social_ads: created A/B experiment %s (%d variants) for %s",
                exp_id, created, config.tenant.schema_name)
    return {"experiment_id": exp_id, "created": created}


def evaluate_experiments(config) -> dict:
    """Judge finished experiments: mark the higher-performing variant as winner.

    An experiment is 'finished' when both variants are PUBLISHED and the older one
    published at least EVALUATION_DELAY_HOURS ago. The winning caption is added to
    the strategy memory's winning examples so future generations echo what worked.
    """
    from social_ads.models import SocialPost

    cutoff = timezone.now() - timedelta(hours=EVALUATION_DELAY_HOURS)
    # Candidate experiments: have a published variant not yet judged.
    exp_ids = list(
        SocialPost.objects.filter(
            config=config, status=SocialPost.Status.PUBLISHED,
        ).exclude(experiment_id="").values_list("experiment_id", flat=True).distinct()
    )
    judged = 0
    for exp_id in exp_ids:
        variants = list(SocialPost.objects.filter(config=config, experiment_id=exp_id))
        published = [p for p in variants if p.status == SocialPost.Status.PUBLISHED and p.published_at]
        if len(published) < 2:
            continue  # need both variants live to compare
        if any(p.ab_winner for p in variants):
            continue  # already judged
        if max(p.published_at for p in published) > cutoff:
            continue  # give it time to gather data

        winner = max(published, key=lambda p: p.performance_score())
        winner.ab_winner = True
        winner.save(update_fields=["ab_winner", "updated_at"])
        judged += 1

        # Teach the winning style back into memory.
        try:
            memory = strategist.ensure_memory(config)
            examples = list(memory.winning_examples or [])
            examples.insert(0, {
                "angle": winner.strategy_angle,
                "caption": (winner.caption or "")[:200],
                "engagement_rate": winner.engagement_rate,
                "likes": winner.likes, "comments": winner.comments, "shares": winner.shares,
                "ab_winner": True,
            })
            memory.winning_examples = examples[:8]
            memory.save(update_fields=["winning_examples", "updated_at"])
        except Exception:
            logger.warning("social_ads: could not record A/B winner into memory", exc_info=True)

    if judged:
        logger.info("social_ads: judged %d A/B experiments for %s", judged, config.tenant.schema_name)
    return {"judged": judged}
