import logging
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.utils.crypto import get_random_string
from django_tenants.utils import schema_context
from celery import current_app # 🚀 ابتكار: استدعاء طابور المهام المركزي
from .models import Client, Domain, EscrowLedger

# تهيئة رادار المراقبة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🚀 الإشارة المركزية: المايسترو الموجه لتأسيس الشركات (Provisioning Orchestrator)
# =====================================================================
@receiver(post_save, sender=Client)
def auto_setup_new_tenant(sender, instance, created, **kwargs):
    """
    سلسلة الأتمتة (Pipeline):
    1. إنشاء النطاق الفرعي الديناميكي.
    2. تأسيس دفتر الضامن المالي (Genesis Ledger).
    3. زراعة الفرع، الخزينة، ومدير النظام (Smart Data Seeding).
    4. توجيه أمر لبوت الترحيب عبر الـ Celery Queue (Orchestration).
    """
    if created:
        domain_name = ""
        # -------------------------------------------------------------
        # 1. التوجيه الديناميكي وإنشاء النطاق (Smart Domain Resolution)
        # -------------------------------------------------------------
        if not Domain.objects.filter(tenant=instance).exists():
            try:
                base_domain = getattr(settings, 'BASE_DOMAIN', 'mousstec.com') # الدومين الافتراضي للإنتاج
                
                if instance.schema_name == 'public':
                    domain_name = base_domain
                else:
                    domain_name = f"{instance.schema_name}.{base_domain}"
                
                Domain.objects.create(
                    domain=domain_name,
                    tenant=instance,
                    is_primary=True
                )
                logger.info(f"🌐 [ORCHESTRATOR]: Domain '{domain_name}' created for tenant '{instance.name}'")
            except Exception as e:
                logger.error(f"🔴 [ORCHESTRATOR ERROR]: Domain creation failed for {instance.name} - {e}")
                return # وقف السلسلة إذا فشل النطاق

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
        # 3. محرك الحقن الاستباقي (Data Seeding & Fail-Safe Profile)
        # -------------------------------------------------------------
        admin_username = instance.email if instance.email else f"admin_{instance.schema_name}"
        admin_password = get_random_string(12) # 🔑 باسوورد أعقد للأمان

        if instance.schema_name != 'public':
            try:
                # استخدام apps.get_model لمنع الـ Circular Imports
                from django.apps import apps
                Branch = apps.get_model('inventory', 'Branch')
                Treasury = apps.get_model('inventory', 'Treasury')
                ServiceCatalog = apps.get_model('inventory', 'ServiceCatalog')
                ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')
                User = apps.get_model('auth', 'User')
                EmployeeProfile = apps.get_model('inventory', 'EmployeeProfile') # 🚀 جلب الموديل لتفادي Race Conditions

                with schema_context(instance.schema_name):
                    with transaction.atomic(): # 🛡️ حماية متتالية
                        
                        # أ. حقن الفرع الرئيسي
                        main_branch, _ = Branch.objects.get_or_create(
                            name="الفرع الرئيسي",
                            defaults={'location': "المقر الرئيسي", 'phone': instance.phone}
                        )
                        
                        # ب. حقن الخزينة
                        Treasury.objects.get_or_create(
                            name="الخزينة النقدية (الرئيسية)",
                            branch=main_branch,
                            defaults={'type': 'cash', 'balance': 0.00, 'is_active': True}
                        )

                        # ج. حقن بيانات ذكية حسب نوع النشاط
                        ExpenseCategory.objects.get_or_create(name="مصروفات تشغيلية (إيجار/كهرباء)")
                        ExpenseCategory.objects.get_or_create(name="رواتب وسلف")
                        
                        if instance.business_type in ['service_center', 'both']:
                            ServiceCatalog.objects.get_or_create(
                                name="فحص أعطال رقمي شامل (AI Diagnostic)", 
                                defaults={'labor_price': 250.00, 'estimated_hours': 1.0, 'tech_commission_percent': 10.00}
                            )

                        # د. زراعة المدير الآمنة
                        admin_user, u_created = User.objects.get_or_create(
                            username=admin_username,
                            defaults={
                                'email': instance.email or f"{admin_username}@mousstec.com",
                                'first_name': instance.owner_name or 'مدير',
                                'last_name': 'النظام',
                                'is_staff': True,
                                'is_superuser': True
                            }
                        )
                        
                        if u_created:
                            admin_user.set_password(admin_password)
                            admin_user.save()
                        
                        # 🛡️ ابتكار: حل جذري للـ Race Condition الخاص ببروفايل الموظف
                        profile, _ = EmployeeProfile.objects.get_or_create(
                            user=admin_user,
                            defaults={'role': 'admin', 'branch': main_branch, 'can_edit_posted_invoices': True}
                        )
                        profile.branch = main_branch
                        profile.role = 'admin'
                        profile.save()

                logger.info(f"🏢 [ORCHESTRATOR]: Provisioning complete for schema '{instance.schema_name}'")
            except Exception as e:
                logger.error(f"🔴 [ORCHESTRATOR ERROR]: Provisioning crashed for '{instance.schema_name}' - {e}")

        # -------------------------------------------------------------
        # 4. نقل المهمة لبوت الترحيب في الـ Celery Queue (State Transfer)
        # -------------------------------------------------------------
        try:
            # تحديد الرابط بناءً على بيئة التشغيل
            protocol = "http" if getattr(settings, 'DEBUG', False) else "https"
            port_suffix = ":8000" if getattr(settings, 'DEBUG', False) else ""
            admin_url = getattr(settings, 'ADMIN_URL', 'secure-portal')
            full_login_url = f"{protocol}://{domain_name}{port_suffix}/{admin_url}/"

            # 🚀 استدعاء البوت بشكل غير متزامن عبر Celery Task
            current_app.send_task(
                'clients.tasks.async_welcome_bot_task', # اسم البوت في ملف الـ tasks
                args=[instance.name, instance.phone, instance.business_type, full_login_url, admin_username, admin_password]
            )
            logger.info(f"📨 [ORCHESTRATOR]: Task successfully routed to Welcome Bot for {instance.name}")
        except Exception as e:
            logger.error(f"🔴 [ORCHESTRATOR ERROR]: Failed to route task to Welcome Bot - {e}")