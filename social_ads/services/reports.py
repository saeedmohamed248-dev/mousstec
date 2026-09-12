"""
Weekly performance report builder for Social Studio.

Produces a per-tenant summary of the last 7 days — posts published, engagement,
reach, top post, ad spend/results, and the bot's current learned brief — as a
context dict the email template renders. Pure reads; never raises.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Avg, Sum
from django.utils import timezone

logger = logging.getLogger("mouss_tec_core")


def build_weekly_report(config) -> dict:
    """Return a context dict summarizing the tenant's last 7 days (or {} if nothing)."""
    from social_ads.models import AdCampaign, SocialPost

    since = timezone.now() - timedelta(days=7)
    posts = SocialPost.objects.filter(
        config=config, status=SocialPost.Status.PUBLISHED, published_at__gte=since,
    )
    published_count = posts.count()

    agg = posts.aggregate(
        reach=Sum("reach"), impressions=Sum("impressions"),
        likes=Sum("likes"), comments=Sum("comments"), shares=Sum("shares"),
        er=Avg("engagement_rate"),
        sales=Sum("attributed_sales_count"), sales_value=Sum("attributed_sales_value"),
    )
    # Best & worst posts of the week by real performance (interactions-aware).
    ranked = sorted(posts, key=lambda p: p.performance_score(), reverse=True)
    top_post = ranked[0] if ranked else None
    worst_post = ranked[-1] if len(ranked) > 1 else None
    # Posts that actually drove sales this week.
    top_selling = sorted(
        (p for p in posts if (p.attributed_sales_value or 0)),
        key=lambda p: float(p.attributed_sales_value or 0), reverse=True,
    )[:3]

    camps = AdCampaign.objects.filter(config=config)
    active_camps = camps.filter(status=AdCampaign.Status.ACTIVE)
    camp_agg = camps.filter(insights_synced_at__gte=since).aggregate(
        spend=Sum("spend"), results=Sum("results"), reach=Sum("reach"),
    )

    upcoming = SocialPost.objects.filter(
        config=config,
        status__in=[SocialPost.Status.SCHEDULED, SocialPost.Status.DRAFT],
        scheduled_at__gte=timezone.now(),
    ).count()

    try:
        memory = config.memory
    except Exception:
        memory = None

    # Nothing to say if the tenant published nothing and has no upcoming content.
    if published_count == 0 and upcoming == 0:
        return {}

    return {
        "tenant_name": config.business_display_name or config.tenant.name,
        "period_start": since,
        "period_end": timezone.now(),
        "published_count": published_count,
        "reach": agg["reach"] or 0,
        "impressions": agg["impressions"] or 0,
        "interactions": (agg["likes"] or 0) + (agg["comments"] or 0) + (agg["shares"] or 0),
        "avg_engagement": round(agg["er"] or 0.0, 2),
        "attributed_sales": agg["sales"] or 0,
        "attributed_value": agg["sales_value"] or 0,
        "top_post": top_post,
        "worst_post": worst_post,
        "top_selling": top_selling,
        "active_campaigns": active_camps.count(),
        "ad_spend": camp_agg["spend"] or 0,
        "ad_results": camp_agg["results"] or 0,
        "upcoming_count": upcoming,
        "learned_brief": (memory.learned_brief if memory else "") or "",
        "best_angles": (memory.best_angles(3) if memory else []),
        "best_selling_angles": (memory.best_selling_angles(3) if memory else []),
    }


def render_report_email(report: dict):
    """Return (subject, text_body, html_body) for a report context. Never raises."""
    subject = f"📊 تقرير التسويق الأسبوعي — {report['tenant_name']}"
    lines = [
        f"تقرير أداء آخر ٧ أيام لـ {report['tenant_name']}",
        "",
        f"• بوستات منشورة: {report['published_count']}",
        f"• إجمالي الوصول: {report['reach']}",
        f"• التفاعلات: {report['interactions']}",
        f"• متوسط معدل التفاعل: {report['avg_engagement']}%",
        f"• مبيعات منسوبة للبوستات: {report.get('attributed_sales', 0)} "
        f"(بقيمة {report.get('attributed_value', 0):,.0f})",
        f"• بوستات قادمة مجدولة/مسودات: {report['upcoming_count']}",
    ]
    if report.get("top_post"):
        tp = report["top_post"]
        lines += ["", f"🏆 أفضل بوست: 👍{tp.likes} 💬{tp.comments} 🔁{tp.shares} — {tp.caption[:80]}"]
    if report.get("worst_post"):
        wp = report["worst_post"]
        lines += [f"📉 أضعف بوست: 👍{wp.likes} 💬{wp.comments} 🔁{wp.shares} — {wp.caption[:80]}"]
    if report.get("top_selling"):
        lines += ["", "🛒 أكثر البوستات مبيعاً:"]
        for p in report["top_selling"]:
            lines.append(f"  - {p.attributed_sales_count} بيعة ({p.attributed_sales_value:,.0f}) — {p.caption[:70]}")
    if report.get("best_selling_angles"):
        lines += ["", f"💡 ركّز على الزوايا الأكثر بيعاً: {'، '.join(report['best_selling_angles'])}"]
    elif report.get("best_angles"):
        lines += ["", f"💡 أفضل زوايا التفاعل: {'، '.join(report['best_angles'])}"]
    if report["active_campaigns"]:
        lines += [
            f"• حملات نشطة: {report['active_campaigns']}",
            f"• الإنفاق الإعلاني: {report['ad_spend']} — نتائج: {report['ad_results']}",
        ]
    if report["learned_brief"]:
        lines += ["", "ما تعلّمه البوت هذا الأسبوع:", report["learned_brief"]]
    text_body = "\n".join(lines)

    html_body = ""
    try:
        from django.template.loader import render_to_string
        html_body = render_to_string("social_ads/email/weekly_report.html", report)
    except Exception as exc:
        logger.info("social_ads: weekly report HTML render failed: %s", exc)
    return subject, text_body, html_body
