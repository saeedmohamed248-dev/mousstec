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
   بعد الحذف: حدث في «سجل التدقيق» عند مالك المنصة، إيميل للعميل، والملفات المرفوعة
   (لوجو، ملفات الطلبات، الإيصالات…) بتتمسح من التخزين في الخلفية.

3. نسخة من البيانات (export_workspace) — ZIP فيه CSV لكل جدول عنده بيانات، قبل الحذف أو في أي وقت.
"""
from __future__ import annotations

from erp_core.csv_safe import safe_cell
import csv
import io
import json
import logging
import re
import tempfile
import threading
import zipfile

from django.apps import apps
from django.conf import settings
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


def _audit(event_type, tenant, user, description, **metadata):
    """حدث في «سجل التدقيق» بتاع مالك المنصة (PlatformEvent) — من غير ما يوقّع العملية."""
    try:
        from clients.models import PlatformEvent
        with schema_context(get_public_schema_name()):
            PlatformEvent.objects.create(
                event_type=event_type, tenant_schema=tenant.schema_name, tenant_name=tenant.name[:150],
                user_name=(getattr(user, 'email', '') or getattr(user, 'username', '') or '')[:150],
                description=description[:1000], metadata=metadata)
    except Exception:
        logger.exception("[WORKSPACE] audit event %s failed", event_type)


def _tenant_app_labels():
    """التطبيقات اللي جداولها جوه schema الشركة بس (مش المشتركة)."""
    shared = set(settings.SHARED_APPS)
    return {name.rsplit('.', 1)[-1] for name in settings.TENANT_APPS if name not in shared}


def _tenant_models():
    labels = _tenant_app_labels()
    for model in apps.get_models():
        meta = model._meta
        if meta.app_label in labels and not meta.proxy and meta.managed \
                and not model.__name__.startswith('Historical'):     # تاريخ simple_history
            yield model


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
    _audit('industry_change', t, user,
           f"{old[0]}/{old[1]} → {industry}/{business_type} ({t.industry_changes}/{MAX_INDUSTRY_CHANGES})",
           old_industry=old[0], old_business_type=old[1], industry=industry, business_type=business_type,
           changes=t.industry_changes)
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
        files = tenant_file_paths(t)
        info = (t.pk, t.schema_name, t.name, t.email, t.status)
        record = {'email': t.email or '', 'phone': t.phone or '', 'owner_name': t.owner_name or '',
                  'industry': t.industry, 'business_type': t.business_type, 'status': t.status,
                  'plan': t.plan, 'created_on': t.created_on.isoformat() if t.created_on else '',
                  'files': len(files)}
        # DROP SCHEMA جوه نفس الـ transaction — لو مسح الصف وقع، الـ schema بترجع.
        t.delete(force_drop=True)
        # الملفات بتتمسح بعد ما الحذف يتأكد بس (لو الـ transaction رجعت، تفضل زي ما هي)
        transaction.on_commit(lambda: _in_background(purge_files, files))

    cache.delete(f"tenant_guard_status_{info[1]}")
    for email in emails | {(info[3] or '').lower()}:
        cache.delete(f"login_lookup:{email}")
    _audit('tenant_deleted', t, user, f"{info[2]} ({info[1]}) — {record['email']}", **record)
    _send_goodbye(info[3], info[2])
    logger.warning("🗑️ [WORKSPACE] tenant #%s %s (%s, %s, status=%s) deleted permanently by its owner %s",
                   *info, getattr(user, 'email', '') or user.pk)


def _send_goodbye(email, company):
    if not email:
        return
    from django.core.mail import EmailMessage
    msg = EmailMessage(
        subject=_("تم حذف حساب %(company)s | Mouss Tec") % {'company': company},
        body=_("أهلاً،\n\nحساب «%(company)s» على Mouss Tec اتمسح نهائياً بكل بياناته بناءً على طلبك.\n"
               "لو ماطلبتش ده، كلّمنا فوراً بالرد على الإيميل ده.\n\n"
               "لو حبيت ترجع، تقدر تسجّل من جديد في أي وقت.\n\nMouss Tec") % {'company': company},
        to=[email])
    msg.encoding = 'utf-8'
    try:
        msg.send(fail_silently=True)
    except Exception:
        logger.warning("[WORKSPACE] goodbye email to %s failed", email, exc_info=True)


# ─────────────────────────────── الملفات المرفوعة ───────────────────────────────

def tenant_file_paths(tenant) -> list[str]:
    """كل الملفات المرفوعة بتاعة الشركة (من جداولها + اللوجو) — من غير القيم الافتراضية
    المشتركة (صورة افتراضية مثلاً) عشان مانمسحش حاجة شركات تانية بتستخدمها."""
    from django.db.models import FileField
    paths = set()
    if getattr(tenant, 'logo', None):
        paths.add(tenant.logo.name)
    with schema_context(tenant.schema_name):
        for model in _tenant_models():
            for field in model._meta.concrete_fields:
                if not isinstance(field, FileField):
                    continue
                default = field.default if isinstance(field.default, str) else ''
                names = (model._base_manager.exclude(**{f'{field.attname}__in': ['', default]})
                         .exclude(**{f'{field.attname}__isnull': True})
                         .values_list(field.attname, flat=True).distinct())
                paths.update(n for n in names if n)
    return sorted(paths)


def purge_files(paths):
    from django.core.files.storage import default_storage
    removed = 0
    for name in paths:
        try:
            if default_storage.exists(name):
                default_storage.delete(name)
                removed += 1
        except Exception:
            logger.warning("[WORKSPACE] could not delete file %s", name, exc_info=True)
    logger.info("[WORKSPACE] purged %s/%s files of a deleted tenant", removed, len(paths))
    return removed


def _in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ─────────────────────────────── نسخة من البيانات ───────────────────────────────

_SECRET_FIELD = re.compile(r'password|token|secret|api_key|otp|pin_code|mfa', re.I)


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return safe_cell(json.dumps(value, ensure_ascii=False))
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if isinstance(value, str):
        return safe_cell(value)   # اسم/ملاحظة بتبدأ بـ «=» ماتتنفّذش معادلة في Excel
    return str(value)


def export_workspace(tenant):
    """ZIP فيه CSV لكل جدول عنده بيانات (يفتح في Excel بالعربي) + مستخدمين الشركة من غير
    كلمات السر. بيرجّع (اسم الملف، ملف مفتوح على أوله)."""
    from django.contrib.auth import get_user_model
    out = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
    User = get_user_model()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf, schema_context(tenant.schema_name):
        for model in [User, *_tenant_models()]:
            fields = [f for f in model._meta.concrete_fields if not _SECRET_FIELD.search(f.name)]
            qs = model._base_manager.order_by('pk').values_list(*[f.attname for f in fields])
            if not qs.exists():
                continue
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([f"{f.verbose_name} ({f.attname})" for f in fields])
            for row in qs.iterator(chunk_size=2000):
                writer.writerow([_cell(v) for v in row])
            meta = model._meta
            zf.writestr(f"{meta.app_label}/{meta.model_name}.csv", '\ufeff' + buf.getvalue())
    out.seek(0)
    filename = f"{workspace_slug(tenant)}-mousstec-{timezone.localdate():%Y-%m-%d}.zip"
    return filename, out
