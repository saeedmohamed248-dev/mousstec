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
from django_tenants.signals import post_schema_sync
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
                from decimal import Decimal as _D
                EscrowLedger.objects.get_or_create(
                    client=instance,
                    transaction_type='deposit',
                    amount=_D('0.00'),
                    description="التأسيس الآلي: فتح محفظة Mouss Tec للضمان المالي (Genesis Block)",
                    defaults={}
                )
                logger.info(f"💳 [ORCHESTRATOR]: Genesis Escrow Ledger initialized for '{instance.name}'")
        except Exception as e:
            logger.error(f"🔴 [ORCHESTRATOR ERROR]: Genesis ledger failed for {instance.name} - {e}")

        # -------------------------------------------------------------
        # 3. محرك الحقن الاستباقي (Data Seeding) — حسب القطاع
        # ⚠️ التأسيس الفعلي يُنفَّذ في post_schema_sync receiver أدناه،
        #    لأن django-tenants ينشئ الـ schema و migrations بعد post_save،
        #    فأي queries هنا على tenant tables ستفشل بـ ProgrammingError.
        # -------------------------------------------------------------

        # -------------------------------------------------------------
        # 🎁 3.5 هدية الترحيب: 10 تصاميم AI + 5 رسائل واتساب + 5 علامات مائية
        # -------------------------------------------------------------
        if instance.schema_name != 'public':
            try:
                from clients.models import AIBonusGrant
                AIBonusGrant.objects.create(
                    tenant=instance,
                    granted_designs=10,
                    granted_whatsapp=5,
                    granted_watermarks=5,
                    reason='🎁 هدية الترحيب الذهبية من Mouss Tec — جرّب AI Studio بدون تكلفة!',
                    granted_by=None,
                    expires_at=None,  # مدى مفتوح
                )
                logger.info(f"🎁 [WELCOME GIFT]: Granted 10 designs + 5 whatsapp + 5 watermarks to {instance.name}")
            except Exception as e:
                logger.error(f"🔴 [WELCOME GIFT ERROR]: Failed for {instance.name} - {e}")

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

# =====================================================================
# 🌱 الحقن الاستباقي بعد إنشاء الـ Schema (Post-Schema Seeding)
# يُشغَّل بعد ما django-tenants ينشئ الـ schema و migrations،
# فالـ tenant tables تكون موجودة فعلاً.
# =====================================================================
@receiver(post_schema_sync)
def seed_tenant_after_schema_sync(sender, tenant, **kwargs):
    """
    حقن البيانات الافتراضية في الـ tenant schema بعد ما الـ migrations تخلص.
    tenant: instance of Client (من serializable_fields() = self).
    """
    if not tenant or getattr(tenant, 'schema_name', 'public') == 'public':
        return

    industry = getattr(tenant, 'industry', 'automotive')
    business_type = getattr(tenant, 'business_type', '')

    try:
        from django.apps import apps

        with schema_context(tenant.schema_name):
            with transaction.atomic():
                if industry == 'printing':
                    # 🎨 حقن بيانات المطابع الشامل
                    PrintBranch = apps.get_model('printing', 'PrintBranch')
                    PrintTreasury = apps.get_model('printing', 'PrintTreasury')
                    PrintMaterial = apps.get_model('printing', 'PrintMaterial')

                    main_branch, _ = PrintBranch.objects.get_or_create(
                        name="الفرع الرئيسي",
                        defaults={'address': "المقر الرئيسي", 'phone': tenant.phone}
                    )
                    PrintTreasury.objects.get_or_create(
                        name="الخزينة النقدية (الرئيسية)",
                        branch=main_branch,
                        defaults={'balance': 0.00, 'is_active': True}
                    )
                    PrintMaterial.objects.get_or_create(
                        name="ورق A4 (80 جم)",
                        defaults={'category': 'paper', 'unit': 'رزمة', 'quantity': 0, 'cost_per_unit': 180, 'min_stock': 5, 'branch': main_branch}
                    )
                    PrintMaterial.objects.get_or_create(
                        name="ورق A3 (130 جم لامع)",
                        defaults={'category': 'paper', 'unit': 'رزمة', 'quantity': 0, 'cost_per_unit': 450, 'min_stock': 3, 'branch': main_branch}
                    )
                    PrintMaterial.objects.get_or_create(
                        name="حبر أسود (Toner)",
                        defaults={'category': 'ink', 'unit': 'قطعة', 'quantity': 0, 'cost_per_unit': 350, 'min_stock': 2, 'branch': main_branch}
                    )
                else:
                    # 🚗 حقن بيانات السيارات (الافتراضي)
                    Branch = apps.get_model('inventory', 'Branch')
                    Treasury = apps.get_model('inventory', 'Treasury')
                    ServiceCatalog = apps.get_model('inventory', 'ServiceCatalog')
                    ExpenseCategory = apps.get_model('inventory', 'ExpenseCategory')

                    main_branch, _ = Branch.objects.get_or_create(
                        name="الفرع الرئيسي",
                        defaults={'location': "المقر الرئيسي للمؤسسة", 'phone': tenant.phone}
                    )
                    Treasury.objects.get_or_create(
                        name="الخزينة النقدية (الرئيسية)",
                        branch=main_branch,
                        defaults={'type': 'cash', 'balance': 0.00, 'is_active': True}
                    )
                    ExpenseCategory.objects.get_or_create(name="مصروفات تشغيلية (إيجار/كهرباء/صيانة)")
                    ExpenseCategory.objects.get_or_create(name="رواتب، أجور، وعمولات فنيين")
                    ExpenseCategory.objects.get_or_create(name="مصروفات شحن ولوجستيات (B2B)")

                    if business_type in ['service_center', 'both']:
                        ServiceCatalog.objects.get_or_create(
                            name="فحص أعطال رقمي شامل بجهاز OBD2 (AI Diagnostic)",
                            defaults={'labor_price': 300.00, 'estimated_hours': 1.0, 'tech_commission_percent': 10.00}
                        )
                        ServiceCatalog.objects.get_or_create(
                            name="فحص 36 نقطة الشامل (Standard 36-Point Vehicle Inspection)",
                            defaults={'labor_price': 0.00, 'estimated_hours': 0.5, 'tech_commission_percent': 0.00}
                        )

                # 📊 Seed default Chart of Accounts (shared by all industries)
                ChartOfAccount = apps.get_model('inventory', 'ChartOfAccount')
                default_accounts = [
                    ('1000', 'الأصول المتداولة', 'asset'),
                    ('1001', 'النقدية والخزائن', 'asset'),
                    ('1002', 'البنك', 'asset'),
                    ('1100', 'المدينون (ذمم العملاء)', 'asset'),
                    ('1200', 'المخزون', 'asset'),
                    ('1300', 'أصول ثابتة', 'asset'),
                    ('2000', 'الخصوم المتداولة', 'liability'),
                    ('2100', 'الدائنون (ذمم الموردين)', 'liability'),
                    ('2200', 'ضريبة القيمة المضافة المستحقة', 'liability'),
                    ('2300', 'مصروفات مستحقة', 'liability'),
                    ('3000', 'رأس المال', 'equity'),
                    ('3100', 'الأرباح المحتجزة', 'equity'),
                    ('4001', 'إيرادات المبيعات', 'revenue'),
                    ('4002', 'إيرادات الخدمات', 'revenue'),
                    ('4099', 'إيرادات أخرى', 'revenue'),
                    ('5001', 'تكلفة البضاعة المباعة', 'expense'),
                    ('5002', 'تكلفة قطع الغيار', 'expense'),
                    ('5099', 'مصروفات عمومية', 'expense'),
                    ('5100', 'رواتب وأجور', 'expense'),
                    ('5200', 'إيجار ومرافق', 'expense'),
                    ('5300', 'مصروفات شحن', 'expense'),
                ]
                for code, name, acc_type in default_accounts:
                    ChartOfAccount.objects.get_or_create(
                        code=code,
                        defaults={'name': name, 'account_type': acc_type}
                    )

        logger.info(
            f"🏢 [SCHEMA SYNC]: Provisioning complete for schema '{tenant.schema_name}' (industry={industry})"
        )
    except Exception as e:
        logger.error(
            f"🔴 [SCHEMA SYNC ERROR]: Provisioning failed for '{tenant.schema_name}' - {e}"
        )
