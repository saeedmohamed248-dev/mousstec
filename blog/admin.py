from django.contrib import admin
from django.utils.html import format_html

from .models import Article


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ('title', 'sector', 'is_published', 'published_at', 'link')
    list_filter = ('is_published', 'sector')
    search_fields = ('title', 'description', 'body')
    prepopulated_fields = {}
    fields = ('title', 'slug', 'description', 'sector', 'service', 'cover', 'body', 'is_published', 'published_at')

    @admin.display(description='الرابط')
    def link(self, obj):
        return format_html('<a href="{}" target="_blank">فتح ↗</a>', obj.get_absolute_url())
