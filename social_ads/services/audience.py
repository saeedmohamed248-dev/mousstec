"""
Audience knowledge base — learn from ALL the page's comments (no LLM).

"Analyze my page" imports the posts; this reads the comments across a broad set
of them and distills, in pure Python, what the audience actually says:
  • total number of comments scanned
  • the top QUESTIONS customers ask (price/availability/fit …), ranked by likes
  • the most frequent meaningful TERMS (car models, part names, price words …)
  • a most-liked SAMPLE of real comments

The result is stored on StrategyMemory.audience_insights so the conversational
assistant can answer "الناس بتسأل عن إيه؟" / "أكتر حاجة العملاء عايزينها؟"
INSTANTLY from the tenant's own data — even when the LLM quota is exhausted.

Everything is best-effort: any failure returns/keeps whatever we have and never
raises into the caller.
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from django.utils import timezone

from . import meta_marketing

logger = logging.getLogger("mouss_tec_core")

# How many posts to read comments from, and how many comments per post. Kept
# bounded so a large page doesn't trigger Meta rate limits — we prioritise the
# posts most likely to carry signal (most-commented first).
_MAX_POSTS = 200
_PER_POST = 50

# Arabic + English stop words we don't want cluttering the "frequent terms" list.
_STOP = {
    "في", "من", "على", "الى", "إلى", "عن", "مع", "هو", "هي", "ده", "دي", "ان",
    "إن", "أن", "او", "أو", "يا", "لا", "ما", "مش", "كل", "بس", "كده", "كدة",
    "اللي", "الي", "على", "و", "يعني", "عايز", "عاوز", "ممكن", "لو", "انا", "أنا",
    "احنا", "هل", "ايه", "إيه", "فين", "ازاي", "إزاي", "كام", "the", "and", "for",
    "you", "are", "with", "this", "that", "have", "your",
}

_QUESTION_MARKS = ("؟", "?")
_INTENT_WORDS = ("سعر", "بكام", "كام", "متوفر", "موجود", "تناسب", "يركب",
                 "أصلي", "اصلي", "تقليد", "ضمان", "توصيل", "فرع", "عنوان",
                 "رقم", "واتس", "price", "available")


def refresh_audience_insights(config, *, max_posts: int = _MAX_POSTS,
                              per_post: int = _PER_POST) -> dict:
    """Read comments across the tenant's imported posts and store distilled insights.

    Returns the insights dict (also saved to memory.audience_insights). "" fields
    on any failure; never raises.
    """
    from social_ads.models import SocialPost, StrategyMemory

    memory, _ = StrategyMemory.objects.get_or_create(
        config=config, defaults={"tenant": config.tenant})

    if not config.has_facebook():
        return memory.audience_insights or {}

    token = meta_marketing.resolve_page_token(config.page_access_token, config.facebook_page_id)
    if not token:
        return memory.audience_insights or {}

    # Prioritise the most-commented posts — that's where the audience signal is.
    posts = list(
        SocialPost.objects.filter(config=config, status=SocialPost.Status.PUBLISHED)
        .exclude(fb_post_id="")
        .order_by("-comments", "-published_at")[:max_posts]
    )
    if not posts:
        return memory.audience_insights or {}

    all_comments: list[tuple[int, str]] = []  # (like_count, message)
    scanned = 0
    for p in posts:
        try:
            rows = meta_marketing.fetch_post_comments(
                access_token=token, post_id=p.fb_post_id, limit=per_post)
        except Exception:
            continue
        scanned += 1
        for c in rows:
            msg = (c.get("message") or "").strip().replace("\n", " ")
            if len(msg) >= 2:
                all_comments.append((int(c.get("like_count", 0) or 0), msg[:280]))

    insights = _distill(all_comments, scanned)
    memory.audience_insights = insights
    memory.save(update_fields=["audience_insights", "updated_at"])
    logger.info("social_ads: audience insights for %s — %d comments over %d posts",
                config.tenant.schema_name, insights.get("total_comments", 0), scanned)
    return insights


def _distill(comments: list[tuple[int, str]], posts_scanned: int) -> dict:
    """Turn raw (likes, text) comments into a compact insights dict — pure Python."""
    if not comments:
        return {"total_comments": 0, "posts_scanned": posts_scanned,
                "top_questions": [], "common_terms": [], "sample": [],
                "updated_at": timezone.now().isoformat()}

    # Questions = comments that end/contain a question mark OR carry buying intent.
    questions = [
        (lk, txt) for lk, txt in comments
        if any(q in txt for q in _QUESTION_MARKS)
        or any(w in txt.lower() for w in _INTENT_WORDS)
    ]
    # De-dup near-identical questions, keep the most-liked.
    seen: set[str] = set()
    questions.sort(key=lambda x: x[0], reverse=True)
    top_questions = []
    for lk, txt in questions:
        key = re.sub(r"\s+", " ", txt.lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        top_questions.append({"text": txt, "likes": lk})
        if len(top_questions) >= 20:
            break

    # Frequent meaningful terms across all comments.
    counter: Counter[str] = Counter()
    for _lk, txt in comments:
        for tok in re.findall(r"[A-Za-z؀-ۿ][A-Za-z0-9؀-ۿ]+", txt):
            t = tok.strip().lower()
            if len(t) >= 3 and t not in _STOP:
                counter[t] += 1
    common_terms = [[t, n] for t, n in counter.most_common(25) if n >= 2]

    # Most-liked sample (for the brief / display).
    sample_sorted = sorted(comments, key=lambda x: x[0], reverse=True)
    sample = [{"text": txt, "likes": lk} for lk, txt in sample_sorted[:15]]

    return {
        "total_comments": len(comments),
        "posts_scanned": posts_scanned,
        "top_questions": top_questions,
        "common_terms": common_terms,
        "sample": sample,
        "updated_at": timezone.now().isoformat(),
    }


def summarize_for_context(insights: dict, *, max_questions: int = 8,
                          max_terms: int = 12) -> str:
    """Compact Arabic text of the audience insights for prompts / the assistant."""
    if not insights or not insights.get("total_comments"):
        return ""
    lines = [
        f"إجمالي تعليقات العملاء اللي البوت قراها: {insights.get('total_comments', 0)} "
        f"(من {insights.get('posts_scanned', 0)} بوست)",
    ]
    qs = insights.get("top_questions") or []
    if qs:
        lines.append("أكتر أسئلة/اهتمامات العملاء (مرتبة حسب الإعجابات):")
        for q in qs[:max_questions]:
            lines.append(f"  - {q.get('text','')}")
    terms = insights.get("common_terms") or []
    if terms:
        joined = "، ".join(f"{t}({n})" for t, n in terms[:max_terms])
        lines.append(f"أكتر كلمات بتتكرر في التعليقات: {joined}")
    return "\n".join(lines)
