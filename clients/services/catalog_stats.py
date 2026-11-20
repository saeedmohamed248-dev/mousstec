"""📊 أرقام كتالوج السوق الحقيقية — من الطلبات والتقييمات والزيارات الفعلية بس.

مفيش أي رقم متألّف هنا: لو مفيش طلبات/تقييمات، الواجهة بتعرض «جديد على السوق» أو مابتعرضش حاجة.
"""
from datetime import timedelta

from django.db.models import Avg, Count, Max, Q
from django.utils import timezone

MIN_REVIEWS = 3            # تقييم العملاء بيظهر بعد ٣ تقييمات حقيقية
ACTIVITY_DAYS = 7


def store_ratings(store_ids):
    """{store_id: (المتوسط، العدد)} — للتقييمات الظاهرة بس، ولو عددها ≥ MIN_REVIEWS."""
    from clients.models import CatalogReview
    rows = (CatalogReview.objects.filter(store_id__in=list(store_ids), is_visible=True)
            .values('store_id').annotate(avg=Avg('rating'), n=Count('id')))
    return {r['store_id']: (round(float(r['avg']), 1), r['n']) for r in rows if r['n'] >= MIN_REVIEWS}


def item_activity(item_ids, days=ACTIVITY_DAYS):
    """{item_id: {'week': عدد طلبات السعر في آخر N يوم، 'last': وقت آخر طلب}}"""
    from clients.models import CatalogQuoteRequest
    since = timezone.now() - timedelta(days=days)
    rows = (CatalogQuoteRequest.objects.filter(item_id__in=list(item_ids))
            .exclude(status='cancelled')
            .values('item_id')
            .annotate(week=Count('id', filter=Q(created_at__gte=since)), last=Max('created_at')))
    return {r['item_id']: {'week': r['week'], 'last': r['last']} for r in rows}


def decorate_items(items):
    """بيلزق على كل منتج: week_requests، last_request، store_rating، store_reviews."""
    items = list(items)
    act = item_activity([i.pk for i in items])
    ratings = store_ratings({i.store_id for i in items})
    for i in items:
        a = act.get(i.pk, {})
        i.week_requests = a.get('week', 0)
        i.last_request = a.get('last')
        i.store_rating, i.store_reviews = ratings.get(i.store_id, (None, 0))
    return items


def decorate_stores(stores):
    stores = list(stores)
    ratings = store_ratings([s.pk for s in stores])
    for s in stores:
        s.rating, s.reviews_count = ratings.get(s.pk, (None, 0))
    return stores


def recent_requests(sector, limit=8, hours=72):
    """آخر طلبات أسعار حقيقية (من غير أسامي ولا أرقام) — لشريط «بيحصل دلوقتي»."""
    from clients.models import CatalogQuoteRequest
    since = timezone.now() - timedelta(hours=hours)
    return list(CatalogQuoteRequest.objects.filter(sector=sector, created_at__gte=since, item__isnull=False,
                                                   item__is_active=True)
                .exclude(status='cancelled').select_related('item', 'item__store')
                .order_by('-created_at')[:limit])


def top_items(sector, days=ACTIVITY_DAYS, limit=8):
    """الأكتر طلباً في آخر N يوم (منتجات عليها طلب واحد على الأقل)."""
    from clients.models import CatalogItem
    since = timezone.now() - timedelta(days=days)
    return list(CatalogItem.objects.filter(store__sector=sector, is_active=True, store__is_active=True)
                .annotate(n=Count('quote_requests', filter=Q(quote_requests__created_at__gte=since)
                                  & ~Q(quote_requests__status='cancelled')))
                .filter(n__gt=0).select_related('store').order_by('-n', 'sort')[:limit])


def merchant_pitch_stats(days=30):
    """أرقام حقيقية لصفحة «اعرض منتجاتك»: زوار السوق، طلبات الأسعار، الأقسام الأكتر طلباً."""
    from clients.models import CatalogQuoteRequest, VisitorLog
    since = timezone.now() - timedelta(days=days)
    visitors = (VisitorLog.objects.filter(tenant_schema='public', timestamp__gte=since,
                                          path__startswith='/marketplace/')
                .exclude(ip_address__isnull=True).values('ip_address').distinct().count())
    page_views = VisitorLog.objects.filter(tenant_schema='public', timestamp__gte=since,
                                           path__startswith='/marketplace/', method='GET').count()
    quotes = CatalogQuoteRequest.objects.filter(created_at__gte=since).exclude(status='cancelled')
    top_cats = list(quotes.filter(item__isnull=False).values('sector', 'item__category')
                    .annotate(n=Count('id')).order_by('-n')[:6])
    return {
        'days': days, 'visitors': visitors, 'page_views': page_views,
        'quotes': quotes.count(), 'top_categories': top_cats,
    }
