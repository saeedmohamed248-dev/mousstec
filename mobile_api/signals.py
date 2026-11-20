"""🔔 الأحداث اللي بتبعت إشعار للموبايل.

كل handler محمي بـ try/except: الإشعار عمره ما يوقّع العملية الأصلية
(بيع، رد عميل على بروفة، طلب إجازة...). الإرسال الفعلي بعد الـ commit وفي الخلفية.
"""
import logging

from django.db import connection, transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger('mouss_tec_core')


def _industry():
    return getattr(getattr(connection, 'tenant', None), 'industry', None) or 'automotive'


def _safe(fn):
    def wrapper(*args, **kwargs):
        if getattr(connection, 'schema_name', 'public') == 'public' or kwargs.get('raw'):
            return
        try:
            # savepoint: لو استعلام هنا وقع (مثلاً جدول الأجهزة لسه ماتعملش وقت النشر)
            # الـ transaction الأصلي (البيع/الرد على البروفة) مايتبوّظش على Postgres.
            with transaction.atomic():
                fn(*args, **kwargs)
        except Exception:   # noqa: BLE001
            logger.exception('[PUSH] handler %s', fn.__name__)
    return wrapper


# ─── 🎨 المطابع: العميل رد على البروفة ─────────────────────────────────
@receiver(pre_save, sender='printing.PrintProof')
def _remember_proof_status(sender, instance, **kwargs):
    if instance.pk and not kwargs.get('raw'):
        instance._push_old_status = (sender.objects.filter(pk=instance.pk)
                                     .values_list('status', flat=True).first())


@receiver(post_save, sender='printing.PrintProof')
@_safe
def proof_answered(sender, instance, created=False, **kwargs):
    old = getattr(instance, '_push_old_status', None)
    if created or old != 'pending' or instance.status == 'pending':
        return
    from .push import notify_users, users_with_perm
    job = instance.job
    if instance.status == 'approved':
        title = '✅ العميل وافق على البروفة'
        body = f'{job.description[:80]} — طلب #{job.order.order_number} جاهز للطباعة'
    else:
        title = '✏️ العميل طلب تعديل على البروفة'
        body = f'{job.description[:60]}: {(instance.customer_comment or "")[:100]}'
    users = set(users_with_perm('printing.change_printjob'))
    if instance.created_by_id:
        users.add(instance.created_by_id)
    notify_users(users, title, body, {'type': 'print_job', 'id': job.pk, 'order_id': job.order_id})


# ─── 🚗 الورش: تنبيه نقص مخزون جديد ───────────────────────────────────
@receiver(post_save, sender='inventory.StockAlert')
@_safe
def stock_alert_created(sender, instance, created=False, **kwargs):
    if not created or instance.is_resolved or _industry() != 'automotive':
        return
    from .permissions import STOCK
    from .push import notify_users, users_with_roles
    title = '⚠️ نفاد مخزون' if instance.alert_type == 'out_of_stock' else '📦 مخزون منخفض'
    body = f'{instance.product} — الكمية {instance.current_quantity} ({instance.branch})'
    notify_users(users_with_roles(STOCK), title, body, {'type': 'stock_alert', 'id': instance.pk})


# ─── 👷 طلب إجازة جديد ⇒ الـ HR والمدير ───────────────────────────────
@receiver(post_save, sender='hr.LeaveRequest')
@_safe
def leave_requested(sender, instance, created=False, **kwargs):
    if not created or instance.status != 'pending':
        return
    from .permissions import HR_STAFF
    from .push import notify_users, users_with_roles
    notify_users(users_with_roles(HR_STAFF), '🗓️ طلب إجازة جديد',
                 f'{instance.employee} — من {instance.from_date} لـ {instance.to_date}',
                 {'type': 'leave_request', 'id': instance.pk})
