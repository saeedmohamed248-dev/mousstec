"""
🖨️ موديلات سوق المطابع (public schema — مشتركة بين كل الشركات).

دورة الطلب:
    requested ─(المطبعة تقدّم عرض)→ offered ─(العميل يقبل)→ awaiting_payment
    ─(العميل يرفع إيصال)→ payment_review ─(Mouss Tec تأكّد)→ paid
    ─(المطبعة تسلّم)→ delivered ─(العميل يأكّد الاستلام)→ completed
    العميل يرفض العرض ← يرجع requested والمطبعة تقدّم عرض جديد.
    الإلغاء مسموح قبل الدفع بس.

الفلوس: العميل بيدفع لـ Mouss Tec بس (إنستاباي / فودافون كاش) = سعر العرض + رسوم التحويل.
مفيش عمولة. Mouss Tec بتحوّل للمطبعة بعد تأكيد الاستلام.
"""
import os
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

IMAGE_EXT = ('png', 'jpg', 'jpeg', 'webp', 'gif')
VIDEO_EXT = ('mp4', 'webm', 'mov', 'm4v')
PROOF_EXT = IMAGE_EXT + VIDEO_EXT + ('pdf',)
ATTACH_EXT = IMAGE_EXT + ('pdf', 'ai', 'psd', 'eps', 'svg', 'zip', 'cdr')


def _ext(name):
    return (name or '').lower().rsplit('.', 1)[-1] if '.' in (name or '') else ''


@deconstructible
class _rand_path:
    """اسم ملف عشوائي (مش الاسم الأصلي) — الملفات الخاصة مابتتخمّنش."""
    def __init__(self, prefix):
        self.prefix = prefix

    def __call__(self, instance, filename):
        return f'{self.prefix}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.{_ext(filename) or "bin"}'

    def __eq__(self, other):
        return isinstance(other, _rand_path) and other.prefix == self.prefix


product_image_path = _rand_path('print_market_pub/products')
logo_path = _rand_path('print_market_pub/logos')
attachment_path = _rand_path('print_market/attachments')
proof_path = _rand_path('print_market/proofs')
receipt_path = _rand_path('print_market/receipts')


def _q(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def transfer_fee(amount, method):
    """رسوم التحويل (الحاجة الوحيدة اللي بتتزوّد على السعر) — من الإعدادات."""
    cfg = settings.PRINT_MARKET_TRANSFER_FEES.get(method)
    if cfg is None:
        raise ValidationError(_('طريقة دفع غير صالحة.'))
    amount = Decimal(amount)
    fee = amount * Decimal(str(cfg['pct'])) / Decimal('100')
    fee = max(fee, Decimal(str(cfg['min'])))
    if cfg.get('max'):
        fee = min(fee, Decimal(str(cfg['max'])))
    return _q(fee)


class MarketAccount(models.Model):
    """حساب في السوق — مطبعة (مجاني) أو عميل. الدخول بالإيميل وكلمة سر."""
    ROLE_SHOP, ROLE_CUSTOMER = 'shop', 'customer'
    ROLE_CHOICES = ((ROLE_SHOP, _('مطبعة')), (ROLE_CUSTOMER, _('عميل')))
    PAYOUT_CHOICES = (('instapay', _('إنستاباي')), ('vodafone_cash', _('فودافون كاش')))

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, db_index=True, verbose_name=_('النوع'))
    email = models.EmailField(unique=True, verbose_name=_('الإيميل'))
    password = models.CharField(max_length=128, editable=False)
    name = models.CharField(max_length=120, verbose_name=_('الاسم'))
    city = models.CharField(max_length=60, blank=True, verbose_name=_('المحافظة'))
    # للمطابع بس
    about = models.TextField(blank=True, max_length=1000, verbose_name=_('نبذة عن المطبعة'))
    logo = models.ImageField(upload_to=logo_path, blank=True, verbose_name=_('اللوجو'))
    # بيانات التحويل للمطبعة — بيشوفها صاحب المنصة بس (مابتظهرش للعميل أبداً)
    payout_method = models.CharField(max_length=20, choices=PAYOUT_CHOICES, blank=True,
                                     verbose_name=_('طريقة استلام الفلوس'))
    payout_account = models.CharField(max_length=100, blank=True, verbose_name=_('رقم/عنوان الاستلام'))
    is_active = models.BooleanField(default=True, verbose_name=_('مفعّل'),
                                    help_text=_('اقفله لو الحساب خالف القواعد.'))
    blocked_attempts = models.PositiveIntegerField(default=0, editable=False,
                                                   verbose_name=_('محاولات بعت وسيلة تواصل'))
    session_key = models.UUIDField(default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('تاريخ التسجيل'))

    class Meta:
        verbose_name = _('حساب في سوق المطابع')
        verbose_name_plural = _('حسابات سوق المطابع')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.get_role_display()})'

    @property
    def is_shop(self):
        return self.role == self.ROLE_SHOP

    def set_password(self, raw):
        self.password = make_password(raw)

    def check_password(self, raw):
        return check_password(raw, self.password)

    def rotate_session(self):
        """يطلّع الحساب من كل الأجهزة (خروج / تغيير كلمة السر)."""
        self.session_key = uuid.uuid4()
        self.save(update_fields=['session_key'])

    def save(self, *args, **kwargs):
        self.email = (self.email or '').strip().lower()
        super().save(*args, **kwargs)


class MarketProduct(models.Model):
    """منتج بتعرضه المطبعة في السوق."""
    CATEGORY_CHOICES = (
        ('cards', _('كروت شخصية')),
        ('flyers', _('فلايرز وبروشورات')),
        ('banners', _('بنرات ورول أب')),
        ('stickers', _('ستيكرز وليبل')),
        ('packaging', _('علب وتغليف')),
        ('gifts', _('هدايا ومطبوعات دعائية')),
        ('apparel', _('طباعة على الملابس')),
        ('stationery', _('مطبوعات مكتبية')),
        ('other', _('أخرى')),
    )
    shop = models.ForeignKey(MarketAccount, on_delete=models.CASCADE, related_name='products',
                             limit_choices_to={'role': 'shop'}, verbose_name=_('المطبعة'))
    title = models.CharField(max_length=150, verbose_name=_('اسم المنتج'))
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other',
                                db_index=True, verbose_name=_('القسم'))
    description = models.TextField(blank=True, max_length=2000, verbose_name=_('الوصف'))
    price_from = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                     verbose_name=_('السعر يبدأ من'))
    unit = models.CharField(max_length=40, blank=True, verbose_name=_('الوحدة'),
                            help_text=_('مثال: ١٠٠٠ كارت'))
    image = models.ImageField(upload_to=product_image_path, blank=True, verbose_name=_('الصورة'))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_('ظاهر في السوق'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('منتج في سوق المطابع')
        verbose_name_plural = _('منتجات سوق المطابع')
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class MarketOrder(models.Model):
    """طلب عميل لمطبعة — من منتج أو طلب خاص."""
    STATUS_CHOICES = (
        ('requested', _('مستني عرض المطبعة')),
        ('offered', _('وصل عرض — مستني ردّك')),
        ('awaiting_payment', _('مستني الدفع')),
        ('payment_review', _('بنراجع التحويل')),
        ('paid', _('اتدفع — جاري التنفيذ')),
        ('delivered', _('اتسلّم — مستني تأكيد العميل')),
        ('completed', _('مكتمل')),
        ('cancelled', _('ملغي')),
    )
    ACTIVE = ('requested', 'offered', 'awaiting_payment', 'payment_review', 'paid', 'delivered')
    CANCELLABLE = ('requested', 'offered', 'awaiting_payment')

    code = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    customer = models.ForeignKey(MarketAccount, on_delete=models.PROTECT, related_name='orders',
                                 limit_choices_to={'role': 'customer'}, verbose_name=_('العميل'))
    shop = models.ForeignKey(MarketAccount, on_delete=models.PROTECT, related_name='shop_orders',
                             limit_choices_to={'role': 'shop'}, verbose_name=_('المطبعة'))
    product = models.ForeignKey(MarketProduct, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='orders', verbose_name=_('المنتج'))
    title = models.CharField(max_length=150, verbose_name=_('عنوان الطلب'))
    details = models.TextField(max_length=3000, verbose_name=_('التفاصيل'))
    quantity = models.PositiveIntegerField(default=1, verbose_name=_('الكمية'))
    attachment = models.FileField(upload_to=attachment_path, blank=True, verbose_name=_('ملف مرفق'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested',
                              db_index=True, verbose_name=_('الحالة'))
    payout_done_at = models.DateTimeField(null=True, blank=True, verbose_name=_('اتحوّل للمطبعة في'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('تاريخ الطلب'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('طلب في سوق المطابع')
        verbose_name_plural = _('طلبات سوق المطابع')
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.title} — {self.customer.name} ← {self.shop.name}'

    @property
    def accepted_offer(self):
        return self.offers.filter(status='accepted').first()

    @property
    def pending_offer(self):
        return self.offers.filter(status='pending').first()

    @property
    def latest_proof(self):
        return self.proofs.order_by('-version').first()

    def _locked(self):
        return MarketOrder.objects.select_for_update().get(pk=self.pk)

    def _set_status(self, order, status):
        order.status = status
        order.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()

    def cancel(self, by):
        with transaction.atomic():
            order = self._locked()
            if by.pk not in (order.customer_id, order.shop_id):
                raise ValidationError(_('مش مسموح.'))
            if order.status not in self.CANCELLABLE:
                raise ValidationError(_('الطلب ماينفعش يتلغي بعد الدفع — كلّم الدعم من المنصة.'))
            order.offers.filter(status='pending').update(status='rejected')
            self._set_status(order, 'cancelled')

    def mark_delivered(self, shop):
        with transaction.atomic():
            order = self._locked()
            if shop.pk != order.shop_id:
                raise ValidationError(_('مش مسموح.'))
            if order.status != 'paid':
                raise ValidationError(_('التسليم بيتسجّل بعد ما الدفع يتأكّد.'))
            proof = order.proofs.order_by('-version').first()
            if proof and proof.status != 'approved':
                raise ValidationError(_('آخر بروفة لسه العميل ماوافقش عليها.'))
            self._set_status(order, 'delivered')

    def confirm_received(self, customer):
        with transaction.atomic():
            order = self._locked()
            if customer.pk != order.customer_id:
                raise ValidationError(_('مش مسموح.'))
            if order.status != 'delivered':
                raise ValidationError(_('المطبعة لسه ماسجّلتش التسليم.'))
            self._set_status(order, 'completed')


class MarketOffer(models.Model):
    """عرض سعر من المطبعة على الطلب."""
    STATUS_CHOICES = (('pending', _('مستني رد العميل')), ('accepted', _('مقبول')), ('rejected', _('مرفوض')))

    order = models.ForeignKey(MarketOrder, on_delete=models.CASCADE, related_name='offers',
                              verbose_name=_('الطلب'))
    price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('السعر (ج.م)'))
    delivery_days = models.PositiveSmallIntegerField(default=3, verbose_name=_('مدة التنفيذ (أيام)'))
    note = models.CharField(max_length=500, blank=True, verbose_name=_('ملاحظات'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', db_index=True,
                              verbose_name=_('الحالة'))
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _('عرض سعر')
        verbose_name_plural = _('عروض الأسعار')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.price} ج.م — {self.order.title}'

    @classmethod
    def submit(cls, order, shop, price, delivery_days, note=''):
        price = Decimal(price)
        if price <= 0:
            raise ValidationError(_('السعر لازم يكون أكبر من صفر.'))
        if not 1 <= int(delivery_days) <= 365:
            raise ValidationError(_('مدة التنفيذ لازم تكون من يوم لـ ٣٦٥ يوم.'))
        with transaction.atomic():
            locked = MarketOrder.objects.select_for_update().get(pk=order.pk)
            if shop.pk != locked.shop_id:
                raise ValidationError(_('مش مسموح.'))
            if locked.status not in ('requested', 'offered'):
                raise ValidationError(_('الطلب ده مابقاش مستني عروض.'))
            # عرض جديد بيلغي القديم اللي لسه ماترّدش عليه
            locked.offers.filter(status='pending').update(status='rejected', responded_at=timezone.now())
            offer = cls.objects.create(order=locked, price=_q(price), delivery_days=int(delivery_days),
                                       note=note)
            locked.status = 'offered'
            locked.save(update_fields=['status', 'updated_at'])
        order.refresh_from_db()
        return offer

    def respond(self, customer, accept):
        with transaction.atomic():
            order = MarketOrder.objects.select_for_update().get(pk=self.order_id)
            offer = MarketOffer.objects.select_for_update().get(pk=self.pk)
            if customer.pk != order.customer_id:
                raise ValidationError(_('مش مسموح.'))
            if offer.status != 'pending' or order.status != 'offered':
                raise ValidationError(_('العرض ده اترد عليه قبل كده.'))
            offer.status = 'accepted' if accept else 'rejected'
            offer.responded_at = timezone.now()
            offer.save(update_fields=['status', 'responded_at'])
            order.status = 'awaiting_payment' if accept else 'requested'
            order.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()
        return self


class MarketPayment(models.Model):
    """تحويل العميل لـ Mouss Tec — بيتأكّد يدوي من صاحب المنصة."""
    METHOD_CHOICES = (('instapay', _('إنستاباي')), ('vodafone_cash', _('فودافون كاش')))
    STATUS_CHOICES = (('pending', _('بنراجعه')), ('confirmed', _('اتأكّد')), ('rejected', _('مرفوض')))

    order = models.ForeignKey(MarketOrder, on_delete=models.PROTECT, related_name='payments',
                              verbose_name=_('الطلب'))
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, verbose_name=_('طريقة الدفع'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, editable=False, verbose_name=_('سعر العرض'))
    fee = models.DecimalField(max_digits=12, decimal_places=2, editable=False, verbose_name=_('رسوم التحويل'))
    total = models.DecimalField(max_digits=12, decimal_places=2, editable=False, verbose_name=_('الإجمالي'))
    reference = models.CharField(max_length=100, verbose_name=_('رقم العملية / اسم المحوِّل'))
    receipt = models.ImageField(upload_to=receipt_path, verbose_name=_('صورة الإيصال'))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', db_index=True,
                              verbose_name=_('الحالة'))
    review_note = models.CharField(max_length=300, blank=True, verbose_name=_('ملاحظة المراجعة'))
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name='+', editable=False)
    reviewed_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('تاريخ الرفع'))

    class Meta:
        verbose_name = _('دفعة في سوق المطابع')
        verbose_name_plural = _('مدفوعات سوق المطابع')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.total} ج.م — {self.order.title}'

    @classmethod
    def submit(cls, order, customer, method, reference, receipt):
        with transaction.atomic():
            locked = MarketOrder.objects.select_for_update().get(pk=order.pk)
            if customer.pk != locked.customer_id:
                raise ValidationError(_('مش مسموح.'))
            if locked.status != 'awaiting_payment':
                raise ValidationError(_('الطلب ده مش مستني دفع.'))
            offer = locked.offers.filter(status='accepted').first()
            if offer is None:
                raise ValidationError(_('مفيش عرض مقبول على الطلب.'))
            fee = transfer_fee(offer.price, method)
            pay = cls.objects.create(order=locked, method=method, amount=offer.price, fee=fee,
                                     total=offer.price + fee, reference=reference.strip()[:100],
                                     receipt=receipt)
            locked.status = 'payment_review'
            locked.save(update_fields=['status', 'updated_at'])
        order.refresh_from_db()
        return pay

    def review(self, user, confirm, note=''):
        """صاحب المنصة بيأكّد التحويل (الطلب يتدفع) أو يرفضه (العميل يرفع تاني)."""
        with transaction.atomic():
            pay = MarketPayment.objects.select_for_update().get(pk=self.pk)
            order = MarketOrder.objects.select_for_update().get(pk=pay.order_id)
            if pay.status != 'pending':
                raise ValidationError(_('الدفعة دي اتراجعت قبل كده.'))
            pay.status = 'confirmed' if confirm else 'rejected'
            pay.review_note = (note or '')[:300]
            pay.reviewed_by = user
            pay.reviewed_at = timezone.now()
            pay.save(update_fields=['status', 'review_note', 'reviewed_by', 'reviewed_at'])
            if order.status == 'payment_review':
                order.status = 'paid' if confirm else 'awaiting_payment'
                order.save(update_fields=['status', 'updated_at'])
        self.refresh_from_db()
        return self


class MarketProof(models.Model):
    """بروفة (صورة / PDF / فيديو) — العميل بيوافق أو يطلب تعديل."""
    STATUS_CHOICES = (
        ('pending', _('مستني رد العميل')),
        ('approved', _('العميل وافق')),
        ('changes_requested', _('العميل طلب تعديل')),
    )
    order = models.ForeignKey(MarketOrder, on_delete=models.CASCADE, related_name='proofs',
                              verbose_name=_('الطلب'))
    version = models.PositiveSmallIntegerField(default=1, editable=False, verbose_name=_('رقم النسخة'))
    file = models.FileField(upload_to=proof_path, verbose_name=_('ملف البروفة'))
    note = models.CharField(max_length=500, blank=True, verbose_name=_('ملاحظات للعميل'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True,
                              verbose_name=_('الحالة'))
    customer_comment = models.TextField(blank=True, max_length=2000, verbose_name=_('تعليق العميل'))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_('تاريخ الإرسال'))
    responded_at = models.DateTimeField(null=True, blank=True, verbose_name=_('تاريخ الرد'))

    OPEN_ORDER_STATUSES = ('awaiting_payment', 'payment_review', 'paid')

    class Meta:
        verbose_name = _('بروفة في سوق المطابع')
        verbose_name_plural = _('بروفات سوق المطابع')
        ordering = ['-version']
        constraints = [models.UniqueConstraint(fields=['order', 'version'], name='pm_proof_order_version')]

    def __str__(self):
        return f'بروفة {self.version} — {self.order.title}'

    @property
    def kind(self):
        ext = _ext(self.file.name)
        if ext in VIDEO_EXT:
            return 'video'
        if ext in IMAGE_EXT:
            return 'image'
        return 'pdf'

    @classmethod
    def submit(cls, order, shop, file, note=''):
        with transaction.atomic():
            locked = MarketOrder.objects.select_for_update().get(pk=order.pk)
            if shop.pk != locked.shop_id:
                raise ValidationError(_('مش مسموح.'))
            if locked.status not in cls.OPEN_ORDER_STATUSES:
                raise ValidationError(_('البروفة بتتبعت بعد ما العميل يقبل العرض ولحد التسليم.'))
            last = locked.proofs.aggregate(v=models.Max('version'))['v'] or 0
            # البروفة الجديدة بتلغي أي بروفة قديمة لسه مستنية
            locked.proofs.filter(status='pending').update(status='changes_requested')
            proof = cls.objects.create(order=locked, version=last + 1, file=file, note=note)
            locked.save(update_fields=['updated_at'])
        return proof

    def respond(self, customer, approve, comment=''):
        with transaction.atomic():
            proof = MarketProof.objects.select_for_update().get(pk=self.pk)
            order = proof.order
            if customer.pk != order.customer_id:
                raise ValidationError(_('مش مسموح.'))
            if proof.status != 'pending':
                raise ValidationError(_('البروفة دي اترد عليها قبل كده.'))
            if not approve and not (comment or '').strip():
                raise ValidationError(_('اكتب التعديل اللي محتاجه عشان المطبعة تفهمك.'))
            proof.status = 'approved' if approve else 'changes_requested'
            proof.customer_comment = (comment or '').strip()[:2000]
            proof.responded_at = timezone.now()
            proof.save(update_fields=['status', 'customer_comment', 'responded_at'])
        self.refresh_from_db()
        return self


class MarketMessage(models.Model):
    """رسالة جوه الطلب — التواصل الوحيد المسموح بين العميل والمطبعة."""
    order = models.ForeignKey(MarketOrder, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(MarketAccount, on_delete=models.CASCADE, related_name='+')
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('رسالة في سوق المطابع')
        verbose_name_plural = _('رسائل سوق المطابع')
        ordering = ['created_at']


def file_size_ok(f, max_mb):
    return getattr(f, 'size', 0) <= max_mb * 1024 * 1024


def check_upload(f, allowed_ext, max_mb):
    """يتأكّد من نوع وحجم الملف المرفوع."""
    if not f:
        return
    if _ext(f.name) not in allowed_ext:
        raise ValidationError(_('نوع الملف ده مش مسموح.'))
    if not file_size_ok(f, max_mb):
        raise ValidationError(_('الملف أكبر من المسموح (%(mb)s ميجا).') % {'mb': max_mb})


def basename(f):
    return os.path.basename(getattr(f, 'name', '') or '')
