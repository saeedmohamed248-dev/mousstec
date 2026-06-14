"""
🛍️ Design Store — marketplace customer AI-design endpoints.

All 14 design-store endpoints (browse, buy, generate, regenerate, refine,
download, watermark, send-to-print, chat history, send-to-marketplace).
Heavy AI lifting is delegated to ``_ai_pipeline._run_marketplace_image_pipeline``
so C1/C2/C3 all share the unified Brand + Smart Router + Composite +
Quality-Gate pipeline.

Extracted from ``_legacy.py`` (Step 4 of the incremental split). The
package facade (``clients/views/__init__.py``) preserves the public URL
surface — ``erp_core/urls.py`` continues to reference ``client_views.<name>``
unchanged.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from clients.models import (
    CustomerDesign,
    DesignPackage,
    DesignPurchase,
    MarketplaceCustomer,
)

from .._ai_pipeline import (
    _composite_brand_logo,
    _persist_remote_image,
    _resolve_brand_context,
    _resolve_quality_size,
    _run_marketplace_image_pipeline,
    _upscale_local_image,
)
from .._shared import (
    _build_customer_topup_cards,
    _marketplace_auth,
)

logger = logging.getLogger('mouss_tec_core')



# Post-generation actions: share, download, print, marketplace, watermark.

from .navigation import *  # noqa: F401, F403



@csrf_exempt
def design_store_send_whatsapp(request, design_code):
    """📱 إرسال التصميم للعميل على واتساب."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    # 🎯 Package gate — only packs that bought WhatsApp delivery
    pkg = design.purchase.package if design.purchase_id else None
    if pkg is not None and not getattr(pkg, 'allows_whatsapp_delivery', True):
        return JsonResponse({
            "error": "إرسال الواتساب غير متاح في باقتك.",
            "code": "whatsapp_not_in_package",
        }, status=403)

    target_phone = request.POST.get('phone', customer.phone).strip()
    custom_message = request.POST.get('message', '').strip()

    if not target_phone:
        return JsonResponse({"error": "رقم الواتساب مطلوب"}, status=400)

    # Build wa.me deep link
    from urllib.parse import quote
    msg = custom_message or f"تصميمك من Mouss Tec AI Store جاهز!\n\nالعنوان: {design.title}\nالمقاس: {design.actual_size_label}\n\n{design.image_url}"
    phone_clean = target_phone.lstrip('+').lstrip('0')
    if not phone_clean.startswith('20'):
        phone_clean = '20' + phone_clean
    wa_url = f"https://wa.me/{phone_clean}?text={quote(msg)}"

    # Mark as sent
    design.sent_to_whatsapp = target_phone
    design.sent_at = timezone.now()
    design.save(update_fields=['sent_to_whatsapp', 'sent_at'])

    return JsonResponse({"status": "success", "whatsapp_url": wa_url})


def design_store_download(request, design_code, fmt):
    """📥 تحميل التصميم بصيغ مختلفة (png, jpg, pdf)."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "التحميل غير متاح في التجربة المجانية"}, status=403)

    fmt = fmt.lower()
    if fmt not in ('png', 'jpg', 'jpeg', 'pdf', 'source'):
        return JsonResponse({"error": "صيغة غير مدعومة. الصيغ المتاحة: png, jpg, pdf, source"}, status=400)

    # 🎯 'source' (PNG عالي الدقة + SVG wrapper) متاح فقط لباقات allows_source_files
    if fmt == 'source':
        pkg = design.purchase.package if design.purchase_id else None
        if pkg is None or not getattr(pkg, 'allows_source_files', False):
            return JsonResponse({
                "error": "ملفات المصدر غير متاحة في باقتك. ترقّى لباقة المصممين.",
                "code": "source_not_in_package",
            }, status=403)

    import io
    from django.core.files.storage import default_storage

    # ── Step 1: Load image bytes ──────────────────────────────────
    img_data = None
    if design.image_url:
        url = design.image_url

        # Try local file first (extract path from URL)
        for prefix in ['/media/', 'media/']:
            if prefix in url:
                rel_path = url.split(prefix, 1)[-1]
                try:
                    if default_storage.exists(rel_path):
                        with default_storage.open(rel_path, 'rb') as f:
                            img_data = f.read()
                        break
                except Exception:
                    pass

        # Fallback: download from URL
        if not img_data:
            try:
                import requests as _req
                r = _req.get(url, timeout=30)
                if r.status_code == 200:
                    img_data = r.content
            except Exception as e:
                logger.error(f"[DOWNLOAD] Failed to fetch image: {e}")

    if not img_data:
        return JsonResponse({"error": "تعذر تحميل الصورة — الملف غير موجود"}, status=404)

    # ── Step 2: For PNG, serve raw (no conversion needed) ─────────
    if fmt == 'png':
        # Even if source is WebP, convert to actual PNG
        try:
            from PIL import Image as PILImage
            img = PILImage.open(io.BytesIO(img_data))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            img_data = buf.getvalue()
        except Exception:
            pass  # Serve raw bytes if PIL fails
        response = HttpResponse(img_data, content_type='image/png')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.png"'
        design.download_count += 1
        design.save(update_fields=['download_count'])
        return response

    # ── Step 3: Convert to JPG or PDF ────────────────────────────
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(img_data))
    except Exception as e:
        logger.error(f"[DOWNLOAD] PIL cannot open image: {e}")
        return JsonResponse({"error": "تعذر فتح الصورة للتحويل"}, status=500)

    # Convert to RGB for JPG/PDF (remove alpha channel)
    if img.mode in ('RGBA', 'P', 'LA', 'PA'):
        bg = PILImage.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        try:
            bg.paste(img, mask=img.split()[-1] if 'A' in img.mode else None)
        except Exception:
            bg.paste(img)
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    buf = io.BytesIO()

    if fmt in ('jpg', 'jpeg'):
        img.save(buf, format='JPEG', quality=95)
        response = HttpResponse(buf.getvalue(), content_type='image/jpeg')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.jpg"'
    elif fmt == 'pdf':
        img.save(buf, format='PDF', resolution=300)
        response = HttpResponse(buf.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.pdf"'
    elif fmt == 'source':
        # 🎨 ZIP فيه: PNG عالي الدقة (max quality) + SVG wrapper
        # المصمم يقدر يفتحه في Illustrator/Affinity ويضيف vector layers فوقه.
        import base64 as _b64
        import zipfile
        png_buf = io.BytesIO()
        img.save(png_buf, format='PNG', compress_level=1)
        png_bytes = png_buf.getvalue()
        w, h = img.size
        b64 = _b64.b64encode(png_bytes).decode('ascii')
        svg = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}" height="{h}" viewBox="0 0 {w} {h}">\n'
            f'  <image x="0" y="0" width="{w}" height="{h}" '
            f'xlink:href="data:image/png;base64,{b64}"/>\n'
            f'</svg>\n'
        )
        # License text reflects whatever the package paid for.
        pkg_src = design.purchase.package if design.purchase_id else None
        commercial = bool(pkg_src and getattr(pkg_src, 'allows_commercial_use', False))
        license_text = (
            f'Mouss Tec — Design License\n'
            f'Design code: {design.design_code}\n'
            f'Customer: {customer.full_name or customer.email or customer.uid}\n'
            f'Package: {getattr(pkg_src, "name_ar", "—")}\n'
            f'Issued: {timezone.now().date().isoformat()}\n\n'
        )
        if commercial:
            license_text += (
                'Grant: Full commercial use. The customer may reproduce, sell,\n'
                'and incorporate this design into client work and paid projects\n'
                'without further royalties.\n'
            )
        else:
            license_text += (
                'Grant: Personal use only. Commercial reproduction or resale\n'
                'is not permitted under this package.\n'
            )

        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f'design_{design.design_code}.png', png_bytes)
            zf.writestr(f'design_{design.design_code}.svg', svg.encode('utf-8'))
            zf.writestr('LICENSE.txt', license_text.encode('utf-8'))
            zf.writestr(
                'README.txt',
                'Mouss Tec — Source files bundle\n'
                'Open the SVG in Illustrator / Affinity Designer to add\n'
                'vector layers (text, shapes, masks) on top of the base art.\n'
                'PNG is the highest-resolution raster export of your design.\n'
                'LICENSE.txt covers the usage rights for your package.\n',
            )
        response = HttpResponse(zbuf.getvalue(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}_source.zip"'

    design.download_count += 1
    design.save(update_fields=['download_count'])
    return response


@csrf_exempt
def design_store_print_request(request, design_code):
    """🖨️ طلب طباعة تصميم — العميل عجبه التصميم وعاوز يطبعه."""
    from clients.models import DesignPrintRequest

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    product_type = request.POST.get('product_type', 'other')
    quantity = request.POST.get('quantity', '1')
    width_cm = request.POST.get('width_cm', '').strip()
    height_cm = request.POST.get('height_cm', '').strip()
    paper_type = request.POST.get('paper_type', '').strip()
    color_mode = request.POST.get('color_mode', 'full_color')
    finishing = request.POST.get('finishing', '').strip()
    notes = request.POST.get('notes', '').strip()
    delivery_address = request.POST.get('delivery_address', '').strip()
    delivery_phone = request.POST.get('delivery_phone', '').strip()

    try:
        qty = int(quantity)
        if qty < 1:
            qty = 1
    except (ValueError, TypeError):
        qty = 1

    print_req = DesignPrintRequest.objects.create(
        design=design,
        customer=customer,
        product_type=product_type,
        quantity=qty,
        width_cm=Decimal(width_cm) if width_cm else None,
        height_cm=Decimal(height_cm) if height_cm else None,
        paper_type=paper_type,
        color_mode=color_mode,
        finishing=finishing,
        notes=notes,
        delivery_address=delivery_address,
        delivery_phone=delivery_phone or customer.phone,
        status='pending',
    )

    logger.info(f"[PRINT REQUEST] #{print_req.pk} — {customer.full_name} wants to print design {design.design_code}")

    return JsonResponse({
        "status": "success",
        "request_id": print_req.pk,
        "request_code": str(print_req.request_code),
        "message": "تم إرسال طلب الطباعة بنجاح! سنتواصل معك قريباً بعرض السعر.",
    })


@csrf_exempt
def design_store_send_to_marketplace(request, design_code):
    """🛒 إرسال التصميم لسوق B2B — ينشئ ServiceRequest للتجار (المطابع) يقدموا عروض."""
    from clients.models import ServiceRequest

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "هذه الميزة غير متاحة في التجربة المجانية"}, status=403)

    # Check if already sent to marketplace
    existing = ServiceRequest.objects.filter(
        customer=customer,
        title__contains=str(design.design_code)[:8],
        status='open',
    ).first()
    if existing:
        return JsonResponse({
            "status": "already_exists",
            "request_code": str(existing.request_code),
            "message": "التصميم موجود بالفعل في السوق وبيستقبل عروض.",
        })

    notes = request.POST.get('notes', '').strip()
    quantity = request.POST.get('quantity', '1').strip()
    urgency = request.POST.get('urgency', 'normal')

    try:
        qty = int(quantity)
        if qty < 1:
            qty = 1
    except (ValueError, TypeError):
        qty = 1

    # Build description for merchants
    desc = (
        f"طلب طباعة تصميم AI — {design.get_category_display()}\n"
        f"المقاس: {design.actual_size_label}\n"
        f"الكمية: {qty}\n"
    )
    if notes:
        desc += f"ملاحظات العميل: {notes}\n"
    desc += f"\nرابط التصميم: {design.image_url}"

    # Create ServiceRequest in B2B marketplace
    from datetime import timedelta
    sr = ServiceRequest.objects.create(
        customer=customer,
        sector='printing',
        title=f"طباعة {design.get_category_display()} — {design.title[:60]} [{str(design.design_code)[:8]}]",
        description=desc,
        urgency=urgency if urgency in ('normal', 'soon', 'urgent') else 'normal',
        customer_city=customer.city or '',
        expires_at=timezone.now() + timedelta(days=7),
    )

    # Attach design image as reference
    if design.image_url:
        try:
            import requests as _req
            from django.core.files.base import ContentFile
            r = _req.get(design.image_url, timeout=15)
            if r.status_code == 200:
                from django.core.files.uploadedfile import InMemoryUploadedFile
                import io
                sr.attachment_1.save(
                    f"design_{design.design_code}.png",
                    ContentFile(r.content),
                    save=True,
                )
        except Exception as e:
            logger.warning(f"[MARKETPLACE] Failed to attach design image: {e}")

    logger.info(f"[MARKETPLACE] Design {design.design_code} sent to B2B by {customer.full_name}")

    return JsonResponse({
        "status": "success",
        "request_code": str(sr.request_code),
        "message": f"تم نشر تصميمك في سوق الطباعة. المطابع هتبدأ تبعتلك عروض أسعار قريباً.",
    })


@csrf_exempt
def design_store_watermark(request, design_code):
    """💧 إضافة / إزالة علامة مائية على التصميم."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "العلامة المائية غير متاحة في التجربة المجانية"}, status=403)

    # 🎯 Package gate — only packs that bought the "custom watermark" feature
    pkg = design.purchase.package if design.purchase_id else None
    if pkg is not None and not getattr(pkg, 'allows_watermark', False):
        return JsonResponse({
            "error": "العلامة المائية المخصصة غير متاحة في باقتك. ترقّى لباقة أعلى.",
            "code": "watermark_not_in_package",
        }, status=403)

    watermark_text = request.POST.get('text', customer.company_name or customer.full_name).strip()
    if not watermark_text:
        watermark_text = 'Mouss Tec AI Design'

    # Get the original image
    from django.core.files.storage import default_storage
    import io
    from PIL import Image as PILImage, ImageDraw, ImageFont

    img_data = None
    # Try local storage first
    if design.image_url:
        url = design.image_url
        for prefix in ['/media/', 'media/']:
            if prefix in url:
                rel_path = url.split(prefix, 1)[-1]
                if default_storage.exists(rel_path):
                    with default_storage.open(rel_path, 'rb') as f:
                        img_data = f.read()
                break

    if not img_data:
        try:
            import requests as _req
            r = _req.get(design.image_url, timeout=30)
            if r.status_code == 200:
                img_data = r.content
        except Exception:
            pass

    if not img_data:
        return JsonResponse({"error": "تعذر تحميل الصورة"}, status=404)

    # Apply watermark
    img = PILImage.open(io.BytesIO(img_data)).convert('RGBA')
    w, h = img.size

    # Create transparent overlay
    overlay = PILImage.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Use a large font size relative to image
    font_size = max(w, h) // 15
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Draw diagonal watermark text multiple times across image
    import math
    diagonal = int(math.sqrt(w**2 + h**2))
    step_y = font_size * 3

    for y_offset in range(-diagonal, diagonal, step_y):
        for x_offset in range(-w, w * 2, len(watermark_text) * font_size):
            draw.text(
                (x_offset, y_offset),
                watermark_text,
                font=font,
                fill=(255, 255, 255, 45),  # Semi-transparent white
            )

    # Rotate overlay
    overlay = overlay.rotate(30, expand=False, center=(w // 2, h // 2))

    # Composite
    watermarked = PILImage.alpha_composite(img, overlay)
    watermarked_rgb = watermarked.convert('RGB')

    # Save watermarked version
    import uuid as _uuid
    from django.core.files.base import ContentFile
    buf = io.BytesIO()
    watermarked_rgb.save(buf, format='PNG', quality=95)
    buf.seek(0)

    filename = f"ai_store/{customer.uid}/wm_{_uuid.uuid4().hex}.png"
    saved_path = default_storage.save(filename, ContentFile(buf.getvalue()))
    wm_url = default_storage.url(saved_path)
    if wm_url.startswith('/'):
        wm_url = request.build_absolute_uri(wm_url)

    return JsonResponse({
        "status": "success",
        "watermarked_url": wm_url,
        "message": "تم إضافة العلامة المائية بنجاح",
    })
