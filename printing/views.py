"""
🤖 Printing AI Studio Views
==============================
AI-powered design generation and smart watermark for printing tenants.
Gated by TenantSubscription + AILimitTracker.
"""
import logging
import base64
import json
import re
from io import BytesIO
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


def _get_tenant():
    """Get current tenant from connection schema."""
    from clients.models import Client
    schema = connection.schema_name
    if schema == 'public':
        return None
    return Client.objects.filter(schema_name=schema).first()


def _check_ai_access(tenant, action_type='ai_generation'):
    """
    Check if tenant has active AI subscription and remaining quota.
    Returns (allowed: bool, error_message: str or None)
    """
    if not tenant:
        return False, 'لا يمكن تحديد المستأجر.'

    from clients.models import TenantSubscription, AILimitTracker
    try:
        sub = tenant.subscription
    except TenantSubscription.DoesNotExist:
        return False, 'لا يوجد اشتراك مفعّل. تواصل مع الإدارة لتفعيل حزمة AI Studio.'

    if not sub.is_active:
        return False, 'اشتراكك غير مفعّل حالياً. تواصل مع الإدارة لتجديد الاشتراك.'

    if not sub.ai_addon:
        return False, 'لم يتم تفعيل حزمة AI Studio على اشتراكك. تواصل مع الإدارة لإضافة حزمة AI.'

    if not AILimitTracker.can_use(tenant, action_type):
        return False, 'تم استنفاد حصتك الشهرية من هذه الخدمة. يتم تجديد الحصة في بداية كل شهر.'

    return True, None


@login_required
@require_POST
def ai_generate_design(request):
    """
    Generate an AI design using OpenAI DALL-E API.
    Gated by subscription + AI quota.
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'ai_generation')
    if not allowed:
        return JsonResponse({'success': False, 'error': error}, status=403)

    api_key = getattr(settings, 'OPENAI_API_KEY', '')
    if not api_key:
        return JsonResponse({
            'success': False,
            'error': 'مفتاح OpenAI API غير مُعد في النظام. تواصل مع مسؤول المنصة.'
        }, status=500)

    prompt = request.POST.get('prompt', '').strip()
    size = request.POST.get('size', '1024x1024')
    quality = request.POST.get('quality', 'standard')

    if not prompt:
        return JsonResponse({'success': False, 'error': 'يرجى كتابة وصف التصميم المطلوب.'}, status=400)

    # Validate size
    valid_sizes = ['1024x1024', '1024x1792', '1792x1024']
    if size not in valid_sizes:
        size = '1024x1024'

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)

        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size=size,
            quality=quality,
            n=1,
        )

        image_url = response.data[0].url
        revised_prompt = response.data[0].revised_prompt

        # Deduct quota
        from clients.models import AILimitTracker
        AILimitTracker.deduct(tenant, 'ai_generation', metadata={
            'prompt': prompt[:200],
            'size': size,
            'quality': quality,
            'user': request.user.username,
        })

        logger.info(f"🤖 [AI STUDIO]: {tenant.name} — Generated design by {request.user.username}")

        return JsonResponse({
            'success': True,
            'image_url': image_url,
            'revised_prompt': revised_prompt,
        })

    except openai.RateLimitError:
        return JsonResponse({'success': False, 'error': 'تم تجاوز حدود OpenAI API. حاول مرة أخرى بعد دقيقة.'}, status=429)
    except openai.APIError as e:
        logger.error(f"🔴 [AI STUDIO ERROR]: {tenant.name} — {e}")
        return JsonResponse({'success': False, 'error': f'خطأ في OpenAI API: {str(e)}'}, status=500)
    except ImportError:
        return JsonResponse({'success': False, 'error': 'مكتبة openai غير مثبتة على السيرفر.'}, status=500)
    except Exception as e:
        logger.error(f"🔴 [AI STUDIO ERROR]: {tenant.name} — {e}")
        return JsonResponse({'success': False, 'error': 'حدث خطأ غير متوقع. حاول مرة أخرى.'}, status=500)


@login_required
@require_POST
def ai_smart_watermark(request):
    """
    Apply smart watermark to an uploaded image.
    Uses PIL (Pillow) — no external API needed, but gated by AI subscription.
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'smart_watermark')
    if not allowed:
        return JsonResponse({'success': False, 'error': error}, status=403)

    image_file = request.FILES.get('image')
    watermark_text = request.POST.get('watermark_text', tenant.name if tenant else 'Mousstec')
    opacity = int(request.POST.get('opacity', 40))

    if not image_file:
        return JsonResponse({'success': False, 'error': 'يرجى رفع صورة.'}, status=400)

    try:
        from PIL import Image, ImageDraw, ImageFont
        import io

        img = Image.open(image_file).convert('RGBA')
        txt_layer = Image.new('RGBA', img.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)

        # Dynamic font size based on image width
        font_size = max(int(img.width / 15), 24)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()

        # Get text bounding box
        bbox = draw.textbbox((0, 0), watermark_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        # Diagonal watermark pattern
        import math
        for y in range(0, img.height, text_h * 4):
            for x in range(-img.width, img.width * 2, text_w + 100):
                draw.text((x, y), watermark_text, font=font, fill=(255, 255, 255, opacity))

        watermarked = Image.alpha_composite(img, txt_layer).convert('RGB')

        # Save to buffer
        buffer = io.BytesIO()
        watermarked.save(buffer, format='JPEG', quality=92)
        buffer.seek(0)

        img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

        # Deduct quota
        from clients.models import AILimitTracker
        AILimitTracker.deduct(tenant, 'smart_watermark', metadata={
            'watermark_text': watermark_text,
            'user': request.user.username,
        })

        logger.info(f"🏷️ [WATERMARK]: {tenant.name} — Applied by {request.user.username}")

        return JsonResponse({
            'success': True,
            'image_base64': f'data:image/jpeg;base64,{img_base64}',
        })

    except ImportError:
        return JsonResponse({'success': False, 'error': 'مكتبة Pillow غير مثبتة.'}, status=500)
    except Exception as e:
        logger.error(f"🔴 [WATERMARK ERROR]: {tenant.name} — {e}")
        return JsonResponse({'success': False, 'error': 'حدث خطأ أثناء معالجة الصورة.'}, status=500)


@login_required
@require_POST
def ai_send_whatsapp(request):
    """
    Generate a WhatsApp send link for a design image.
    Uses wa.me deep link (no API needed). Deducts from whatsapp quota.
    """
    tenant = _get_tenant()
    allowed, error = _check_ai_access(tenant, 'whatsapp_send')
    if not allowed:
        return JsonResponse({'success': False, 'error': error}, status=403)

    phone = request.POST.get('phone', '').strip()
    image_url = request.POST.get('image_url', '').strip()
    message = request.POST.get('message', '').strip()

    if not phone:
        return JsonResponse({'success': False, 'error': 'يرجى إدخال رقم واتساب العميل.'}, status=400)

    # Normalize phone number
    import re
    phone_clean = re.sub(r'[^\d+]', '', phone)
    if phone_clean.startswith('0'):
        phone_clean = '2' + phone_clean  # Egypt country code
    if not phone_clean.startswith('+'):
        phone_clean = '+' + phone_clean

    # Build WhatsApp message
    if not message:
        company_name = tenant.name if tenant else 'الاستوديو'
        message = f'مرحباً، تصميمك جاهز من {company_name}!'
    if image_url:
        message += f'\n\nالتصميم: {image_url}'

    # URL encode
    from urllib.parse import quote
    wa_url = f'https://wa.me/{phone_clean.lstrip("+")}?text={quote(message)}'

    # Deduct quota
    from clients.models import AILimitTracker
    AILimitTracker.deduct(tenant, 'whatsapp_send', metadata={
        'phone': phone_clean,
        'user': request.user.username,
    })

    logger.info(f"📱 [WHATSAPP]: {tenant.name} — Sent by {request.user.username} to {phone_clean}")

    return JsonResponse({
        'success': True,
        'whatsapp_url': wa_url,
    })


@login_required
def ai_studio_status(request):
    """Return AI Studio subscription status and remaining quota for current tenant."""
    tenant = _get_tenant()
    if not tenant:
        return JsonResponse({'active': False, 'reason': 'no_tenant'})

    from clients.models import TenantSubscription, AILimitTracker

    try:
        sub = tenant.subscription
    except TenantSubscription.DoesNotExist:
        return JsonResponse({'active': False, 'reason': 'no_subscription'})

    if not sub.is_active or not sub.ai_addon:
        return JsonResponse({'active': False, 'reason': 'no_ai_addon'})

    ai_used = AILimitTracker.get_monthly_usage(tenant, 'ai_generation')
    wm_used = AILimitTracker.get_monthly_usage(tenant, 'smart_watermark')

    return JsonResponse({
        'active': True,
        'addon_name': sub.ai_addon.name,
        'ai_limit': sub.ai_addon.ai_generations_limit,
        'ai_used': ai_used,
        'ai_remaining': max(0, sub.ai_addon.ai_generations_limit - ai_used),
        'wm_limit': sub.ai_addon.whatsapp_messages_limit,
        'wm_used': wm_used,
        'wm_remaining': max(0, sub.ai_addon.whatsapp_messages_limit - wm_used),
    })


# =====================================================================
# 🧠 Smart Business Copilot — متوصل بالداتابيز الفعلية
# =====================================================================

def _query_business_data(query):
    """
    يحلل سؤال المستخدم ويجيب من الداتابيز الفعلية.
    يرجع dict فيه: context (البيانات), intent (نوع السؤال)
    """
    from printing.models import (
        PrintOrder, PrintJob, PrintTransaction, PrintTreasury,
        PrintCustomer, PrintMaterial, Designer, DesignerWorkLog,
        MachineProfile, PrintBranch,
    )

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    q = query.lower()

    # ============ 1. مبيعات اليوم ============
    if any(k in q for k in ['بيع', 'مبيعات', 'ايراد', 'إيراد', 'دخل', 'بعنا', 'بيعنا', 'revenue', 'sales']):
        period = 'اليوم'
        date_filter = {'date__gte': today_start}
        if any(k in q for k in ['الشهر', 'شهر', 'شهري']):
            period = 'الشهر'
            date_filter = {'date__gte': month_start}
        elif any(k in q for k in ['امبارح', 'أمس', 'البارحه']):
            period = 'أمس'
            yesterday_start = today_start - timedelta(days=1)
            date_filter = {'date__gte': yesterday_start, 'date__lt': today_start}
        elif any(k in q for k in ['اسبوع', 'أسبوع', 'الاسبوع']):
            period = 'الأسبوع'
            date_filter = {'date__gte': today_start - timedelta(days=7)}

        income = PrintTransaction.objects.filter(
            transaction_type='in', **date_filter
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        order_count = PrintOrder.objects.filter(
            date_created__gte=date_filter.get('date__gte', today_start),
        ).count()

        return {
            'intent': 'sales',
            'context': f"إجمالي المبيعات/الإيرادات {period}: {income:,.2f} ج.م\nعدد الطلبات {period}: {order_count} طلب",
        }

    # ============ 2. مصاريف ============
    if any(k in q for k in ['مصاريف', 'مصروف', 'صرف', 'خرج', 'expense', 'مصروفات']):
        period = 'اليوم'
        date_filter = {'date__gte': today_start}
        if any(k in q for k in ['الشهر', 'شهر', 'شهري']):
            period = 'الشهر'
            date_filter = {'date__gte': month_start}
        elif any(k in q for k in ['اسبوع', 'أسبوع', 'الاسبوع']):
            period = 'الأسبوع'
            date_filter = {'date__gte': today_start - timedelta(days=7)}

        expenses = PrintTransaction.objects.filter(
            transaction_type='out', **date_filter
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        # تفاصيل أكبر 5 مصاريف
        top_expenses = PrintTransaction.objects.filter(
            transaction_type='out', **date_filter
        ).order_by('-amount')[:5]
        details = "\n".join(
            f"  • {tx.description or 'بدون وصف'}: {tx.amount:,.2f} ج.م"
            for tx in top_expenses
        )

        return {
            'intent': 'expenses',
            'context': f"إجمالي المصروفات {period}: {expenses:,.2f} ج.م\nأكبر المصروفات:\n{details}" if details else f"إجمالي المصروفات {period}: {expenses:,.2f} ج.م",
        }

    # ============ 3. فاتورة / طلب محدد ============
    order_match = re.search(r'(?:طلب|فاتور[ةه]|order|اوردر|أوردر)\s*(?:رقم|#|no)?\s*[#]?(\d+|PO-[\d-]+)', q, re.IGNORECASE)
    if not order_match:
        # Try standalone number with context
        order_match = re.search(r'(?:رقم|#)\s*(\d+)', q)
    if order_match:
        order_ref = order_match.group(1)
        # Try by number suffix or full order_number
        orders = PrintOrder.objects.filter(
            Q(order_number__icontains=order_ref) | Q(pk=int(order_ref) if order_ref.isdigit() else 0)
        )[:1]
        if orders:
            order = orders[0]
            jobs = order.jobs.all()
            total_cost = sum(j.actual_cost or j.calculated_cost for j in jobs)
            total_revenue = order.net_total
            profit = total_revenue - total_cost
            profit_status = "ربح ✅" if profit > 0 else ("خسارة ❌" if profit < 0 else "تعادل")

            jobs_detail = "\n".join(
                f"  • {j.description[:60]}: سعر {j.total_price:,.2f} — تكلفة {j.actual_cost or j.calculated_cost:,.2f}"
                for j in jobs
            )
            return {
                'intent': 'order_detail',
                'context': (
                    f"طلب #{order.order_number} — العميل: {order.customer.name}\n"
                    f"الحالة: {order.get_status_display()}\n"
                    f"الإجمالي: {order.total_amount:,.2f} ج.م | خصم: {order.discount:,.2f} | صافي: {total_revenue:,.2f}\n"
                    f"المدفوع: {order.paid_amount:,.2f} | المتبقي: {order.remaining:,.2f}\n"
                    f"التكلفة الفعلية: {total_cost:,.2f} ج.م\n"
                    f"الربح: {profit:,.2f} ج.م ({profit_status})\n"
                    f"المهام:\n{jobs_detail}" if jobs_detail else ""
                ),
            }
        return {'intent': 'order_not_found', 'context': f"لم أجد طلب برقم {order_ref}"}

    # ============ 4. أرباح ============
    if any(k in q for k in ['ربح', 'أرباح', 'ارباح', 'كسب', 'كسبنا', 'profit', 'صافي']):
        period = 'الشهر'
        date_filter = {'date_created__gte': month_start}
        if any(k in q for k in ['يوم', 'النهاردة', 'اليوم', 'today']):
            period = 'اليوم'
            date_filter = {'date_created__gte': today_start}

        income = PrintTransaction.objects.filter(
            transaction_type='in', date__gte=date_filter['date_created__gte']
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        expenses = PrintTransaction.objects.filter(
            transaction_type='out', date__gte=date_filter['date_created__gte']
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        profit = income - expenses

        # Also calculate from completed jobs
        completed_jobs = PrintJob.objects.filter(
            is_complete=True,
            completed_at__gte=date_filter['date_created__gte'],
        )
        job_profit = completed_jobs.aggregate(total=Sum('actual_profit'))['total'] or Decimal('0')

        return {
            'intent': 'profit',
            'context': (
                f"التقرير المالي — {period}:\n"
                f"  إجمالي الإيرادات: {income:,.2f} ج.م\n"
                f"  إجمالي المصروفات: {expenses:,.2f} ج.م\n"
                f"  صافي الربح (خزينة): {profit:,.2f} ج.م\n"
                f"  صافي ربح المهام المكتملة: {job_profit:,.2f} ج.م"
            ),
        }

    # ============ 5. خزينة / رصيد ============
    if any(k in q for k in ['خزينة', 'خزنة', 'رصيد', 'كاش', 'balance', 'treasury', 'فلوس']):
        treasuries = PrintTreasury.objects.filter(is_active=True)
        total = sum(t.balance for t in treasuries)
        details = "\n".join(f"  • {t.name}: {t.balance:,.2f} ج.م" for t in treasuries)
        return {
            'intent': 'treasury',
            'context': f"رصيد الخزائن:\n{details}\nالإجمالي: {total:,.2f} ج.م",
        }

    # ============ 6. عملاء ============
    if any(k in q for k in ['عميل', 'عملاء', 'customer', 'زبون', 'زباين']):
        # Search for specific customer
        name_match = re.search(r'(?:عميل|زبون)\s+(.+)', q)
        if name_match:
            name = name_match.group(1).strip()
            customers = PrintCustomer.objects.filter(name__icontains=name)[:5]
            if customers:
                details = "\n".join(
                    f"  • {c.name} | {c.phone or 'بدون رقم'} | {c.company or ''}"
                    for c in customers
                )
                return {'intent': 'customer_search', 'context': f"نتائج البحث:\n{details}"}
            return {'intent': 'customer_not_found', 'context': f"لم أجد عميل باسم '{name}'"}

        total_customers = PrintCustomer.objects.count()
        new_this_month = PrintCustomer.objects.filter(created_at__gte=month_start).count()
        return {
            'intent': 'customers',
            'context': f"إجمالي العملاء: {total_customers}\nعملاء جدد هذا الشهر: {new_this_month}",
        }

    # ============ 7. طلبات مفتوحة ============
    if any(k in q for k in ['طلب', 'طلبات', 'اوردر', 'order', 'شغل', 'مفتوح']):
        open_orders = PrintOrder.objects.filter(
            status__in=['draft', 'confirmed', 'in_progress']
        ).order_by('-date_created')[:10]
        if open_orders:
            details = "\n".join(
                f"  • #{o.order_number} — {o.customer.name} | {o.get_status_display()} | {o.net_total:,.2f} ج.م"
                for o in open_orders
            )
            return {
                'intent': 'open_orders',
                'context': f"الطلبات المفتوحة ({open_orders.count()}):\n{details}",
            }
        return {'intent': 'open_orders', 'context': "لا توجد طلبات مفتوحة حالياً 🎉"}

    # ============ 8. مخزون / خامات ============
    if any(k in q for k in ['مخزون', 'خامات', 'ورق', 'حبر', 'stock', 'inventory', 'خامه', 'material']):
        low_stock = PrintMaterial.objects.filter(quantity__lte=F('min_stock'))
        total_materials = PrintMaterial.objects.count()
        stock_value = PrintMaterial.objects.aggregate(
            val=Sum(F('quantity') * F('cost_per_unit'))
        )['val'] or 0

        if low_stock.exists():
            alerts = "\n".join(
                f"  ⚠️ {m.name}: {m.quantity} {m.unit} (الحد الأدنى: {m.min_stock})"
                for m in low_stock[:10]
            )
            return {
                'intent': 'stock',
                'context': f"إجمالي الخامات: {total_materials} | قيمة المخزون: {stock_value:,.2f} ج.م\n\nتنبيهات نقص:\n{alerts}",
            }
        return {
            'intent': 'stock',
            'context': f"إجمالي الخامات: {total_materials} | قيمة المخزون: {stock_value:,.2f} ج.م\nلا توجد تنبيهات نقص ✅",
        }

    # ============ 9. مصممين / أداء ============
    if any(k in q for k in ['مصمم', 'مصممين', 'designer', 'أداء', 'اداء', 'performance']):
        designers = Designer.objects.filter(is_active=True)
        results = []
        for d in designers:
            stats = d.get_month_stats()
            results.append(
                f"  • {d.user.get_full_name() or d.user.username}: "
                f"{stats['total_works'] or 0} عمل | "
                f"{stats['total_hours'] or 0} ساعة | "
                f"تقييم: {stats['avg_rating'] or '-'}/5"
            )
        if results:
            return {'intent': 'designers', 'context': f"أداء المصممين هذا الشهر:\n" + "\n".join(results)}
        return {'intent': 'designers', 'context': "لا يوجد مصممين مسجلين بعد."}

    # ============ 10. ماكينات ============
    if any(k in q for k in ['ماكينة', 'ماكينات', 'طابعة', 'machine', 'printer']):
        machines = MachineProfile.objects.filter(is_active=True)
        if machines:
            details = "\n".join(
                f"  • {m.name} ({m.get_machine_type_display()}) — تكلفة/ساعة: {m.hourly_operating_cost:,.2f} ج.م"
                for m in machines
            )
            return {'intent': 'machines', 'context': f"الماكينات النشطة ({machines.count()}):\n{details}"}
        return {'intent': 'machines', 'context': "لا توجد ماكينات مسجلة بعد."}

    # ============ لم يتطابق — ارجع None ============
    return None


@login_required
def copilot_chat(request):
    """
    🧠 Smart Business Copilot — يرد على أسئلة من الداتابيز الفعلية.
    مجاني — لا يستهلك حصة AI ولا API خارجي (إلا لتنسيق الرد).
    """
    query = request.GET.get('query', '').strip()
    if not query:
        return JsonResponse({
            'status': 'success',
            'recommendations': 'أهلاً! اسألني عن المبيعات، المصاريف، الأرباح، الطلبات، أو أي شيء يخص شغلك.'
        })

    # الخطوة 1: استعلم من الداتابيز
    db_result = _query_business_data(query)

    if db_result:
        context = db_result['context']

        # الخطوة 2: لو Gemini متاح، خليه ينسق الرد بشكل لطيف
        try:
            from inventory.ai_services import call_gemini_layer
            if getattr(settings, 'ENABLE_AI_PREDICTIONS', False) and getattr(settings, 'AI_VISION_API_KEY', None):
                sys_msg = (
                    "أنت Mouss Tec Copilot — مساعد ذكي لمطبعة. "
                    "المستخدم سألك سؤال وأنا جبتلك البيانات الفعلية من النظام. "
                    "نسّق الرد بشكل مختصر ومهني بالعربي المصري. "
                    "لا تخترع أرقام — استخدم البيانات الفعلية فقط. "
                    "لو في ملاحظة مهمة أو نصيحة بناءً على الأرقام، اذكرها. "
                    "الرد يكون قصير ومفيد."
                )
                messages = [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": f"سؤال المستخدم: {query}\n\nالبيانات الفعلية:\n{context}"},
                ]
                ai_response = call_gemini_layer(messages, json_mode=False, max_retries=1)
                if ai_response:
                    return JsonResponse({
                        'status': 'success',
                        'recommendations': ai_response.replace('\n', '<br>'),
                    })
        except Exception as e:
            logger.warning(f"[COPILOT] Gemini formatting failed, returning raw: {e}")

        # Fallback: رجّع البيانات بدون تنسيق AI
        return JsonResponse({
            'status': 'success',
            'recommendations': context.replace('\n', '<br>'),
        })

    # لو مش مفهوم — حاول Gemini مباشرة
    try:
        from inventory.ai_services import call_gemini_layer
        if getattr(settings, 'ENABLE_AI_PREDICTIONS', False) and getattr(settings, 'AI_VISION_API_KEY', None):
            sys_msg = (
                "أنت Mouss Tec Copilot — مساعد ذكي لمطبعة / استوديو تصميم. "
                "أجب بالعربي المصري، مختصر ومهني. "
                "لو السؤال عن بيانات محددة (أرقام مبيعات أو فواتير)، "
                "قول للمستخدم يسأل بشكل أوضح (مثلاً: بيعنا كام النهاردة؟ / فاتورة رقم 5 / مصاريف الشهر)."
            )
            messages = [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": query},
            ]
            raw = call_gemini_layer(messages, json_mode=False, max_retries=1)
            if raw:
                return JsonResponse({
                    'status': 'success',
                    'recommendations': raw.replace('\n', '<br>'),
                })
    except Exception as e:
        logger.warning(f"[COPILOT] Gemini fallback failed: {e}")

    return JsonResponse({
        'status': 'success',
        'recommendations': 'أهلاً! أقدر أساعدك في:<br>• بيعنا كام النهاردة/الشهر؟<br>• مصاريفنا كام؟<br>• فاتورة رقم 5 كسبنا فيها ولا خسرنا؟<br>• رصيد الخزينة كام؟<br>• أداء المصممين<br>• الطلبات المفتوحة<br>• حالة المخزون',
    })
