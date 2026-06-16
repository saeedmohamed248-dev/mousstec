"""Admin for repair_atlas — quick visibility into incoming sessions/answers."""
from django.contrib import admin

from .models import (
    RepairSession, RepairQuery, RepairAnswer, TechPhoto, VerifiedKnowledge,
)


@admin.register(RepairSession)
class RepairSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'brand', 'model_name', 'year', 'title',
                    'created_at')
    list_filter = ('brand',)
    search_fields = ('user__username', 'brand', 'model_name', 'vin', 'title')
    date_hierarchy = 'created_at'


@admin.register(RepairQuery)
class RepairQueryAdmin(admin.ModelAdmin):
    list_display = ('id', 'session', 'mode', 'part_or_system', 'created_at')
    list_filter = ('mode',)
    search_fields = ('question_text', 'part_or_system')


@admin.register(RepairAnswer)
class RepairAnswerAdmin(admin.ModelAdmin):
    list_display = ('id', 'query', 'source', 'review_status', 'reviewed_by',
                    'created_at')
    list_filter = ('source', 'review_status')
    search_fields = ('body_markdown',)
    readonly_fields = ('llm_model_used', 'tokens_in', 'tokens_out', 'cost_cents')


@admin.register(TechPhoto)
class TechPhotoAdmin(admin.ModelAdmin):
    list_display = ('id', 'query', 'ai_verdict', 'created_at')
    list_filter = ('ai_verdict',)


@admin.register(VerifiedKnowledge)
class VerifiedKnowledgeAdmin(admin.ModelAdmin):
    list_display = ('id', 'brand_norm', 'model_norm', 'mode',
                    'part_or_system_norm', 'times_served', 'updated_at')
    list_filter = ('brand_norm', 'mode')
    search_fields = ('brand_norm', 'model_norm', 'part_or_system_norm',
                     'question_pattern', 'answer_markdown')
