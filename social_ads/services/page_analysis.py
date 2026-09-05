"""
"Analyze my current page" — backfill.

Pulls the tenant's existing Facebook Page posts (published before/outside the
add-on), reads their real performance from Meta, imports them as PUBLISHED
SocialPost rows, then runs the learning cycle — so the bot understands what works
for this page from day one instead of waiting to publish its own posts.

Idempotent: re-running updates the same rows (keyed on fb_post_id) rather than
duplicating. Pure best-effort; never raises into the caller.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import meta_marketing, strategist

logger = logging.getLogger("mouss_tec_core")

# Cheap keyword heuristics to tag an imported post with a content angle, so the
# learning cycle has angle signal without an LLM call per post.
_PRICE_WORDS = ("خصم", "عرض", "سعر", "جنيه", "ج.م", "offer", "sale", "discount", "%")
_TESTIMONIAL_WORDS = ("عميل", "رأي", "تجربة", "شكراً", "review", "شهادة")
_BTS_WORDS = ("خلف الكواليس", "من داخل", "فريق", "ورشة", "behind")


def _guess_angle(text: str) -> str:
    t = (text or "").lower()
    if not t:
        return "غير_مصنّف"
    if "؟" in t or "?" in t:
        return "سؤال_تفاعلي"
    if any(w in t for w in _PRICE_WORDS):
        return "عرض_سعري"
    if any(w in t for w in _TESTIMONIAL_WORDS):
        return "شهادة_عميل"
    if any(w in t for w in _BTS_WORDS):
        return "خلف_الكواليس"
    return "نصيحة"


def backfill_page(config, *, limit: int = 25, learn_after: bool = True) -> dict:
    """Import recent page posts + insights, then (optionally) learn. Returns a summary."""
    from social_ads.models import PerformanceSnapshot, SocialPost

    if not config.has_facebook():
        return {"imported": 0, "reason": "no_facebook_connection"}

    token = config.page_access_token
    posts = meta_marketing.fetch_page_posts(
        access_token=token, page_id=config.facebook_page_id, limit=limit)
    if not posts:
        return {"imported": 0, "reason": "no_posts_returned"}

    imported = 0
    for row in posts:
        fb_id = row.get("id")
        if not fb_id:
            continue
        published_at = parse_datetime(row.get("created_time") or "") or timezone.now()
        caption = row.get("message", "")

        post, _created = SocialPost.objects.update_or_create(
            config=config, fb_post_id=fb_id,
            defaults={
                "tenant": config.tenant,
                "platform": SocialPost.Platform.FACEBOOK,
                "status": SocialPost.Status.PUBLISHED,
                "source": SocialPost.Source.IMPORTED,
                "caption": caption or "(بدون نص)",
                "image_url": row.get("picture", ""),
                "permalink": row.get("permalink", ""),
                "strategy_angle": _guess_angle(caption),
                "published_at": published_at,
                "likes": row.get("likes", 0),
                "comments": row.get("comments", 0),
                "shares": row.get("shares", 0),
            },
        )

        # Pull reach/impressions/clicks for this post (best-effort).
        try:
            data = meta_marketing.fetch_post_insights(access_token=token, post_id=fb_id)
            post.reach = data["reach"]
            post.impressions = data["impressions"]
            post.clicks = data["clicks"]
            # Prefer summary counts we already have; fall back to insights values.
            post.likes = post.likes or data["likes"]
            post.comments = post.comments or data["comments"]
            post.shares = post.shares or data["shares"]
        except Exception:
            logger.debug("social_ads: insights fetch failed during backfill for %s", fb_id)

        post.recompute_engagement()
        post.insights_synced_at = timezone.now()
        post.save()

        PerformanceSnapshot.objects.create(
            tenant=config.tenant, kind=PerformanceSnapshot.Kind.POST, post=post,
            reach=post.reach, impressions=post.impressions,
            interactions=post.likes + post.comments + post.shares,
            clicks=post.clicks, engagement_rate=post.engagement_rate,
            captured_at=published_at,
        )
        imported += 1

    result = {"imported": imported}
    if learn_after and imported:
        try:
            result["learning"] = strategist.learn(config)
        except Exception:
            logger.exception("social_ads: learn after backfill failed for %s", config.tenant.schema_name)
    logger.info("social_ads: backfilled %d page posts for %s", imported, config.tenant.schema_name)
    return result
