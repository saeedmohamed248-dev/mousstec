import logging
import uuid
from datetime import timedelta
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.utils import timezone
from django.utils.crypto import get_random_string
from django_tenants.utils import schema_context
from celery import current_app

from .models import Client, Domain, EscrowLedger

# تهيئة رادار المراقبة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🚀 الإشارة المركزية: المايسترو الموجه لتأسيس الشركات (Provisioning Orchestrator)
# =====================================================================
@receiver(post_save, sender=Client)
def auto_setup_new_tenant(sender, instance, created, **kwargs):
    """
    سلسلة الأتمتة المتقدمة (State-of-the-Art Provisioning Pipeline):
    1. Smart Domain Resolution: توجيه النطاق المعزول.
    2. FinTech Genesis: تهيئة دفتر الأستاذ والمحفظة.
    3. Cognitive Data Seeding: حقن بيانات الورشة، الفحوصات القياسية، وتهيئة بيئة الـ AI.
    4. Secure Orchestration: إرسال Magic Link آمن بدلاً من كلمات المرور للـ Celery.
    """
    if created:
        domain_name = ""
        # -------------------------------------------------------------
        # 1. التوجيه الديناميكي وإنشاء النطاق (Smart Domain Resolution)
        # -------------------------------------------------------------
        if not Domain.objects.filter(tenant=instance).exists():
            try:
                base_domain = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
                
                if instance.schema_name == 'public':
                    domain_name = base_domain
                else:
                    url_safe = instance.schema_name.replace('_', '-')
                    domain_name = f"{url_safe}.{base_domain}"
                
                Domain.objects.create(domain=domain_name, tenant=instance, is_primary=True)
                logger.info(f"🌐 [ORCHESTRATOR]: Domain '{domain_name}' created for tenant '{instance.name}'")
            except Exception as e:
                logger.error(f"🔴 [ORCHESTRATOR ERROR]: Domain creation failed for {instance.name} - {e}")
                return # وقف السلسلة إذا فشل النطاق حمايةً للنظام

        # -------------------------------------------------------------
        # 2. التأسيس المالي (FinTech Genesis Block)
        # -------------------------------------------------------------
        try:
            with transaction.atomic():
                EscrowLedger.objects.get_or_create(
                    client=instance,
                    transaction_type='deposit',
                    amount=0.00,
                    defaults={'description': "التأسيس الآلي: فتح محفظة Mouss Tec للضمان المالي (Genesis Block)"}
                )
                logger.info(f"💳 [ORCHESTRATOR]: Genesis Escrow Ledger initialized for '{instance.name}'")
        except Exception as e:
            logger.error(f"🔴 [ORCHESTRATOR ERROR]: Genesis ledger failed for {instance.name} - {e}")

        # -------------------------------------------------------------
        # 3. محرك الحقن الاستباقي (Data Seeding) — حسب القطاع
        # ⚠️ ملاحظة: حساب الأدمن يُنشأ في الـ View وليس هنا لتجنب تضارب كلمات المرور
        # -------------------------------------------------------------
        if instance.schema_name != 'public':
            industry = getattr(instance, 'industry', 'automotive')

            try:
                from django.apps import apps

                with schema_context(instance.schema_name):
                    with transaction.atomic():

                        if industry == 'printing':
                            # 🎨 حقن بيانات المطابع
                            PrintBranch = apps.get_model('printing', 'PrintBranch')
                            PrintTreasury = apps.get_model('printing', 'PrintTreasury')

                            main_branch, _ = PrintBranch.objects.get_or_create(
                                name="الفرع الرئيسي",
                                defaults={'address': "المقر الرئيسي", 'phone': instance.phone}
                            )
                            PrintTreasury.objects.get_or_create(
                                name="الخزينة النقدية (الرئيسية)",
                                branch=main_branch,
                                defaults={'balance': 0.00, 'is_active': True}
                            )
                        else:
                            # 🚗 حقن بيانات السيارات (الافتراضي)
                            Branch = apps.get_model('inventory', 'Branch')
                            Treasury = apps.get_model('inventory', 'Treasury')
                            ServiceCatalog = apps.get_model('inventory', 'ServiceCatalog')
                            ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')

                            main_branch, _ = Branch.objects.get_or_create(
                                name="الفرع الرئيسي",
                                defaults={'location': "المقر الرئيسي للمؤسسة", 'phone': instance.phone}
                            )
                            Treasury.objects.get_or_create(
                                name="الخزينة النقدية (الرئيسية)",
                                branch=main_branch,
                                defaults={'type': 'cash', 'balance': 0.00, 'is_active': True}
                            )
                            ExpenseCategory.objects.get_or_create(name="مصروفات تشغيلية (إيجار/كهرباء/صيانة)")
                            ExpenseCategory.objects.get_or_create(name="رواتب، أجور، وعمولات فنيين")
                            ExpenseCategory.objects.get_or_create(name="مصروفات شحن ولوجستيات (B2B)")

                            if instance.business_type in ['service_center', 'both']:
                                ServiceCatalog.objects.get_or_create(
                                    name="فحص أعطال رقمي شامل بجهاز OBD2 (AI Diagnostic)",
                                    defaults={'labor_price': 300.00, 'estimated_hours': 1.0, 'tech_commission_percent': 10.00}
                                )
                                ServiceCatalog.objects.get_or_create(
                                    name="فحص 36 نقطة الشامل (Standard 36-Point Vehicle Inspection)",
                                    defaults={'labor_price': 0.00, 'estimated_hours': 0.5, 'tech_commission_percent': 0.00,
                                              'description': "فحص مجاني وقائي لزيادة ولاء العملاء."}
                                )

                logger.info(f"🏢 [ORCHESTRATOR]: Provisioning complete for schema '{instance.schema_name}' (industry={industry})")
            except Exception as e:
                logger.error(f"🔴 [ORCHESTRATOR ERROR]: Provisioning failed for '{instance.schema_name}' - {e}")

        # -------------------------------------------------------------
        # 4. نقل المهمة لبوت الترحيب في الـ Celery Queue (Secure Orchestration)
        # ⚠️ ملاحظة: حساب الأدمن يُنشأ في الـ View — هنا نرسل رابط الدخول فقط بدون بيانات حساسة
        # -------------------------------------------------------------
        try:
            protocol = "http" if getattr(settings, 'DEBUG', False) else "https"
            port_suffix = ":8000" if getattr(settings, 'DEBUG', False) else ""
            admin_url = getattr(settings, 'ADMIN_URL', 'secure-portal')

            # بناء رابط الدخول المباشر (بدون token — اليوزر يدخل بالإيميل والباسورد اللي اختارهم)
            full_login_url = f"{protocol}://{domain_name}{port_suffix}/{admin_url}/"

            current_app.send_task(
                'clients.tasks.async_welcome_bot_task',
                args=[
                    instance.name,
                    instance.phone,
                    instance.business_type,
                    full_login_url,
                    instance.email,   # الإيميل كـ username للدخول
                    None              # لا نمرر كلمة المرور — اليوزر يعرفها من صفحة التسجيل
                ],
                expires=300
            )
            logger.info(f"📨 [ORCHESTRATOR]: Secured task successfully routed to Welcome Bot for {instance.name}")
        except Exception as e:
            logger.error(f"🔴 [ORCHESTRATOR ERROR]: Failed to route secure task to Welcome Bot - {e}")