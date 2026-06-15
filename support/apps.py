"""
Support app — tickets, live chat, support agent dashboard.

Phase 2A of Wave 2 (see docs/ARCHITECTURE.md). Owns the business-hours
gate that the live chat uses to decide between "agent online" and the
deflection message. Models (SupportTicket, ChatSession, ChatMessage,
StaffRole) still live in clients.models.support until Phase 2B.
"""
from django.apps import AppConfig


class SupportConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'support'
    verbose_name = 'Support (Tickets, Chat, Staff RBAC)'
