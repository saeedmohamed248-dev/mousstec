import logging
import threading
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.utils.crypto import get_random_string
from django_tenants.utils import schema_context
from .models import Client, Domain, EscrowLedger

# تهيئة الرادار لتسجيل الحركات
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🤖 روبوت الترحيب غير المتزامن (AI Onboarding & Credential Bot)
# =====================================================================
def async_welcome_bot(client_name, client_phone, business_type, domain_url, admin_user, admin_pass):
    """
    🚀 ابتكار: روبوت لا يرسل ترحيباً فقط، بل يسلم العميل "مفاتيح مملكته الرقمية"
    عبر قناة آمنة في الخلفية دون تعطيل واجهة الاستجابة (Zero-Blocking).
    """
    try:
        # تجهيز محتوى الـ Welcome Kit بناءً على نشاط العميل
        type_str = "مركز الصيانة" if business_type == 'service_center' else "تجارة قطع الغيار"
        
        msg = (
            f"مرحباً بك في Mouss Tec Ecosystem 🚀\n\n"
            f"تم تأسيس نظام {type_str} الخاص بك ({client_name}) بنجاح.\n\n"
            f"🌐 رابط لوحة التحكم: http://{domain_url}:8000/fixit-secure-portal/\n"
            f"👤 اسم المستخدم: {admin_user}\n"
            f"🔑 كلمة المرور الافتراضية: {admin_pass}\n\n"
            f"ننصح بتغيير كلمة المرور فور الدخول. نتمنى لك أرباحاً هائلة!"
        )
        
        # 💡 هنا يتم ربط كود إرسال الـ WhatsApp API أو الـ Email (Twilio / SendGrid)
        logger.info(f"📨 [AI BOT]: Welcome kit & credentials generated for {client_name}. Ready to dispatch to {client_phone}.")
        # print(msg) # للعرض في الـ Console أثناء التطوير
        
    except Exception as e:
        logger.error(f"🔴 [AI BOT ERROR]: Failed to generate welcome kit - {e}")

# =====================================================================
# 🚀 الإشارة المركزية: أتمتة الـ SaaS وتأسيس الشركات (Enterprise Provisioning)
# =====================================================================
@receiver(post_save, sender=Client)
def auto_setup_new_tenant(sender, instance, created, **kwargs):
    """
    بمجرد إنشاء شركة جديدة (Tenant)، يتم:
    1. إنشاء النطاق (Domain) أوتوماتيكياً.
    2. تسجيل نقطة الصفر في الدفتر المالي (Genesis Ledger).
    3. إنشاء مدير النظام (Superuser) وحقن بيانات التأسيس الأساسية (Smart Seeding).
    4. إرسال مفاتيح الدخول عبر روبوت الذكاء الاصطناعي.
    """
    if created:
        domain_name = ""
        # -------------------------------------------------------------
        # 1. إنشاء نطاق (Domain) أوتوماتيكي للشركة
        # -------------------------------------------------------------
        if not Domain.objects.filter(tenant=instance).exists():
            try:
                base_domain = getattr(settings, 'BASE_DOMAIN', 'localhost')
                
                if instance.schema_name == 'public':
                    domain_name = base_domain
                else:
                    domain_name = f"{instance.schema_name}.{base_domain}"
                
                Domain.objects.create(
                    domain=domain_name,
                    tenant=instance,
                    is_primary=True
                )
                logger.info(f"🌐 [SaaS AUTO-SETUP]: Domain '{domain_name}' created for tenant '{instance.name}'")
            except Exception as e:
                logger.error(f"🔴 [SaaS AUTO-SETUP ERROR]: Could not create domain for {instance.name} - {e}")
        
        # -------------------------------------------------------------
        # 2. التأسيس المالي (FinTech Genesis Ledger Entry)
        # -------------------------------------------------------------
        try:
            EscrowLedger.objects.create(
                client=instance,
                transaction_type='deposit',
                amount=0.00,
                description="التأسيس الآلي: فتح محفظة Mouss Tec للضمان المالي (Genesis Block)"
            )
            logger.info(f"💳 [ESCROW READY]: Immutable Ledger initialized for '{instance.name}'")
        except Exception as e:
            logger.error(f"🔴 [ESCROW ERROR]: Could not create genesis ledger for {instance.name} - {e}")

        # -------------------------------------------------------------
        # 3. محرك الحقن الاستباقي وإنشاء الإدارة (Smart Auto-Provisioning)
        # -------------------------------------------------------------
        admin_username = f"admin_{instance.schema_name}"
        admin_password = get_random_string(10) # 🔑 توليد باسوورد عشوائي معقد

        if instance.schema_name != 'public':
            try:
                # استخدام get_model لتجنب مشاكل الـ Circular Imports
                from django.apps import apps
                Branch = apps.get_model('inventory', 'Branch')
                Treasury = apps.get_model('inventory', 'Treasury')
                ServiceCatalog = apps.get_model('inventory', 'ServiceCatalog')
                ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')
                User = apps.get_model('auth', 'User')

                with schema_context(instance.schema_name):
                    with transaction.atomic(): # 🛡️ حماية العمليات بالـ Atomic Transaction
                        
                        # أ. حقن الفرع الرئيسي
                        main_branch, br_created = Branch.objects.get_or_create(
                            name="الفرع الرئيسي",
                            defaults={'location': "المقر الرئيسي", 'phone': instance.phone}
                        )
                        
                        # ب. حقن الخزينة النقدية الافتراضية
                        Treasury.objects.get_or_create(
                            name="الخزينة النقدية (الرئيسية)",
                            branch=main_branch,
                            defaults={'type': 'cash', 'balance': 0.00, 'is_active': True}
                        )

                        # ج. حقن بيانات مساعدة للعميل حسب نشاطه (Smart Data Seeding)
                        ExpenseCategory.objects.get_or_create(name="مصروفات تشغيلية (إيجار/كهرباء)")
                        ExpenseCategory.objects.get_or_create(name="رواتب وسلف")
                        
                        if instance.business_type in ['service_center', 'both']:
                            ServiceCatalog.objects.get_or_create(
                                name="فحص أعطال رقمي شامل (AI Diagnostic)", 
                                defaults={'labor_price': 250.00, 'estimated_hours': 1.0, 'tech_commission_percent': 10.00}
                            )

                        # د. إنشاء مدير النظام الآلي وربطه بالفرع
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
                            # ملف الموظف يتم إنشاؤه عبر إشارة أخرى (signals.py)، نقوم بتحديثه فقط
                            profile = admin_user.employee_profile
                            profile.branch = main_branch
                            profile.role = 'admin'
                            profile.save()

                logger.info(f"🏢 [AUTO-PROVISIONING]: Setup complete (Admin, Branch, Treasury & Data Seed) inside '{instance.schema_name}'")
            except Exception as e:
                logger.error(f"🔴 [AUTO-PROVISIONING ERROR]: Failed inside schema '{instance.schema_name}' - {e}")

        # -------------------------------------------------------------
        # 4. إطلاق روبوت الترحيب في الخلفية (Background Dispatcher)
        # -------------------------------------------------------------
        threading.Thread(
            target=async_welcome_bot, 
            args=(instance.name, instance.phone, instance.business_type, domain_name, admin_username, admin_password),
            daemon=True
        ).start()