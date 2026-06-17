"""
📨 Support Ticket views.
- submit_help_form: عام (لأي عميل/زائر)، يحفظ تذكرة + يبعت إيميل للمالك المخفي.
- support_inbox: للسوبر أدمن — قائمة + تفاصيل + تحديث الحالة.
"""
import logging

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.db import connection
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from django_tenants.utils import schema_context

from clients.models import Client, SupportTicket
from clients.permissions import widget_required
from clients.views.saas_admin_views import saas_admin_required

logger = logging.getLogger('mouss_tec_core')


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


@csrf_protect
@require_POST
def submit_help_form(request):
    name = (request.POST.get('name') or '').strip()[:120]
    email = (request.POST.get('email') or '').strip()[:200]
    subject = (request.POST.get('subject') or '').strip()[:200] or '(بدون موضوع)'
    message = (request.POST.get('message') or '').strip()
    phone = (request.POST.get('phone') or '').strip()[:30]
    source = request.POST.get('source', 'form')
    if source not in dict(SupportTicket.SOURCE_CHOICES):
        source = 'form'

    if not (name and email and message):
        return JsonResponse({'ok': False, 'error': 'الاسم والبريد والرسالة مطلوبة'}, status=400)
    if len(message) > 5000:
        return JsonResponse({'ok': False, 'error': 'الرسالة طويلة جداً'}, status=400)

    tenant = getattr(request, 'tenant', None)
    tenant_obj = tenant if isinstance(tenant, Client) else None

    # نكتب التذكرة دائماً في public schema (السوبر أدمن مكان واحد)
    with schema_context('public'):
        ticket = SupportTicket.objects.create(
            tenant=tenant_obj,
            name=name, email=email, phone=phone,
            subject=subject, message=message,
            source=source,
            ip_address=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
        )

    # 📧 الإيميل المخفي — مش معروض في الـ frontend مطلقاً، جاي من .env
    inbox = getattr(settings, 'SUPPORT_INBOX_EMAIL', '') or settings.DEFAULT_FROM_EMAIL
    try:
        # 🐛 UTF-8 encoding عشان الـ Arabic subject/body يوصل صح عبر SMTP
        from django.core.mail import EmailMessage
        msg = EmailMessage(
            subject=f"[Mousstec Ticket #{ticket.id}] {subject}",
            body=(
                f"From: {name} <{email}>\n"
                f"Phone: {phone or '—'}\n"
                f"Tenant: {tenant_obj.name if tenant_obj else '—'}\n"
                f"Source: {ticket.get_source_display()}\n"
                f"IP: {ticket.ip_address or '—'}\n\n"
                f"{message}\n\n"
                f"— Reply to {email}"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[inbox],
            reply_to=[email] if email else None,
        )
        msg.encoding = 'utf-8'
        msg.send(fail_silently=False)
        with schema_context('public'):
            SupportTicket.objects.filter(pk=ticket.pk).update(email_delivered=True)
    except Exception as e:
        logger.exception("Help-form email failed for ticket #%s", ticket.id)
        with schema_context('public'):
            SupportTicket.objects.filter(pk=ticket.pk).update(email_error=str(e)[:255])

    return JsonResponse({
        'ok': True,
        'ticket_id': ticket.id,
        'message': 'وصلتنا رسالتك. هنرد عليك على بريدك في أقرب وقت 💚',
    })


# ─────────────────────────────────────────────────────────────────────
# 📋 Support Inbox — للسوبر أدمن
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tickets')
def support_inbox(request):
    qs = SupportTicket.objects.filter(is_deleted=False)
    show = request.GET.get('show', 'open')
    if show == 'open':
        qs = qs.exclude(status='closed')
    elif show == 'closed':
        qs = qs.filter(status='closed')

    return render(request, 'clients/saas_admin/support_inbox.html', {
        'tickets': qs.order_by('-created_at')[:200],
        'show': show,
        'count_open': SupportTicket.objects.filter(is_deleted=False).exclude(status='closed').count(),
        'count_urgent': SupportTicket.objects.filter(
            is_deleted=False, priority__in=['high', 'urgent']
        ).exclude(status='closed').count(),
    })


@saas_admin_required
@widget_required('tickets')
def support_ticket_detail(request, ticket_id):
    ticket = get_object_or_404(SupportTicket, pk=ticket_id, is_deleted=False)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_status':
            new_status = request.POST.get('status')
            if new_status in dict(SupportTicket.STATUS_CHOICES):
                ticket.status = new_status
                if new_status == 'closed' and not ticket.closed_at:
                    ticket.closed_at = timezone.now()
                if not ticket.assigned_to_id:
                    ticket.assigned_to = request.user
                ticket.save(update_fields=['status', 'closed_at', 'assigned_to', 'updated_at'])
                messages.success(request, 'تم تحديث حالة التذكرة.')
        elif action == 'add_note':
            note = request.POST.get('note', '').strip()
            if note:
                ts = timezone.now().strftime('%Y-%m-%d %H:%M')
                ticket.admin_notes = (ticket.admin_notes + f'\n\n[{ts} — {request.user.username}]\n{note}').strip()
                ticket.save(update_fields=['admin_notes', 'updated_at'])
                messages.success(request, 'تم إضافة الملاحظة.')
        elif action == 'set_priority':
            p = request.POST.get('priority')
            if p in dict(SupportTicket.PRIORITY_CHOICES):
                ticket.priority = p
                ticket.save(update_fields=['priority', 'updated_at'])
                messages.success(request, 'تم تحديث الأولوية.')
        return redirect('saas_support_ticket_detail', ticket_id=ticket.id)

    return render(request, 'clients/saas_admin/support_ticket_detail.html', {'ticket': ticket})
