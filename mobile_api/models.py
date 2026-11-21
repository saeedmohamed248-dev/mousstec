"""📱 أجهزة تطبيق الموبايل — توكن إشعارات Firebase (FCM) لكل موظف وجهاز.

جوه schema كل شركة (mobile_api في TENANT_APPS) فالإشعار بيروح لموظفين الشركة دي بس.
"""
from django.conf import settings
from django.db import models


class MobileDevice(models.Model):
    PLATFORM_CHOICES = (('android', 'Android'), ('ios', 'iOS'), ('web', 'Web'))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='mobile_devices', verbose_name='المستخدم')
    token = models.CharField(max_length=512, unique=True, verbose_name='توكن الإشعارات')
    platform = models.CharField(max_length=10, choices=PLATFORM_CHOICES, default='android',
                                verbose_name='النظام')
    language = models.CharField(max_length=5, default='ar', verbose_name='اللغة')
    is_active = models.BooleanField(default=True, verbose_name='مفعّل')
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'جهاز موبايل'
        verbose_name_plural = 'أجهزة الموبايل'
        ordering = ['-last_seen']

    def __str__(self):
        return f'{self.user} — {self.get_platform_display()}'
