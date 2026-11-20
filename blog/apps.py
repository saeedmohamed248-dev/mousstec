"""📝 مدونة Mouss Tec — مقالات SEO عن الطباعة والتصميم (والسيارات بعدين). public schema."""
from django.apps import AppConfig


class BlogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'blog'
    verbose_name = '📝 المقالات'
