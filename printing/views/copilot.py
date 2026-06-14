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
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')



# AI copilot chat: business data queries + knowledge base + live context.

from .utils import *  # noqa: F401, F403



# =====================================================================
# 🧠 Smart Business Copilot — متوصل بالداتابيز الفعلية
# =====================================================================

def _query_business_data(query, request=None):
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

    # ============ 11. تصاميم / رصيد تصاميم AI ============
    if any(k in q for k in ['تصميم', 'تصاميم', 'design', 'باقي', 'رصيدي', 'كريدت']):
        try:
            from hr.models import AIDesignSubscription
            from printing.models import Designer
            # Check if user is a designer (request may be None)
            current_user = getattr(request, 'user', None) if request else None
            designer = Designer.objects.filter(user=current_user, is_active=True).first() if current_user else None
            if designer:
                # Get AI subscription
                sub = AIDesignSubscription.objects.filter(
                    designer__user=current_user, status='active'
                ).first()
                if sub:
                    remaining = (sub.ai_generations_limit - sub.ai_generations_used) if sub.ai_generations_limit > 0 else '∞'
                    return {
                        'intent': 'designs_balance',
                        'context': (
                            f"🎨 رصيد تصاميمك AI:\n"
                            f"  الباقة: {sub.get_plan_display()}\n"
                            f"  التصاميم المستخدمة: {sub.ai_generations_used}\n"
                            f"  المتبقي: {remaining}\n"
                            f"  الحالة: {sub.get_status_display()}\n"
                            f"  تنتهي: {sub.end_date or 'غير محدد'}"
                        ),
                    }
                return {
                    'intent': 'designs_balance',
                    'context': "ليس لديك اشتراك AI نشط حالياً. تواصل مع الإدارة لتفعيل باقة تصاميم AI.",
                }
            # Not a designer — show general design stats
            total_designs = DesignerWorkLog.objects.filter(
                date__gte=month_start
            ).count() if 'DesignerWorkLog' in dir() else 0
            return {
                'intent': 'designs_stats',
                'context': f"إجمالي أعمال التصميم هذا الشهر: {total_designs} عمل",
            }
        except Exception:
            return {'intent': 'designs_balance', 'context': "لم أتمكن من جلب بيانات التصاميم."}

    # ============ لم يتطابق — ارجع None ============
    return None


def _get_system_knowledge_printing():
    """بناء قاعدة معرفية شاملة عن سيستم المطبعة لـ Gemini"""
    return (
        "أنت Mouss Tec Copilot — المساعد الذكي الرسمي لنظام Mouss Tec لإدارة المطابع واستوديوهات التصميم.\n"
        "أنت عارف كل حاجة عن السيستم وبتساعد المستخدمين يفهموه ويستخدموه صح.\n\n"
        "## معرفتك بالسيستم:\n"
        "1. **طلبات الطباعة (PrintOrder)**: العميل بيعمل طلب → بيتضاف مهام طباعة (PrintJob) → كل مهمة ليها نوع بند (تيشرت/كارت/بنر/إلخ) وماكينة ومصمم وسعر وتكلفة\n"
        "2. **نوع البند (ProductType)**: أي حاجة المطبعة بتطبعها — تيشرت، كارت بزنس، بنر، ماج، فلاير، ستيكر. بيتسجل أوتوماتيك ويعمل autocomplete\n"
        "3. **الماكينات (MachineProfile)**: كل ماكينة ليها تكلفة تشغيل بالساعة (كهرباء + عمالة + أحبار CMYK). السيستم بيحسب التكلفة الفعلية لكل مهمة أوتوماتيك\n"
        "4. **المصممين (Designer)**: كل مصمم ليه ملف — بتتبع عدد أعماله الشهرية، ساعات العمل، تقييم العملاء (1-5 نجوم)، ونوع التنفيذ (يدوي/AI/AI+تعديل)\n"
        "5. **الخزينة (PrintTreasury)**: إيداع وسحب مع تتبع الرصيد. كل حركة مرتبطة بالطلب اللي اتعملت عليه\n"
        "6. **المخزون (PrintMaterial)**: خامات الطباعة (ورق/حبر/فينيل/بنر/لامينيشن). فيه تنبيه أوتوماتيك لما الكمية تقل عن الحد الأدنى\n"
        "7. **العملاء (PrintCustomer)**: اسم + تليفون + واتساب + شركة. بتقدر تبحث عن أي عميل بالاسم\n"
        "8. **ملفات المشاريع**: كل طلب يقدر يتضاف عليه 3 ملفات مشروع (PSD, AI, PDF)\n"
        "9. **AI Studio**: توليد تصاميم بالذكاء الاصطناعي (DALL-E) + علامة مائية ذكية + إرسال واتساب — محمي بنظام حصص شهرية\n"
        "10. **صلاحيات الموظفين (StaffPermission)**: الأدمن بيتحكم مين يشوف الخزينة/الأرباح/الملفات/AI Studio/المخزون/التقارير\n\n"
        "## طريقة حساب الربح:\n"
        "ربح المهمة = سعر البيع (unit_price × quantity × copies) - تكلفة التشغيل (ساعات الماكينة + أحبار)\n"
        "ربح الطلب = مجموع أرباح المهام\n"
        "الربح الشهري = إجمالي الإيرادات (إيداعات الخزينة) - إجمالي المصروفات (سحوبات الخزينة)\n\n"
        "## إزاي تعلّم المستخدم:\n"
        "- لو سأل سؤال مش واضح، اقترح عليه أسئلة محددة يقدر يسألها\n"
        "- لو سأل عن ميزة مش عارفها، اشرحله إزاي يوصلها في السيستم\n"
        "- لو سأل عن تقرير، اشرحله الأرقام ومعناها ونصيحتك\n"
        "- أجب بالعربي المصري، مختصر ومهني\n"
        "- لا تخترع أرقام — استخدم البيانات الفعلية فقط\n"
    )


def _get_live_context_printing():
    """جلب سياق حي شامل من داتابيز المطبعة"""
    from printing.models import (
        PrintOrder, PrintJob, PrintTransaction, PrintTreasury,
        PrintCustomer, PrintMaterial, Designer, MachineProfile,
    )

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # مبيعات ومصاريف
    income_today = PrintTransaction.objects.filter(transaction_type='in', date__gte=today_start).aggregate(t=Sum('amount'))['t'] or 0
    income_month = PrintTransaction.objects.filter(transaction_type='in', date__gte=month_start).aggregate(t=Sum('amount'))['t'] or 0
    expenses_today = PrintTransaction.objects.filter(transaction_type='out', date__gte=today_start).aggregate(t=Sum('amount'))['t'] or 0
    expenses_month = PrintTransaction.objects.filter(transaction_type='out', date__gte=month_start).aggregate(t=Sum('amount'))['t'] or 0

    # خزينة
    treasuries = PrintTreasury.objects.filter(is_active=True)
    treasury_info = ", ".join(f"{t.name}: {t.balance:,.2f}" for t in treasuries)
    total_balance = sum(t.balance for t in treasuries)

    # طلبات
    open_orders = PrintOrder.objects.filter(status__in=['draft', 'confirmed', 'in_progress']).count()
    today_orders = PrintOrder.objects.filter(date_created__gte=today_start).count()

    # عملاء
    total_customers = PrintCustomer.objects.count()
    recent_customers = PrintCustomer.objects.order_by('-created_at')[:5]
    customers_list = ", ".join(f"{c.name}" for c in recent_customers)

    # مخزون
    low_stock = PrintMaterial.objects.filter(quantity__lte=F('min_stock'))
    low_stock_items = ", ".join(f"{m.name} ({m.quantity} {m.unit})" for m in low_stock[:5])

    # مصممين
    designers = Designer.objects.filter(is_active=True)
    designers_info = []
    for d in designers:
        stats = d.get_month_stats()
        designers_info.append(f"{d.user.get_full_name() or d.user.username}: {stats['total_works'] or 0} عمل")

    return (
        f"## البيانات الحية الآن:\n"
        f"📅 التاريخ: {now.strftime('%Y-%m-%d %H:%M')}\n"
        f"💰 إيرادات اليوم: {income_today:,.2f} ج.م | إيرادات الشهر: {income_month:,.2f} ج.م\n"
        f"💸 مصروفات اليوم: {expenses_today:,.2f} ج.م | مصروفات الشهر: {expenses_month:,.2f} ج.م\n"
        f"📊 صافي ربح الشهر: {float(income_month) - float(expenses_month):,.2f} ج.م\n"
        f"🏦 الخزائن: {treasury_info} | الإجمالي: {total_balance:,.2f} ج.م\n"
        f"📋 طلبات مفتوحة: {open_orders} | طلبات اليوم: {today_orders}\n"
        f"👥 إجمالي العملاء: {total_customers} | آخر العملاء: {customers_list}\n"
        f"📦 تنبيهات مخزون: {low_stock_items or 'لا يوجد نقص ✅'}\n"
        f"🎨 المصممين: {', '.join(designers_info) or 'لا يوجد مصممين مسجلين'}\n"
    )


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
            'recommendations': 'أهلاً! اسألني عن أي حاجة — المبيعات، المصاريف، الأرباح، الطلبات، العملاء، المخزون، أو حتى إزاي تستخدم السيستم.'
        })

    # الخطوة 1: استعلم من الداتابيز للبيانات المحددة
    db_result = _query_business_data(query, request=request)
    db_context = db_result['context'] if db_result else ""

    # الخطوة 2: جلب سياق حي شامل + معرفة السيستم
    try:
        live_context = _get_live_context_printing()
    except Exception as e:
        logger.warning(f"[COPILOT] Live context failed: {e}")
        live_context = ""

    system_knowledge = _get_system_knowledge_printing()

    # الخطوة 3: Gemini للرد الذكي
    try:
        from inventory.ai_services import call_llm_layer
        if getattr(settings, 'ENABLE_AI_PREDICTIONS', False) and getattr(settings, 'AI_VISION_API_KEY', None):
            user_content = f"سؤال المستخدم: {query}"
            if db_context:
                user_content += f"\n\nنتيجة البحث في الداتابيز:\n{db_context}"
            user_content += f"\n\n{live_context}"

            messages = [
                {"role": "system", "content": system_knowledge},
                {"role": "user", "content": user_content},
            ]
            ai_response = call_llm_layer(messages, json_mode=False, max_retries=1)
            if ai_response:
                return JsonResponse({
                    'status': 'success',
                    'recommendations': ai_response.replace('\n', '<br>'),
                })
    except Exception as e:
        logger.warning(f"[COPILOT] Gemini failed: {e}")

    # Fallback: رجّع البيانات الخام
    if db_context:
        return JsonResponse({
            'status': 'success',
            'recommendations': db_context.replace('\n', '<br>'),
        })

    # Fallback ذكي — ردود محلية حسب نوع السؤال بدون Gemini
    q_lower = query.lower()

    # تحيات
    if any(k in q_lower for k in ['hi', 'hello', 'اهلا', 'أهلا', 'مرحبا', 'سلام', 'صباح', 'مساء', 'ازيك', 'إزيك']):
        return JsonResponse({
            'status': 'success',
            'recommendations': (
                'أهلاً بيك! 👋 أنا المستشار الذكي لمطبعتك.<br>'
                'أقدر أساعدك في:<br>'
                '📊 <b>بيانات حية:</b> اسألني "بيعنا كام؟" أو "مصاريفنا كام؟"<br>'
                '📋 <b>تفاصيل طلب:</b> "فاتورة رقم 5 كسبنا فيها ولا خسرنا؟"<br>'
                '💰 <b>الخزينة:</b> "رصيد الخزينة كام؟"<br>'
                '👤 <b>العملاء:</b> "عميل أحمد — بياناته إيه؟"<br>'
                '🎨 <b>المصممين:</b> "أداء المصممين" أو "مين أشطر مصمم؟"<br>'
                '📦 <b>المخزون:</b> "حالة المخزون" أو "إيه الخامات اللي قربت تخلص؟"<br>'
                '📖 <b>تعلّم:</b> "إزاي أعمل طلب؟" أو "عاوز أتعلم النظام"'
            ),
        })

    # طلبات التعلم
    if any(k in q_lower for k in ['اتعلم', 'أتعلم', 'تعلم', 'علمني', 'شرح', 'اشرح', 'ازاي', 'إزاي', 'كيف', 'طريقة']):
        # تعلم عام
        if any(k in q_lower for k in ['النظام', 'السيستم', 'البرنامج', 'كله', 'عموما']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '📖 <b>دليلك السريع لنظام المطبعة:</b><br><br>'
                    '1️⃣ <b>طلب جديد:</b> الطلبات → طلب جديد → اختر العميل → أضف المهام (تيشرت، كروت، بوستر...) → احفظ<br>'
                    '2️⃣ <b>المهام:</b> كل طلب فيه مهام — كل مهمة ليها نوع بند وسعر وتكلفة<br>'
                    '3️⃣ <b>المصممين:</b> سجّل أعمال كل مصمم يومياً + تقييم الشغل<br>'
                    '4️⃣ <b>الماكينات:</b> سجّل كل ماكينة + تكلفة CMYK → النظام يحسبلك الربح الحقيقي<br>'
                    '5️⃣ <b>الخامات:</b> أضف الخامات (ورق، حبر، خام تيشرت) + حد أدنى → تنبيه تلقائي لما يقرب يخلص<br>'
                    '6️⃣ <b>الخزينة:</b> كل تحصيل ومصروف يتسجل تلقائي → رصيدك لحظي<br>'
                    '7️⃣ <b>التقارير:</b> أرباح، مبيعات، أداء المصممين، تكلفة كل ماكينة<br><br>'
                    'اسألني عن أي نقطة بالتفصيل! 💡'
                ),
            })

        # طلب طباعة
        if any(k in q_lower for k in ['طلب', 'اوردر', 'أوردر', 'فاتور']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '📋 <b>إزاي تعمل طلب طباعة جديد:</b><br><br>'
                    '1. ادخل على "طلبات الطباعة" → "إضافة طلب جديد"<br>'
                    '2. اختر العميل (أو أضف عميل جديد)<br>'
                    '3. أضف المهام: كل مهمة = بند (مثلاً: تيشرت، كارت شخصي، بوستر)<br>'
                    '4. حدد الكمية والسعر — النظام يحسب الإجمالي تلقائي<br>'
                    '5. لو عاوز ترفع ملف المشروع (PSD/AI)، ارفعه من خانة "ملف المشروع"<br>'
                    '6. احفظ الطلب → ابدأ التنفيذ → غيّر الحالة لـ "قيد التنفيذ" → "مكتمل"<br><br>'
                    '💡 النظام بيحسبلك الربح لكل طلب تلقائي!'
                ),
            })

        # مصممين
        if any(k in q_lower for k in ['مصمم', 'ديزاين', 'تصميم', 'designer']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '🎨 <b>إدارة المصممين:</b><br><br>'
                    '1. ادخل "المصممين" → "إضافة مصمم" → اكتب اسمه وتخصصه<br>'
                    '2. سجّل أعماله يومياً من "سجل الأعمال" → اختر المصمم → أضف الشغل + ساعات العمل<br>'
                    '3. قيّم كل شغلة (ممتاز/جيد/مقبول)<br>'
                    '4. شوف الإحصائيات: أشطر مصمم، أكتر واحد شغّال، متوسط التقييمات<br><br>'
                    '💡 اسألني "أداء المصممين" وهقولك الإحصائيات الحية!'
                ),
            })

        # خامات / مخزون
        if any(k in q_lower for k in ['خام', 'مخزون', 'ورق', 'حبر', 'صنف', 'stock']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '📦 <b>إدارة الخامات والمخزون:</b><br><br>'
                    '1. ادخل "مخزون الخامات" → "إضافة خامة" → اسمها ونوعها وسعرها<br>'
                    '2. حدد "حد أدنى" — النظام يحذّرك تلقائي لما الكمية تنزل تحته<br>'
                    '3. سجّل الوارد والمنصرف → الرصيد يتحدث تلقائي<br>'
                    '4. تحويل بين الفروع: من خامة معينة → حدد الكمية → اختر الفرع<br><br>'
                    '💡 اسألني "حالة المخزون" وهقولك إيه اللي قرب يخلص!'
                ),
            })

        # ماكينات
        if any(k in q_lower for k in ['ماكين', 'طابع', 'printer', 'cmyk']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '🖨️ <b>إدارة الماكينات:</b><br><br>'
                    '1. ادخل "الماكينات" → "إضافة ماكينة" → اسمها ونوعها<br>'
                    '2. سجّل تكلفة CMYK لكل لون (Cyan, Magenta, Yellow, Black) → حاسبة التكلفة<br>'
                    '3. النظام يحسبلك تكلفة الطباعة الفعلية لكل مهمة<br>'
                    '4. تقرير ربحية كل ماكينة → تعرف أنهي ماكينة بتكسبك أكتر<br>'
                ),
            })

        # خزينة
        if any(k in q_lower for k in ['خزين', 'فلوس', 'كاش', 'treasury', 'دفع']):
            return JsonResponse({
                'status': 'success',
                'recommendations': (
                    '💰 <b>الخزينة والمدفوعات:</b><br><br>'
                    '1. "الخزائن" → شوف رصيد كل خزينة لحظياً<br>'
                    '2. تحصيل من عميل: "المعاملات" → "تحصيل" → اختر العميل والمبلغ<br>'
                    '3. مصروف: "المعاملات" → "صرف" → اكتب الوصف والمبلغ<br>'
                    '4. تحويل بين خزائن: "تحويل" → من خزينة → إلى خزينة<br><br>'
                    '💡 اسألني "رصيد الخزينة كام؟" وهقولك!'
                ),
            })

        # عام
        return JsonResponse({
            'status': 'success',
            'recommendations': (
                '📖 أقدر أعلّمك أي حاجة في النظام! اسألني عن:<br>'
                '• "إزاي أعمل طلب طباعة؟"<br>'
                '• "إزاي أضيف مصمم؟"<br>'
                '• "إزاي أدير المخزون والخامات؟"<br>'
                '• "إزاي أشوف أرباحي؟"<br>'
                '• "إزاي أسجل ماكينة وأحسب تكلفتها؟"<br>'
                '• "إزاي أدير الخزينة؟"<br>'
                '• أو اسأل أي سؤال تاني وأنا هساعدك! 💡'
            ),
        })

    # Fallback نهائي — قائمة المساعدة
    return JsonResponse({
        'status': 'success',
        'recommendations': (
            'مش متأكد فهمت سؤالك 🤔 جرّب تسأل بشكل تاني، مثلاً:<br>'
            '📊 <b>بيانات:</b> "بيعنا كام؟" | "مصاريفنا كام؟" | "رصيد الخزينة؟"<br>'
            '📋 <b>طلبات:</b> "فاتورة رقم 5" | "آخر الطلبات"<br>'
            '👤 <b>عملاء:</b> "عميل أحمد" | "أكتر عميل بيشتري"<br>'
            '📖 <b>تعلّم:</b> "عاوز أتعلم النظام" | "إزاي أعمل طلب؟"<br>'
            '🎨 <b>مصممين:</b> "أداء المصممين" | "مين أشطر مصمم؟"'
        ),
    })
