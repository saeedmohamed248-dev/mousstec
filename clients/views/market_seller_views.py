"""
🏪 لوحة بائع السوق — مطبعة أو محل قطع غيار بيعرض منتجاته ويتابع عملاءه بس.

مالهاش أي علاقة ببرنامج الإدارة (فواتير/مخزون/حسابات): مفيش Tenant ولا schema.
* تسجيل خفيف: اسم المتجر + المسؤول + الموبايل + كلمة سر + القطاع ⇒ متجر «تحت المراجعة».
* منتجاتي: إضافة/تعديل/إخفاء + رفع شيت Excel — كل منتج جديد أو متعدّل بيستنى مراجعة Mouss Tec.
* عملائي: طلبات الأسعار على منتجاته — بيكلّم العميل مباشرة ويغيّر حالة الطلب.
* السوق المفتوح: طلبات العملاء في قطاعه + يقدّم عرض سعر.
* ترقية اختيارية لبرنامج الإدارة (التسجيل العادي وهو داخل بحسابه ⇒ الشركة بتتربط بيه).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from clients.models import (
    CatalogItem,
    CatalogQuoteRequest,
    CatalogStore,
    MarketSeller,
    ServiceRequest,
    TenderOffer,
)
from clients.services import market_seller as svc

from .catalog_views import SECTORS, _categories, _category_label

logger = logging.getLogger('mouss_tec_core')

SIGNUPS_PER_IP_HOUR = 5
LOGIN_FAILS_PER_PHONE = 10
IMPORT_MAX_ROWS = 300
IMAGE_MAX = 8 * 1024 * 1024
# حالات طلب السعر اللي البائع يقدر يحطها (نفس حالات الطلب في الكتالوج)
SELLER_STATUSES = ('new', 'contacted', 'quoted', 'done', 'cancelled')


# ─────────────────────────── أدوات ───────────────────────────

def _seller_required(view):
    def wrapper(request, *args, **kwargs):
        seller = svc.seller_from_request(request)
        if seller is None:
            return redirect(f'/marketplace/seller/login/?next={request.path}')
        request.seller = seller
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def _safe_next(value):
    value = (value or '').strip()
    if value.startswith('/marketplace/seller/') and '//' not in value and '\\' not in value:
        return value
    return '/marketplace/seller/'


def _hit(key, limit, window):
    """True لو العدد وصل للحد. بيزوّد العداد."""
    n = cache.get(key, 0)
    if n >= limit:
        return True
    cache.set(key, n + 1, window)
    return False


def _unique_slug(name, sector):
    base = slugify(name, allow_unicode=True)[:60] or sector
    slug = base
    while CatalogStore.objects.filter(slug=slug).exists():
        slug = f'{base}-{uuid.uuid4().hex[:5]}'
    return slug


def _ctx(request, active, **extra):
    seller = request.seller
    store = seller.store
    pending = store.items.filter(pending_review=True).count()
    new_quotes = CatalogQuoteRequest.objects.filter(item__store=store, status='new').count()
    return {'seller': seller, 'store': store, 'sector': store.sector, 'meta': SECTORS[store.sector],
            'active': active, 'pending_count': pending, 'new_quotes': new_quotes, **extra}


# ─────────────────────────── التسجيل والدخول ───────────────────────────

class SellerSignupForm(forms.Form):
    store_name = forms.CharField(max_length=120, label=_l('اسم المطبعة أو المحل'))
    owner_name = forms.CharField(max_length=120, label=_l('اسم المسؤول'))
    phone = forms.CharField(max_length=30, label=_l('رقم الموبايل'))
    password = forms.CharField(min_length=6, max_length=128, widget=forms.PasswordInput, label=_l('كلمة السر'))
    sector = forms.ChoiceField(choices=(('printing', _l('مطبعة / تصميم')), ('automotive', _l('قطع غيار سيارات'))),
                               label=_l('النشاط'))
    city = forms.CharField(max_length=60, label=_l('المحافظة / المدينة'))
    email = forms.EmailField(required=False, label=_l('الإيميل (اختياري)'))
    tagline = forms.CharField(max_length=200, required=False, label=_l('جملة عن شغلك (اختياري)'))

    def clean_store_name(self):
        v = ' '.join(self.cleaned_data['store_name'].split())
        if len(v) < 3:
            raise forms.ValidationError(_('اكتب اسم المتجر كامل.'))
        return v

    def clean_phone(self):
        phone = svc.normalize_phone(self.cleaned_data['phone'])
        if not phone:
            raise forms.ValidationError(_('رقم الموبايل مش مظبوط — اكتبه زي 01012345678.'))
        if MarketSeller.objects.filter(phone=phone).exists():
            raise forms.ValidationError(_('الرقم ده عنده حساب بائع — ادخل بيه.'))
        return phone


def seller_join(request):
    """📝 تسجيل بائع جديد — متجر في السوق بس، من غير برنامج الإدارة."""
    if svc.seller_from_request(request):
        return redirect('/marketplace/seller/')
    initial_sector = request.GET.get('sector') if request.GET.get('sector') in SECTORS else 'printing'
    form = SellerSignupForm(request.POST or None, initial={'sector': initial_sector})
    if request.method == 'POST':
        from erp_core.client_ip import client_ip
        if _hit(f'seller_join:{client_ip(request) or "x"}', SIGNUPS_PER_IP_HOUR, 3600):
            form.add_error(None, _('محاولات تسجيل كتير من نفس الجهاز — جرّب بعد ساعة.'))
        elif form.is_valid():
            d = form.cleaned_data
            try:
                with transaction.atomic():
                    meta = SECTORS[d['sector']]
                    store = CatalogStore.objects.create(
                        sector=d['sector'], name=d['store_name'], slug=_unique_slug(d['store_name'], d['sector']),
                        tagline=d['tagline'], city=d['city'], icon=meta['icon'], color=meta['color'],
                        is_active=False,   # بيظهر للعملاء بعد مراجعة Mouss Tec
                        sort=1000,
                    )
                    seller = MarketSeller(store=store, owner_name=d['owner_name'], phone=d['phone'],
                                          email=d['email'] or '', last_login_at=timezone.now())
                    seller.set_password(d['password'])
                    seller.save()
            except IntegrityError:
                form.add_error('phone', _('الرقم ده عنده حساب بائع — ادخل بيه.'))
            else:
                from clients.services.owner_notify import notify_owner
                notify_owner(f'بائع جديد في السوق ({store.get_sector_display()})',
                             f'{store.name} — {seller.owner_name} — {seller.phone} — {store.city}\n'
                             'المتجر مستني موافقتك عشان يظهر للعملاء.',
                             f'/admin/clients/marketseller/{seller.pk}/change/')
                return svc.login_response(redirect('/marketplace/seller/?welcome=1'), seller)
    return render(request, 'clients/marketplace/seller/join.html', {'form': form, 'sector': initial_sector})


def seller_login(request):
    nxt = _safe_next(request.GET.get('next') or request.POST.get('next'))
    if svc.seller_from_request(request):
        return redirect(nxt)
    error = ''
    phone_raw = ''
    if request.method == 'POST':
        from erp_core.client_ip import client_ip
        phone_raw = (request.POST.get('phone') or '').strip()
        phone = svc.normalize_phone(phone_raw)
        fail_key = f'seller_login_fail:{phone}'
        if _hit(f'seller_login_ip:{client_ip(request) or "x"}', 20, 60):
            error = _('محاولات كتير — استنى دقيقة وجرّب تاني.')
        elif phone and cache.get(fail_key, 0) >= LOGIN_FAILS_PER_PHONE:
            error = _('محاولات كتير على الرقم ده — جرّب بعد ربع ساعة.')
        else:
            seller = MarketSeller.objects.filter(phone=phone).first() if phone else None
            if seller and not seller.is_blocked and seller.check_password(request.POST.get('password') or ''):
                cache.delete(fail_key)
                seller.session_token = uuid.uuid4()
                seller.last_login_at = timezone.now()
                seller.save(update_fields=['session_token', 'last_login_at'])
                return svc.login_response(redirect(nxt), seller)
            if seller and seller.is_blocked:
                error = _('الحساب ده موقوف — كلّم دعم Mouss Tec.')
            else:
                if phone:
                    cache.set(fail_key, cache.get(fail_key, 0) + 1, 900)
                error = _('رقم الموبايل أو كلمة السر غلط.')
    return render(request, 'clients/marketplace/seller/login.html',
                  {'error': error, 'phone': phone_raw, 'next': nxt})


@require_POST
def seller_logout(request):
    seller = svc.seller_from_request(request)
    if seller:
        MarketSeller.objects.filter(pk=seller.pk).update(session_token=uuid.uuid4())
    resp = redirect('/marketplace/sell/')
    resp.delete_cookie(svc.COOKIE)
    return resp


# ─────────────────────────── اللوحة ───────────────────────────

@_seller_required
def seller_dashboard(request):
    store = request.seller.store
    items = store.items.all()
    stats = {
        'live': items.filter(is_active=True).count(),
        'pending': items.filter(pending_review=True).count(),
        'quotes_30': CatalogQuoteRequest.objects.filter(
            item__store=store, created_at__gte=timezone.now() - timedelta(days=30)).count(),
        'open_requests': ServiceRequest.objects.filter(
            sector=store.sector, status='open', is_approved=True, expires_at__gt=timezone.now())
        .exclude(offers__seller=request.seller).count(),
        'accepted': TenderOffer.objects.filter(seller=request.seller, status='accepted').count(),
    }
    recent = (CatalogQuoteRequest.objects.filter(item__store=store).select_related('item')
              .order_by('-created_at')[:5])
    return render(request, 'clients/marketplace/seller/dashboard.html', _ctx(
        request, 'home', stats=stats, recent=recent, welcome=request.GET.get('welcome') == '1'))


# ─────────────────────────── منتجاتي ───────────────────────────

class SellerItemForm(forms.ModelForm):
    class Meta:
        model = CatalogItem
        fields = ('title', 'description', 'category', 'image', 'min_qty', 'price_from',
                  'car_make', 'car_models', 'part_number', 'condition')

    def __init__(self, *args, sector='printing', **kwargs):
        super().__init__(*args, **kwargs)
        self.sector = sector
        self.fields['category'] = forms.ChoiceField(
            choices=[(k, label) for k, label, _icon in _categories(SECTORS[sector])], label=_l('القسم'))
        self.fields['description'].required = False
        if sector != 'automotive':
            for f in ('car_make', 'car_models', 'part_number', 'condition'):
                del self.fields[f]
        self.fields['price_from'].label = _l('السعر يبدأ من (اختياري)')
        self.fields['price_from'].min_value = 0

    def clean_image(self):
        img = self.cleaned_data.get('image')
        if img and hasattr(img, 'size') and img.size > IMAGE_MAX:
            raise forms.ValidationError(_('الصورة أكبر من ٨ ميجا — صغّرها وجرّب تاني.'))
        return img

    def clean_price_from(self):
        v = self.cleaned_data.get('price_from')
        if v is not None and v < 0:
            raise forms.ValidationError(_('السعر مينفعش يبقى بالسالب.'))
        return v


@_seller_required
def seller_products(request):
    store = request.seller.store
    items = list(store.items.order_by('-pending_review', '-is_active', '-created_at'))
    meta = SECTORS[store.sector]
    for it in items:
        it.category_label = _category_label(meta, it.category)
    return render(request, 'clients/marketplace/seller/products.html', _ctx(
        request, 'products', items=items, imported=request.GET.get('imported'),
        saved=request.GET.get('saved') == '1'))


@_seller_required
def seller_product_edit(request, item_code=None):
    store = request.seller.store
    item = get_object_or_404(CatalogItem, item_code=item_code, store=store) if item_code else None
    form = SellerItemForm(request.POST or None, request.FILES or None, instance=item, sector=store.sector)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.store = store
        meta = SECTORS[store.sector]
        obj.icon = meta['categories'].get(obj.category, ('', 'fa-box'))[1]
        # 🛡️ أي إضافة أو تعديل ⇒ يختفي لحد ما Mouss Tec تراجعه
        obj.is_active = False
        obj.pending_review = True
        obj.save()
        _notify_owner_review(store, 1)
        return redirect('/marketplace/seller/products/?saved=1')
    return render(request, 'clients/marketplace/seller/product_form.html', _ctx(
        request, 'products', form=form, item=item))


@_seller_required
@require_POST
def seller_product_hide(request, item_code):
    """إخفاء منتج من السوق (أو إلغاء طلب مراجعته) — الإظهار تاني بيرجع للمراجعة."""
    item = get_object_or_404(CatalogItem, item_code=item_code, store=request.seller.store)
    if request.POST.get('action') == 'show':
        item.pending_review = True
        _notify_owner_review(request.seller.store, 1)
    else:
        item.pending_review = False
    item.is_active = False
    item.save(update_fields=['is_active', 'pending_review'])
    return redirect('/marketplace/seller/products/')


def _notify_owner_review(store, n):
    # إشعار واحد كل ساعة لكل متجر — مايغرّقش صاحب المنصة لو البائع بيضيف كتير ورا بعض
    if not cache.add(f'seller_review_ping:{store.pk}', 1, 3600):
        return
    from clients.services.owner_notify import notify_owner
    notify_owner(f'منتجات بائع مستنية مراجعة — {store.name}',
                 f'{store.name} ضاف/عدّل منتجات ومستنية موافقتك قبل ما تظهر للعملاء.',
                 f'/admin/clients/catalogitem/?pending_review__exact=1&store__id__exact={store.pk}')


def _cell(v):
    return ' '.join(str(v).split()) if v is not None else ''


def _read_sheet(upload):
    """صفوف الشيت (xlsx أو csv) كـ dicts بأسماء الأعمدة (أول صف)."""
    ext = os.path.splitext(upload.name or '')[1].lower()
    if ext == '.xlsx':
        from openpyxl import load_workbook
        ws = load_workbook(upload, read_only=True, data_only=True).active
        rows = ws.iter_rows(values_only=True)
    elif ext == '.csv':
        import csv
        import io
        text = upload.read().decode('utf-8-sig', errors='replace')
        rows = csv.reader(io.StringIO(text))
    else:
        raise ValueError(_('ابعت الشيت Excel (.xlsx) أو CSV.'))
    header = None
    for row in rows:
        cells = [_cell(c) for c in row]
        if header is None:
            if any(cells):
                header = [c.lower() for c in cells]
            continue
        if any(cells):
            yield dict(zip(header, cells))


# أسماء الأعمدة المقبولة (عربي أو إنجليزي)
_COLS = {
    'title': ('اسم المنتج', 'المنتج', 'الاسم', 'title', 'name', 'product'),
    'description': ('الوصف', 'description', 'details'),
    'category': ('القسم', 'category'),
    'min_qty': ('أقل كمية', 'اقل كمية', 'الكمية', 'min qty', 'min_qty', 'minimum'),
    'price_from': ('السعر', 'السعر يبدأ من', 'price', 'price_from'),
    'car_make': ('الماركة', 'make', 'car_make'),
    'car_models': ('الموديلات', 'models', 'car_models'),
    'part_number': ('رقم القطعة', 'part number', 'part_number'),
}


def _pick(row, key):
    for name in _COLS[key]:
        if row.get(name.lower()):
            return row[name.lower()]
    return ''


def _match_category(meta, raw):
    raw = (raw or '').strip().lower()
    for k, (ar, _icon) in meta['categories'].items():
        if raw in (k, ar.lower(), meta['categories_en'].get(k, '').lower()):
            return k
    return next(iter(meta['categories']))


@_seller_required
def seller_products_import(request):
    """📄 رفع شيت منتجات — كل صف منتج «مستني مراجعة»."""
    store = request.seller.store
    meta = SECTORS[store.sector]
    errors = []
    if request.method == 'POST':
        upload = request.FILES.get('sheet')
        if not upload:
            errors.append(_('اختار ملف الشيت.'))
        elif upload.size > 5 * 1024 * 1024:
            errors.append(_('الشيت أكبر من ٥ ميجا.'))
        else:
            try:
                new = []
                for row in _read_sheet(upload):
                    title = _pick(row, 'title')[:200]
                    if not title:
                        continue
                    try:
                        price = Decimal(_pick(row, 'price_from').replace(',', '')) if _pick(row, 'price_from') else None
                        if price is not None and (not price.is_finite() or price < 0):
                            price = None
                    except InvalidOperation:
                        price = None
                    cat = _match_category(meta, _pick(row, 'category'))
                    new.append(CatalogItem(
                        store=store, title=title, description=_pick(row, 'description')[:3000], category=cat,
                        icon=meta['categories'][cat][1], min_qty=_pick(row, 'min_qty')[:60], price_from=price,
                        car_make=_pick(row, 'car_make')[:60], car_models=_pick(row, 'car_models')[:200],
                        part_number=_pick(row, 'part_number')[:80],
                        is_active=False, pending_review=True,
                    ))
                    if len(new) >= IMPORT_MAX_ROWS:
                        break
            except ValueError as e:
                errors.append(str(e))
            except Exception:  # noqa: BLE001 — ملف بايظ
                logger.exception('[SELLER] sheet import failed for store %s', store.pk)
                errors.append(_('مقدرناش نقرا الشيت — اتأكد إنه Excel أو CSV وأول صف فيه أسماء الأعمدة.'))
            else:
                if not new:
                    errors.append(_('مالقيناش منتجات في الشيت — لازم عمود «اسم المنتج».'))
                else:
                    CatalogItem.objects.bulk_create(new)
                    _notify_owner_review(store, len(new))
                    return redirect(f'/marketplace/seller/products/?imported={len(new)}')
    return render(request, 'clients/marketplace/seller/import.html', _ctx(
        request, 'products', errors=errors, categories=_categories(meta), max_rows=IMPORT_MAX_ROWS))


# ─────────────────────────── عملائي (طلبات الأسعار على منتجاتي) ───────────────────────────

@_seller_required
def seller_customers(request):
    store = request.seller.store
    status = request.GET.get('status', '')
    qs = CatalogQuoteRequest.objects.filter(item__store=store).select_related('item').order_by('-created_at')
    if status in SELLER_STATUSES:
        qs = qs.filter(status=status)
    from django.core.paginator import Paginator
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    from clients.services import catalog_request_desk as desk
    for r in page.object_list:
        r.wa = desk.wa_digits(r.phone)
        r.review_url = (request.build_absolute_uri(f'/marketplace/catalog/review/{r.review_token}/')
                        if r.review_token else '')
    counts = dict(CatalogQuoteRequest.objects.filter(item__store=store)
                  .values_list('status').annotate(n=Count('id')))
    statuses = [(k, label, counts.get(k, 0)) for k, label in CatalogQuoteRequest.STATUS_CHOICES]
    return render(request, 'clients/marketplace/seller/customers.html', _ctx(
        request, 'customers', page=page, status=status, statuses=statuses))


@_seller_required
@require_POST
def seller_customer_update(request, request_code):
    req = get_object_or_404(CatalogQuoteRequest, request_code=request_code, item__store=request.seller.store)
    status = request.POST.get('status', '')
    fields = ['updated_at']
    if status in SELLER_STATUSES:
        req.status = status
        fields.append('status')
        if status == 'done' and not req.review_token:
            req.review_token = uuid.uuid4()   # ⭐ لينك تقييم للعميل (مرة واحدة)
            fields.append('review_token')
    req.save(update_fields=fields)
    return redirect(request.POST.get('next') if (request.POST.get('next') or '').startswith(
        '/marketplace/seller/customers/') else '/marketplace/seller/customers/')


# ─────────────────────────── السوق المفتوح ───────────────────────────

@_seller_required
def seller_requests(request):
    seller = request.seller
    from .marketplace_core_views import _expire_overdue_requests
    _expire_overdue_requests()
    offered = set(TenderOffer.objects.filter(seller=seller).values_list('service_request_id', flat=True))
    open_qs = (ServiceRequest.objects.filter(sector=seller.store.sector, status='open', is_approved=True,
                                             expires_at__gt=timezone.now())
               .exclude(id__in=offered).select_related('customer').order_by('-created_at'))
    my_offers = (TenderOffer.objects.filter(seller=seller)
                 .select_related('service_request', 'service_request__customer').order_by('-created_at')[:30])
    return render(request, 'clients/marketplace/seller/requests.html', _ctx(
        request, 'requests', requests=open_qs[:50], my_offers=my_offers,
        focus=request.GET.get('req', ''), sent=request.GET.get('sent') == '1',
        error=request.GET.get('error', '')))


_OFFER_ERRORS = {
    'missing': _l('السعر والتفاصيل مطلوبين.'),
    'price': _l('السعر مش مظبوط.'),
    'days': _l('مدة التنفيذ أو الضمان مش مظبوطة.'),
    'images': _l('العميل طالب صور مع العرض — ارفع صورة واحدة على الأقل.'),
    'file': _l('حجم الملف المرفق أكبر من ١٠ ميجا.'),
    'dup': _l('إنت قدّمت عرض على الطلب ده قبل كده.'),
    'closed': _l('الطلب ده قفل.'),
}


@_seller_required
@require_POST
def seller_submit_offer(request, request_code):
    seller = request.seller
    svc_request = ServiceRequest.objects.filter(request_code=request_code).first()
    if svc_request is None or svc_request.sector != seller.store.sector:
        raise Http404

    def fail(code):
        return redirect(f'/marketplace/seller/requests/?req={request_code}&error={code}#req-{request_code}')

    if svc_request.status != 'open' or not svc_request.is_approved or (
            svc_request.expires_at and svc_request.expires_at <= timezone.now()):
        return fail('closed')
    price_raw = (request.POST.get('price') or '').strip()
    description = (request.POST.get('description') or '').strip()
    if not price_raw or not description:
        return fail('missing')
    try:
        price = Decimal(price_raw.replace(',', ''))
        if not price.is_finite() or price <= 0:
            raise InvalidOperation
        price = price.quantize(Decimal('0.01'))
    except InvalidOperation:
        return fail('price')
    try:
        days = int(request.POST.get('estimated_days') or 1)
        warranty = int(request.POST.get('warranty_days') or 0)
    except ValueError:
        return fail('days')
    if not (1 <= days <= 365) or not (0 <= warranty <= 3650):
        return fail('days')
    files = {k: request.FILES.get(k) for k in ('image_1', 'image_2', 'image_3', 'file_attachment')}
    if any(f and f.size > 10 * 1024 * 1024 for f in files.values()):
        return fail('file')
    if svc_request.wants_images and not files['image_1']:
        return fail('images')
    try:
        with transaction.atomic():
            offer = TenderOffer.objects.create(
                service_request=svc_request, seller=seller, price=price, description=description[:5000],
                estimated_days=days, warranty_days=warranty,
                merchant_city=(request.POST.get('merchant_city') or seller.store.city or '')[:100],
                merchant_address=(request.POST.get('merchant_address') or '')[:300],
                **{k: f for k, f in files.items() if f},
            )
            ServiceRequest.objects.filter(pk=svc_request.pk).update(offers_count=F('offers_count') + 1)
    except IntegrityError:
        return fail('dup')
    from .marketplace_core_views import _notify_customer
    _notify_customer(
        svc_request.customer, title='💬 عرض سعر جديد على طلبك',
        body=f'«{svc_request.title[:80]}» — عرض بسعر {price} من تاجر في {offer.merchant_city[:40]}.',
        level='info', icon='fa-tags',
        action_url=f'/marketplace/request/{svc_request.request_code}/', action_label='شوف العروض',
    )
    return redirect('/marketplace/seller/requests/?sent=1')


@_seller_required
@require_POST
def seller_withdraw_offer(request, offer_code):
    offer = get_object_or_404(TenderOffer, offer_code=offer_code, seller=request.seller)
    if offer.status == 'pending':
        with transaction.atomic():
            if TenderOffer.objects.filter(pk=offer.pk, status='pending').update(status='withdrawn'):
                ServiceRequest.objects.filter(pk=offer.service_request_id, offers_count__gt=0).update(
                    offers_count=F('offers_count') - 1)
    return redirect('/marketplace/seller/requests/')


# ─────────────────────────── بيانات المتجر + الترقية ───────────────────────────

class SellerStoreForm(forms.ModelForm):
    class Meta:
        model = CatalogStore
        fields = ('tagline', 'description', 'city')


class SellerAccountForm(forms.Form):
    owner_name = forms.CharField(max_length=120, label=_l('اسم المسؤول'))
    email = forms.EmailField(required=False, label=_l('الإيميل (اختياري)'))
    new_password = forms.CharField(required=False, min_length=6, max_length=128, widget=forms.PasswordInput,
                                   label=_l('كلمة سر جديدة (سيبها فاضية لو مش هتغيّرها)'))


@_seller_required
def seller_store(request):
    seller = request.seller
    store = seller.store
    sform = SellerStoreForm(request.POST or None, instance=store, prefix='s')
    aform = SellerAccountForm(request.POST or None, prefix='a',
                              initial={'owner_name': seller.owner_name, 'email': seller.email})
    if request.method == 'POST' and sform.is_valid() and aform.is_valid():
        sform.save()
        seller.owner_name = aform.cleaned_data['owner_name']
        seller.email = aform.cleaned_data['email'] or ''
        fields = ['owner_name', 'email']
        if aform.cleaned_data['new_password']:
            seller.set_password(aform.cleaned_data['new_password'])
            seller.session_token = uuid.uuid4()   # الأجهزة التانية تخرج
            fields += ['password_hash', 'session_token']
        seller.save(update_fields=fields)
        resp = redirect('/marketplace/seller/store/?saved=1')
        return svc.login_response(resp, seller) if 'session_token' in fields else resp
    return render(request, 'clients/marketplace/seller/store.html', _ctx(
        request, 'store', sform=sform, aform=aform, saved=request.GET.get('saved') == '1'))


@_seller_required
def seller_upgrade(request):
    """⬆️ شرح الترقية لبرنامج الإدارة — اختيارية تماماً."""
    return render(request, 'clients/marketplace/seller/upgrade.html', _ctx(request, 'upgrade'))


