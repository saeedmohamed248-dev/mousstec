"""📊 لوحة مالك المنصة — الإيرادات، تكلفة الذكاء الاصطناعي، مصادر التسويق، تحليلات التشخيص، ذكاء الإيرادات."""
from __future__ import annotations

import logging
from decimal import Decimal
from datetime import timedelta

from django.db.models import Sum, Count, Q
from django.shortcuts import render
from django.utils import timezone

from clients.models import (
    Client, PlatformInvoice,
    TenantSubscription,
)
from clients.permissions import widget_required
from ._common import saas_admin_required

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 💸 تكلفة الذكاء الاصطناعي لكل شركة قصاد اللي بتدفعه
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('revenue')
def ai_usage(request):
    """كل نداء AI نصّي بيتسجّل (inventory.ai_services._log_llm_usage) + الصور المولّدة + أسئلة المستشار.
    التكلفة تقديرية من أسعار المزوّدين في الإعدادات (AI_COST_*)."""
    from collections import defaultdict
    from django.conf import settings as _st
    from clients.models import AILimitTracker
    today = timezone.localdate()
    try:
        year, month = (int(x) for x in (request.GET.get('month') or today.strftime('%Y-%m')).split('-'))
    except ValueError:
        year, month = today.year, today.month
    rates = getattr(_st, 'AI_COST_USD_PER_MTOK', {})
    img_usd = getattr(_st, 'AI_COST_USD_PER_IMAGE', 0.04)
    fx = Decimal(str(getattr(_st, 'AI_COST_USD_TO_EGP', 50.0)))
    rows = defaultdict(lambda: {'calls': 0, 'tokens': 0, 'usd': 0.0, 'questions': 0, 'images': 0})
    qs = (AILimitTracker.objects.filter(used_at__year=year, used_at__month=month,
                                        action_type__in=('llm_text', 'copilot_question', 'ai_generation'))
          .values_list('tenant_id', 'action_type', 'metadata'))
    for tenant_id, action, meta in qs.iterator(chunk_size=2000):
        r = rows[tenant_id]
        if action == 'llm_text':
            meta = meta or {}
            tokens = int(meta.get('tokens_in') or 0) + int(meta.get('tokens_out') or 0)
            r['calls'] += 1
            r['tokens'] += tokens
            r['usd'] += tokens / 1_000_000 * float(rates.get(meta.get('provider'), 0.88))
        elif action == 'copilot_question':
            r['questions'] += 1
        else:
            r['images'] += 1
            r['usd'] += float(img_usd)
    tenants = {t.pk: t for t in Client.objects.filter(pk__in=rows.keys())}
    subs = {s.tenant_id: s for s in TenantSubscription.objects.filter(tenant_id__in=rows.keys()).select_related('plan')}
    data = []
    for tenant_id, r in rows.items():
        sub = subs.get(tenant_id)
        price = (sub.locked_monthly_price or sub.plan.monthly_price) if sub and sub.plan_id else None
        cost = (Decimal(str(r['usd'])) * fx).quantize(Decimal('0.01'))
        data.append({'tenant': tenants.get(tenant_id), 'plan': sub.plan if sub else None, 'price': price,
                     'cost': cost, 'share': (cost / price * 100).quantize(Decimal('0.1')) if price else None, **r})
    data.sort(key=lambda d: d['cost'], reverse=True)
    total_cost = sum((d['cost'] for d in data), Decimal('0'))
    return render(request, 'clients/saas_admin/ai_usage.html', {
        'rows': data, 'month': f'{year:04d}-{month:02d}', 'total_cost': total_cost,
        'total_calls': sum(d['calls'] for d in data), 'total_questions': sum(d['questions'] for d in data),
        'total_images': sum(d['images'] for d in data), 'fx': fx, 'rates': rates, 'img_usd': img_usd,
    })


# ─────────────────────────────────────────────────────────────────────
# 📣 الإعلانات: كل حملة جابت كام تسجيل وكام مشترك وكام فلوس
# ─────────────────────────────────────────────────────────────────────
def _parse_day(value, default):
    from datetime import date
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


@saas_admin_required
@widget_required('revenue')
def marketing_sources(request):
    """الشركات اللي اتسجّلت في الفترة مقسومة على مصدرها (utm_source / utm_campaign / utm_content).

    «دفعوا» = عندهم فاتورة اشتراك مدفوعة بفلوس حقيقية (مش هدية ولا كوبون 100%)، و«الإيراد» = كل
    اللي دفعوه لحد النهارده — عشان تقارن تكلفة الحملة في مدير الإعلانات باللي رجع منها فعلاً."""
    from django.conf import settings as _st
    from clients.models import SignupAttribution
    from clients.services import meta_capi
    today = timezone.localdate()
    date_from = _parse_day(request.GET.get('from'), today - timedelta(days=30))
    date_to = _parse_day(request.GET.get('to'), today)
    tenants = list(Client.objects.exclude(schema_name='public')
                   .filter(created_on__gte=date_from, created_on__lte=date_to)
                   .order_by('-created_on', '-pk'))
    ids = [t.pk for t in tenants]
    attrs = {a.tenant_id: a for a in SignupAttribution.objects.filter(tenant_id__in=ids)}
    real_paid = Q(status='paid') & ~Q(payment_provider__in=('gift', 'coupon')) & Q(total__gt=0)
    paid = {row['tenant_id']: row for row in PlatformInvoice.objects.filter(real_paid, tenant_id__in=ids)
            .values('tenant_id').annotate(n=Count('id'), revenue=Sum('total'))}

    groups = {}
    for t in tenants:
        a = attrs.get(t.pk)
        if a and (a.source or a.campaign or a.content):
            key = (a.source or '—', a.campaign or '—', a.content or '—')
        elif a and (a.fbclid or a.gclid):
            key = ('facebook' if a.fbclid else 'google', '—', '—')
        elif a and a.referrer:
            from urllib.parse import urlparse
            key = (urlparse(a.referrer).netloc or a.referrer[:40], '—', '—')
        else:
            key = ('direct', '—', '—')
        g = groups.setdefault(key, {'source': key[0], 'campaign': key[1], 'content': key[2],
                                    'signups': 0, 'verified': 0, 'paid': 0, 'revenue': Decimal('0')})
        g['signups'] += 1
        g['verified'] += 1 if t.is_active else 0
        if t.pk in paid:
            g['paid'] += 1
            g['revenue'] += paid[t.pk]['revenue'] or Decimal('0')
    rows = sorted(groups.values(), key=lambda g: (g['revenue'], g['paid'], g['signups']), reverse=True)
    for g in rows:
        g['conversion'] = (Decimal(g['paid'] * 100) / g['signups']).quantize(Decimal('0.1')) if g['signups'] else None
    recent = [{'tenant': t, 'channel': attrs[t.pk].channel if t.pk in attrs else 'direct',
               'paid': t.pk in paid} for t in tenants[:25]]
    return render(request, 'clients/saas_admin/marketing.html', {
        'rows': rows, 'recent': recent, 'date_from': date_from, 'date_to': date_to,
        'total_signups': len(tenants), 'total_paid': len(paid),
        'total_revenue': sum((g['revenue'] for g in rows), Decimal('0')),
        'pixel_on': bool(getattr(_st, 'META_PIXEL_ID', '')), 'capi_on': meta_capi.configured(),
        'test_code_on': bool(getattr(_st, 'META_CAPI_TEST_CODE', '')),
        'base_url': f"https://{getattr(_st, 'BASE_DOMAIN', 'mousstec.com')}",
    })


# ─────────────────────────────────────────────────────────────────────
# 📊 Revenue Dashboard
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('revenue')
def revenue_dashboard(request):
    """أرقام إيرادات المنصة من PlatformInvoice (status=paid)."""
    today = timezone.localdate()
    start_of_month = today.replace(day=1)
    start_of_30d = today - timedelta(days=30)

    paid_qs = PlatformInvoice.objects.filter(status='paid')

    total_all_time = paid_qs.aggregate(s=Sum('total'))['s'] or Decimal('0.00')
    total_this_month = paid_qs.filter(paid_at__date__gte=start_of_month).aggregate(s=Sum('total'))['s'] or Decimal('0.00')
    total_last_30d = paid_qs.filter(paid_at__date__gte=start_of_30d).aggregate(s=Sum('total'))['s'] or Decimal('0.00')

    invoices_count = paid_qs.count()
    pending_count = PlatformInvoice.objects.filter(status='issued').count()

    # Per-plan breakdown
    by_plan = (
        paid_qs.values('plan_revision__plan__slug', 'plan_revision__plan__name')
        .annotate(total=Sum('total'), count=Count('id'))
        .order_by('-total')
    )

    recent_invoices = (
        PlatformInvoice.objects.select_related('tenant', 'plan_revision', 'plan_revision__plan')
        .order_by('-issued_at')[:25]
    )

    return render(request, 'clients/saas_admin/revenue_dashboard.html', {
        'total_all_time': total_all_time,
        'total_this_month': total_this_month,
        'total_last_30d': total_last_30d,
        'invoices_count': invoices_count,
        'pending_count': pending_count,
        'by_plan': by_plan,
        'recent_invoices': recent_invoices,
    })


# ─────────────────────────────────────────────────────────────────────
# 🔧 Smart Diagnostics — Cross-Tenant API Spend Analytics
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('revenue')
def diagnostics_spend_dashboard(request):
    """Aggregate Smart Diagnostics APICallLog across all tenant schemas.

    Walks every tenant via django_tenants schema_context (this is the
    correct way — APICallLog lives per-schema). Caches results for 5min
    so we don't melt the DB if a SuperAdmin refreshes the page.
    """
    from django.core.cache import cache
    from django_tenants.utils import schema_context
    from collections import defaultdict
    import json

    CACHE_KEY = 'diag_spend_dashboard_v1'
    CACHE_TTL = 300  # 5 minutes

    cached = cache.get(CACHE_KEY)
    if cached and not request.GET.get('refresh'):
        ctx = cached
    else:
        # Lazy imports — these models live in non-shared apps that may be
        # absent in some deploys.
        try:
            from smart_diagnostics.models import APICallLog
        except Exception:
            return render(request, 'clients/saas_admin/diag_spend.html', {
                'unavailable': True,
            })

        today = timezone.localdate()
        start_of_30d = today - timedelta(days=30)
        start_of_24h = timezone.now() - timedelta(hours=24)

        # Per-tenant aggregates
        per_tenant = []  # list of dicts
        daily_buckets = defaultdict(lambda: {'cost': Decimal('0'), 'calls': 0, 'cache_hits': 0})

        all_tenants = (
            Client.objects.exclude(schema_name='public')
            .filter(status__in=['active', 'trial', 'grace'])
            .only('id', 'schema_name', 'name')
        )

        global_total_cost = Decimal('0')
        global_calls = 0
        global_cache_hits = 0

        for tenant in all_tenants:
            try:
                with schema_context(tenant.schema_name):
                    qs = APICallLog.objects.all()
                    total_cost = qs.aggregate(s=Sum('cost_usd'))['s'] or Decimal('0')
                    calls = qs.count()
                    cache_hits = qs.filter(cache_hit=True).count()
                    last_30d_cost = (
                        qs.filter(timestamp__gte=start_of_30d).aggregate(s=Sum('cost_usd'))['s']
                        or Decimal('0')
                    )
                    last_24h_calls = qs.filter(timestamp__gte=start_of_24h).count()
                    last_call = qs.order_by('-timestamp').values_list('timestamp', flat=True).first()

                    if calls == 0:
                        continue  # tenant never used the diagnostics module

                    per_tenant.append({
                        'tenant_id': tenant.id,
                        'tenant_name': tenant.name,
                        'schema': tenant.schema_name,
                        'total_cost': total_cost,
                        'calls': calls,
                        'cache_hits': cache_hits,
                        'cache_hit_pct': round((cache_hits * 100.0 / calls), 1) if calls else 0,
                        'last_30d_cost': last_30d_cost,
                        'last_24h_calls': last_24h_calls,
                        'last_call': last_call,
                    })

                    global_total_cost += total_cost
                    global_calls += calls
                    global_cache_hits += cache_hits

                    # Daily cost trend (last 30 days, paid calls only)
                    paid_qs = qs.filter(timestamp__gte=start_of_30d, cache_hit=False)
                    for row in paid_qs.values('timestamp__date').annotate(s=Sum('cost_usd'), n=Count('id')):
                        day = row['timestamp__date']
                        daily_buckets[day]['cost'] += row['s'] or Decimal('0')
                        daily_buckets[day]['calls'] += row['n']
            except Exception as e:
                logger.warning(f"[DiagSpend] skip tenant {tenant.schema_name}: {e}")

        per_tenant.sort(key=lambda r: r['total_cost'], reverse=True)
        global_cache_pct = (
            round(global_cache_hits * 100.0 / global_calls, 1) if global_calls else 0
        )

        # Build daily series (fill missing days with 0 for a continuous chart)
        days = [(start_of_30d + timedelta(days=i)) for i in range(31)]
        daily_series = [
            {
                'date': d.isoformat(),
                'cost': float(daily_buckets[d]['cost']),
                'calls': daily_buckets[d]['calls'],
            }
            for d in days
        ]

        ctx = {
            'global_total_cost': global_total_cost,
            'global_calls': global_calls,
            'global_cache_hits': global_cache_hits,
            'global_cache_pct': global_cache_pct,
            'tenants_using': len(per_tenant),
            'top_consumers': per_tenant[:10],
            'all_tenants': per_tenant,
            'daily_series_json': json.dumps(daily_series),
            'unavailable': False,
        }
        cache.set(CACHE_KEY, ctx, CACHE_TTL)

    return render(request, 'clients/saas_admin/diag_spend.html', ctx)


# ─────────────────────────────────────────────────────────────────────
# 💎 Revenue Intelligence — MRR, conversion, cohorts, churn
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('revenue')
def revenue_intel(request):
    """
    /superadmin/revenue-intel/ — لقطة عميقة على صحة الـ SaaS:
      • MRR الحالية + breakdown آخر 6 شهور (new / churn / net)
      • Trial → Paid conversion funnel آخر 30/60/90 يوم
      • Cohort retention (شركات سجلت في الشهر X — كام لسه شغالة)
      • ARPU + churn rate
    """
    from clients.models import PlatformInvoice
    from decimal import Decimal as _D
    from datetime import date as _date

    today = timezone.localdate()
    first_of_month = today.replace(day=1)

    # ── Helper: "first of month" N months back ──
    def month_back(d, n):
        m = d.month - n
        y = d.year
        while m <= 0:
            m += 12
            y -= 1
        return _date(y, m, 1)

    def next_month_start(d):
        return month_back(d, -1)

    # ──────────────────────────────────────────────────────────
    # 1. Current MRR
    # ──────────────────────────────────────────────────────────
    # MRR = Σ(active subscriptions' monthly_price)
    # نستبعد public + soft-deleted + غير active
    from clients.models import TenantSubscription
    active_subs = (
        TenantSubscription.objects
        .filter(is_active=True, tenant__is_deleted=False, plan__isnull=False)
        .select_related('plan', 'tenant')
        .exclude(tenant__schema_name='public')
    )
    current_mrr = _D('0.00')
    paying_count = 0
    by_plan_mrr = {}
    for s in active_subs:
        # locked_monthly_price لو موجود يـ override الـ plan.monthly_price
        price = s.locked_monthly_price or s.plan.monthly_price
        if price is None:
            continue
        current_mrr += price
        paying_count += 1
        key = s.plan.slug
        by_plan_mrr.setdefault(key, {
            'slug': s.plan.slug,
            'name': s.plan.name,
            'mrr': _D('0.00'),
            'count': 0,
        })
        by_plan_mrr[key]['mrr'] += price
        by_plan_mrr[key]['count'] += 1
    by_plan_mrr = sorted(by_plan_mrr.values(), key=lambda r: r['mrr'], reverse=True)

    arpu = (current_mrr / paying_count) if paying_count else _D('0')

    # ──────────────────────────────────────────────────────────
    # 2. MRR trend — آخر 6 شهور
    # ──────────────────────────────────────────────────────────
    # نقرأ من PlatformInvoice.paid_at — كل فاتورة مدفوعة بـ period_start/end
    mrr_trend = []
    for n in range(5, -1, -1):
        m_start = month_back(first_of_month, n)
        m_end = next_month_start(m_start)
        # كل الـ paid invoices اللي fall في الشهر ده (paid_at)
        paid_qs = PlatformInvoice.objects.filter(
            status='paid', paid_at__date__gte=m_start, paid_at__date__lt=m_end,
        )
        total = paid_qs.aggregate(s=Sum('total'))['s'] or _D('0')
        # عدد الـ tenants الفريدين اللي دفعوا في الشهر ده
        paid_tenants = paid_qs.values('tenant_id').distinct().count()
        # tenants اللي سجلوا أول مرة في الشهر ده (cohort)
        new_signups = Client.objects.filter(
            created_on__gte=m_start, created_on__lt=m_end,
        ).exclude(schema_name='public').count()
        mrr_trend.append({
            'month': m_start.strftime('%Y-%m'),
            'month_label': m_start.strftime('%b %Y'),
            'total': total,
            'paid_tenants': paid_tenants,
            'new_signups': new_signups,
        })

    # ──────────────────────────────────────────────────────────
    # 3. Trial → Paid conversion funnel — آخر 90 يوم
    # ──────────────────────────────────────────────────────────
    funnel = []
    for window_days, label in [(30, 'آخر 30 يوم'), (60, 'آخر 60 يوم'), (90, 'آخر 90 يوم')]:
        cutoff = today - timedelta(days=window_days)
        cohort = Client.objects.filter(
            created_on__gte=cutoff, is_deleted=False,
        ).exclude(schema_name='public')
        signed_up = cohort.count()
        verified = cohort.filter(is_active=True).count()
        # converted = حالة active + عنده فاتورة paid على الأقل
        converted = cohort.filter(
            status='active',
            id__in=PlatformInvoice.objects.filter(status='paid').values('tenant_id'),
        ).count()
        funnel.append({
            'label': label,
            'window_days': window_days,
            'signed_up': signed_up,
            'verified': verified,
            'verified_pct': round(verified / signed_up * 100, 1) if signed_up else 0,
            'converted': converted,
            'converted_pct': round(converted / signed_up * 100, 1) if signed_up else 0,
        })

    # ──────────────────────────────────────────────────────────
    # 4. Cohort retention — آخر 6 شهور
    # ──────────────────────────────────────────────────────────
    # كل شهر = صف. الأعمدة = M0, M1, M2 ...
    # القيمة في cell (cohort, M_n) = نسبة المتبقين من الـ cohort اللي لسه عندهم
    #   فاتورة paid في الشهر n بعد ما سجلوا.
    cohorts = []
    for n in range(5, -1, -1):
        cohort_month = month_back(first_of_month, n)
        cohort_end = next_month_start(cohort_month)
        cohort_tenants = list(
            Client.objects.filter(
                created_on__gte=cohort_month, created_on__lt=cohort_end,
            ).exclude(schema_name='public').values_list('id', flat=True)
        )
        size = len(cohort_tenants)
        if size == 0:
            cohorts.append({
                'month_label': cohort_month.strftime('%b %Y'),
                'size': 0,
                'cells': [],
            })
            continue
        # لكل شهر بعد cohort_month حتى دلوقتي:
        cells = []
        for k in range(0, n + 1):
            m_s = month_back(cohort_month, -k)
            m_e = next_month_start(m_s)
            if m_s > today:
                break
            still_paying = (
                PlatformInvoice.objects.filter(
                    status='paid', tenant_id__in=cohort_tenants,
                    paid_at__date__gte=m_s, paid_at__date__lt=m_e,
                ).values('tenant_id').distinct().count()
            )
            cells.append({
                'month_idx': k,
                'still_paying': still_paying,
                'pct': round(still_paying / size * 100) if size else 0,
            })
        cohorts.append({
            'month_label': cohort_month.strftime('%b %Y'),
            'size': size,
            'cells': cells,
        })

    # ──────────────────────────────────────────────────────────
    # 5. Churn this month
    # ──────────────────────────────────────────────────────────
    # Churn = اشتراك كان active آخر مرة الشهر اللي فات، ومش active دلوقتي
    last_month_start = month_back(first_of_month, 1)
    last_month_end = first_of_month
    # tenants اللي كان عندهم paid invoice في الشهر اللي فات
    paid_last_month_ids = set(
        PlatformInvoice.objects.filter(
            status='paid', paid_at__date__gte=last_month_start,
            paid_at__date__lt=last_month_end,
        ).values_list('tenant_id', flat=True).distinct()
    )
    paid_this_month_ids = set(
        PlatformInvoice.objects.filter(
            status='paid', paid_at__date__gte=first_of_month,
        ).values_list('tenant_id', flat=True).distinct()
    )
    churned_ids = paid_last_month_ids - paid_this_month_ids
    churn_count = len(churned_ids)
    churn_rate = round(churn_count / len(paid_last_month_ids) * 100, 1) if paid_last_month_ids else 0

    return render(request, 'clients/saas_admin/revenue_intel.html', {
        'current_mrr': current_mrr,
        'arr': current_mrr * 12,
        'paying_count': paying_count,
        'arpu': arpu,
        'by_plan_mrr': by_plan_mrr,
        'mrr_trend': mrr_trend,
        'funnel': funnel,
        'cohorts': cohorts,
        'churn_count': churn_count,
        'churn_rate': churn_rate,
        'last_month_active': len(paid_last_month_ids),
        'today': today,
    })
