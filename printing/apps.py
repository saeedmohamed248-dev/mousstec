from django.apps import AppConfig


class PrintingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'printing'
    verbose_name = '🎨 إدارة المطابع والتصميم'

    def ready(self):
        from . import signals  # noqa: F401
        from .jazzmin_search import install
        install()   # 🔎 مربع البحث اللي فوق حسب مجال الشركة
