"""يفتح فرع (tenant) بالكامل: اشتراك دائم + كل المزايا + حدود غير محدودة.

الاستخدام:
    python manage.py grant_unlimited --schema fixit_02e0
    python manage.py grant_unlimited --domain fixit-02e0.mousstec.com

بيعمل إيه (كله idempotent — تشغيله أكتر من مرة آمن):
  • حالة الحساب = active، فعّال، مش محظور، **بدون تاريخ انتهاء** (اشتراك دائم).
  • الباقة = أعلى باقة في قطاع الشركة (empire للسيارات / print_enterprise للطباعة)
    عشان أعلى حد rate-limit.
  • حدود الفروع/المستخدمين/الخزائن = عملياً غير محدودة، وكروت الصيانة/أصناف
    المخزن = 0 (0 معناها غير محدود في الموديل).
  • كل مزايا الـ Feature catalog مفعّلة بدون أي limit (عبر
    TenantSubscription.locked_entitlements — الـ source of truth للـ
    EntitlementService).
  • وصول غرفة التشخيص (OBD) مدى الحياة + رصيد API تشخيص كبير.

ملاحظة: تصاميم AI Studio وفحوصات التشخيص الخارجية عدّادات استهلاك (بتكلّف
استدعاءات خارجية مدفوعة)، فمش بتتفتح "بلا نهاية" من هنا — بتتشحن من لوحة
السوبر أدمن (هدايا/حزم). الأمر ده بيفتح كل مزايا وحدود الـ ERP نفسه.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context

_UNLIMITED = 1_000_000  # حد عملي "غير محدود" للحقول اللي مش بتقبل 0=∞

# أعلى باقة لكل قطاع (للـ rate-limit tier + fallback لو الـ Plan catalog ناقص).
_TOP_PLAN_BY_INDUSTRY = {
    'automotive': 'empire',
    'printing': 'print_enterprise',
}


class Command(BaseCommand):
    help = "يفتح فرع (tenant) بالكامل: اشتراك دائم + كل المزايا + حدود غير محدودة."

    def add_arguments(self, parser):
        parser.add_argument('--schema', help="اسم schema الفرع (مثلاً fixit_02e0)")
        parser.add_argument('--domain', help="دومين الفرع (مثلاً fixit-02e0.mousstec.com)")

    def handle(self, *args, **opts):
        from clients.models import Client, Domain, Feature, Plan, TenantSubscription

        schema = opts.get('schema')
        domain = opts.get('domain')
        if not schema and not domain:
            raise CommandError("لازم تمرّر --schema أو --domain.")

        # كل الجداول دي في الـ public schema (SHARED_APPS).
        with schema_context('public'):
            tenant = self._find_tenant(Client, Domain, schema, domain)

            # 1) بناء dict المزايا: كل ميزة في الـ catalog مفعّلة بدون limit.
            feature_codes = list(
                Feature.objects.filter(is_active=True).values_list('code', flat=True)
            )
            entitlements = {code: {'enabled': True} for code in feature_codes}

            with transaction.atomic():
                # ⚠️ الترتيب مهم: الاشتراك الأول، وحدود الفرع في الآخر.
                # السبب: TenantSubscription.save() بيـ sync حدود الباقة (max_*)
                # على الفرع لو الـ plan اتغيّر — فلو ظبّطنا الحدود الأول، الاشتراك
                # كان هيرجّعها لقيم الباقة. بنسيب الـ plan زي ما هو (المزايا
                # بتتفتح عبر locked_entitlements، والـ rate tier عبر tenant.plan)
                # عشان ما نـ trigger الـ sync أصلاً، وبنكتب الحدود آخر حاجة.
                self._open_subscription(TenantSubscription, tenant, entitlements)
                self._open_client(tenant)

            self.stdout.write(self.style.SUCCESS(
                f"\n✅ الفرع «{tenant.name}» (schema={tenant.schema_name}) "
                f"مفتوح بالكامل:"))
            # نعيد قراءة الفرع من الداتابيز للتأكد إن الحدود اتحفظت فعلاً.
            tenant.refresh_from_db()
            self.stdout.write(
                f"   • الحالة: {tenant.status} — نهاية الاشتراك: "
                f"{tenant.subscription_end_date or 'بدون (دائم)'}")
            self.stdout.write(
                f"   • حدود (فروع/مستخدمين/خزائن): "
                f"{tenant.total_allowed_branches}/"
                f"{tenant.total_allowed_users}/"
                f"{tenant.total_allowed_treasuries}")
            self.stdout.write(
                f"   • المزايا المفعّلة: {len(entitlements)} ميزة (كل الـ catalog)")
            self.stdout.write(
                f"   • غرفة التشخيص (OBD): مدى الحياة")
            if not feature_codes:
                self.stdout.write(self.style.WARNING(
                    "   ⚠️ الـ Feature catalog فاضي — مفيش مزايا اتفعّلت. شغّل "
                    "سيد المزايا الأول لو المزايا مش ظاهرة."))

    # ─────────────────────────────────────────────────────────────────
    def _find_tenant(self, Client, Domain, schema, domain):
        if schema:
            tenant = Client.objects.filter(schema_name=schema).first()
            if not tenant:
                raise CommandError(f"مفيش فرع بالـ schema «{schema}».")
            return tenant
        dom = Domain.objects.filter(domain=domain).select_related('tenant').first()
        if not dom:
            raise CommandError(f"مفيش دومين «{domain}» مسجّل.")
        return dom.tenant

    def _open_client(self, tenant):
        tenant.status = 'active'
        tenant.is_active = True
        tenant.is_fraud_flagged = False
        tenant.subscription_end_date = None      # ⇒ is_valid_subscription دائماً True
        tenant.plan = _TOP_PLAN_BY_INDUSTRY.get(
            getattr(tenant, 'industry', 'automotive'), 'empire')
        tenant.max_branches = _UNLIMITED
        tenant.max_users = _UNLIMITED
        tenant.max_treasuries = _UNLIMITED
        tenant.max_repair_cards = 0              # 0 = غير محدود
        tenant.max_inventory_items = 0           # 0 = غير محدود
        tenant.has_obd_access = True
        tenant.obd_access_expiry = None          # مدى الحياة
        tenant.save()

    def _open_subscription(self, TenantSubscription, tenant, entitlements):
        # ⚠️ مبنغيّرش sub.plan عن قصد — تغييره بيـ trigger
        # sync_limits_to_tenant() اللي بيرجّع حدود الفرع لقيم الباقة (اللي
        # ممكن تكون صغيرة). المزايا بتتفتح عبر locked_entitlements تحت،
        # والـ rate tier بيتحدد من tenant.plan (CharField) في _open_client.
        sub, _ = TenantSubscription.objects.get_or_create(tenant=tenant)
        sub.is_active = True
        sub.current_period_end = None            # مفيش نهاية دورة
        # نثبّت المزايا كلها كـ snapshot — ده بياخد أولوية على plan.entitlements
        # في effective_entitlements، فبيضمن إن كل ميزة مفتوحة مهما كانت الباقة.
        sub.locked_entitlements = entitlements
        sub.locked_at = timezone.now()
        # رصيد API التشخيص الخارجي — نخليه كبير عشان الفحوصات ماتقفش.
        if hasattr(sub, 'diag_api_quota_remaining'):
            sub.diag_api_quota_remaining = _UNLIMITED
        sub.save()
        return sub
