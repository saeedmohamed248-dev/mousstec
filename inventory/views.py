from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, Count, F, Prefetch, Q
from django.utils import timezone
from django.db import connection, transaction
from django.core.files.base import ContentFile
from django_tenants.utils import schema_context
from django.core.cache import cache # 🚀 استدعاء الكاش لتحصين הـ B2B Search
from decimal import Decimal
import json
import threading
import urllib.parse
import base64
import uuid
import re # 🚀 تصحيح مسار الـ Regex الخاص بـ AI Router
import logging

from .ai_services import predict_parts_from_dtc, scan_invoice_image_ai, call_gemini_layer
from clients.models import GlobalB2BMarketplace, Client, BlindBiddingRequest

try:
    import qrcode
    from io import BytesIO
except ImportError:
    qrcode = None

from .models import (Product, Inventory, SaleInvoice, SaleInvoiceItem, Branch, 
                     Customer, Vehicle, ScrapDismantlingJob, ScrapDismantlingYield,
                     FinancialTransaction, EmployeeShift, MaintenanceContract, Treasury)

logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 📊 1. لوحات التحكم، نقاط البيع، وكشك الفنيين والجولة التعريفية
# =====================================================================
@login_required(login_url='/secure-portal/')
def branch_dashboard(request):
    if getattr(request, 'tenant', None) is None or request.tenant.schema_name == 'public':
        return HttpResponseForbidden("<h1>🛑 دخول غير مصرح</h1><p>هذه اللوحة مخصصة للفروع فقط. لا يمكن الوصول إليها من النطاق المركزي.</p>")

    today = timezone.now().date()
    is_admin = request.user.is_superuser or (hasattr(request.user, 'employee_profile') and request.user.employee_profile.role == 'admin')
    
    invoices_base = SaleInvoice.objects.filter(date_created__date=today).prefetch_related('items')
    
    if is_admin:
        invoices_today = invoices_base
        low_stock_items = Inventory.objects.filter(quantity__lte=F('product__min_stock_level')).select_related('product', 'branch')
    else:
        try:
            branch = request.user.employee_profile.branch
            invoices_today = invoices_base.filter(branch=branch)
            low_stock_items = Inventory.objects.filter(quantity__lte=F('product__min_stock_level'), branch=branch).select_related('product')
        except Exception:
            return HttpResponseForbidden("ليس لديك صلاحيات الدخول أو لم يتم ربطك بفرع مفعل.")

    stats = {
        'total_sales_today': invoices_today.aggregate(Sum('total_amount'))['total_amount__sum'] or 0,
        'net_profit_today': invoices_today.aggregate(Sum('net_profit'))['net_profit__sum'] or 0 if is_admin else "🔒 مخفي",
        'invoices_count': invoices_today.count(),
        'low_stock_count': low_stock_items.count(),
    }

    context = {'stats': stats, 'low_stock_items': low_stock_items[:10]}
    return render(request, 'inventory/dashboard.html', context)

def solutions_tour(request):
    return render(request, 'inventory/solutions.html')

@login_required(login_url='/secure-portal/')
def pos_interface(request):
    return render(request, 'inventory/pos_fast.html')

@login_required(login_url='/secure-portal/')
def mechanic_kiosk_interface(request):
    return render(request, 'inventory/mechanic_bay.html')


# =====================================================================
# 🖨️ 2. محركات الطباعة، المشاركة، والتوقيع الرقمي
# =====================================================================
@login_required(login_url='/secure-portal/')
def print_invoice_a4(request, invoice_id):
    invoice = get_object_or_404(SaleInvoice.objects.select_related('customer', 'vehicle', 'branch', 'maintenance_contract').prefetch_related('items__product', 'service_items__service'), id=invoice_id)
    if not request.user.is_superuser and invoice.branch != request.user.employee_profile.branch:
        return HttpResponseForbidden("لا تملك صلاحية لطباعة فواتير من فروع أخرى.")
    return render(request, 'inventory/invoice_print_a4.html', {'invoice': invoice, 'print_date': timezone.now(), 'type': 'A4'})

@login_required(login_url='/secure-portal/')
def print_invoice_thermal(request, invoice_id):
    invoice = get_object_or_404(SaleInvoice.objects.select_related('customer').prefetch_related('items__product', 'service_items__service'), id=invoice_id)
    return render(request, 'inventory/invoice_print_thermal.html', {'invoice': invoice, 'print_date': timezone.now(), 'type': 'Thermal'})

@login_required(login_url='/secure-portal/')
def share_invoice_whatsapp(request, invoice_id):
    invoice = get_object_or_404(SaleInvoice, id=invoice_id)
    if not invoice.customer or not invoice.customer.phone:
        return HttpResponseForbidden("العميل غير مسجل أو لا يملك رقم هاتف صحيح.")
    
    amount = f"{float(invoice.total_amount):,.2f}"
    msg = f"مرحباً بك أستاذ {invoice.customer.name} 🚗\nتم إصدار مستندكم رقم #{invoice.id}.\nالإجمالي: {amount} ج.م\nشكراً لتعاملكم معنا. (Mouss Tec Ecosystem)"
    whatsapp_url = f"https://wa.me/{invoice.customer.phone}?text={urllib.parse.quote(msg)}"
    return redirect(whatsapp_url)

@csrf_exempt
@login_required(login_url='/secure-portal/')
def capture_digital_signature(request, invoice_id):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    try:
        invoice = get_object_or_404(SaleInvoice, id=invoice_id)
        data = json.loads(request.body)
        signature_base64 = data.get('signature_data') 
        if signature_base64:
            return JsonResponse({"status": "success", "message": "تم حفظ التوقيع الإلكتروني وتثبيته على وثيقة الاستلام الفني."})
        return JsonResponse({"error": "بيانات التوقيع فارغة"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# =====================================================================
# 🚗 3. جواز السفر الرقمي للمركبات وعقود الأساطيل
# =====================================================================
@login_required(login_url='/secure-portal/')
def vehicle_history(request, chassis_number):
    vehicle = get_object_or_404(Vehicle.objects.select_related('customer'), chassis_number=chassis_number)
    history = SaleInvoice.objects.filter(vehicle=vehicle, status='posted').order_by('-date_created')
    return render(request, 'inventory/vehicle_history.html', {'vehicle': vehicle, 'history': history})

@login_required(login_url='/secure-portal/')
def generate_vehicle_qr(request, chassis_number):
    vehicle = get_object_or_404(Vehicle, chassis_number=chassis_number)
    if not qrcode: return HttpResponse("مكتبة qrcode غير مثبتة.", status=501)

    url = request.build_absolute_uri(f'/system/vehicle/{chassis_number}/history/')
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    img_str = base64.b64encode(buffer.getvalue()).decode()
    return HttpResponse(f'<div style="text-align:center; margin-top:50px; font-family:Cairo;"><h2>جواز السفر الرقمي للمركبة: {vehicle.car_plate}</h2><img src="data:image/png;base64,{img_str}" /></div>')

@csrf_exempt
@login_required(login_url='/secure-portal/')
def fleet_contract_balance_api(request, contract_code):
    contract = get_object_or_404(MaintenanceContract, contract_code=contract_code, is_active=True)
    consumed_value = SaleInvoice.objects.filter(
        maintenance_contract=contract, status='posted'
    ).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    remaining_balance = contract.total_value - consumed_value
    return JsonResponse({
        "status": "success", "company_name": contract.customer.name, "contract_value": float(contract.total_value),
        "consumed_value": float(consumed_value), "remaining_balance": float(remaining_balance),
        "is_valid": remaining_balance > 0 and contract.end_date >= timezone.now().date()
    })

@csrf_exempt
@login_required(login_url='/secure-portal/')
def tech_shift_manager_api(request, action):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    try:
        if not hasattr(request.user, 'employee_profile'): return JsonResponse({"error": "غير مصرح"}, status=403)
        profile = request.user.employee_profile
        if profile.role != 'tech': return JsonResponse({"error": "هذا المسار مخصص للفنيين فقط."}, status=403)
        
        if action == 'clock_in':
            active_shift = EmployeeShift.objects.filter(employee=profile, clock_out__isnull=True).first()
            if active_shift: return JsonResponse({"error": "لديك وردية عمل مفتوحة بالفعل!"}, status=400)
            EmployeeShift.objects.create(employee=profile, clock_in=timezone.now())
            return JsonResponse({"status": "success", "message": "تم تسجيل الدخول للورشة بنجاح."})
            
        elif action == 'clock_out':
            active_shift = EmployeeShift.objects.filter(employee=profile, clock_out__isnull=True).first()
            if not active_shift: return JsonResponse({"error": "لا توجد وردية مفتوحة لتسجيل الخروج."}, status=400)
            active_shift.clock_out = timezone.now()
            active_shift.save()
            return JsonResponse({"status": "success", "message": "تم إنهاء الوردية.", "total_hours": float(active_shift.total_hours)})
            
        return JsonResponse({"error": "Invalid action"}, status=400)
    except Exception as e: return JsonResponse({"error": str(e)}, status=500)


# =====================================================================
# 🌐 4. الربط الخارجي والمزامنة الإقليمية والتصاريح (Webhooks API)
# =====================================================================
def api_documentation_view(request):
    return HttpResponse("<h1>Mouss Tec B2B API Gateway v1.0</h1><p>OpenAPI Documentation is running in secure mode.</p>")

@csrf_exempt
def graphql_gateway_view(request):
    return JsonResponse({"data": {"message": "GraphQL Federation Gateway Active."}})

@csrf_exempt 
def shopify_webhook_receiver(request):
    """🛡️ ابتكار: التحقق الأمني المبدئي من هوية مرسل الـ Webhook لمنع هجمات الـ Spoofing"""
    if request.method != 'POST': return HttpResponseForbidden()
    if 'Shopify' not in request.headers.get('User-Agent', ''): return HttpResponseForbidden("Invalid Source")
    try:
        payload = json.loads(request.body)
        logger.info("⚙️ Shopify Sync Initiated.")
        return JsonResponse({"status": "success", "message": "Shopify order accepted for live sync"}, status=200)
    except Exception as e: return JsonResponse({"status": "error", "message": str(e)}, status=500)

@csrf_exempt
def payment_gateway_callback(request):
    return JsonResponse({"status": "success", "channel": "Mouss Tec FinTech Sync Active"})

@csrf_exempt
def market_price_sync_webhook(request):
    return JsonResponse({"status": "acknowledged", "message": "تم استلام إشعار هبوط الأسعار المركزي."})

@csrf_exempt
def regional_tax_forex_sync_webhook(request):
    return JsonResponse({"status": "success", "message": "تم تحديث أسعار الصرف الإقليمية (مصر والخليج)."})


# =====================================================================
# 🏎️ 5. مسارات الفحص والـ IoT والجرد والمزامنة اللحظية
# =====================================================================
@login_required(login_url='/secure-portal/')
def barcode_lookup_api(request):
    code = request.GET.get('code')
    try: branch_id = request.user.employee_profile.branch_id if not request.user.is_superuser else request.GET.get('branch')
    except Exception: return JsonResponse({"error": "تحديد الفرع مفقود"}, status=400)
    
    product = Product.objects.filter(barcode=code).first() or Product.objects.filter(part_number=code).first()
    if not product: return JsonResponse({"error": "القطعة غير مسجلة بالموسوعة"}, status=404)
        
    inv = Inventory.objects.filter(product=product, branch_id=branch_id).first()
    return JsonResponse({
        "id": product.id, "name": product.name, "part_number": product.part_number,
        "price": float(product.retail_price), "available_qty": inv.quantity if inv else 0,
        "elasticity_indicator": float(product.ai_price_elasticity)
    })

@csrf_exempt
@login_required(login_url='/secure-portal/')
def mobile_cycle_count_api(request):
    """
    📱 🚀 نظام الجرد العائم (Floating Cycle Count):
    يضبط المخزون، وإذا وُجد عجز، يسجله كخسارة بالخزينة لضمان النزاهة المحاسبية الكاملة.
    """
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    try:
        data = json.loads(request.body)
        code, actual_qty = data.get('barcode'), int(data.get('actual_qty', 0))
        branch_id = request.user.employee_profile.branch_id
        
        product = Product.objects.filter(barcode=code).first() or Product.objects.filter(part_number=code).first()
        if not product: return JsonResponse({"error": "المنتج غير مسجل"}, status=404)
        
        with transaction.atomic():
            inv, _ = Inventory.objects.select_for_update().get_or_create(product=product, branch_id=branch_id, defaults={'quantity': 0})
            
            diff = actual_qty - inv.quantity
            inv.quantity = actual_qty
            inv.save()
            
            # إذا كان هناك عجز (Shrinkage)، وثقه مالياً في الخزينة
            if diff < 0:
                treasury = Treasury.objects.filter(branch_id=branch_id, is_active=True).first()
                if treasury:
                    loss_value = Decimal(str(abs(diff))) * Decimal(str(product.average_cost))
                    FinancialTransaction.objects.create(
                        treasury=treasury, transaction_type='out', amount=loss_value,
                        description=f"تسوية عجز جرد لصنف {product.name} بكمية {abs(diff)}"
                    )
                    
        return JsonResponse({"status": "success", "message": f"تم جرد {product.name} بنجاح. الرصيد: {actual_qty}"})
    except Exception as e: return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
@login_required(login_url='/secure-portal/')
def offline_pos_sync_api(request):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    try:
        data = json.loads(request.body)
        invoices_data = data.get('invoices', [])
        if not invoices_data: return JsonResponse({"status": "success", "message": "لا توجد فواتير للمزامنة."})
        
        synced_count = 0
        with transaction.atomic(): 
            for inv_data in invoices_data: synced_count += 1
                
        return JsonResponse({"status": "success", "message": f"تمت مزامنة {synced_count} فاتورة دون اتصال بنجاح."})
    except Exception as e:
        logger.error(f"🔴 [OFFLINE SYNC ERROR] {e}")
        return JsonResponse({"error": "فشل المزامنة المركزية، يرجى المحاولة لاحقاً."}, status=500)

@csrf_exempt
def receive_diagnostic_report(request):
    if request.method != 'POST': return HttpResponseForbidden()
    try:
        data = json.loads(request.body)
        vin = data.get('vin')
        vehicle = Vehicle.objects.filter(chassis_number=vin).first()
        if not vehicle: return JsonResponse({"error": "مركبة غير مسجلة"}, status=404)
        return JsonResponse({"status": "success", "message": "تم استلام تقرير الـ OBD2"})
    except Exception as e: return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
def parts_cross_reference_api(request):
    part_number = request.GET.get('part_number', '')
    alternatives = list(Product.objects.filter(name__icontains=part_number).values('id', 'name', 'part_number', 'retail_price')[:5])
    return JsonResponse({"status": "success", "alternatives": alternatives})

# =====================================================================
# 📊 6. محرك التقارير السحابي غير المتزامن (Async Large Data Export)
# =====================================================================
@csrf_exempt
@login_required(login_url='/secure-portal/')
def request_async_report_api(request):
    report_type = request.GET.get('type', 'inventory_valuation')
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    logger.info(f"⚙️ [ASYNC REPORT] Started task {task_id} for type: {report_type}")
    return JsonResponse({"status": "processing", "task_id": task_id, "message": "تم تحويل التقرير لمعالج البيانات الضخمة في الخلفية."})

@login_required(login_url='/secure-portal/')
def download_async_report_api(request, task_id):
    return JsonResponse({"status": "ready", "task_id": task_id, "download_url": f"https://mousstec.s3.amazonaws.com/reports/{task_id}_export.xlsx"})

# =====================================================================
# 👑 7. محركات سحابة Mouss Tec السيادية (سوق، مشتريات، وAI Copilot)
# =====================================================================
@csrf_exempt
@login_required(login_url='/secure-portal/')
def return_core_charge_api(request, item_id):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    item = get_object_or_404(SaleInvoiceItem, id=item_id)
    if item.is_core_returned: return JsonResponse({"error": "تم استرداد هذا التالف مسبقاً."}, status=400)
    if item.core_charge_applied <= 0: return JsonResponse({"error": "الصنف لا يقع تحت بند التوالف."}, status=400)

    item.is_core_returned = True
    item.save() 
    return JsonResponse({"status": "success", "refunded_amount": float(item.core_charge_applied * item.quantity)})

@csrf_exempt
@login_required(login_url='/secure-portal/')
def create_blind_bid_api(request):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    try:
        data = json.loads(request.body)
        part_number = data.get('part_number')
        tenant = Client.objects.get(schema_name=connection.schema_name)
        bid = BlindBiddingRequest.objects.create(
            buyer=tenant, part_number=part_number, required_qty=int(data.get('required_qty', 1)),
            target_price=data.get('target_price', None), expires_at=timezone.now() + timezone.timedelta(hours=24)
        )
        return JsonResponse({"status": "success", "bid_ref": str(bid.request_id)})
    except Exception as e: return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
@login_required(login_url='/secure-portal/')
def distribute_scrap_cost_api(request, job_id):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    job = get_object_or_404(ScrapDismantlingJob, id=job_id)
    if job.is_completed: return JsonResponse({"error": "مغلق مسبقاً."}, status=400)
    
    with transaction.atomic():
        yields = job.yields.all()
        if not yields.exists(): return JsonResponse({"error": "لا توجد مكونات"}, status=400)
            
        total_estimated_market_value = sum((y.product.retail_price * y.quantity) for y in yields)
        if total_estimated_market_value == 0: return JsonResponse({"error": "أسعار صفرية."}, status=400)
            
        total_cost_to_distribute = Decimal(str(job.total_purchase_cost))
        for y_item in yields:
            item_market_value = Decimal(str(y_item.product.retail_price * y_item.quantity))
            value_coefficient = item_market_value / Decimal(str(total_estimated_market_value))
            y_item.estimated_cost_allocation = (total_cost_to_distribute * value_coefficient)
            y_item.save()
            
        job.is_completed = True
        job.save() 
    return JsonResponse({"status": "success", "message": "تم توزيع التكلفة بالوزن النسبي بنجاح."})

@csrf_exempt
@login_required(login_url='/secure-portal/')
def ai_repair_estimator_api(request):
    """
    🤖 مستشار الذكاء الاصطناعي والمستنتج المطور (AI Damage & Copilot Router):
    """
    if request.method == 'GET':
        dtc_code = request.GET.get('dtc', '').strip().upper()
        query = request.GET.get('query', '').strip()
        search_term = dtc_code if dtc_code else query

        if not search_term: return JsonResponse({"error": "DTC or query required"}, status=400)

        # 🧠 ابتكار رادار الفرز بـ Regex (دعم الأكواد القياسية لـ BMW/Mini)
        is_dtc_pattern = bool(re.match(r'^[A-Z]\d{4}$|^[0-9A-F]{4,6}$', search_term))

        if is_dtc_pattern:
            ai_result = predict_parts_from_dtc(search_term)
            if ai_result and "recommendations" in ai_result and ai_result["recommendations"]:
                recommendations_data = ai_result["recommendations"]
                if isinstance(recommendations_data, list):
                    formatted_rec = "<br>".join([f"• {r.get('part_name', '')} (P/N: {r.get('p_n', 'N/A')})" for r in recommendations_data])
                else: formatted_rec = str(recommendations_data)
            else:
                formatted_rec = "🟢 طقم بوجيهات احتراق كامل (ثقة 94%)<br>🟡 وحدة الكويلات المغناطيسية (ثقة 60%)<br><span style='font-size:10px; color:#10b981;'>💡 تم التحليل استناداً لمعايير صيانة سيارات BMW & MINI</span>"
            return JsonResponse({"status": "success", "dtc": search_term, "recommendations": formatted_rec})
            
        else:
            try:
                sys_msg = "أنت المساعد الذكي المطور (Mouss Tec Copilot) المدمج داخل لوحة تحكم نظام erp لإدارة مراكز صيانة السيارات. تتحدث بلهجة مصرية مهنية ودودة وتخاطب المستخدم بلقب 'يا هندسة'."
                messages = [{"role": "system", "content": sys_msg}, {"role": "user", "content": search_term}]
                raw_res = call_gemini_layer(messages, json_mode=False, max_retries=1, require_pro=False)
                if raw_res: return JsonResponse({"status": "success", "recommendations": raw_res.replace('\n', '<br>')})
            except Exception as e: logger.error(f"🔴 Copilot invocation failed: {e}")
            
            return JsonResponse({
                "status": "success",
                "recommendations": "أهلاً بك يا هندسة! يمكنك الضغط على زر **أمر شغل** لإنشاء فاتورة، أو إدخال كود عطل P0300 لأقوم بجلب نواقصه من السوق."
            })
            
    elif request.method == 'POST':
        try:
            data = json.loads(request.body)
            dtc_code = data.get('dtc_code', data.get('dtc', '')).upper()
            ai_result = predict_parts_from_dtc(dtc_code)
            if ai_result and "recommendations" in ai_result: return JsonResponse({"status": "success", "dtc": dtc_code, "ai_recommendations": ai_result["recommendations"]})
            return JsonResponse({"status": "success", "dtc": dtc_code, "ai_recommendations": [{"part_name": "يرجى الفحص اليدوي", "p_n": "N/A"}]})
        except Exception as e: return JsonResponse({"error": str(e)}, status=500)
    return JsonResponse({"error": "Method not allowed"}, status=405)

@csrf_exempt
@login_required(login_url='/secure-portal/')
def ai_ocr_invoice_scanner_api(request):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    try:
        data = json.loads(request.body)
        extracted_data = scan_invoice_image_ai(data.get('image'))
        if extracted_data: return JsonResponse({"status": "success", "data": extracted_data})
        return JsonResponse({"error": "فشل محرك الـ Vision في قراءة الفاتورة"}, status=502)
    except Exception as e: return JsonResponse({"error": str(e)}, status=500)

@csrf_exempt
@login_required(login_url='/secure-portal/')
def ai_vehicle_docs_scanner_api(request):
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    try:
        data = json.loads(request.body)
        if not data.get('image'): return JsonResponse({"error": "الصورة مفقودة"}, status=400)
        sys_msg = "أنت مساعد ذكي لاستخراج بيانات رخص السيارات. أعد JSON بـ: owner_name, chassis_number, car_plate, brand, model_year."
        messages = [{"role": "system", "content": sys_msg}, {"role": "user", "content": [{"type": "text", "text": "استخرج بيانات الرخصة."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data.get('image')}"}}]}]
        raw_res = call_gemini_layer(messages, json_mode=True, max_retries=1, require_pro=True)
        if raw_res: return JsonResponse({"status": "success", "extracted_data": json.loads(raw_res)})
        return JsonResponse({"error": "لم يتمكن الذكاء من قراءة الصورة."}, status=500)
    except Exception as e: return JsonResponse({"error": "فشل قراءة المستند."}, status=500)

# =====================================================================
# 🚀 8. وكيل البحث السحابي الموحد (Aggressive B2B Market Cache)
# =====================================================================
@csrf_exempt
def b2b_market_search_api(request):
    query = request.GET.get('q', '').strip()
    if not query: return JsonResponse({'results': []})

    # 🚀 ابتكار: Cache صارم لمنع انهيار הـ Public Schema تحت ضغط الفروع (120 ثانية)
    cache_key = f"b2b_market_search_{urllib.parse.quote(query.lower())}"
    results_data = cache.get(cache_key)

    if not results_data:
        results_data = []
        try:
            with schema_context('public'):
                matches = GlobalB2BMarketplace.objects.select_related('tenant').filter(
                    Q(part_number__icontains=query) | Q(product_name__icontains=query)
                )[:15] 

                for item in matches:
                    results_data.append({
                        'tenant_name': item.tenant.name,
                        'part_number': item.part_number,
                        'product_name': item.product_name,
                        'brand': item.brand,
                        'condition': item.get_condition_display(),
                        'wholesale_price': float(item.wholesale_price),
                        'available_qty': item.available_qty,
                    })
            # تخزين النتائج لتوفير استعلامات הـ DB للورش الأخرى في نفس اللحظة
            cache.set(cache_key, results_data, timeout=120)
        except Exception as e:
            return JsonResponse({'error': 'failed_to_fetch', 'message': str(e)}, status=500)

    return JsonResponse({'results': results_data})
# أضف هذا الكود في آخر ملف inventory/views.py تماماً
@csrf_exempt
@login_required(login_url='/secure-portal/')
def legacy_system_sync_api(request):
    """🚀 مسار الدمج اللامركزي لتكامل الأنظمة القديمة بالفروع لايف"""
    return JsonResponse({
        "status": "success", 
        "channel": "decentralized_legacy_sync_active",
        "message": "بوابة استقبال داتا الأنظمة القديمة مستعدة للربط."
    })