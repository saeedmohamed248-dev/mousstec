"""Unified conversation backbone — أي غرفة تكتب turns هنا."""
from __future__ import annotations

from django.conf import settings
from django.db import models


class RoomKind(models.TextChoices):
    DIAGNOSTIC_ROOM   = 'diagnostic_room',   '🩺 غرفة التشخيص (OBD)'
    AUTO_DIAGNOSTIC   = 'auto_diagnostic',   '🚗 خبير التشخيص متعدد الماركات'
    REPAIR_ATLAS      = 'repair_atlas',      '🔧 أطلس الإصلاح'


class Audience(models.TextChoices):
    SHOP     = 'shop',     '🔧 فني / ورشة'
    CUSTOMER = 'customer', '🚙 صاحب السيارة'


class AIRoomConversation(models.Model):
    """A single back-and-forth conversation in one of the 3 rooms.

    `turns` is a JSONField holding a list of:
        {'role': 'user'|'assistant', 'text': str, 'ts': iso8601,
         'meta': {...}}  # tier/confidence/sources etc.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_room_conversations',
    )
    room = models.CharField(max_length=24, choices=RoomKind.choices,
                             db_index=True, verbose_name='الغرفة')
    audience = models.CharField(max_length=10, choices=Audience.choices,
                                 default=Audience.SHOP, db_index=True,
                                 verbose_name='النوع')

    # Optional vehicle context (free-form)
    brand = models.CharField(max_length=40, blank=True, db_index=True)
    model_name = models.CharField(max_length=80, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    vin = models.CharField(max_length=20, blank=True, db_index=True)

    title = models.CharField(max_length=200, blank=True,
                              help_text='ملخص (أول سؤال غالباً)')
    turns = models.JSONField(default=list, blank=True)
    turn_count = models.PositiveIntegerField(default=0)

    # Cross-link to room-specific records (lazy; not enforced FK)
    external_session_id = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='RepairSession.id أو DiagnosticScan.id إلخ',
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'محادثة غرفة'
        verbose_name_plural = '💬 محادثات الغرف'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', 'room', '-updated_at']),
            models.Index(fields=['audience', '-updated_at']),
        ]

    def __str__(self) -> str:
        return f'[{self.get_room_display()}] {self.title or self.user.username}'

    def append_turn(self, role: str, text: str, meta: dict | None = None) -> None:
        from django.utils import timezone
        if role not in {'user', 'assistant', 'system'}:
            role = 'user'
        turn = {
            'role': role,
            'text': (text or '')[:8000],
            'ts': timezone.now().isoformat(),
        }
        if meta:
            turn['meta'] = meta
        self.turns = (self.turns or []) + [turn]
        self.turn_count = len(self.turns)
        if not self.title and role == 'user':
            self.title = (text or '')[:120]
        self.save(update_fields=['turns', 'turn_count', 'title', 'updated_at'])
