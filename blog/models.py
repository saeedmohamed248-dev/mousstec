"""📝 مقال في المدونة — بيتكتب Markdown بسيط من الأدمن (شوف blog/markdown.py)."""
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Article(models.Model):
    SECTOR_CHOICES = (('printing', _('طباعة وتصميم')), ('automotive', _('سيارات')), ('general', _('عام')))

    slug = models.SlugField(max_length=120, unique=True, verbose_name=_('الرابط'),
                            help_text=_('بالإنجليزي وبشَرطة — مثال: business-card-prices'))
    title = models.CharField(max_length=160, verbose_name=_('العنوان'))
    description = models.CharField(max_length=300, verbose_name=_('الوصف (بيظهر في جوجل)'),
                                   help_text=_('جملة أو اتنين — ١٢٠ لـ ١٦٠ حرف أحسن حاجة.'))
    body = models.TextField(verbose_name=_('المقال'),
                            help_text=_('## عنوان — ### عنوان فرعي — - بند في قايمة — **كلام عريض** — '
                                        '[نص اللينك](/print/business-cards/) — والجداول بـ | عمود | عمود |'))
    sector = models.CharField(max_length=20, choices=SECTOR_CHOICES, default='printing', verbose_name=_('القسم'))
    service = models.CharField(max_length=60, blank=True, verbose_name=_('خدمة مرتبطة'),
                               help_text=_('slug خدمة من /print/ (مثال business-cards) — بيظهر لينكها في آخر المقال.'))
    cover = models.ImageField(upload_to='blog/', blank=True, verbose_name=_('صورة الغلاف'))
    is_published = models.BooleanField(default=True, db_index=True, verbose_name=_('منشور'))
    published_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_('تاريخ النشر'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('آخر تعديل'))

    class Meta:
        verbose_name = _('مقال')
        verbose_name_plural = _('المقالات')
        ordering = ['-published_at']

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('blog:article', args=[self.slug])

    @property
    def is_live(self):
        return self.is_published and self.published_at <= timezone.now()

    @property
    def reading_minutes(self):
        from .markdown import plain
        return max(1, round(len(plain(self.body).split()) / 180))
