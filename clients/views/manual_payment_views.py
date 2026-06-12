"""
💵 Manual Payment Receipt — Vodafone Cash / InstaPay unified flow.

Used by: SaaS subscriptions, Parts marketplace, Design store, Diagnostics
upgrades. Single template, single view, single super-admin review pipeline.

Flow:
  1. User selects "Vodafone Cash" at any payment point
  2. Backend creates a pending purchase record (DesignPurchase, PlatformInvoice,
     PartOrder, etc.) and redirects to ``manual_payment_upload`` with the
     receipt_code.
  3. User sees Vodafone number ``01094850763`` + transfer instructions.
  4. User uploads screenshot + transaction reference + their sending phone.
  5. Receipt enters super-admin queue. Admin approves → underlying purchase
     activates via ``ManualPaymentReceipt.confirm()``.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from clients.models import (
    Client,
    DesignPackage,
    DesignPurchase,
    ManualPaymentReceipt,
    MarketplaceCustomer,
    PartListing,
    PartOrder,
    Plan,
    PlanRevision,
    PlatformInvoice,
    TenantSubscription,
)
from clients.views._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')

# 💳 الرقم الموحّد لكل عمليات الدفع اليدوي
VODAFONE_CASH_NUMBER = '01094850763'
INSTAPAY_HANDLE      = '01094850763@instapay'


# ─────────────────────────────────────────────────────────────────────────────
# 1) Generic upload page — works for ALL purchase types
# ─────────────────────────────────────────────────────────────────────────────
@csrf_exempt
def manual_payment_upload(request, receipt_code):
    """
    صفحة رفع إيصال التحويل اليدوي.

    GET  → يعرض رقم Vodafone + نموذج الرفع.
    POST → يحفظ رقم العملية + رقم المرسل + الصورة → status='pending'.
    """
    receipt = get_object_or_404(ManualPaymentReceipt, receipt_code=receipt_code)

    if request.method == 'POST':
        # User submitting their receipt
        txn_ref       = (request.POST.get('txn_reference') or '').strip()
        sender_phone  = (request.POST.get('sender_phone')  or '').strip()
        notes         = (request.POST.get('notes')         or '').strip()
        receipt_image = request.FILES.get('receipt_image')

        if not txn_ref or len(txn_ref) < 4:
            return JsonResponse({'error': 'رقم العملية مطلوب (4 حروف على الأقل).'}, status=400)
        if not sender_phone or len(sender_phone) < 10:
            return JsonResponse({'error': 'رقم المرسل مطلوب.'}, status=400)
        if not receipt_image:
            return JsonResponse({'error': 'لازم ترفع صورة سكرين شوت التحويل.'}, status=400)
        if receipt_image.size > 5 * 1024 * 1024:
            return JsonResponse({'error': 'حجم الصورة لازم أقل من 5 ميجا.'}, status=400)
        if not (receipt_image.content_type or '').startswith('image/'):
            return JsonResponse({'error': 'الملف لازم يكون صورة (PNG / JPG).'}, status=400)

        with transaction.atomic():
            receipt.txn_reference  = txn_ref[:200]
            receipt.sender_phone   = sender_phone[:20]
            receipt.receipt_image  = receipt_image
            if notes:
                receipt.notes = notes[:1000]
            # Status stays 'pending' — admin review activates it.
            receipt.save(update_fields=[
                'txn_reference', 'sender_phone', 'receipt_image', 'notes',
            ])

        logger.info("[MANUAL PAY] Receipt %s uploaded — type=%s id=%s amount=%s",
                    receipt.receipt_code, receipt.purchase_type,
                    receipt.purchase_id, receipt.amount)

        return JsonResponse({
            'ok': True,
            'message': '✅ تم استلام الإيصال — هيتم التفعيل خلال دقائق بعد التأكيد.',
            'redirect': '/marketplace/' if receipt.customer_id else '/',
        })

    # GET — render the upload page
    purchase = receipt.get_purchase_object()
    return render(request, 'clients/manual_payment_upload.html', {
        'receipt':            receipt,
        'purchase':           purchase,
        'vodafone_number':    VODAFONE_CASH_NUMBER,
        'instapay_handle':    INSTAPAY_HANDLE,
        'amount_display':     f"{receipt.amount:.2f}",
    })


# ─────────────────────────────────────────────────────────────────────────────
# 2) Initiator endpoints — one per purchase type. Each creates the
#    underlying record + the ManualPaymentReceipt, then redirects to upload.
# ─────────────────────────────────────────────────────────────────────────────
def manual_pay_subscription_start(request):
    """
    🏢 بداية دفع اشتراك SaaS بفودافون كاش.

    POST: plan (legacy slug), billing_period, shop (schema_name), amount
    """
    if request.method != 'POST':
        return redirect('saas_pricing')

    plan_slug      = (request.POST.get('plan') or '').strip()
    shop           = (request.POST.get('shop') or '').strip()
    billing_period = (request.POST.get('billing_period') or 'monthly').strip()
    payment_method = (request.POST.get('payment_method') or 'vodafone_cash').strip()
    amount_raw     = request.POST.get('amount') or '0'

    if payment_method not in ('vodafone_cash', 'instapay'):
        return redirect('saas_pricing')

    try:
        amount = Decimal(str(amount_raw))
        if amount <= 0:
            raise ValueError
    except Exception:
        messages.error(request, "المبلغ غير صحيح.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

    tenant = Client.objects.filter(schema_name=shop).first() if shop else None
    if not tenant:
        messages.error(request, "لم يتم العثور على المتجر.")
        return redirect('saas_pricing')

    # Resolve plan
    from clients.services.plan_mapping import resolve_plan_slug
    target_slug = resolve_plan_slug(plan_slug)
    plan_obj = Plan.objects.filter(slug=target_slug).first() if target_slug else None
    if not plan_obj:
        messages.error(request, "الباقة المختارة غير موجودة.")
        return redirect(reverse('saas_pricing') + f'?shop={shop}')

    months_map = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}
    days_map   = {'monthly': 30, 'quarterly': 90, 'semi_annual': 180, 'annual': 365}
    months = months_map.get(billing_period, 1)
    days   = days_map.get(billing_period, 30)

    subtotal = (plan_obj.monthly_price * months).quantize(Decimal('0.01'))
    total    = plan_obj.price_for_period(months)
    discount = (subtotal - total).quantize(Decimal('0.01'))
    discount_pct = int(round((discount / subtotal) * 100)) if subtotal else 0

    with transaction.atomic():
        # Create pending subscription + invoice
        sub, _ = TenantSubscription.objects.get_or_create(
            tenant=tenant, defaults={'plan': plan_obj, 'is_active': False},
        )
        revision = PlanRevision.objects.filter(plan=plan_obj).order_by('-effective_from').first()
        if revision is None:
            revision = PlanRevision.create_from_plan(
                plan_obj, change_reason='Auto-created on manual Vodafone payment',
            )
        period_start = max(tenant.subscription_end_date or timezone.localdate(), timezone.localdate())
        period_end = period_start + timezone.timedelta(days=days)

        invoice = PlatformInvoice.objects.create(
            tenant=tenant, subscription=sub, plan_revision=revision,
            period_start=period_start, period_end=period_end,
            billing_cycle_months=months,
            subtotal=subtotal,
            discount_percent=discount_pct,
            discount_amount=discount,
            total=total,
            entitlements_snapshot=dict(plan_obj.entitlements or {}),
            status='issued',
            payment_provider=payment_method,
        )

        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='subscription',
            purchase_id=invoice.pk,
            amount=total,
            payment_method=payment_method,
            tenant=tenant,
            contact_name=tenant.owner_name or tenant.name,
            contact_phone=tenant.phone or '',
            sender_phone='', txn_reference='',  # to be filled at upload time
        )

    return redirect(reverse('manual_payment_upload', args=[receipt.receipt_code]))


def manual_pay_parts_start(request, listing_code):
    """🚗 بداية دفع قطعة غيار بفودافون كاش (مع escrow بعد التأكيد)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)

    listing = get_object_or_404(PartListing, listing_code=listing_code)
    if listing.status != 'active':
        return JsonResponse({'error': 'القطعة لم تعد متاحة.'}, status=400)
    if listing.seller_customer_id == customer.pk:
        return JsonResponse({'error': 'لا يمكنك شراء قطعتك الخاصة.'}, status=400)

    shipping_name    = (request.POST.get('shipping_name')    or customer.full_name).strip()[:120]
    shipping_phone   = (request.POST.get('shipping_phone')   or customer.phone).strip()[:30]
    shipping_address = (request.POST.get('shipping_address') or '').strip()
    shipping_city    = (request.POST.get('shipping_city')    or customer.city or '').strip()[:80]

    if not shipping_address or len(shipping_address) < 10:
        return JsonResponse({'error': 'لازم تكتب العنوان كاملاً.'}, status=400)

    with transaction.atomic():
        listing = PartListing.objects.select_for_update().get(pk=listing.pk)
        if listing.status != 'active':
            return JsonResponse({'error': 'القطعة محجوزة الآن.'}, status=400)
        listing.status = 'reserved'
        listing.save(update_fields=['status'])

        order = PartOrder.objects.create(
            listing=listing, buyer_customer=customer,
            amount_paid=listing.price_egp,
            commission_amount=listing.commission_amount,
            seller_payout=listing.seller_payout,
            warranty_days=listing.warranty_days,
            status='pending_payment',
            shipping_name=shipping_name,
            shipping_phone=shipping_phone,
            shipping_address=shipping_address,
            shipping_city=shipping_city,
        )

        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='parts',
            purchase_id=order.pk,
            amount=order.amount_paid,
            payment_method='vodafone_cash',
            customer=customer,
            contact_name=customer.full_name,
            contact_phone=customer.phone or '',
            sender_phone='', txn_reference='',
        )

    return JsonResponse({
        'ok': True,
        'redirect': reverse('manual_payment_upload', args=[receipt.receipt_code]),
    })


def manual_pay_design_start(request, package_slug):
    """🎨 بداية شراء باقة تصاميم بفودافون كاش."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)

    package = get_object_or_404(DesignPackage, slug=package_slug, is_active=True)
    payment_method = (request.POST.get('payment_method') or 'vodafone_cash').strip()
    if payment_method not in ('vodafone_cash', 'instapay'):
        return JsonResponse({'error': 'طريقة دفع غير صالحة.'}, status=400)

    is_designer = (customer.sector == 'printing' and
                   any(kw in (customer.job_title or '').lower()
                       for kw in ('مصمم', 'design', 'جرافيك', 'graphic', 'فنان')))
    designs_count = (package.designer_designs_count or package.designs_count) if is_designer else package.designs_count

    with transaction.atomic():
        purchase = DesignPurchase.objects.create(
            customer=customer, package=package,
            designs_total=designs_count,
            price_paid=package.price_egp,
            payment_method=payment_method,
            status='pending',
        )
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='design',
            purchase_id=purchase.pk,
            amount=package.price_egp,
            payment_method=payment_method,
            customer=customer,
            contact_name=customer.full_name,
            contact_phone=customer.phone or '',
            sender_phone='', txn_reference='',
        )

    return JsonResponse({
        'ok': True,
        'redirect': reverse('manual_payment_upload', args=[receipt.receipt_code]),
    })


@login_required
def diag_topup_purchase(request):
    """🔍 صفحة شراء حزمة شحن التشخيص (للورش).

    GET → يعرض الحزم النشطة + الرصيد الحالي + الحصة الشهرية المتبقية.
    POST → يبني ManualPaymentReceipt على الحزمة المختارة ويوجّه لشاشة رفع
    سكرين شوت التحويل (نفس flow اشتراك SaaS).
    """
    from clients.models import DiagnosticsTopUpPack
    from clients.services.diagnostics_quota import check_quota

    tenant = getattr(request, 'tenant', None)
    if tenant is None:
        return HttpResponseForbidden('متاحة فقط من نطاق الشركة (tenant).')

    packs = list(DiagnosticsTopUpPack.objects.filter(is_active=True).order_by('sort_order', 'price_egp'))

    if request.method == 'POST':
        slug = request.POST.get('pack_slug') or ''
        method = request.POST.get('payment_method') or 'vodafone_cash'
        if method not in ('vodafone_cash', 'instapay'):
            return JsonResponse({'error': 'طريقة دفع غير صالحة.'}, status=400)
        pack = next((p for p in packs if p.slug == slug), None)
        if pack is None:
            return JsonResponse({'error': 'الحزمة غير موجودة.'}, status=400)

        with transaction.atomic():
            receipt = ManualPaymentReceipt.objects.create(
                purchase_type='diag_topup',
                purchase_id=pack.pk,
                amount=pack.price_egp,
                payment_method=method,
                tenant=tenant,
                contact_name=getattr(tenant, 'owner_name', '') or tenant.name,
                contact_phone=getattr(tenant, 'phone', '') or '',
                notes=f'topup={pack.uses_granted}',
                sender_phone='', txn_reference='',
            )
        return redirect(reverse('manual_payment_upload', args=[receipt.receipt_code]))

    # Surface the current scan + bot status so the merchant can see why they
    # need the top-up before they pay.
    scan_status = check_quota(tenant, kind='scan')
    bot_status = check_quota(tenant, kind='bot')

    return render(request, 'clients/diag_topup_purchase.html', {
        'tenant': tenant,
        'packs': packs,
        'scan_status': scan_status,
        'bot_status': bot_status,
        'vodafone_number': VODAFONE_CASH_NUMBER,
        'instapay_handle': INSTAPAY_HANDLE,
    })


def manual_pay_diagnostics_start(request, tier):
    """🔧 بداية ترقية باقة Diagnostics بفودافون كاش."""
    from clients.models import CustomerDiagnosticsSubscription
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'سجل دخول أولاً.'}, status=401)
    if tier not in ('basic', 'pro', 'empire'):
        return JsonResponse({'error': 'باقة غير صالحة.'}, status=400)

    price = CustomerDiagnosticsSubscription.TIER_PRICES_EGP.get(tier)
    if not price:
        return JsonResponse({'error': 'لا يوجد سعر لهذه الباقة.'}, status=400)

    sub = CustomerDiagnosticsSubscription.objects.filter(customer=customer).first()
    if not sub:
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)

    with transaction.atomic():
        receipt = ManualPaymentReceipt.objects.create(
            purchase_type='diagnostics',
            purchase_id=sub.pk,
            amount=Decimal(str(price)),
            payment_method='vodafone_cash',
            customer=customer,
            contact_name=customer.full_name,
            contact_phone=customer.phone or '',
            notes=tier,  # save tier in notes for confirm() to pick up
            sender_phone='', txn_reference='',
        )

    if request.method == 'POST':
        return JsonResponse({
            'ok': True,
            'redirect': reverse('manual_payment_upload', args=[receipt.receipt_code]),
        })
    return redirect(reverse('manual_payment_upload', args=[receipt.receipt_code]))


# ─────────────────────────────────────────────────────────────────────────────
# 3) Super-admin review actions (confirm / reject)
# ─────────────────────────────────────────────────────────────────────────────
@login_required(login_url='/secure-portal/login/')
def admin_review_receipt(request, receipt_code):
    """✅ Super admin يوافق أو يرفض إيصال يدوي."""
    if not request.user.is_superuser:
        return HttpResponseForbidden("Super admin only.")
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    receipt = get_object_or_404(ManualPaymentReceipt, receipt_code=receipt_code)
    action  = (request.POST.get('action') or '').strip()
    notes   = (request.POST.get('notes')  or '').strip()

    if action == 'confirm':
        receipt.confirm(by_user=request.user, notes=notes)
        logger.info("[MANUAL PAY] Receipt %s CONFIRMED by %s", receipt.receipt_code, request.user)
        return JsonResponse({
            'ok': True,
            'message': '✅ تم التأكيد وتفعيل الخدمة للعميل.',
            'status': 'confirmed',
        })
    elif action == 'reject':
        receipt.reject(by_user=request.user, notes=notes)
        logger.info("[MANUAL PAY] Receipt %s REJECTED by %s", receipt.receipt_code, request.user)
        return JsonResponse({
            'ok': True,
            'message': 'تم رفض الإيصال.',
            'status': 'rejected',
        })
    return JsonResponse({'error': 'Invalid action'}, status=400)
