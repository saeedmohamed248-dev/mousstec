from django.conf import settings
from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
from clients.soft_delete import SoftDeleteMixin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db.models import F
from datetime import timedelta
from decimal import Decimal
import uuid
import logging

logger = logging.getLogger('mouss_tec_core')


# Cross-domain references resolved via:
from .tenancy import *  # noqa: F401, F403

# Staff RBAC, support tickets, live-chat sessions (super-admin desk).

# =====================================================================
# 🔐 StaffRole — Enterprise RBAC للوحة الـ Super Admin
# =====================================================================
class StaffRole(models.Model):
    """
    يعرّف صلاحيات موظفي الـ Super Admin (مش موظفين الـ tenants).
    is_superuser=True يتجاوز كل القيود (god mode).
    """
    ROLE_CHOICES = (
        ('god',         _('المالك الأعلى')),
        ('tech_admin',  _('مدير تقني')),
        ('support',     _('موظف دعم')),
        ('sales',       _('مبيعات')),
        ('finance',     _('محاسبة ومالية')),
    )
    # خريطة الصلاحيات: أي widgets يقدر يشوفها كل دور
    ROLE_WIDGETS = {
        'god':        {'revenue', 'tenants', 'tickets', 'chat', 'errors', 'plans', 'escrow', 'b2b', 'visitors'},
        'tech_admin': {'tenants', 'tickets', 'chat', 'errors', 'plans', 'visitors'},
        'support':    {'tickets', 'chat'},
        'sales':      {'revenue', 'tenants', 'plans', 'visitors'},
        'finance':    {'revenue', 'escrow', 'plans'},
    }

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='staff_role', verbose_name=_("المستخدم"),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name=_("الدور"))
    can_force_delete = models.BooleanField(default=False, verbose_name=_("صلاحية الحذف النهائي؟"))
    notes = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("دور موظف")
        verbose_name_plural = _("أدوار موظفي الإدارة")

    def __str__(self):
        return f"{self.user.username} — {self.get_role_display()}"

    @property
    def visible_widgets(self):
        return self.ROLE_WIDGETS.get(self.role, set())

    def can_view(self, widget_name):
        return widget_name in self.visible_widgets


# =====================================================================
# 📨 SupportTicket — تذاكر دعم العملاء (Help Form + Chat Offline)
# =====================================================================
class SupportTicket(SoftDeleteMixin, models.Model):
    STATUS_CHOICES = (
        ('open',        _('مفتوحة')),
        ('in_progress', _('جاري الحل')),
        ('waiting',     _('بانتظار رد العميل')),
        ('closed',      _('مغلقة')),
    )
    PRIORITY_CHOICES = (
        ('low',    _('عادية')),
        ('medium', _('متوسطة')),
        ('high',   _('عاجلة')),
        ('urgent', _('طارئة')),
    )
    SOURCE_CHOICES = (
        ('form',         _('فورم اتصل بنا')),
        ('chat_offline', _('شات خارج أوقات العمل')),
        ('ai_chatbot',   _('المساعد الذكي')),
        ('email',        _('بريد إلكتروني')),
        ('phone',        _('مكالمة هاتفية')),
    )

    tenant = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='support_tickets', verbose_name=_("المستأجر"),
    )
    name = models.CharField(max_length=120, verbose_name=_("اسم المرسل"))
    email = models.EmailField(verbose_name=_("البريد الإلكتروني"))
    phone = models.CharField(max_length=30, blank=True, default='', verbose_name=_("الهاتف"))
    subject = models.CharField(max_length=200, verbose_name=_("الموضوع"))
    message = models.TextField(verbose_name=_("الرسالة"))

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='open', db_index=True)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='form')

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='assigned_tickets',
    )
    admin_notes = models.TextField(blank=True, default='', verbose_name=_("ملاحظات داخلية"))

    email_delivered = models.BooleanField(default=False)
    email_error = models.CharField(max_length=255, blank=True, default='')

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("تذكرة دعم")
        verbose_name_plural = _("تذاكر الدعم")
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', '-created_at'])]

    def __str__(self):
        return f"#{self.id} {self.subject[:40]} ({self.get_status_display()})"


# =====================================================================
# 💬 Live Chat — جلسات الدعم الحي + Business Hours routing
# =====================================================================
class ChatSession(models.Model):
    STATUS_CHOICES = (
        ('waiting', _('بانتظار رد')),
        ('active',  _('جارية')),
        ('closed',  _('مغلقة')),
    )
    tenant = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='chat_sessions',
    )
    visitor_name = models.CharField(max_length=120, blank=True, default='')
    visitor_email = models.EmailField(blank=True, default='')
    visitor_session_key = models.CharField(max_length=64, db_index=True, blank=True, default='')
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='handled_chats',
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='waiting', db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_activity_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("جلسة شات")
        verbose_name_plural = _("جلسات الشات")
        ordering = ['-started_at']

    def __str__(self):
        return f"Chat #{self.id} — {self.visitor_name or 'ضيف'} ({self.get_status_display()})"

    @property
    def unread_count(self):
        return self.messages.filter(sender='visitor', is_read=False).count()


class ChatMessage(models.Model):
    SENDER_CHOICES = (
        ('visitor', _('زائر')),
        ('agent',   _('موظف دعم')),
        ('bot',     _('بوت تلقائي')),
        ('system',  _('نظام')),
    )
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES)
    body = models.TextField()
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['session', 'created_at'])]


