"""🖨️ منطق مشترك بين الصفحات والمهام."""
from django.utils.translation import gettext as _

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
