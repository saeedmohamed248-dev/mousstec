"""
📢 Broadcast Service
====================
Helpers for the super-admin email broadcast feature.

  resolve_audience(audience, plan='', extra={})  → QuerySet[Client]
  send_campaign(campaign)                         → updates campaign in place

The send loop iterates tenants, skips those without an email, calls
django.core.mail.send_mail with fail_silently=False, and records
per-tenant outcomes onto the BroadcastCampaign row.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


def resolve_audience(audience: str, *, plan: str = '', extra: dict | None = None):
    """Return a Client queryset matching the audience selector."""
    from clients.models import Client
    today = timezone.localdate()
    qs = Client.objects.exclude(schema_name='public').filter(is_deleted=False)

    if audience == 'active':
        qs = qs.filter(status='active')
    elif audience == 'trial':
        qs = qs.filter(status='trial')
    elif audience == 'expiring':
        qs = qs.filter(
            status='active',
            subscription_end_date__isnull=False,
            subscription_end_date__lte=today + timedelta(days=14),
            subscription_end_date__gte=today,
        )
    elif audience == 'at_risk':
        # شركات إما suspended أو trial انتهت أو subscription منتهي
        from django.db.models import Q
        qs = qs.filter(
            Q(status='suspended')
            | Q(status='trial', trial_ends_at__lt=today)
            | Q(status='active', subscription_end_date__lt=today)
            | Q(is_fraud_flagged=True),
        )
    elif audience == 'plan' and plan:
        qs = qs.filter(plan=plan)
    # 'all' / 'custom' / unknown → بدون فلتر إضافي هنا

    if extra:
        # فلتر إضافي JSON آمن: industry / business_type / plan
        for field in ('industry', 'business_type', 'plan', 'status'):
            v = extra.get(field)
            if v:
                qs = qs.filter(**{field: v})
    return qs


def send_campaign(campaign):
    """
    Send a draft campaign. Updates the campaign row with progress.
    Returns the same campaign instance with status='sent' or 'failed'.
    """
    from clients.models import BroadcastCampaign

    if campaign.status not in ('draft', 'failed'):
        raise ValueError(f"Campaign #{campaign.id} not sendable (status={campaign.status})")

    audience_qs = resolve_audience(
        campaign.audience,
        plan=campaign.audience_plan,
        extra=campaign.audience_filter or {},
    )
    tenants = list(audience_qs.only('id', 'name', 'email', 'schema_name'))
    campaign.audience_size = len(tenants)
    campaign.status = 'sending'
    campaign.save(update_fields=['audience_size', 'status'])

    sent = failed = skipped = 0
    errors = []

    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@mousstec.com')

    # 🔁 connection واحد لكل الـ batch — أسرع 5-10× من فتح SMTP لكل إيميل
    connection = get_connection()
    try:
        connection.open()
        for t in tenants:
            recipient = (t.email or '').strip()
            if not recipient:
                skipped += 1
                continue
            personalised = (
                campaign.body
                .replace('{tenant_name}', t.name or '')
                .replace('{schema}', t.schema_name or '')
            )
            try:
                EmailMessage(
                    subject=campaign.subject,
                    body=personalised,
                    from_email=from_email,
                    to=[recipient],
                    connection=connection,
                ).send(fail_silently=False)
                sent += 1
            except Exception as e:
                failed += 1
                errors.append(f"{t.schema_name} <{recipient}>: {e}")
                logger.warning("broadcast #%s → %s failed: %s", campaign.id, recipient, e)
    finally:
        try:
            connection.close()
        except Exception:
            pass

    campaign.sent_count = sent
    campaign.failed_count = failed
    campaign.skipped_count = skipped
    campaign.error_log = '\n'.join(errors[:50])
    campaign.status = 'sent' if failed == 0 else ('sent' if sent > 0 else 'failed')
    campaign.sent_at = timezone.now()
    campaign.save(update_fields=[
        'sent_count', 'failed_count', 'skipped_count',
        'error_log', 'status', 'sent_at',
    ])
    logger.info(
        "broadcast #%s done: %s sent / %s failed / %s skipped (audience=%s)",
        campaign.id, sent, failed, skipped, campaign.audience_size,
    )
    return campaign
