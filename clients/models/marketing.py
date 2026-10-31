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
