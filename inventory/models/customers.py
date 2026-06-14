from django.db import models, transaction, connection
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from simple_history.models import HistoricalRecords
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from datetime import timedelta
from django.contrib.auth.models import User
from django.db.models import F, Sum, Q, ExpressionWrapper, DecimalField

import uuid
import logging

logger = logging.getLogger('mouss_tec_core')

# Customers, vehicles, fleet contracts.

from .organization import *  # noqa: F401, F403

class Customer(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("اسم العميل أو الشركة"))
    phone = models.CharField(max_length=20, unique=True, verbose_name=_("رقم الهاتف"))
    is_b2b_company = models.BooleanField(default=False, verbose_name=_("حساب شركة (B2B Fleet)")) 
    tax_id = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("الرقم الضريبي"))
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, verbose_name=_("الرصيد / المديونية"))
    loyalty_points = models.IntegerField(default=0, verbose_name=_("نقاط الولاء"))
    date_added = models.DateTimeField(auto_now_add=True)

    @property
    def vip_tier(self):
        if self.is_b2b_company: return "🏢 حساب شركة"
        # Defensive: loyalty_points may be a CombinedExpression (F()) right after
        # save() with F() updates — refresh from DB if so to get the real int.
        pts = self.loyalty_points
        try:
            pts = int(pts)
        except (TypeError, ValueError):
            try:
                self.refresh_from_db(fields=['loyalty_points'])
                pts = int(self.loyalty_points)
            except Exception:
                return "🥉 عادي"
        if pts > 5000: return "💎 VIP"
        elif pts > 2000: return "🥇 ذهبي"
        elif pts > 500: return "🥈 فضي"
        return "🥉 عادي"

    class Meta:
        verbose_name = _("عميل / شركة")
        verbose_name_plural = _("سجل العملاء والشركات (CRM)")

    @staticmethod
    def normalize_phone(raw):
        """Apply the same phone normalization that .save() uses, so callers
        that want to look up a record by phone get the same form that's
        actually stored. Without this, code that does
        ``Customer.objects.get_or_create(phone=raw)`` misses the existing row
        and crashes on the UNIQUE constraint.
        """
        if not raw:
            return raw
        import re as _re
        phone = _re.sub(r'[\s\-\(\)]+', '', str(raw))
        if phone.startswith('00'):
            phone = '+' + phone[2:]
        elif phone.startswith('0') and not phone.startswith('+'):
            phone = '+2' + phone
        return phone

    def save(self, *args, **kwargs):
        self.phone = self.normalize_phone(self.phone) if self.phone else self.phone
        super().save(*args, **kwargs)

    def __str__(self): return f"{self.name} - {self.vip_tier}"

class MaintenanceContract(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, limit_choices_to={'is_b2b_company': True}, related_name='contracts')
    contract_code = models.CharField(max_length=50, unique=True, default=uuid.uuid4)
    start_date = models.DateField(default=timezone.now, verbose_name=_("بداية العقد"))
    end_date = models.DateField(verbose_name=_("نهاية العقد"))
    total_value = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("قيمة العقد السنوية"))
    is_active = models.BooleanField(default=True, verbose_name=_("ساري المفعول"))
    class Meta: verbose_name_plural = _("عقود صيانة الشركات (B2B)")
    def __str__(self): return f"عقد {self.customer.name} - {self.contract_code[:6]}"

class Vehicle(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='vehicles', verbose_name=_("المالك"))
    chassis_number = models.CharField(max_length=17, unique=True, verbose_name=_("رقم الشاسيه (VIN)"))
    car_plate = models.CharField(max_length=20, blank=True, null=True, verbose_name=_("رقم اللوحة"))
    brand = models.CharField(max_length=50, default="BMW", verbose_name=_("ماركة السيارة"))
    model_name = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("الموديل"))
    color = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("لون السيارة"))
    transmission = models.CharField(max_length=20, choices=(('Auto', 'أوتوماتيك'), ('Manual', 'مانيوال')), default='Auto', verbose_name=_("نوع الفتيس"))
    last_mileage = models.IntegerField(default=0, verbose_name=_("آخر قراءة للعداد (كم)"))
    estimated_next_visit = models.DateField(blank=True, null=True, verbose_name=_("موعد الصيانة المتوقع"))
    
    ai_health_score = models.IntegerField(default=100, verbose_name=_("مؤشر صحة المركبة بالـ AI (%)"))
    predicted_failure_notes = models.TextField(blank=True, null=True, verbose_name=_("الأعطال المتوقعة قريباً (AI)"))

    class Meta:
        verbose_name = _("مركبة")
        verbose_name_plural = _("سجل المركبات")
    def __str__(self): return f"{self.car_plate or 'بدون لوحة'} - {self.chassis_number[-6:]}"

class VehicleTelemetryLog(models.Model):
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='telemetry_logs')
    dtc_codes_found = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("أكواد الأعطال المرصودة"))
    battery_voltage = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True, verbose_name=_("فولتية البطارية"))
    raw_json_data = models.JSONField(blank=True, null=True, verbose_name=_("البيانات الخام من جهاز الفحص"))
    requires_immediate_attention = models.BooleanField(default=False, verbose_name=_("تحذير: عطل حرج يتطلب تدخلاً فورياً"))
    timestamp = models.DateTimeField(auto_now_add=True)
    class Meta: verbose_name_plural = _("سجل الفحوصات الرقمية الحية (IoT)")

