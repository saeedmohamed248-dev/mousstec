"""
📲 التواصل مع الشركات المسجّلة على واتساب (/superadmin/whatsapp/)
==================================================================
طريقتين للإرسال:

1. **من موبايل المالك** — زرار لكل شركة بيفتح wa.me والرسالة جاهزة باسم الشركة والمالك.
   مجاني وشغال من غير أي إعداد. «ابعت للكل من موبايلي» بيفتح الشركات واحدة ورا التانية
   (واتساب نفسه مابيسمحش لموقع يبعت من موبايلك لوحده).
2. **أوتوماتيك من رقم المنصة** — WhatsApp Cloud API (نفس مفاتيح الـ OTP). بيتبعت في الخلفية
   (Celery). ⚠️ Meta مابتوصّلش رسالة لحد ماكلّمش الرقم آخر 24 ساعة إلا بـ «قالب» متوافَق
   عليه — `WHATSAPP_OUTREACH_TEMPLATE` (MARKETING، {{1}} المالك، {{2}} الشركة).

كل إرسال بيتسجّل في `TenantOutreach`، والمالك بيسجّل النتيجة (ردّ/مهتم/اشترك…) والميعاد الجاي
— فالصفحة بترتّب الشركات: اللي ميعاد متابعتها جه الأول، وبعدين اللي عمرنا ماكلّمناها.
"""
from __future__ import annotations

import logging
import uuid
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from clients.services.whatsapp_notify import normalize_phone, send_platform_whatsapp

logger = logging.getLogger('mouss_tec_core')

# رسايل جاهزة — {owner} اسم المالك، {company} اسم الشركة، {days_left} الأيام الفاضلة
PRESETS = {
    'checkin': (
        'متابعة',
        'أهلاً أستاذ {owner} 👋\n'
        'معاك سعيد من Mouss Tec — حبيت أطمّن على «{company}»: السيستم ماشي معاك تمام؟\n'
        'لو محتاج أي مساعدة في البداية أو نظبطلك حاجة، أنا موجود.',
    ),
    'trial_ending': (
        'التجربة بتخلص',
        'أهلاً أستاذ {owner} 👋\n'
        'التجربة المجانية لـ «{company}» فاضلها {days_left} يوم.\n'
        'لو حابب تكمّل من غير ما بياناتك تقف، ابعتلي وأفعّللك الاشتراك بعرض خاص.',
    ),
    'expired': (
        'التجربة خلصت',
        'أهلاً أستاذ {owner} 👋\n'
        'التجربة المجانية لـ «{company}» خلصت، بس بياناتك كلها محفوظة.\n'
        'تحب أفعّللك الاشتراك وترجع تكمّل من مكان ما وقفت؟',
    ),
}
DEFAULT_PRESET = 'checkin'


def tenant_state(tenant, today=None):
    """(مفتاح، الأيام الفاضلة أو None) — trial / trial_over / active / expired / suspended."""
    today = today or timezone.localdate()
    status = getattr(tenant, 'status', '')
    if status == 'trial':
        ends = getattr(tenant, 'trial_ends_at', None)
        days = (ends - today).days if ends else None
        return ('trial_over' if days is not None and days < 0 else 'trial'), days
    if status == 'active':
        end = getattr(tenant, 'subscription_end_date', None)
        days = (end - today).days if end else None
        return ('expired' if days is not None and days < 0 else 'active'), days
    return (status or 'suspended'), None


def render_message(text: str, tenant, today=None) -> str:
    """يملا {owner} و{company} و{days_left} — من غير str.format عشان أي { تانية في النص ماتوقّعش."""
    _, days = tenant_state(tenant, today)
    values = {
        '{owner}': (getattr(tenant, 'owner_name', '') or '').strip() or (getattr(tenant, 'name', '') or ''),
        '{company}': getattr(tenant, 'name', '') or '',
        '{days_left}': str(max(days, 0)) if days is not None else '',
    }
    for key, val in values.items():
        text = text.replace(key, val)
    return text.strip()


def wa_phone(tenant) -> str | None:
    return normalize_phone(getattr(tenant, 'phone', ''), getattr(tenant, 'country', '') or 'EG')


def wa_url(tenant, text: str = '') -> str | None:
    """رابط wa.me بيفتح محادثة الشركة على موبايل/واتساب ويب المالك والرسالة مكتوبة."""
    phone = wa_phone(tenant)
    if not phone:
        return None
    return f'https://wa.me/{phone}' + (f'?text={quote(text)}' if text else '')


def _last_by_tenant(tenant_ids):
    from clients.models import TenantOutreach
    last, counts = {}, {}
    for o in TenantOutreach.objects.filter(tenant_id__in=tenant_ids).order_by('tenant_id', '-created_at', '-pk'):
        counts[o.tenant_id] = counts.get(o.tenant_id, 0) + 1
        last.setdefault(o.tenant_id, o)
    return last, counts


def outreach_rows(tenants, *, due_only=False, today=None):
    """صف لكل شركة: حالتها، رقمها، آخر تواصل ونتيجته، والمتابعة الجاية — مترتبين بالأولوية."""
    today = today or timezone.localdate()
    tenants = list(tenants)
    last, counts = _last_by_tenant([t.pk for t in tenants])
    rows = []
    for t in tenants:
        state, days = tenant_state(t, today)
        o = last.get(t.pk)
        nxt = o.next_follow_up if o else None
        due = bool(nxt and nxt <= today)
        if due_only and not due:
            continue
        rows.append({
            'tenant': t, 'state': state, 'days_left': days, 'wa_phone': wa_phone(t),
            'last': o, 'contacts': counts.get(t.pk, 0), 'next_follow_up': nxt, 'due': due,
        })
    # المتابعة اللي ميعادها جه ⇒ اللي عمرنا ماكلّمناهم ⇒ الأقدم في التواصل
    rows.sort(key=lambda r: (
        not r['due'],
        r['last'] is not None,
        r['last'].created_at if r['last'] else timezone.now(),
    ))
    return rows


def log_phone_open(tenant, text: str, user=None, batch: str = ''):
    """الزرار اتداس: نسجّل التواصل ونرجّع رابط wa.me (أو None لو الرقم مش صالح)."""
    from clients.models import TenantOutreach
    url = wa_url(tenant, text)
    if not url:
        return None
    TenantOutreach.objects.create(
        tenant=tenant, channel='phone', phone=wa_phone(tenant) or '', message=text,
        status='opened', batch=batch, created_by=user if getattr(user, 'pk', None) else None,
    )
    return url


def record_outcome(tenant, *, status: str, notes: str = '', next_follow_up=None, user=None):
    """نتيجة المتابعة بتتسجّل على آخر تواصل مع الشركة — ولو مفيش (كلّمه تليفون مثلاً) بيتعمل صف جديد."""
    from clients.models import TenantOutreach
    if status not in TenantOutreach.OUTCOME_STATUSES:
        raise ValueError(f'unknown outcome {status!r}')
    o = TenantOutreach.objects.filter(tenant=tenant).order_by('-created_at', '-pk').first()
    if o is None:
        o = TenantOutreach(tenant=tenant, channel='phone', phone=wa_phone(tenant) or '',
                           created_by=user if getattr(user, 'pk', None) else None)
    o.status = status
    o.notes = (notes or '').strip()[:2000]
    o.next_follow_up = next_follow_up
    o.save()
    return o


def queue_api_batch(tenants, text: str, user=None) -> tuple[str, int, int]:
    """يحط رسالة لكل شركة في الطابور (status=queued) — والمهمة تبعتهم. (batch, في الطابور، رقم غلط)."""
    from clients.models import TenantOutreach
    batch = uuid.uuid4().hex[:12]
    queued = bad = 0
    rows = []
    by = user if getattr(user, 'pk', None) else None
    for t in tenants:
        phone = wa_phone(t)
        msg = render_message(text, t)
        if phone:
            queued += 1
            rows.append(TenantOutreach(tenant=t, channel='api', phone=phone, message=msg,
                                       status='queued', batch=batch, created_by=by))
        else:
            bad += 1
            rows.append(TenantOutreach(tenant=t, channel='api', phone=(t.phone or '')[:20], message=msg,
                                       status='failed', batch=batch, error='رقم الموبايل مش صالح',
                                       created_by=by))
    TenantOutreach.objects.bulk_create(rows)
    return batch, queued, bad


def send_api_batch(batch: str) -> dict:
    """بيبعت الرسايل اللي في الطابور من رقم المنصة. القالب لو متظبط، وإلا نص (نافذة 24 ساعة بس)."""
    from clients.models import TenantOutreach
    template = getattr(settings, 'WHATSAPP_OUTREACH_TEMPLATE', '') or ''
    lang = getattr(settings, 'WHATSAPP_OUTREACH_TEMPLATE_LANG', 'ar') or 'ar'
    sent = failed = 0
    for o in TenantOutreach.objects.filter(batch=batch, status='queued').select_related('tenant'):
        t = o.tenant
        country = getattr(t, 'country', '') or 'EG'
        try:
            if template:
                ok = send_platform_whatsapp(o.phone, country=country, template=template, lang=lang,
                                            params=(render_message('{owner}', t), t.name))
            else:
                ok = send_platform_whatsapp(o.phone, country=country, text=o.message)
        except Exception as exc:  # noqa: BLE001 — شركة واحدة ماتوقّفش الباقي
            logger.exception('whatsapp outreach %s', t.schema_name)
            ok, o.error = False, str(exc)[:300]
        if ok:
            o.status, o.error = 'sent', ''
            sent += 1
        else:
            o.status = 'failed'
            o.error = o.error or 'Meta رفضت الرسالة أو مفاتيح الواتساب مش متظبطة'
            failed += 1
        o.save(update_fields=['status', 'error', 'updated_at'])
    logger.info('whatsapp outreach batch %s: sent=%s failed=%s', batch, sent, failed)
    return {'sent': sent, 'failed': failed}


def api_ready() -> bool:
    return bool(getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') and getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', ''))
