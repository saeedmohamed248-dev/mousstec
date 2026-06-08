from django.contrib import admin

from .models import ConversationLog, SystemUpdate


@admin.register(SystemUpdate)
class SystemUpdateAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "is_active", "updated_at")
    list_filter = ("kind", "is_active")
    search_fields = ("title", "content")


@admin.register(ConversationLog)
class ConversationLogAdmin(admin.ModelAdmin):
    list_display = ("sender_id", "created_at", "error")
    search_fields = ("sender_id", "user_message", "bot_response")
    readonly_fields = ("sender_id", "user_message", "bot_response", "error", "created_at")
