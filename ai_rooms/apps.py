"""
🧠 AI Rooms Hub — Backbone موحد للغرف الذكية:
    1. Diagnostic Room (OBD + Web Bluetooth)
    2. Auto Diagnostic Expert (Multi-brand chat)
    3. Repair Atlas (Disassembly/Install/Wiring)

غرض الـ app:
    • موديل واحد AIRoomConversation يخزن كل محادثة من أي غرفة (شات/فني/عميل).
    • Hub page تجمع الـ 3 غرف وتعرض آخر المحادثات.
    • Cross-room navigation bar مشتركة (template include).

تأكيد:
    • Repair Atlas بيخزن RepairSession/RepairQuery بنظامه الخاص، لكنه برضه
      بيكتب turn لـ AIRoomConversation عشان يظهر في الـ Hub.
    • Auto Diagnostic + Diagnostic Room كانوا session-only — دلوقتي مع كل
      رد بنده persist_turn().
"""
from django.apps import AppConfig


class AIRoomsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ai_rooms'
    verbose_name = '🧠 الغرف الذكية الموحدة'
