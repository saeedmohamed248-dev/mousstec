from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class HrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'hr'
    verbose_name = _('الموارد البشرية (HR)')

    def ready(self):
        import hr.signals  # noqa: F401
