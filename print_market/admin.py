"""🖨️ أدمن سوق المطابع — صاحب المنصة بيأكّد التحويلات ويسجّل التحويل للمطبعة."""
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .notify import notify
from .models import (MarketAccount, MarketMessage, MarketOffer, MarketOrder, MarketPayment, MarketProduct,
                     MarketProof, MarketReview)


@admin.register(MarketAccount)
class MarketAccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'role', 'email', 'city', 'is_active', 'blocked_attempts', 'created_at')
    list_filter = ('role', 'is_active', 'city')
    search_fields = ('name', 'email')
    readonly_fields = ('blocked_attempts', 'created_at')


@admin.register(MarketReview)
class MarketReviewAdmin(admin.ModelAdmin):
    list_display = ('shop', 'customer', 'rating', 'comment', 'is_visible', 'created_at')
    list_filter = ('rating', 'is_visible')
    list_editable = ('is_visible',)
    search_fields = ('shop__name', 'customer__name', 'comment')
    readonly_fields = ('order', 'shop', 'customer', 'rating', 'comment', 'created_at')


@admin.register(MarketProduct)
class MarketProductAdmin(admin.ModelAdmin):
    list_display = ('title', 'shop', 'category', 'price_from', 'is_active')
    list_filter = ('category', 'is_active')
    search_fields = ('title', 'shop__name')


class _OfferInline(admin.TabularInline):
    model = MarketOffer
    extra = 0
    readonly_fields = ('price', 'delivery_days', 'note', 'status', 'created_at')
    can_delete = False


class _ProofInline(admin.TabularInline):
    model = MarketProof
    extra = 0
    readonly_fields = ('version', 'file', 'note', 'status', 'customer_comment', 'created_at')
    can_delete = False


class _MessageInline(admin.TabularInline):
    model = MarketMessage
    extra = 0
    readonly_fields = ('sender', 'body', 'created_at')
    can_delete = False


@admin.register(MarketOrder)
class MarketOrderAdmin(admin.ModelAdmin):
    list_display = ('title', 'customer', 'shop', 'status', 'payout_info', 'updated_at')
    fieldsets = ((None, {'fields': ('code', 'customer', 'shop', 'product', 'title', 'details', 'quantity',
                                    'attachment', 'status', 'payout_done_at', 'created_at')}),
                 ('التسليم والمشاكل', {'fields': ('delivered_at', 'auto_completed', 'disputed_at',
                                                  'dispute_reason')}))
    list_filter = ('status',)
    search_fields = ('title', 'customer__name', 'shop__name', 'customer__email', 'shop__email')
    readonly_fields = ('code', 'customer', 'shop', 'product', 'status', 'payout_done_at', 'created_at',
                       'delivered_at', 'auto_completed', 'disputed_at', 'dispute_reason')
    inlines = (_OfferInline, _ProofInline, _MessageInline)
    actions = ('mark_payout_done', 'dispute_for_shop', 'dispute_for_customer')

    def has_add_permission(self, request):
        # 🐛 [FIX]: «أضف طلب» كان ظاهر — الطلب بيعمله العميل من السوق، والعميل/المطبعة هنا للقراءة بس
        #    فالحفظ كان بيقع (العميل مطلوب ومش قابل للتعديل).
        return False

    def _resolve(self, request, queryset, for_shop):
        n = 0
        for order in queryset.filter(status='disputed'):
            order.resolve_dispute(for_shop)
            n += 1
            if for_shop and order.status == 'paid':
                # المشكلة اتفتحت قبل التسليم وحكمنا إن المطبعة تكمّل
                notify(request, order.customer, 'قرار المشكلة', f'راجعنا «{order.title}» — المطبعة هتكمّل التنفيذ.', order)
                notify(request, order.shop, 'قرار المشكلة', f'راجعنا «{order.title}» — كمّل التنفيذ وسجّل التسليم.', order)
            elif for_shop:
                notify(request, order.customer, 'قرار المشكلة', f'راجعنا «{order.title}» والطلب اتقفل مكتمل.', order)
                notify(request, order.shop, 'قرار المشكلة', f'راجعنا «{order.title}» — هنحوّلك المبلغ.', order)
            else:
                notify(request, order.customer, 'قرار المشكلة',
                       f'راجعنا «{order.title}» وهنرجّعلك فلوسك على نفس طريقة الدفع.', order)
                notify(request, order.shop, 'قرار المشكلة', f'راجعنا «{order.title}» والطلب اتلغى لصالح العميل.', order)
        self.message_user(request, f'اتحل {n} نزاع (الطلبات اللي فيها مشكلة مفتوحة بس).')

    @admin.action(description='⚖️ حل المشكلة لصالح المطبعة (يكتمل لو اتسلّم — أو ترجع تكمّل التنفيذ)')
    def dispute_for_shop(self, request, queryset):
        self._resolve(request, queryset, True)

    @admin.action(description='↩️ حل المشكلة لصالح العميل (يتلغي — رجّع الفلوس للعميل يدوي)')
    def dispute_for_customer(self, request, queryset):
        self._resolve(request, queryset, False)

    @admin.display(description='تحويل المطبعة')
    def payout_info(self, obj):
        if obj.payout_done_at:
            return '✅'
        if obj.status == 'completed':
            offer = obj.accepted_offer
            return f'⏳ {offer.price if offer else ""} ج.م ← {obj.shop.get_payout_method_display()} {obj.shop.payout_account}'
        return '—'

    @admin.action(description='✅ سجّل إن المبلغ اتحوّل للمطبعة')
    def mark_payout_done(self, request, queryset):
        # 🐛 [FIX]: التحويل كان بيتسجّل من غير ما المطبعة تعرف — آخر إشعار عندها «هنحوّلك المبلغ».
        orders = list(queryset.filter(status='completed', payout_done_at__isnull=True).select_related('shop'))
        n = MarketOrder.objects.filter(pk__in=[o.pk for o in orders], payout_done_at__isnull=True).update(
            payout_done_at=timezone.now())
        for order in orders:
            offer = order.accepted_offer
            notify(request, order.shop, 'اتحوّلك المبلغ',
                   f'Mouss Tec حوّلتلك {offer.price if offer else ""} ج.م تمن «{order.title}»'
                   f' على {order.shop.get_payout_method_display()} {order.shop.payout_account}'.rstrip() + '.', order)
        self.message_user(request, f'اتسجّل التحويل لـ {n} طلب (الطلبات المكتملة بس).')


@admin.register(MarketPayment)
class MarketPaymentAdmin(admin.ModelAdmin):
    list_display = ('order', 'method', 'amount', 'fee', 'total', 'reference', 'receipt_link', 'status',
                    'created_at')
    list_filter = ('status', 'method')
    search_fields = ('reference', 'order__title', 'order__customer__email')
    readonly_fields = ('order', 'method', 'amount', 'fee', 'total', 'reference', 'receipt_link', 'status',
                       'reviewed_by', 'reviewed_at', 'created_at')
    exclude = ('receipt',)
    actions = ('confirm_payments', 'reject_payments')

    def has_add_permission(self, request):
        # 🐛 [FIX]: الدفعة بيعملها العميل وهو بيرفع الإيصال — صفحة «إضافة» من الأدمن (كل
        #    الحقول للقراءة بس) كانت بتقع NoReverseMatch لأن receipt_link بيتنادى من غير pk.
        return False

    def has_delete_permission(self, request, obj=None):
        # 🐛 [FIX]: «احذف المدفوعات المحدّدة» كان ظاهر جنب «أكّد التحويل» — مسح إيصال (حتى المرفوض)
        #    بيمسح أثر فلوس العميل ومين راجعها. الإيصال بيترفض بس، مابيتمسحش.
        return False

    @admin.display(description='الإيصال')
    def receipt_link(self, obj):
        if not getattr(obj, 'pk', None):
            return '—'
        return format_html('<a href="{}" target="_blank">عرض</a>', reverse('print_market:file', args=['receipt', obj.pk]))

    def _review(self, request, queryset, confirm):
        done = 0
        for pay in queryset.filter(status='pending'):
            try:
                pay.review(request.user, confirm, pay.review_note)
                done += 1
                order = pay.order
                if confirm:
                    notify(request, order.customer, 'الدفع اتأكّد', f'استلمنا فلوس «{order.title}» — المطبعة هتبدأ.', order)
                    notify(request, order.shop, 'العميل دفع — ابدأ', f'Mouss Tec أكّدت دفع «{order.title}» — ابدأ التنفيذ.', order)
                else:
                    notify(request, order.customer, 'التحويل محتاج مراجعة',
                           f'مالقيناش تحويل «{order.title}» — ارفع الإيصال تاني.', order)
            except ValidationError as e:
                self.message_user(request, f'{pay}: {"; ".join(e.messages)}', messages.ERROR)
        self.message_user(request, f'اتراجع {done} دفعة.')

    @admin.action(description='✅ أكّد التحويل (الطلب يبدأ تنفيذ)')
    def confirm_payments(self, request, queryset):
        self._review(request, queryset, True)

    @admin.action(description='❌ ارفض التحويل (العميل يرفع تاني)')
    def reject_payments(self, request, queryset):
        self._review(request, queryset, False)
