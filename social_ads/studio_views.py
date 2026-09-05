"""
Social Studio console — subscribers-only workspace.

Screens:
  • studio_home    — KPIs, learned-strategy brief, 14-day engagement chart, feed
  • generate_now   — generate a fresh AI draft on demand
  • post_edit / post_approve / post_publish_now / post_delete
  • campaigns / campaign_create / campaign_launch / campaign_pause
  • run_learning_now — trigger a learning cycle immediately

A guard redirects non-subscribers back to the overview/subscribe page, mirroring
the omnichannel console.
"""
from __future__ import annotations

import functools
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Avg, Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import QuickPostForm
from .models import AdCampaign, SocialAdsConfig, SocialPost
from .services import content_ai, strategist

logger = logging.getLogger("mouss_tec_core")


def _tenant():
    tenant = getattr(connection, "tenant", None)
    if tenant is None or getattr(tenant, "schema_name", "public") == "public":
        return None
    return tenant


def _studio_guard(view):
    @functools.wraps(view)
    @login_required
    def _wrapped(request, *args, **kwargs):
        tenant = _tenant()
        if tenant is None:
            messages.error(request, "هذه الصفحة متاحة داخل حساب الشركة فقط.")
            return redirect("/")
        config, _ = SocialAdsConfig.objects.get_or_create(tenant=tenant)
        if not config.subscription_is_valid:
            messages.info(request, "اشترك في استوديو التسويق أولاً لاستخدام لوحة التحكم.")
            return redirect("social_ads_overview")
        request.social_config = config
        request.social_tenant = tenant
        return view(request, *args, **kwargs)
    return _wrapped


def _nav(active: str) -> dict:
    return {
        "nav_ready": True,
        "active": active,
        "home_url": reverse("social_ads_studio"),
        "calendar_url": reverse("social_ads_calendar"),
        "campaigns_url": reverse("social_ads_campaigns"),
        "settings_url": reverse("social_ads_settings"),
        "guide_url": reverse("social_ads_guide"),
    }


# =====================================================================
# Home / dashboard
# =====================================================================
@_studio_guard
def studio_home(request):
    config = request.social_config
    tenant = request.social_tenant

    published = SocialPost.objects.filter(config=config, status=SocialPost.Status.PUBLISHED)
    upcoming = SocialPost.objects.filter(
        config=config,
        status__in=[SocialPost.Status.SCHEDULED, SocialPost.Status.DRAFT],
    ).order_by("scheduled_at")
    drafts = upcoming.filter(status=SocialPost.Status.DRAFT)

    kpis = {
        "published": published.count(),
        "scheduled": upcoming.filter(status=SocialPost.Status.SCHEDULED).count(),
        "drafts": drafts.count(),
        "avg_engagement": round(published.aggregate(a=Avg("engagement_rate"))["a"] or 0.0, 2),
        "total_reach": published.aggregate(s=Sum("reach"))["s"] or 0,
        "ad_spend_month": config.spend_this_month(),
    }
    memory = strategist.ensure_memory(config)

    context = {
        **_nav("home"),
        "config": config,
        "tenant": tenant,
        "kpis": kpis,
        "memory": memory,
        "recent_posts": published.order_by("-published_at")[:8],
        "upcoming_posts": upcoming[:8],
        "chart": _engagement_series(config),
        "angles": content_ai.CONTENT_ANGLES,
        "currency": _currency(tenant),
    }
    return render(request, "social_ads/studio.html", context)


def _engagement_series(config, days: int = 14):
    """Daily published-count + avg engagement for the sparkline chart."""
    from social_ads.models import PerformanceSnapshot

    start = (timezone.now() - timedelta(days=days)).date()
    buckets = {}
    snaps = PerformanceSnapshot.objects.filter(
        tenant=config.tenant, kind=PerformanceSnapshot.Kind.POST,
        captured_at__date__gte=start,
    )
    for s in snaps:
        d = timezone.localtime(s.captured_at).date().isoformat()
        b = buckets.setdefault(d, {"er": [], "reach": 0})
        b["er"].append(s.engagement_rate)
        b["reach"] += s.reach
    labels, er_vals, reach_vals = [], [], []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        labels.append(d[5:])  # MM-DD
        b = buckets.get(d)
        er_vals.append(round(sum(b["er"]) / len(b["er"]), 2) if b and b["er"] else 0)
        reach_vals.append(b["reach"] if b else 0)
    return {"labels": labels, "engagement": er_vals, "reach": reach_vals}


def _currency(tenant) -> str:
    try:
        return tenant.effective_currency
    except Exception:
        return "ج.م"


# =====================================================================
# Content calendar
# =====================================================================
@_studio_guard
def studio_calendar(request):
    """A month grid of scheduled + published posts, grouped by day."""
    config = request.social_config

    # Resolve the month to show (?m=YYYY-MM), default current month.
    now = timezone.localtime(timezone.now())
    year, month = now.year, now.month
    m = (request.GET.get("m") or "").strip()
    if m:
        try:
            year, month = (int(x) for x in m.split("-")[:2])
        except (ValueError, TypeError):
            year, month = now.year, now.month

    import calendar as _cal
    from datetime import date

    first = date(year, month, 1)
    _, days_in_month = _cal.monthrange(year, month)
    last = date(year, month, days_in_month)

    posts = SocialPost.objects.filter(
        config=config,
        status__in=[SocialPost.Status.SCHEDULED, SocialPost.Status.PUBLISHED,
                    SocialPost.Status.DRAFT, SocialPost.Status.FAILED],
    ).filter(
        scheduled_at__date__gte=first, scheduled_at__date__lte=last,
    ).order_by("scheduled_at")

    by_day: dict[int, list] = {}
    for p in posts:
        if not p.scheduled_at:
            continue
        d = timezone.localtime(p.scheduled_at).day
        by_day.setdefault(d, []).append(p)

    # Build week rows (Saturday-first to match Arabic calendars) with ready cells.
    _cal.setfirstweekday(_cal.SATURDAY)
    today = now.day if (now.year == year and now.month == month) else 0
    weeks = []
    for week in _cal.monthcalendar(year, month):  # day numbers, 0 = padding
        row = []
        for day in week:
            row.append({
                "day": day or None,
                "posts": by_day.get(day, []) if day else [],
                "is_today": bool(day and day == today),
            })
        weeks.append(row)

    prev_month = (first.replace(day=1) - timedelta(days=1))
    next_first = last + timedelta(days=1)

    context = {
        **_nav("calendar"),
        "config": config,
        "month_name": first.strftime("%B %Y"),
        "calendar_weeks": weeks,
        "prev_m": f"{prev_month.year}-{prev_month.month:02d}",
        "next_m": f"{next_first.year}-{next_first.month:02d}",
        "weekday_names": ["السبت", "الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة"],
    }
    return render(request, "social_ads/calendar.html", context)


# =====================================================================
# Post lifecycle
# =====================================================================
@_studio_guard
@require_POST
def generate_now(request):
    """Generate one fresh draft on demand (optional angle + occasion hint)."""
    config = request.social_config
    angle = (request.POST.get("angle") or "").strip()
    occasion = (request.POST.get("occasion") or "").strip()[:160]
    memory = strategist.ensure_memory(config)
    try:
        content = content_ai.generate_post(config, memory, angle=angle, occasion=occasion)
    except Exception as exc:
        logger.exception("social_ads: manual generate failed: %s", exc)
        messages.error(request, "تعذّر توليد المحتوى الآن، حاول مرة أخرى.")
        return redirect("social_ads_studio")

    slots = strategist._next_slots(config, count=1)
    SocialPost.objects.create(
        config=config, tenant=config.tenant,
        platform=strategist._resolve_platform(
            config, has_image=bool(config.generate_images and content.get("image_prompt")))
        or SocialPost.Platform.FACEBOOK,
        status=SocialPost.Status.DRAFT, source=SocialPost.Source.MANUAL,
        caption=content["caption"], hashtags=content["hashtags"],
        image_prompt=content.get("image_prompt", ""),
        strategy_angle=content.get("strategy_angle", angle or ""),
        ai_rationale=content.get("rationale", ""),
        scheduled_at=slots[0] if slots else None,
    )
    messages.success(request, "تم إنشاء مسودة جديدة ✅ راجعها ثم اعتمدها للجدولة.")
    return redirect("social_ads_studio")


@_studio_guard
def post_edit(request, pk):
    config = request.social_config
    post = get_object_or_404(SocialPost, pk=pk, config=config)
    if request.method == "POST":
        form = QuickPostForm(request.POST, instance=post)
        if form.is_valid():
            form.save()
            messages.success(request, "تم حفظ التعديلات ✅")
            return redirect("social_ads_studio")
        messages.error(request, "برجاء مراجعة الحقول.")
    else:
        form = QuickPostForm(instance=post)
    return render(request, "social_ads/post_edit.html", {**_nav("home"), "form": form, "post": post})


@_studio_guard
@require_POST
def post_approve(request, pk):
    """Approve a draft → schedule it (keeps its scheduled_at, else next slot)."""
    config = request.social_config
    post = get_object_or_404(SocialPost, pk=pk, config=config)
    if post.status != SocialPost.Status.DRAFT:
        messages.info(request, "هذا البوست ليس مسودة.")
        return redirect("social_ads_studio")
    if not post.scheduled_at or post.scheduled_at <= timezone.now():
        slots = strategist._next_slots(config, count=1)
        post.scheduled_at = slots[0] if slots else timezone.now() + timedelta(minutes=10)
    post.status = SocialPost.Status.SCHEDULED
    post.approved_at = timezone.now()
    post.save(update_fields=["status", "scheduled_at", "approved_at", "updated_at"])
    messages.success(request, f"تم جدولة البوست للنشر في {timezone.localtime(post.scheduled_at):%Y-%m-%d %H:%M} ✅")
    return redirect("social_ads_studio")


@_studio_guard
@require_POST
def post_publish_now(request, pk):
    config = request.social_config
    post = get_object_or_404(SocialPost, pk=pk, config=config)
    if post.status in (SocialPost.Status.PUBLISHED, SocialPost.Status.PUBLISHING):
        messages.info(request, "هذا البوست منشور أو جارٍ نشره.")
        return redirect("social_ads_studio")
    post.status = SocialPost.Status.SCHEDULED
    post.scheduled_at = timezone.now()
    post.approved_at = timezone.now()
    post.save(update_fields=["status", "scheduled_at", "approved_at", "updated_at"])
    from .tasks import publish_post
    publish_post.delay(post.id)
    messages.success(request, "جارٍ النشر الآن… حدّث الصفحة بعد لحظات لرؤية النتيجة.")
    return redirect("social_ads_studio")


@_studio_guard
@require_POST
def post_delete(request, pk):
    config = request.social_config
    post = get_object_or_404(SocialPost, pk=pk, config=config)
    if post.status == SocialPost.Status.PUBLISHED:
        messages.error(request, "لا يمكن حذف بوست منشور — يمكنك حذفه من فيسبوك مباشرة.")
        return redirect("social_ads_studio")
    post.delete()
    messages.success(request, "تم حذف البوست.")
    return redirect("social_ads_studio")


@_studio_guard
@require_POST
def analyze_page(request):
    """Import the tenant's existing page posts + insights and learn from them."""
    config = request.social_config
    if not config.has_facebook():
        messages.error(request, "اربط صفحة فيسبوك و Page Access Token من الإعدادات أولاً.")
        return redirect("social_ads_settings")
    from .tasks import import_page_posts
    import_page_posts.delay(config.id, 25)
    messages.success(
        request,
        "جارٍ تحليل صفحتك… سنستورد آخر بوستاتك ونقرأ أداءها ويبدأ البوت يتعلّم منها. "
        "حدّث الصفحة بعد دقيقة لرؤية النتائج.",
    )
    return redirect("social_ads_studio")


@_studio_guard
@require_POST
def run_learning_now(request):
    config = request.social_config
    res = strategist.learn(config)
    if res.get("learned"):
        messages.success(request, f"تم تحليل {res['measured']} بوست وتحديث استراتيجية البوت ✅")
    else:
        messages.info(request, "لا توجد بيانات أداء كافية بعد للتعلّم — انشر بعض البوستات أولاً.")
    return redirect("social_ads_studio")


# =====================================================================
# Campaigns
# =====================================================================
@_studio_guard
def campaigns(request):
    config = request.social_config
    tenant = request.social_tenant
    camps = AdCampaign.objects.filter(config=config).order_by("-created_at")[:50]
    promotable = SocialPost.objects.filter(
        config=config, status=SocialPost.Status.PUBLISHED,
    ).exclude(fb_post_id="").order_by("-published_at")[:20]
    context = {
        **_nav("campaigns"),
        "config": config,
        "campaigns": camps,
        "promotable_posts": promotable,
        "objectives": AdCampaign.Objective.choices,
        "currency": _currency(tenant),
        "remaining_budget": config.remaining_ad_budget(),
        "can_run_ads": config.can_run_ads(),
    }
    return render(request, "social_ads/campaigns.html", context)


@_studio_guard
@require_POST
def campaign_create(request):
    config = request.social_config
    if not config.can_run_ads():
        messages.error(request, "فعّل الإعلانات واربط حساب الإعلانات من الإعدادات أولاً.")
        return redirect("social_ads_campaigns")

    objective = request.POST.get("objective") or AdCampaign.Objective.TRAFFIC
    name = (request.POST.get("name") or "").strip()[:180] or f"حملة {timezone.localtime(timezone.now()):%Y-%m-%d}"
    post_id = request.POST.get("post_id")
    try:
        daily = Decimal(str(request.POST.get("daily_budget") or "0"))
    except (InvalidOperation, TypeError):
        daily = Decimal("0")
    duration = int(request.POST.get("duration_days") or 7)

    if daily <= 0:
        messages.error(request, "أدخل ميزانية يومية صحيحة.")
        return redirect("social_ads_campaigns")
    if daily > config.max_daily_ad_budget:
        messages.error(request, f"الميزانية اليومية تتجاوز الحد المسموح ({config.max_daily_ad_budget}).")
        return redirect("social_ads_campaigns")

    base_post = None
    if post_id:
        base_post = SocialPost.objects.filter(pk=post_id, config=config).first()

    memory = strategist.ensure_memory(config)
    copy = content_ai.generate_ad_copy(config, memory, objective=objective, base_post=base_post)

    camp = AdCampaign.objects.create(
        config=config, tenant=config.tenant, post=base_post,
        name=name, objective=objective, status=AdCampaign.Status.DRAFT,
        source="manual",
        primary_text=copy["primary_text"], headline=copy["headline"],
        audience_spec=copy["audience_spec"], daily_budget=daily, duration_days=duration,
    )
    messages.success(request, "تم إنشاء مسودة الحملة ونص الإعلان ✅ راجعها ثم أطلقها.")
    return redirect("social_ads_campaigns")


@_studio_guard
@require_POST
def campaign_launch(request, pk):
    config = request.social_config
    camp = get_object_or_404(AdCampaign, pk=pk, config=config)
    if camp.status not in (AdCampaign.Status.DRAFT, AdCampaign.Status.FAILED, AdCampaign.Status.PAUSED):
        messages.info(request, "لا يمكن إطلاق هذه الحملة في حالتها الحالية.")
        return redirect("social_ads_campaigns")
    activate = request.POST.get("activate") == "1"
    camp.status = AdCampaign.Status.PENDING
    camp.save(update_fields=["status", "updated_at"])
    from .tasks import launch_campaign
    launch_campaign.delay(camp.id, activate=activate)
    messages.success(request, "جارٍ إنشاء الحملة على Meta… ستظهر حالتها بعد لحظات.")
    return redirect("social_ads_campaigns")


@_studio_guard
@require_POST
def campaign_pause(request, pk):
    config = request.social_config
    camp = get_object_or_404(AdCampaign, pk=pk, config=config)
    from .services import meta_marketing
    if camp.meta_campaign_id:
        try:
            meta_marketing.set_campaign_status(
                access_token=config.page_access_token,
                campaign_id=camp.meta_campaign_id, status="PAUSED")
        except meta_marketing.MetaMarketingError as exc:
            messages.error(request, f"تعذّر إيقاف الحملة على Meta: {exc}")
            return redirect("social_ads_campaigns")
    camp.status = AdCampaign.Status.PAUSED
    camp.save(update_fields=["status", "updated_at"])
    messages.success(request, "تم إيقاف الحملة.")
    return redirect("social_ads_campaigns")
