"""🗺️ صفحات «الطباعة حسب الخدمة والمحافظة» — /print/ (SEO)."""
from django.urls import path

from . import seo_pages

app_name = 'print_seo'

urlpatterns = [
    path('', seo_pages.index, name='index'),
    path('in/<slug:gov>/', seo_pages.gov_page, name='gov'),
    path('<slug:service>/', seo_pages.service_page, name='service'),
    path('<slug:service>/<slug:gov>/', seo_pages.service_gov_page, name='service_gov'),
]
