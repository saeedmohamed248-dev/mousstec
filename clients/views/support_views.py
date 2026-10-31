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
from django.db.models import Count
from django.http import FileResponse, Http404, JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_tenants.utils import schema_context

from clients.models import Client, SupportAttachment, SupportTicket
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
# 🆘 زرار «تواصل معانا» جوه السيستم — مشكلة + صور/فيديو/تسجيل صوتي
# ─────────────────────────────────────────────────────────────────────
_MAX_FILES = 6
_MAX_BYTES = {'image': 10 * 1024 * 1024, 'video': 80 * 1024 * 1024, 'audio': 20 * 1024 * 1024}
_KIND_LABEL = {'image': 'صورة', 'video': 'فيديو', 'audio': 'تسجيل صوتي'}


def _attachment_kind(upload):
    ctype = (getattr(upload, 'content_type', '') or '').split(';')[0].strip().lower()
    for kind in ('image', 'video', 'audio'):
        if ctype.startswith(kind + '/'):
            return kind, ctype
    return None, ctype


def _checked_uploads(request):
    """المرفقات (files + voice) بعد فحص النوع والحجم والعدد ⇒ ([(upload, kind, ctype)], رسالة_خطأ)."""
    uploads = request.FILES.getlist('files') + request.FILES.getlist('voice')
    if len(uploads) > _MAX_FILES:
        return [], f'أقصى عدد {_MAX_FILES} ملفات في الرسالة.'
    checked = []
    for up in uploads:
        kind, ctype = _attachment_kind(up)
        if kind is None:
            return [], f'نوع الملف «{up.name}» مش مدعوم — صور أو فيديو أو صوت بس.'
        if up.size > _MAX_BYTES[kind]:
            return [], f'الـ{_KIND_LABEL[kind]} «{up.name}» أكبر من {_MAX_BYTES[kind] // (1024 * 1024)} ميجا.'
        checked.append((up, kind, ctype))
    return checked, None


def _save_uploads(ticket, checked, reply=None):
    for up, kind, ctype in checked:
        SupportAttachment.objects.create(ticket=ticket, reply=reply, kind=kind, file=up, content_type=ctype,
                                         size=up.size, original_name=(up.name or '')[:200])


def _tenant_of(request):
    tenant = getattr(request, 'tenant', None)
    if isinstance(tenant, Client) and tenant.schema_name != 'public':
        return tenant
    return None


def _notify_owner_of_report(ticket, tenant, n_files):
    """إشعار فوري لصاحب المنصة: إيميل (بالمرفقات كلينكات من اللوحة) + واتساب best-effort."""
    link = f"https://{getattr(settings, 'BASE_DOMAIN', 'mousstec.com')}/superadmin/support/{ticket.id}/"
    body = (f"🆘 بلاغ جديد من {tenant.name} ({tenant.schema_name})\n"
            f"المستخدم: {ticket.reporter or ticket.name} — {ticket.phone or 'بدون رقم'}\n"
            f"الصفحة: {ticket.page_url or '—'}\n"
            f"المرفقات: {n_files}\n\n{ticket.message}\n\n{link}")
    try:
        from django.core.mail import EmailMessage
        msg = EmailMessage(subject=f"[Mousstec 🆘 #{ticket.id}] {ticket.subject}", body=body,
                           from_email=settings.DEFAULT_FROM_EMAIL,
                           to=[getattr(settings, 'SUPPORT_INBOX_EMAIL', '') or settings.DEFAULT_FROM_EMAIL],
                           reply_to=[ticket.email] if ticket.email else None)
        msg.encoding = 'utf-8'
        delivered = bool(msg.send(fail_silently=False))
        SupportTicket.objects.filter(pk=ticket.pk).update(email_delivered=delivered)
    except Exception as e:  # noqa: BLE001 — البلاغ اتسجّل، الإشعار مايوقفهوش
        logger.warning("[SUPPORT] report #%s email failed: %s", ticket.id, e)
        SupportTicket.objects.filter(pk=ticket.pk).update(email_error=str(e)[:255])
    try:
        from clients.services.whatsapp_notify import send_platform_whatsapp
        send_platform_whatsapp(getattr(settings, 'SUPPORT_WHATSAPP', ''), text=body[:1500])
    except Exception:  # noqa: BLE001
        logger.warning("[SUPPORT] report #%s whatsapp failed", ticket.id)


@require_http_methods(['GET', 'POST'])
def in_app_report(request):
    """GET: الزرار يظهر ولا لأ (شركة + مستخدم داخل). POST: يسجّل البلاغ في صندوق الدعم."""
    tenant = _tenant_of(request)
    allowed = bool(tenant and request.user.is_authenticated)
    if request.method == 'GET':
        from django.middleware.csrf import get_token
        if not allowed:
            return JsonResponse({'enabled': False, 'csrf': ''})
        # جداول clients في public بس — بتتقري من سكيمة الشركة عادي (search_path)، وكده بيانات
        # المستخدم (employee_profile) اللي في سكيمة الشركة متاحة
        unread = _visible_tickets(request, tenant).filter(customer_unread=True).count()
        return JsonResponse({'enabled': True, 'csrf': get_token(request), 'unread': unread,
                             'tickets_url': '/support/tickets/'})
    if not allowed:
        return JsonResponse({'ok': False, 'error': 'سجّل دخول الأول.'}, status=403)

    message = (request.POST.get('message') or '').strip()[:5000]
    checked, error = _checked_uploads(request)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)
    if not message and not checked:
        return JsonResponse({'ok': False, 'error': 'اكتب المشكلة أو ابعت صورة/فيديو/تسجيل صوتي.'}, status=400)

    user = request.user
    full_name = (user.get_full_name() or user.username)[:120]
    first_line = (message.splitlines()[0] if message else '') or 'بلاغ بالصوت/الصورة'
    with schema_context('public'):
        ticket = SupportTicket.objects.create(
            tenant=tenant, name=full_name, email=(user.email or tenant.email or '')[:200],
            phone=(getattr(tenant, 'phone', '') or '')[:30],
            subject=f"🆘 {first_line}"[:200],
            message=message or '(البلاغ في المرفقات)',
            source='in_app', priority='high' if request.POST.get('urgent') == '1' else 'medium',
            page_url=(request.POST.get('page_url') or request.META.get('HTTP_REFERER', ''))[:500],
            reporter=user.username[:150], ip_address=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
        )
        _save_uploads(ticket, checked)
        _notify_owner_of_report(ticket, tenant, len(checked))
    return JsonResponse({'ok': True, 'ticket_id': ticket.id, 'ticket_url': f'/support/tickets/{ticket.id}/',
                         'message': f'وصلنا بلاغك رقم #{ticket.id} — هنكلمك في أسرع وقت 💚'})


# ─────────────────────────────────────────────────────────────────────
# 🎫 «تذاكري» — العميل بيتابع بلاغاته ويرد عليها جوه حسابه
# ─────────────────────────────────────────────────────────────────────
def _sees_all_company_tickets(user):
    if user.is_superuser:
        return True
    profile = getattr(user, 'employee_profile', None)
    return getattr(profile, 'role', None) in ('admin', 'manager')


def _visible_tickets(request, tenant):
    """مدير الشركة بيشوف كل تذاكر شركته، والموظف بيشوف اللي بعتها بس."""
    qs = SupportTicket.objects.filter(tenant=tenant, is_deleted=False)
    if not _sees_all_company_tickets(request.user):
        qs = qs.filter(reporter=request.user.username)
    return qs


def _customer_ticket_or_404(request, ticket_id):
    tenant = _tenant_of(request)
    if tenant is None or not request.user.is_authenticated:
        raise Http404
    ticket = _visible_tickets(request, tenant).filter(pk=ticket_id).first()
    if ticket is None:
        raise Http404
    return tenant, ticket


def _thread(ticket):
    """الرسالة الأصلية + الردود بالترتيب، وكل واحدة بمرفقاتها."""
    atts = list(ticket.attachments.all())
    items = [{'kind': 'customer', 'name': ticket.reporter or ticket.name, 'body': ticket.message,
              'at': ticket.created_at, 'attachments': [a for a in atts if a.reply_id is None]}]
    for r in ticket.replies.all():
        items.append({'kind': r.author_kind, 'name': r.author_name, 'body': r.body, 'at': r.created_at,
                      'attachments': [a for a in atts if a.reply_id == r.id]})
    return items


def _login_redirect(request):
    from django.contrib.auth.views import redirect_to_login
    return redirect_to_login(request.get_full_path(), '/login/')


def my_tickets(request):
    tenant = _tenant_of(request)
    if tenant is None:
        raise Http404
    if not request.user.is_authenticated:
        return _login_redirect(request)
    tickets = list(_visible_tickets(request, tenant).annotate(n_replies=Count('replies'))
                   .order_by('-updated_at')[:100])
    return render(request, 'clients/support/my_tickets.html', {'tickets': tickets, 'tenant': tenant})


def my_ticket_detail(request, ticket_id):
    if not request.user.is_authenticated:
        return _login_redirect(request)
    from clients.models import SupportReply
    tenant, ticket = _customer_ticket_or_404(request, ticket_id)
    if request.method == 'POST':
        action = request.POST.get('action', 'reply')
        if action == 'close' and ticket.status != 'closed':
            ticket.status, ticket.closed_at = 'closed', timezone.now()
            ticket.save(update_fields=['status', 'closed_at', 'updated_at'])
            messages.success(request, 'التذكرة اتقفلت — شكراً ليك 💚')
        elif action == 'reply':
            body = (request.POST.get('body') or '').strip()[:5000]
            checked, error = _checked_uploads(request)
            if error or not (body or checked):
                messages.error(request, error or 'اكتب ردك أو ابعت مرفق.')
            else:
                reply = SupportReply.objects.create(
                    ticket=ticket, author_kind='customer',
                    author_name=(request.user.get_full_name() or request.user.username)[:150], body=body)
                _save_uploads(ticket, checked, reply)
                # رد العميل بيفتح التذكرة تاني ويرجّع تنبيه «جديد» في لوحة المالك
                ticket.status, ticket.closed_at, ticket.seen_at = 'open', None, None
                ticket.save(update_fields=['status', 'closed_at', 'seen_at', 'updated_at'])
                _notify_owner_of_customer_reply(ticket, tenant, body, len(checked))
                messages.success(request, 'ردك وصلنا ✅')
        return redirect('support_my_ticket', ticket_id=ticket.id)
    if ticket.customer_unread:
        SupportTicket.objects.filter(pk=ticket.pk).update(customer_unread=False)
    thread = _thread(ticket)
    return render(request, 'clients/support/my_ticket_detail.html',
                  {'ticket': ticket, 'thread': thread, 'tenant': tenant})


def _notify_owner_of_customer_reply(ticket, tenant, body, n_files):
    link = f"https://{getattr(settings, 'BASE_DOMAIN', 'mousstec.com')}/superadmin/support/{ticket.id}/"
    text = (f"💬 رد جديد من {tenant.name} على التذكرة #{ticket.id} «{ticket.subject}»\n"
            f"{body or '(مرفقات)'}\nالمرفقات: {n_files}\n\n{link}")
    try:
        from django.core.mail import EmailMessage
        msg = EmailMessage(subject=f"[Mousstec 💬 #{ticket.id}] {ticket.subject}", body=text,
                           from_email=settings.DEFAULT_FROM_EMAIL,
                           to=[getattr(settings, 'SUPPORT_INBOX_EMAIL', '') or settings.DEFAULT_FROM_EMAIL])
        msg.encoding = 'utf-8'
        msg.send(fail_silently=True)
    except Exception:  # noqa: BLE001
        logger.warning("[SUPPORT] reply notify failed for #%s", ticket.id)


def my_ticket_attachment(request, attachment_id):
    """مرفقات التذكرة لأصحابها بس (العميل وفريق الدعم) — مش رابط ميديا عام."""
    if not request.user.is_authenticated:
        raise Http404
    att = SupportAttachment.objects.filter(pk=attachment_id).first()
    if att is None:
        raise Http404
    _customer_ticket_or_404(request, att.ticket_id)
    return _stream_attachment(att)


def _stream_attachment(att):
    try:
        fh = att.file.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    resp = FileResponse(fh, content_type=att.content_type or 'application/octet-stream')
    resp['Content-Disposition'] = 'inline'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp


@saas_admin_required
@widget_required('tickets')
def support_attachment(request, attachment_id):
    """الملف من لوحة المالك بس (فيه شاشات وبيانات العميل) — مش رابط ميديا عام."""
    att = SupportAttachment.objects.filter(pk=attachment_id).select_related('ticket').first()
    if att is None or att.ticket.is_deleted:
        raise Http404
    return _stream_attachment(att)


@saas_admin_required
@widget_required('tickets')
@require_GET
def support_new_reports(request):
    """للتنبيه اللحظي في لوحة المالك: البلاغات اللي لسه ماتشافتش."""
    qs = (SupportTicket.objects.filter(is_deleted=False, seen_at__isnull=True)
          .exclude(status='closed').select_related('tenant').order_by('-created_at'))
    return JsonResponse({'unseen': qs.count(), 'latest': [
        {'id': t.id, 'subject': t.subject, 'tenant': t.tenant.name if t.tenant_id else t.name,
         'attachments': t.attachments.count(), 'created_at': timezone.localtime(t.created_at).strftime('%H:%M')}
        for t in qs[:5]]})


# ─────────────────────────────────────────────────────────────────────
# 📋 Support Inbox — للسوبر أدمن
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tickets')
def support_inbox(request):
    qs = SupportTicket.objects.filter(is_deleted=False)
    # 🔗 زرار «الدعم» في صفحة 360 بيبعت ?tenant=<schema> — كان بيتجاهَل
    tenant_schema = (request.GET.get('tenant') or '').strip()
    if tenant_schema:
        qs = qs.filter(tenant__schema_name=tenant_schema)
    show = request.GET.get('show', 'open')
    if show == 'open':
        qs = qs.exclude(status='closed')
    elif show == 'closed':
        qs = qs.filter(status='closed')

    return render(request, 'clients/saas_admin/support_inbox.html', {
        'tickets': qs.select_related('tenant').annotate(n_att=Count('attachments')).order_by('-created_at')[:200],
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
    if ticket.seen_at is None:
        ticket.seen_at = timezone.now()
        SupportTicket.objects.filter(pk=ticket.pk).update(seen_at=ticket.seen_at)
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
        elif action == 'reply':
            # 💬 الرد بيتسجّل في محادثة التذكرة: العميل بيشوفه جوه حسابه («تذاكري» + شارة على زرار
            #    الدعم) حتى لو الإيميل مش متظبط، وبيتبعتله إيميل كمان لو فيه.
            #    🐛 [FIX]: قبل كده الرد كان بيتكتب في الملاحظات الداخلية بس — من غير SMTP العميل
            #    ماكانش بيوصله أي حاجة.
            from clients.models import SupportReply
            body = (request.POST.get('reply') or '').strip()[:5000]
            checked, error = _checked_uploads(request)
            if error or not (body or checked):
                messages.error(request, error or 'اكتب الرد أو ابعت مرفق.')
            else:
                reply = SupportReply.objects.create(
                    ticket=ticket, author_kind='staff',
                    author_name=(request.user.get_full_name() or 'فريق دعم Mouss Tec')[:150], body=body)
                _save_uploads(ticket, checked, reply)
                sent = _email_customer_reply(ticket, body)
                ticket.customer_unread = True
                fields = ['customer_unread', 'updated_at']
                if ticket.status in ('open', 'in_progress'):
                    ticket.status = 'waiting'          # بانتظار رد العميل
                    fields.append('status')
                if not ticket.assigned_to_id:
                    ticket.assigned_to = request.user
                    fields.append('assigned_to')
                ticket.save(update_fields=fields)
                messages.success(request, 'الرد وصل للعميل في حسابه' + (' وعلى إيميله ✅' if sent else ' ✅'))
        elif action == 'set_priority':
            p = request.POST.get('priority')
            if p in dict(SupportTicket.PRIORITY_CHOICES):
                ticket.priority = p
                ticket.save(update_fields=['priority', 'updated_at'])
                messages.success(request, 'تم تحديث الأولوية.')
        return redirect('saas_support_ticket_detail', ticket_id=ticket.id)

    return render(request, 'clients/saas_admin/support_ticket_detail.html', {
        'ticket': ticket, 'thread': _thread(ticket),
        'whatsapp_phone': _wa_number(ticket),
    })


def _email_customer_reply(ticket, body):
    """إيميل للعميل فيه الرد ولينك التذكرة في حسابه (best-effort)."""
    if not ticket.email:
        return False
    link = ''
    if ticket.tenant_id:
        dom = ticket.tenant.domains.filter(is_primary=True).first() or ticket.tenant.domains.first()
        if dom:
            link = f"\n\nتابع التذكرة ورد علينا من حسابك: https://{dom.domain}/support/tickets/{ticket.id}/"
    try:
        from django.core.mail import EmailMessage
        msg = EmailMessage(subject=f"Re: {ticket.subject} [#{ticket.id}] | Mouss Tec",
                           body=(body or '(مرفقات)') + link, from_email=None, to=[ticket.email])
        msg.encoding = 'utf-8'
        return bool(msg.send(fail_silently=True))
    except Exception:  # noqa: BLE001
        logger.exception("support reply email failed for ticket %s", ticket.id)
        return False


def _wa_number(ticket):
    from clients.services.whatsapp_notify import normalize_phone
    return normalize_phone(ticket.phone, getattr(ticket.tenant, 'country', 'EG') or 'EG') if ticket.phone else None
