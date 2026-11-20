"""🖨️ منطق مشترك بين الصفحات والمهام."""
from django.utils.translation import gettext as _, gettext_lazy

from .models import MarketOrder
from .notify import notify


def run_auto_complete(request):
    """يأكّد الطلبات المتسلّمة اللي عدّى عليها المدة، ويبلّغ الطرفين."""
    done = MarketOrder.auto_complete_due()
    if done:
        from django.conf import settings

        from clients.services.owner_notify import notify_owner
        notify_owner('سوق المطابع: حوّل للمطابع',
                     '\n'.join(f'«{o.title}» — {o.shop.name}' for o in done) + '\n(اتأكّدت تلقائي بعد مدة التسليم)',
                     f'/{settings.ADMIN_URL}/print_market/marketorder/?status__exact=completed')
    for order in done:
        notify(request, order.customer, _('الطلب اتأكّد تلقائي'),
               _('عدّت المدة على تسليم «%(t)s» من غير اعتراض فاتأكّد — قيّم المطبعة.') % {'t': order.title}, order)
        notify(request, order.shop, _('الطلب اكتمل'),
               _('«%(t)s» اكتمل — Mouss Tec هتحوّلك المبلغ.') % {'t': order.title}, order)
    return done


_STALE_REASON = {
    'requested': gettext_lazy('المطبعة ماقدّمتش عرض'),
    'offered': gettext_lazy('العرض ماترّدش عليه'),
    'awaiting_payment': gettext_lazy('الدفع ماتمّش'),
}


def run_housekeeping(request=None):
    """⏰ تأخير التسليم بيتبلّغ للطرفين ولصاحب المنصة، والطلبات المهجورة قبل الدفع بتتلغي."""
    late = MarketOrder.flag_late_orders()
    if late:
        from django.conf import settings

        from clients.services.owner_notify import notify_owner
        notify_owner('سوق المطابع: طلبات اتأخر تسليمها',
                     '\n'.join(f'«{o.title}» — {o.shop.name}' for o in late) + '\n(اتدفعت وعدّت مدة التنفيذ)',
                     f'/{settings.ADMIN_URL}/print_market/marketorder/?status__exact=paid')
    for order in late:
        notify(request, order.customer, _('المطبعة اتأخرت'),
               _('«%(t)s» عدّى ميعاد تسليمه — كلّم المطبعة من الرسائل، ولو عايز تلغي وترجع فلوسك '
                 'افتح «عندي مشكلة».') % {'t': order.title}, order)
        notify(request, order.shop, _('اتأخرت في التسليم'),
               _('«%(t)s» عدّى ميعاد التسليم اللي في عرضك — سلّم أو طمّن العميل من الرسائل.') % {'t': order.title},
               order)
    stale = MarketOrder.cancel_abandoned()
    for order in stale:
        line = _('«%(t)s» اتلغى تلقائي: %(why)s من مدة طويلة.') % {
            't': order.title, 'why': _STALE_REASON[order.stale_status]}
        notify(request, order.customer, _('الطلب اتلغى'), line, order)
        notify(request, order.shop, _('الطلب اتلغى'), line, order)
    return {'late': len(late), 'cancelled': len(stale)}
