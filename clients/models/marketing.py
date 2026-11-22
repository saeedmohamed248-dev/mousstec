"""
📣 مصدر كل شركة (الإعلانات) — عشان نعرف أنهي حملة جابت عملاء بيدفعوا
=======================================================================
قبل كده مفيش أي حاجة بتتسجّل عن الزائر جه منين: إعلان فيسبوك؟ جوجل؟ صاحبه؟ فأي فلوس
إعلانات كانت هتتصرف من غير ما نعرف أنهي إعلان جاب تسجيل وأنهي جاب اشتراك مدفوع.

الزائر اللي داخل من لينك فيه utm_* أو fbclid/gclid بيتحفظ مصدره في كوكي (آخر حملة ضغط
عليها — clients/services/attribution.py)، ولما يسجّل شركة المصدر بيتنسخ هنا.
fbc/fbp/ip/user_agent بيتبعتوا لـ Meta Conversions API مع حدث الدفع (اللي بيحصل على
السيرفر من غير متصفح العميل) عشان فيسبوك يربطه بالإعلان.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SignupAttribution(models.Model):
    tenant = models.OneToOneField('clients.Client', on_delete=models.CASCADE, related_name='attribution',
                                  verbose_name=_("الشركة"))
    source = models.CharField(max_length=100, blank=True, default='', verbose_name=_("المصدر (utm_source)"))
    medium = models.CharField(max_length=100, blank=True, default='', verbose_name=_("الوسيلة (utm_medium)"))
    campaign = models.CharField(max_length=150, blank=True, default='', verbose_name=_("الحملة (utm_campaign)"))
    content = models.CharField(max_length=150, blank=True, default='', verbose_name=_("الإعلان (utm_content)"))
    term = models.CharField(max_length=150, blank=True, default='', verbose_name=_("utm_term"))
    fbclid = models.CharField(max_length=255, blank=True, default='')
    gclid = models.CharField(max_length=255, blank=True, default='')
    fbc = models.CharField(max_length=300, blank=True, default='')
    fbp = models.CharField(max_length=120, blank=True, default='')
    landing_path = models.CharField(max_length=300, blank=True, default='', verbose_name=_("أول صفحة"))
    referrer = models.CharField(max_length=300, blank=True, default='', verbose_name=_("جاي من موقع"))
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("مصدر التسجيل")
        verbose_name_plural = _("مصادر التسجيل")
        indexes = [models.Index(fields=['source', 'campaign'], name='attr_source_campaign')]

    def __str__(self):
        return f'{self.tenant_id}: {self.channel}'

    @property
    def channel(self):
        """اسم مفهوم للمصدر: الحملة لو موجودة، وإلا الموقع اللي جه منه، وإلا «مباشر»."""
        if self.source or self.campaign:
            return ' / '.join(x for x in (self.source, self.campaign) if x)
        if self.fbclid:
            return 'facebook'
        if self.gclid:
            return 'google'
        return self.referrer or 'direct'


class TenantOutreach(models.Model):
    """📲 تواصل مالك المنصة مع شركة مسجّلة على واتساب + نتيجة المتابعة.

    صف لكل رسالة: من موبايل المالك (زرار بيفتح wa.me والرسالة جاهزة) أو أوتوماتيك من رقم
    المنصة (WhatsApp Cloud API). بعد كده المالك بيسجّل النتيجة (ردّ/مهتم/اشترك…) والميعاد
    الجاي للمتابعة — فصفحة /superadmin/whatsapp/ بتقول له النهارده يكلّم مين.
    """
    CHANNEL_CHOICES = (
        ('phone', _('من موبايلي')),
        ('api', _('أوتوماتيك من رقم المنصة')),
    )
    STATUS_CHOICES = (
        ('opened', _('اتفتح الواتساب')),      # من الموبايل: الزرار اتداس (الإرسال نفسه بإيد المالك)
        ('queued', _('في الطابور')),          # من رقم المنصة: مستنية مهمة الإرسال في الخلفية
        ('sent', _('اتبعتت')),               # من رقم المنصة: Meta قبلت الرسالة
        ('failed', _('ماتبعتتش')),
        ('no_reply', _('مارد')),
        ('replied', _('ردّ')),
        ('interested', _('مهتم')),
        ('not_interested', _('مش مهتم')),
        ('subscribed', _('اشترك')),
    )
    OUTCOME_STATUSES = ('no_reply', 'replied', 'interested', 'not_interested', 'subscribed')

    tenant = models.ForeignKey('clients.Client', on_delete=models.CASCADE, related_name='outreach',
                               verbose_name=_("الشركة"))
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='phone', verbose_name=_("طريقة الإرسال"))
    phone = models.CharField(max_length=20, blank=True, default='', verbose_name=_("الرقم"))
    message = models.TextField(blank=True, default='', verbose_name=_("الرسالة"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='opened', db_index=True,
                              verbose_name=_("الحالة"))
    notes = models.TextField(blank=True, default='', verbose_name=_("ملاحظات"))
    next_follow_up = models.DateField(null=True, blank=True, db_index=True, verbose_name=_("المتابعة الجاية"))
    batch = models.CharField(max_length=32, blank=True, default='', db_index=True,
                             verbose_name=_("دفعة الإرسال الجماعي"))
    error = models.CharField(max_length=300, blank=True, default='', verbose_name=_("سبب الفشل"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        verbose_name = _("تواصل واتساب")
        verbose_name_plural = _("📲 التواصل مع الشركات على واتساب")
        indexes = [models.Index(fields=['tenant', '-created_at'], name='outreach_tenant_recent')]

    def __str__(self):
        return f"{self.tenant_id} · {self.get_channel_display()} · {self.get_status_display()}"

