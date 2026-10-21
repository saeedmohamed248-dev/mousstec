"""
🏢 إعدادات مساحة العمل اللي بيعملها المالك بنفسه (صفحة /account/workspace/):

1. تغيير المجال (سيارات ↔ طباعة) أو نوع النشاط — في الفترة التجريبية بس، وبحد أقصى
   MAX_INDUSTRY_CHANGES مرة. اللي سجّل مطبعة وطلعله حساب قطع غيار يصلّحها من غير الدعم.
   الباقة التجريبية بتتبدّل لأعلى باقة في المجال الجديد، وبيانات المجال الجديد الافتراضية
   (فرع/خزنة/خامات) بتتعمل. بيانات المجال القديم بتفضل محفوظة لو رجع له.

2. الحذف النهائي للحساب — الـ schema كلها بتتمسح (كل البيانات) ومعاها الرابط والاشتراك.
   ممنوع لو فيه فلوس في المحفظة/الضمان أو سجلات مالية محمية (فواتير اشتراك، حركات محفظة،
   معاملات سوق، نزاعات) — دي لازم تفضل، فالعميل يكلّم الدعم.
   🛡️ صف الشركة والـ schema بيتمسحوا مع بعض في transaction واحدة: صف من غير schema
   بيوقّع `migrate_schemas --tenant` في النشر.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import router, transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.utils import timezone
from django.utils.translation import gettext as _
from django_tenants.utils import get_public_schema_name, schema_context

logger = logging.getLogger('mouss_tec_core')

MAX_INDUSTRY_CHANGES = 2
TRIAL_PLAN_BY_INDUSTRY = {'automotive': 'empire', 'printing': 'print_enterprise'}


class WorkspaceError(Exception):
    """رسالة للمالك — بتتعرض زي ما هي."""


def _business_types():
    from clients.forms import TenantSignupForm
    return TenantSignupForm.INDUSTRY_BUSINESS_TYPES


# ─────────────────────────────── تغيير المجال ───────────────────────────────

def industry_change_status(tenant) -> dict:
    """{'allowed', 'remaining', 'reason'} — reason فاضية لو مسموح."""
    remaining = tenant.industry_changes_left
    reason = ''
    if tenant.status != 'trial':
        reason = _("تغيير المجال متاح في الفترة التجريبية بس — كلّم الدعم.")
    elif tenant.trial_ends_at and timezone.now().date() > tenant.trial_ends_at:
        reason = _("الفترة التجريبية خلصت — تغيير المجال بقى من خلال الدعم.")
    elif not remaining:
        reason = _("استخدمت مرات تغيير المجال المسموحة — كلّم الدعم لو محتاج تغيّره تاني.")
    return {'allowed': not reason, 'remaining': remaining, 'reason': reason}


def change_industry(tenant, industry, business_type, *, user=None):
    """يغيّر مجال الشركة ونوع نشاطها (مرة من المرات المسموحة) ويرجّع الشركة بعد التعديل."""
    from clients.models import Client, Plan, TenantSubscription
    from clients.services.plan_mapping import resolve_plan_slug
    from clients.signals import seed_industry_defaults

    types = _business_types()
    if industry not in types:
        raise WorkspaceError(_("اختار مجال صحيح."))
    if business_type not in types[industry]:
        raise WorkspaceError(_("نوع النشاط مش من المجال اللي اخترته."))

    with schema_context(get_public_schema_name()), transaction.atomic():
        t = Client.objects.select_for_update().get(pk=tenant.pk)
        status = industry_change_status(t)
        if not status['allowed']:
            raise WorkspaceError(status['reason'])
        if (t.industry, t.business_type) == (industry, business_type):
            raise WorkspaceError(_("ده مجالك ونوع نشاطك الحاليين فعلاً."))

        old = (t.industry, t.business_type)
        t.industry, t.business_type = industry, business_type
        t.plan = TRIAL_PLAN_BY_INDUSTRY[industry]
        t.industry_changes = (t.industry_changes or 0) + 1
        t.save(update_fields=['industry', 'business_type', 'plan', 'industry_changes'])

        # 💳 الباقة التجريبية (والصلاحيات المثبتة منها) لازم تبقى من المجال الجديد —
        #    وإلا المطبعة تفضل شايفة صلاحيات باقة Empire بتاعة السيارات.
        slug = resolve_plan_slug(t.plan)
        plan = Plan.objects.filter(slug=slug, is_active=True).first() if slug else None
        sub = TenantSubscription.objects.filter(tenant=t).first()
        if sub is None:
            if plan:
                TenantSubscription.objects.create(tenant=t, plan=plan, is_active=False)
        elif sub.plan_id != getattr(plan, 'pk', None):
            sub.plan = plan
            sub.save()
            if plan:
                sub.snapshot_from_plan(save=True)
            else:
                sub.locked_at, sub.locked_entitlements, sub.locked_monthly_price = None, {}, None
                sub.save(update_fields=['locked_at', 'locked_entitlements', 'locked_monthly_price'])

        try:
            seed_industry_defaults(t)
        except Exception:
            # الـ transaction بترجع كل حاجة — مفيش نص تغيير
            logger.exception("🔴 [WORKSPACE] seeding %s for %s failed", industry, t.schema_name)
            raise WorkspaceError(_("حصلت مشكلة وإحنا بنجهّز المجال الجديد — جرّب تاني أو كلّم الدعم."))

    cache.delete(f"tenant_guard_status_{t.schema_name}")
    logger.warning("🔄 [WORKSPACE] %s changed industry %s → %s by %s (%s/%s)",
                   t.schema_name, old, (industry, business_type),
                   getattr(user, 'email', '') or getattr(user, 'pk', ''),
                   t.industry_changes, MAX_INDUSTRY_CHANGES)
    return t


# ─────────────────────────────── الحذف النهائي ───────────────────────────────

def workspace_slug(tenant) -> str:
    """اللي المالك بيكتبه عشان يأكد الحذف: أول جزء من رابط الشركة (mmm في mmm.mousstec.com)."""
    from clients.models import Domain
    with schema_context(get_public_schema_name()):
        domain = (Domain.objects.filter(tenant_id=tenant.pk).order_by('-is_primary', 'pk')
                  .values_list('domain', flat=True).first())
    return (domain.split('.', 1)[0] if domain else tenant.schema_name.replace('_', '-')).lower()


def _protected_records(tenant) -> list[str]:
    """أسماء السجلات المحمية اللي بتمنع مسح صف الشركة (PROTECT/RESTRICT)."""
    from clients.models import Client
    with schema_context(get_public_schema_name()):
        try:
            Collector(using=router.db_for_write(Client)).collect([tenant])
        except ProtectedError as exc:
            objs = exc.protected_objects
        except RestrictedError as exc:
            objs = exc.restricted_objects
        else:
            return []
    return sorted({str(o._meta.verbose_name_plural) for o in objs})


def deletion_blockers(tenant) -> list[str]:
    from erp_core.localization import currency_symbol
    symbol = currency_symbol(tenant.effective_currency)
    blockers = []
    if tenant.wallet_balance and tenant.wallet_balance > 0:
        blockers.append(_("في محفظتك %(amount)s %(cur)s — استخدمها أو اطلب سحبها من الدعم قبل الحذف.")
                        % {'amount': tenant.wallet_balance, 'cur': symbol})
    if tenant.escrow_held and tenant.escrow_held > 0:
        blockers.append(_("في %(amount)s %(cur)s محجوزين في الضمان لصفقات لسه ماخلصتش.")
                        % {'amount': tenant.escrow_held, 'cur': symbol})
    protected = _protected_records(tenant)
    if protected:
        blockers.append(_("حسابك عليه سجلات مالية لازم نحتفظ بيها (%(what)s) — كلّم الدعم يقفل الحساب.")
                        % {'what': '، '.join(protected)})
    return blockers


def delete_workspace(tenant, *, user, password, confirm):
    """يمسح الشركة نهائياً: الـ schema بكل بياناتها + الرابط + الاشتراك. مفيش رجوع."""
    from django.contrib.auth import get_user_model
    from clients.models import Client

    if not (user and user.is_authenticated and user.is_superuser):
        raise WorkspaceError(_("حذف الحساب لمالك الشركة بس."))
    if not user.check_password(password or ''):
        raise WorkspaceError(_("كلمة السر مش صحيحة."))
    if (confirm or '').strip().lower() != workspace_slug(tenant):
        raise WorkspaceError(_("اكتب اسم الرابط زي ما هو عشان تأكد الحذف."))

    with schema_context(get_public_schema_name()), transaction.atomic():
        t = Client.objects.select_for_update().filter(pk=tenant.pk).first()
        if t is None:       # طلب تاني (دبل كليك) سبقه ومسحها
            return
        blockers = deletion_blockers(t)
        if blockers:
            raise WorkspaceError(blockers[0])
        with schema_context(t.schema_name):
            emails = {e.lower() for e in get_user_model().objects.exclude(email='')
                      .values_list('email', flat=True)}
        info = (t.pk, t.schema_name, t.name, t.email, t.status)
        # DROP SCHEMA جوه نفس الـ transaction — لو مسح الصف وقع، الـ schema بترجع.
        t.delete(force_drop=True)

    cache.delete(f"tenant_guard_status_{info[1]}")
    for email in emails | {(info[3] or '').lower()}:
        cache.delete(f"login_lookup:{email}")
    logger.warning("🗑️ [WORKSPACE] tenant #%s %s (%s, %s, status=%s) deleted permanently by its owner %s",
                   *info, getattr(user, 'email', '') or user.pk)
