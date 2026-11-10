"""🧭 رحلة الشركة على السيستم — دخلت إمتى، فتحت صفحات إيه، وقفت فين، وعملت إيه بالظبط.

المصادر:
* VisitorLog (public): كل طلب HTTP من الشركة (tenant_schema) — الصفحات والعمليات بالترتيب.
* LogEntry بتاع الأدمن (جوه schema الشركة): مين من الموظفين أضاف/عدّل/حذف إيه — باسم السجل نفسه.

مفيش مهاجرات: VisitorLog ما بيعرفش الموظف (user=None لطلبات الشركات)، فالموظف بييجي من LogEntry.
"""
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.utils import timezone

SESSION_GAP = timedelta(minutes=30)       # سكوت أكتر من كده = زيارة جديدة
MAX_PAGE_SECONDS = 30 * 60
ONLINE_WINDOW = timedelta(minutes=5)      # آخر طلب خلال ٥ دقايق = أونلاين دلوقتي
DEFAULT_DAYS = 7
MAX_ROWS = 600
MAX_ACTIONS = 60

ADMIN_PREFIX = '/' + getattr(settings, 'ADMIN_URL', 'secure-portal').strip('/') + '/'

# صفحات مش من الأدمن العادي — أسماء مفهومة (الأطول الأول)
PAGE_LABELS = (
    ('/accounts/login/', 'تسجيل الدخول'),
    ('/logout/', 'تسجيل خروج'),
    ('/pos/', 'نقطة البيع (الكاشير)'),
    ('/printing-copilot/', 'مساعد المطبعة'),
    ('/printing/', 'المطبعة'),
    ('/workshop/', 'الورشة'),
    ('/hr/', 'الموارد البشرية'),
    ('/inventory/', 'المخزون'),
    ('/marketplace/merchant/', 'سوق التجار'),
    ('/marketplace/', 'السوق'),
    ('/diagnostics/', 'التشخيص'),
    ('/support/', 'الدعم الفني'),
    ('/ai/', 'استوديو الذكاء الاصطناعي'),
    ('/subscription/', 'الاشتراك والباقات'),
)

ADMIN_ACTIONS = {'add': 'إضافة', 'change': 'تعديل', 'delete': 'حذف', 'history': 'سجل'}


def _model_name(app_label, model_name):
    try:
        return str(apps.get_model(app_label, model_name)._meta.verbose_name)
    except (LookupError, ValueError):
        return ''


def describe(path, method='GET'):
    """اسم مفهوم للصفحة/العملية — مثلاً «إضافة فاتورة بيع» بدل /secure-portal/inventory/saleinvoice/add/."""
    if path == ADMIN_PREFIX:
        return 'الصفحة الرئيسية للوحة'
    if path.startswith(ADMIN_PREFIX):
        parts = [p for p in path[len(ADMIN_PREFIX):].split('/') if p]
        if len(parts) >= 2:
            name = _model_name(parts[0], parts[1])
            if name:
                rest = parts[2:]
                if not rest:
                    return f'قائمة {name}'
                if rest[0] == 'add':
                    return f'{"حفظ" if method == "POST" else "فتح"} إضافة {name}'
                action = rest[1] if len(rest) > 1 else 'change'
                verb = ADMIN_ACTIONS.get(action, 'فتح')
                if method == 'POST' and action == 'change':
                    verb = 'حفظ تعديل'
                if method == 'POST' and action == 'delete':
                    verb = 'تأكيد حذف'
                return f'{verb} {name} #{rest[0]}'
        if len(parts) == 1:
            try:
                return f'قسم {apps.get_app_config(parts[0]).verbose_name}'
            except LookupError:
                pass
    for prefix, label in PAGE_LABELS:
        if path.startswith(prefix):
            return label
    if path == '/':
        return 'الصفحة الرئيسية'
    return ''


def _admin_actions(schema_name, since):
    """آخر العمليات من سجل الأدمن جوه schema الشركة — مين عمل إيه في إيه."""
    from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
    from django_tenants.utils import schema_context
    verbs = {ADDITION: 'أضاف', CHANGE: 'عدّل', DELETION: 'حذف'}
    rows = []
    try:
        with schema_context(schema_name):
            qs = (LogEntry.objects.filter(action_time__gte=since)
                  .select_related('user', 'content_type').order_by('-action_time')[:MAX_ACTIONS])
            for e in qs:
                model = ''
                if e.content_type_id:
                    mc = e.content_type.model_class()
                    model = str(mc._meta.verbose_name) if mc else e.content_type.model
                user = e.user
                rows.append({
                    'time': e.action_time,
                    'user': (user.get_full_name() or user.get_username()) if user else '',
                    'verb': verbs.get(e.action_flag, ''),
                    'is_delete': e.action_flag == DELETION,
                    'model': model,
                    'object': e.object_repr,
                    'details': e.get_change_message(),
                })
    except Exception:
        import logging
        logging.getLogger(__name__).exception("tenant_journey: admin log read failed for %s", schema_name)
    return rows


def build_tenant_journey(tenant, days=DEFAULT_DAYS):
    """يرجّع {'sessions', 'last_page', 'total_pages', 'online', 'actions', 'days'}."""
    from clients.models import VisitorLog

    now = timezone.now()
    since = now - timedelta(days=days)
    # الأحدث الأول عشان الحد ياخد آخر الحركة، وبعدين نرتّب تصاعدي للتقسيم
    logs = list(VisitorLog.objects.filter(tenant_schema=tenant.schema_name, timestamp__gte=since)
                .order_by('-timestamp')[:MAX_ROWS])
    logs.reverse()

    sessions = []
    current = None
    for i, log in enumerate(logs):
        if current is None or log.timestamp - current['end'] > SESSION_GAP:
            current = {'start': log.timestamp, 'end': log.timestamp, 'pages': [],
                       'device': log.device_type, 'ip': log.ip_address}
            sessions.append(current)
        nxt = logs[i + 1] if i + 1 < len(logs) else None
        seconds = None
        if nxt is not None and nxt.timestamp - log.timestamp <= SESSION_GAP:
            seconds = min(int((nxt.timestamp - log.timestamp).total_seconds()), MAX_PAGE_SECONDS)
        current['pages'].append({
            'time': log.timestamp,
            'path': log.path,
            'label': describe(log.path, log.method),
            'method': log.method,
            'is_action': log.method not in ('GET', 'HEAD'),
            'status': log.status_code,
            'is_error': (log.status_code or 0) >= 400,
            'seconds': seconds,
        })
        current['end'] = log.timestamp

    for s in sessions:
        s['duration_seconds'] = int((s['end'] - s['start']).total_seconds())
        s['page_count'] = len(s['pages'])
        s['action_count'] = sum(1 for p in s['pages'] if p['is_action'])
        s['last_page'] = s['pages'][-1]
    sessions.reverse()   # الأحدث فوق

    last_page = sessions[0]['last_page'] if sessions else None
    return {
        'sessions': sessions,
        'last_page': last_page,
        'total_pages': len(logs),
        'online': bool(last_page and now - last_page['time'] <= ONLINE_WINDOW),
        'actions': _admin_actions(tenant.schema_name, since),
        'days': days,
    }
