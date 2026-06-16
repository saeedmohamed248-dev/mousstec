from django.contrib import admin

from .models import AIRoomConversation


@admin.register(AIRoomConversation)
class AIRoomConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'room', 'audience', 'brand', 'model_name',
                    'turn_count', 'updated_at')
    list_filter = ('room', 'audience')
    search_fields = ('user__username', 'brand', 'model_name', 'vin', 'title')
    date_hierarchy = 'updated_at'
    readonly_fields = ('turns', 'turn_count', 'created_at', 'updated_at')
