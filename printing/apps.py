from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PrintingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'printing'
    # 🐛 [FIX]: كان بيظهر عربي في مسار التنقّل والعناوين وانت مختار الإنجليزي
    verbose_name = _('🎨 إدارة المطابع والتصميم')

    def ready(self):
        from . import signals  # noqa: F401
        from .jazzmin_search import install
        install()   # 🔎 مربع البحث اللي فوق حسب مجال الشركة
