"""🧭 رحلة عميل السوق — دخل إمتى، راح في صفحات إيه، ووقف فين.

المصدر: VisitorLog (كل طلب HTTP بيتسجّل في الـ middleware).
* الزيارات المربوطة بالعميل (marketplace_customer) = دقيقة 100%.
* العملاء القدام (قبل الربط): بنلاقي طلب الدخول/التسجيل بتاعه جنب created_at / last_login_at،
  وناخد منه الـ IP + المتصفح، ونجيب الصفحات اللي اتفتحت بنفس الاتنين → «تقديري».
"""
from datetime import timedelta

from django.db.models import Q

SESSION_GAP = timedelta(minutes=30)       # سكوت أكتر من كده = زيارة جديدة
MAX_PAGE_SECONDS = 30 * 60                # مدة الصفحة مابتتحسبش أكتر من كده
ANCHOR_WINDOW = timedelta(minutes=10)     # البحث عن طلب الدخول حوالين وقته
ESTIMATE_SPAN = timedelta(hours=24)       # بنتابع الـ IP+المتصفح لحد يوم بعد الدخول
MAX_ROWS = 400

AUTH_PATHS = ('/marketplace/login/', '/marketplace/register/', '/marketplace/verify-otp/',
              '/marketplace/password/')

# أسماء مفهومة لأشهر الصفحات (الباقي بيظهر بالمسار نفسه)
PAGE_LABELS = (
    ('/marketplace/login/', 'تسجيل الدخول'),
    ('/marketplace/register/', 'التسجيل'),
    ('/marketplace/verify-otp/', 'تأكيد كود الموبايل'),
    ('/marketplace/password/', 'نسيت كلمة السر'),
    ('/marketplace/logout/', 'تسجيل خروج'),
    ('/marketplace/dashboard/', 'لوحة العميل'),
    ('/marketplace/printing/', 'قسم الطباعة'),
    ('/marketplace/automotive/', 'قسم السيارات'),
    ('/marketplace/design-store/', 'متجر التصاميم'),
    ('/marketplace/parts/', 'سوق قطع الغيار'),
    ('/marketplace/notifications/', 'الإشعارات'),
    ('/marketplace/', 'الصفحة الرئيسية للسوق'),
    ('/ai/', 'استوديو الذكاء الاصطناعي'),
)


def page_label(path):
    for prefix, label in PAGE_LABELS:
        if path.startswith(prefix):
            return label
    if path == '/':
        return 'الصفحة الرئيسية'
    return ''


def _estimated_logs(customer, VisitorLog):
    """زيارات تقديرية بالـ IP + المتصفح حوالين لحظة التسجيل/الدخول."""
    anchors = [t for t in (customer.created_at, customer.last_login_at) if t]
    fps = set()
    windows = []
    for t in anchors:
        auth_q = Q()
        for p in AUTH_PATHS:
            auth_q |= Q(path__startswith=p)
        hit = (VisitorLog.objects
               .filter(auth_q, method='POST', status_code__lt=400,
                       timestamp__gte=t - ANCHOR_WINDOW, timestamp__lte=t + ANCHOR_WINDOW)
               .exclude(ip_address__isnull=True)
               .order_by('timestamp').first())
        if not hit:
            continue
        fps.add((hit.ip_address, hit.user_agent))
        windows.append((t - timedelta(hours=1), t + ESTIMATE_SPAN))
    if not fps:
        return []
    fp_q = Q()
    for ip, ua in fps:
        fp_q |= Q(ip_address=ip, user_agent=ua)
    win_q = Q()
    for a, b in windows:
        win_q |= Q(timestamp__gte=a, timestamp__lte=b)
    return list(VisitorLog.objects
                .filter(fp_q, win_q, tenant_schema='public')
                .filter(Q(marketplace_customer__isnull=True) | Q(marketplace_customer=customer))
                .order_by('timestamp')[:MAX_ROWS])


def build_customer_journey(customer):
    """يرجّع {'sessions': [...], 'estimated': bool, 'last_page': {...}|None, 'total_pages': int}."""
    from clients.models import VisitorLog

    logs = list(VisitorLog.objects.filter(marketplace_customer=customer)
                .order_by('timestamp')[:MAX_ROWS])
    estimated = False
    if not logs:
        logs = _estimated_logs(customer, VisitorLog)
        estimated = bool(logs)

    sessions = []
    current = None
    for i, log in enumerate(logs):
        if current is None or log.timestamp - current['end'] > SESSION_GAP:
            current = {'start': log.timestamp, 'end': log.timestamp, 'pages': [],
                       'device': log.device_type, 'ip': log.ip_address,
                       'referer': log.referer}
            sessions.append(current)
        nxt = logs[i + 1] if i + 1 < len(logs) else None
        seconds = None
        if nxt is not None and nxt.timestamp - log.timestamp <= SESSION_GAP:
            seconds = min(int((nxt.timestamp - log.timestamp).total_seconds()), MAX_PAGE_SECONDS)
        current['pages'].append({
            'time': log.timestamp,
            'path': log.path,
            'label': page_label(log.path),
            'method': log.method,
            'is_action': log.method != 'GET',
            'status': log.status_code,
            'is_error': (log.status_code or 0) >= 400,
            'seconds': seconds,
        })
        current['end'] = log.timestamp

    for s in sessions:
        s['duration_seconds'] = int((s['end'] - s['start']).total_seconds())
        s['page_count'] = len(s['pages'])
        s['last_page'] = s['pages'][-1]

    sessions.reverse()   # الأحدث فوق
    return {
        'sessions': sessions,
        'estimated': estimated,
        'last_page': sessions[0]['last_page'] if sessions else None,
        'total_pages': len(logs),
    }
