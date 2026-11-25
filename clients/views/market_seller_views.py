"""
🏪 لوحة بائع السوق — مطبعة أو محل قطع غيار بيعرض منتجاته ويتابع عملاءه بس.

مالهاش أي علاقة ببرنامج الإدارة (فواتير/مخزون/حسابات): مفيش Tenant ولا schema.
* تسجيل خفيف: اسم المتجر + المسؤول + الموبايل + كلمة سر + القطاع ⇒ المتجر ظاهر على طول.
* منتجاتي: إضافة/تعديل/إخفاء + رفع شيت Excel — بتظهر على طول، وصاحب المنصة بيوصله إشعار
  ويقدر يخفي أي منتج (admin_hidden ⇒ البائع مايقدرش يرجّعه).
* عملائي: طلبات الأسعار على منتجاته — بيكلّم العميل مباشرة ويغيّر حالة الطلب.
* السوق المفتوح: طلبات العملاء في قطاعه + يقدّم عرض سعر.
* ترقية اختيارية لبرنامج الإدارة (التسجيل العادي وهو داخل بحسابه ⇒ الشركة بتتربط بيه).
"""
from __future__ import annotations

import logging
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

from .catalog_views import OTHER_CATEGORY, SECTORS, _categories, item_category_label

logger = logging.getLogger('mouss_tec_core')

SIGNUPS_PER_IP_HOUR = 5
LOGIN_FAILS_PER_PHONE = 10
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
    # 🐛 [FIX]: كان slugify(allow_unicode=True) ⇒ «مطبعة-النيل» — ومسار <slug:slug> بيقبل إنجليزي بس
    #    ⇒ صفحة المتجر 404 لأي اسم عربي. دلوقتي ASCII دايماً، والاسم العربي ⇒ «printing-a1b2c».
    base = slugify(name)[:60] or f'{sector}-{uuid.uuid4().hex[:5]}'
    slug = base
    while CatalogStore.objects.filter(slug=slug).exists():
        slug = f'{base}-{uuid.uuid4().hex[:5]}'
    return slug


def _ctx(request, active, **extra):
    seller = request.seller
    store = seller.store
    pending = store.items.filter(admin_hidden=True).count()   # منتجات Mouss Tec خبّتها
    new_quotes = CatalogQuoteRequest.objects.filter(item__store=store, status='new').count()
    return {'seller': seller, 'store': store, 'sector': store.sector, 'meta': SECTORS[store.sector],
            'active': active, 'pending_count': pending, 'new_quotes': new_quotes, **extra}


# ─────────────────────────── التسجيل والدخول ───────────────────────────

def _gov_choices():
    from clients.services.price_board import gov_choices
    return [('', '—')] + gov_choices()


class SellerProfileFields(forms.Form):
    """🧭 حقول بروفايل البائع المشتركة بين التسجيل وصفحة «بيانات المتجر».

    التخصصات والإمكانيات بتتعرض لكل قطاع (specialties_printing …) والـ clean بياخد بتاعة القطاع المختار.
    """
    governorate = forms.ChoiceField(label=_l('المحافظة'))
    address = forms.CharField(max_length=250, label=_l('العنوان'),
                              help_text=_l('المنطقة والشارع — بيساعد العميل يختار الأقرب ليه.'))
    whatsapp = forms.CharField(max_length=30, required=False, label=_l('رقم واتساب (لو غير الموبايل)'))
    years_in_business = forms.IntegerField(min_value=0, max_value=80, required=False, label=_l('سنين الخبرة'))

    def __init__(self, *args, sectors=('printing', 'automotive'), **kwargs):
        super().__init__(*args, **kwargs)
        from clients.services import seller_profile as sp
        self.fields['governorate'].choices = _gov_choices()
        self.sectors = sectors
        for sec in sectors:
            self.fields[f'specialties_{sec}'] = forms.MultipleChoiceField(
                choices=sp.specialty_choices(sec), required=False, widget=forms.CheckboxSelectMultiple,
                label=_l('بتشتغل في إيه؟ (اختار كل اللي بتعمله)'))
            self.fields[f'capabilities_{sec}'] = forms.MultipleChoiceField(
                choices=sp.capability_choices(sec), required=False, widget=forms.CheckboxSelectMultiple,
                label=_l('إمكانياتك وخدماتك') if sec == 'automotive' else _l('طرق الطباعة والخدمات'))

    def clean_whatsapp(self):
        raw = (self.cleaned_data.get('whatsapp') or '').strip()
        if not raw:
            return ''
        v = svc.normalize_phone(raw)
        if not v:
            raise forms.ValidationError(_('رقم الواتساب مش مظبوط — اكتبه زي 01012345678.'))
        return v

    def profile_for(self, sector):
        """القيم النهائية للقطاع (بعد is_valid)."""
        d = self.cleaned_data
        return {
            'specialties': list(d.get(f'specialties_{sector}') or []),
            'capabilities': list(d.get(f'capabilities_{sector}') or []),
            'governorate': d.get('governorate') or '', 'address': d.get('address') or '',
            'whatsapp': d.get('whatsapp') or '', 'years_in_business': d.get('years_in_business'),
        }

    def check_specialties(self, sector):
        if not self.cleaned_data.get(f'specialties_{sector}'):
            self.add_error(f'specialties_{sector}', _('اختار تخصص واحد على الأقل — العملاء بيدوّروا بيه.'))


class SellerSignupForm(SellerProfileFields):
    store_name = forms.CharField(max_length=120, label=_l('اسم المطبعة أو المحل'))
    owner_name = forms.CharField(max_length=120, label=_l('اسم المسؤول'))
    phone = forms.CharField(max_length=30, label=_l('رقم الموبايل'))
    password = forms.CharField(min_length=6, max_length=128, widget=forms.PasswordInput, label=_l('كلمة السر'))
    sector = forms.ChoiceField(choices=(('printing', _l('مطبعة / تصميم')), ('automotive', _l('قطع غيار سيارات'))),
                               label=_l('النشاط'))
    # 📧 إجباري: عليه بيوصل كود «نسيت كلمة السر» وإشعارات الطلبات
    email = forms.EmailField(label=_l('إيميل المتجر'), help_text=_l('عليه بيوصلك كود تغيير كلمة السر لو نسيتها.'))
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

    def clean(self):
        data = super().clean()
        if data.get('sector') in SECTORS:
            self.check_specialties(data['sector'])
        return data


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
                    from clients.services.price_board import gov_label
                    profile = form.profile_for(d['sector'])
                    store = CatalogStore.objects.create(
                        sector=d['sector'], name=d['store_name'], slug=_unique_slug(d['store_name'], d['sector']),
                        tagline=d['tagline'], city=str(gov_label(profile['governorate']))[:60],
                        icon=meta['icon'], color=meta['color'],
                        # 🐛 [FIX]: كان مخفي لحد موافقة صاحب المنصة ⇒ المطبعة تسجّل وتستنى. دلوقتي ظاهر على طول
                        is_active=True,
                        sort=1000,
                    )
                    seller = MarketSeller(store=store, owner_name=d['owner_name'], phone=d['phone'],
                                          email=d['email'].strip().lower(), last_login_at=timezone.now(),
                                          **profile)
                    seller.set_password(d['password'])
                    seller.save()
            except IntegrityError:
                form.add_error('phone', _('الرقم ده عنده حساب بائع — ادخل بيه.'))
            else:
                from clients.services.owner_notify import notify_owner
                from clients.services.seller_profile import specialty_items
                specs = '، '.join(str(lbl) for _k, lbl, _i in specialty_items(store.sector, seller.specialties))
                notify_owner(f'بائع جديد في السوق ({store.get_sector_display()})',
                             f'{store.name} — {seller.owner_name} — {seller.phone} — {store.city} — {seller.address}\n'
                             f'التخصصات: {specs}\n'
                             'المتجر ظاهر للعملاء دلوقتي — لو فيه حاجة مش مظبوطة اخفيه من الأدمن.',
                             f'/superadmin/market/sellers/{seller.pk}/')
                return svc.login_response(redirect('/marketplace/seller/?welcome=1'), seller)
    return render(request, 'clients/marketplace/seller/join.html', {
        'form': form, 'sector': (request.POST.get('sector') if request.POST.get('sector') in SECTORS else initial_sector)})


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
        'quotes_30': CatalogQuoteRequest.objects.filter(
            item__store=store, created_at__gte=timezone.now() - timedelta(days=30)).count(),
        'open_requests': ServiceRequest.objects.filter(
            sector=store.sector, status='open', is_approved=True, expires_at__gt=timezone.now())
        .exclude(offers__seller=request.seller).count(),
        'accepted': TenderOffer.objects.filter(seller=request.seller, status='accepted').count(),
    }
    if store.sector == 'printing':   # 📈 بورصة الأسعار: سعّر النهارده ولا لسه؟
        from clients.services import price_board as pb
        stats['priced_today'] = pb.submitted_today(pb.Provider('seller', request.seller))
    recent = (CatalogQuoteRequest.objects.filter(item__store=store).select_related('item')
              .order_by('-created_at')[:5])
    from clients.services.seller_profile import completeness
    return render(request, 'clients/marketplace/seller/dashboard.html', _ctx(
        request, 'home', stats=stats, recent=recent, profile=completeness(request.seller), welcome=request.GET.get('welcome') == '1'))


# ─────────────────────────── منتجاتي ───────────────────────────

class SellerItemForm(forms.ModelForm):
    class Meta:
        model = CatalogItem
        fields = ('title', 'category', 'custom_category', 'description', 'image', 'min_qty', 'price_from',
                  'car_make', 'car_models', 'part_number', 'condition')

    def __init__(self, *args, sector='printing', **kwargs):
        super().__init__(*args, **kwargs)
        self.sector = sector
        # 🐛 [FIX]: البائع كان لازم يختار من أقسامنا بس — منتج مش في القائمة مالوش مكان.
        #    دلوقتي «أخرى» ⇒ يكتب نوع المنتج + تفاصيل أكتر (الاتنين إجباري مع «أخرى»).
        self.fields['category'] = forms.ChoiceField(
            choices=[(k, label) for k, label, _icon in _categories(SECTORS[sector])]
            + [(OTHER_CATEGORY, _l('أخرى — منتج مش في القائمة'))], label=_l('القسم'))
        self.fields['custom_category'].label = _l('نوع المنتج')
        self.fields['custom_category'].help_text = _l('مثلاً: ستاندات رول أب، أختام، ميداليات أكريليك…')
        self.fields['description'].required = False
        self.fields['description'].help_text = _l('مع «أخرى»: اكتب تفاصيل أكتر — الخامة والمقاسات والألوان واستخدام المنتج.')
        if sector != 'automotive':
            for f in ('car_make', 'car_models', 'part_number', 'condition'):
                del self.fields[f]
        self.fields['price_from'].label = _l('السعر يبدأ من (اختياري)')
        self.fields['price_from'].min_value = 0

    def clean(self):
        data = super().clean()
        if data.get('category') == OTHER_CATEGORY:
            if len((data.get('custom_category') or '').strip()) < 3:
                self.add_error('custom_category', _('اكتب نوع المنتج لما تختار «أخرى».'))
            if len((data.get('description') or '').strip()) < 20:
                self.add_error('description', _('مع «أخرى» اكتب تفاصيل أكتر عن المنتج (٢٠ حرف على الأقل).'))
        else:
            data['custom_category'] = ''
        return data

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
    items = list(store.items.order_by('-is_active', '-created_at'))
    meta = SECTORS[store.sector]
    for it in items:
        it.category_label = item_category_label(meta, it)
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
        if obj.category != OTHER_CATEGORY:
            obj.custom_category = ''
        # بيظهر على طول — إلا لو Mouss Tec مخبّياه من الأدمن
        obj.is_active = not obj.admin_hidden
        obj.pending_review = False
        obj.save()
        _notify_owner_new_items(store)
        return redirect('/marketplace/seller/products/?saved=1')
    return render(request, 'clients/marketplace/seller/product_form.html', _ctx(
        request, 'products', form=form, item=item))


@_seller_required
@require_POST
def seller_product_hide(request, item_code):
    """إخفاء منتج من السوق أو إظهاره تاني (إلا لو Mouss Tec مخبّياه)."""
    item = get_object_or_404(CatalogItem, item_code=item_code, store=request.seller.store)
    item.is_active = request.POST.get('action') == 'show' and not item.admin_hidden
    item.pending_review = False
    item.save(update_fields=['is_active', 'pending_review'])
    return redirect('/marketplace/seller/products/')


def _notify_owner_new_items(store):
    # إشعار واحد كل ساعة لكل متجر — مايغرّقش صاحب المنصة لو البائع بيضيف كتير ورا بعض
    if not cache.add(f'seller_review_ping:{store.pk}', 1, 3600):
        return
    from clients.services.owner_notify import notify_owner
    notify_owner(f'منتجات جديدة من بائع — {store.name}',
                 f'{store.name} ضاف/عدّل منتجات وظهرت للعملاء — لو فيه حاجة مش مظبوطة اخفيها من الأدمن.',
                 f'/superadmin/market/sellers/{store.seller.pk}/?tab=products')


# ─────────────── 🤖 المساعد الذكي للاستيراد (أي ملف ⇒ مراجعة ⇒ حفظ) ───────────────

def _import_key(seller, token):
    return f'seller_import:{seller.pk}:{token}'


@_seller_required
def seller_products_import(request):
    """🤖 ارفع أي ملف (Excel / CSV / PDF / صورة) ⇒ المساعد يطلّع المنتجات ⇒ تراجعها ⇒ تتحفظ.

    🐛 [FIX]: كان بيقبل شيت بعناوين محددة في أول صف بس ⇒ قوائم الأسعار الحقيقية كانت بتترفض.
    """
    from clients.services import smart_import as si
    seller = request.seller
    store = seller.store
    meta = SECTORS[store.sector]
    cat_choices = [(k, label) for k, label, _i in _categories(meta)] + [(OTHER_CATEGORY, _('أخرى'))]
    token = request.GET.get('review') or request.POST.get('token') or ''
    errors = []

    if request.method == 'POST' and request.POST.get('action') == 'confirm':
        data = cache.get(_import_key(seller, token))
        if not data:
            errors.append(_('المراجعة دي خلصت — ارفع الملف تاني.'))
        else:
            picked = {int(i) for i in request.POST.getlist('pick') if i.isdigit()}
            new = []
            for i, r in enumerate(data['rows']):
                if i not in picked:
                    continue
                cat = request.POST.get(f'cat_{i}', r['category'])
                if cat not in dict(cat_choices):
                    cat = r['category']
                custom = (r['custom_category'] or r.get('section') or r['title'])[:80] if cat == OTHER_CATEGORY else ''
                new.append(CatalogItem(
                    store=store, title=r['title'], description=r['description'], category=cat,
                    custom_category=custom, icon=meta['categories'].get(cat, ('', 'fa-box'))[1],
                    min_qty=r['min_qty'], price_from=r['price'], is_active=True, pending_review=False))
            if new:
                CatalogItem.objects.bulk_create(new)
                cache.delete(_import_key(seller, token))
                _notify_owner_new_items(store)
                return redirect(f'/marketplace/seller/products/?imported={len(new)}')
            errors.append(_('اختار منتج واحد على الأقل.'))

    elif request.method == 'POST':
        from erp_core.client_ip import client_ip
        upload = request.FILES.get('sheet')
        if not upload:
            errors.append(_('اختار الملف.'))
        elif _hit(f'seller_import:{seller.pk}', 20, 3600) or _hit(f'seller_import_ip:{client_ip(request)}', 40, 3600):
            errors.append(_('رفعت ملفات كتير — جرّب بعد ساعة.'))
        else:
            try:
                rows, how = si.read_any(upload, store.sector)
            except si.ImportFailed as e:
                errors.append(str(e))
            else:
                existing = {si._norm(t) for t in store.items.values_list('title', flat=True)}
                for r in rows:
                    r['exists'] = si._norm(r['title']) in existing
                token = uuid.uuid4().hex
                cache.set(_import_key(seller, token), {'rows': rows, 'how': how, 'name': upload.name[:120]}, 3600)
                return redirect(f'/marketplace/seller/products/import/?review={token}')

    data = cache.get(_import_key(seller, token)) if token else None
    if data:
        rows = data['rows']
        return render(request, 'clients/marketplace/seller/import_review.html', _ctx(
            request, 'products', rows=rows, token=token, how=data['how'], file_name=data['name'],
            cat_choices=cat_choices, errors=errors, priced=sum(1 for r in rows if r['price']),
            dupes=sum(1 for r in rows if r.get('exists'))))
    return render(request, 'clients/marketplace/seller/import.html', _ctx(
        request, 'products', errors=errors, categories=_categories(meta), accept=si.ACCEPT,
        ai_on=si._ai_available()))


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
        fields = ('tagline', 'description', 'logo')
        labels = {'tagline': _l('جملة عن شغلك'), 'description': _l('عن المتجر'), 'logo': _l('اللوجو')}

    def clean_logo(self):
        img = self.cleaned_data.get('logo')
        if img and hasattr(img, 'size') and img.size > 3 * 1024 * 1024:
            raise forms.ValidationError(_('اللوجو أكبر من ٣ ميجا.'))
        return img


class SellerAccountForm(forms.Form):
    owner_name = forms.CharField(max_length=120, label=_l('اسم المسؤول'))
    email = forms.EmailField(label=_l('إيميل المتجر'), help_text=_l('عليه بيوصلك كود تغيير كلمة السر لو نسيتها.'))
    website = forms.URLField(required=False, label=_l('صفحة فيسبوك / موقع (اختياري)'))
    working_hours = forms.CharField(max_length=120, required=False, label=_l('مواعيد العمل (اختياري)'))
    new_password = forms.CharField(required=False, min_length=6, max_length=128, widget=forms.PasswordInput,
                                   label=_l('كلمة سر جديدة (سيبها فاضية لو مش هتغيّرها)'))


@_seller_required
def seller_store(request):
    seller = request.seller
    store = seller.store
    sform = SellerStoreForm(request.POST or None, request.FILES or None, instance=store, prefix='s')
    pform = SellerProfileFields(request.POST or None, prefix='p', sectors=(store.sector,), initial={
        'governorate': seller.governorate, 'address': seller.address,
        'whatsapp': seller_profile_local(seller.whatsapp), 'years_in_business': seller.years_in_business,
        f'specialties_{store.sector}': seller.specialties, f'capabilities_{store.sector}': seller.capabilities})
    aform = SellerAccountForm(request.POST or None, prefix='a', initial={
        'owner_name': seller.owner_name, 'email': seller.email, 'website': seller.website,
        'working_hours': seller.working_hours})
    if request.method == 'POST':
        ok = sform.is_valid() & pform.is_valid() & aform.is_valid()
        if ok:
            pform.check_specialties(store.sector)
            ok = not pform.errors
        if ok:
            from clients.services.price_board import gov_label
            sform.save()
            prof = pform.profile_for(store.sector)
            for k, v in prof.items():
                setattr(seller, k, v)
            CatalogStore.objects.filter(pk=store.pk).update(city=str(gov_label(prof['governorate']))[:60])
            a = aform.cleaned_data
            seller.owner_name = a['owner_name']
            seller.email = a['email'].strip().lower()
            seller.website = a['website'] or ''
            seller.working_hours = a['working_hours'] or ''
            fields = ['owner_name', 'email', 'website', 'working_hours', *prof.keys()]
            if a['new_password']:
                seller.set_password(a['new_password'])
                seller.session_token = uuid.uuid4()   # الأجهزة التانية تخرج
                fields += ['password_hash', 'session_token']
            seller.save(update_fields=fields)
            resp = redirect('/marketplace/seller/store/?saved=1')
            return svc.login_response(resp, seller) if 'session_token' in fields else resp
    return render(request, 'clients/marketplace/seller/store.html', _ctx(
        request, 'store', sform=sform, pform=pform, aform=aform, saved=request.GET.get('saved') == '1',
        spec_field=pform[f'specialties_{store.sector}'], cap_field=pform[f'capabilities_{store.sector}']))


def seller_profile_local(phone):
    return svc.local_phone(phone) if phone else ''


@_seller_required
def seller_upgrade(request):
    """⬆️ شرح الترقية لبرنامج الإدارة — اختيارية تماماً."""
    return render(request, 'clients/marketplace/seller/upgrade.html', _ctx(request, 'upgrade'))




# ─────────────────────────── 🔑 نسيت كلمة السر (كود على إيميل المتجر) ───────────────────────────

RESET_TTL = 600            # ١٠ دقايق
RESET_MAX_TRIES = 5        # محاولات الكود الغلط قبل ما يتلغي
RESET_SENDS_PER_HOUR = 3
RESET_IP_PER_HOUR = 10


def _reset_key(seller_pk):
    return f'seller_pwreset:{seller_pk}'


def _reset_hash(seller_pk, code):
    import hashlib
    import hmac

    from django.conf import settings
    return hmac.new(settings.SECRET_KEY.encode(), f'seller:{seller_pk}:{code}'.encode(), hashlib.sha256).hexdigest()


def _send_reset_email(seller, code):
    from django.conf import settings
    from django.core.mail import send_mail
    body = (f'أهلاً {seller.owner_name}،\n\n'
            f'كود تغيير كلمة السر لحساب «{seller.store.name}» في سوق Mouss Tec:\n\n    {code}\n\n'
            'الكود صالح ١٠ دقايق. لو مش إنت اللي طلبته تجاهل الرسالة — كلمة السر مش هتتغيّر.\n\n— فريق Mouss Tec')
    try:
        return bool(send_mail('Mouss Tec — كود تغيير كلمة السر', body, settings.DEFAULT_FROM_EMAIL,
                              [seller.email], fail_silently=False))
    except Exception:  # noqa: BLE001
        logger.exception('[SELLER PW-RESET] email failed for seller %s', seller.pk)
        return False


def seller_password_forgot(request):
    """الخطوة ١: رقم الموبايل ⇒ كود ٦ أرقام على إيميل المتجر (من إيميل Mouss Tec).

    🛡️ الرد واحد سواء الرقم متسجّل أو لأ، والكود متخزّن hash ١٠ دقايق، وفيه حدود إرسال.
    """
    import secrets

    from erp_core.client_ip import client_ip
    sent = False
    error = ''
    phone_raw = (request.POST.get('phone') or '').strip()
    if request.method == 'POST':
        phone = svc.normalize_phone(phone_raw)
        if not phone:
            error = _('رقم الموبايل مش مظبوط — اكتبه زي 01012345678.')
        elif _hit(f'seller_pwreset_ip:{client_ip(request) or "x"}', RESET_IP_PER_HOUR, 3600):
            error = _('طلبات كتير — جرّب بعد ساعة.')
        else:
            seller = MarketSeller.objects.select_related('store').filter(phone=phone, is_blocked=False).first()
            if seller and seller.email and not _hit(f'seller_pwreset_sends:{seller.pk}', RESET_SENDS_PER_HOUR, 3600):
                code = f'{secrets.randbelow(1_000_000):06d}'
                cache.set(_reset_key(seller.pk), {'h': _reset_hash(seller.pk, code), 'tries': 0}, RESET_TTL)
                _send_reset_email(seller, code)
            sent = True   # نفس الرد في كل الحالات
    return render(request, 'clients/marketplace/seller/password_forgot.html', {
        'sent': sent, 'error': error, 'phone': phone_raw,
    })


class SellerResetForm(forms.Form):
    phone = forms.CharField(max_length=30, label=_l('رقم الموبايل'))
    code = forms.CharField(max_length=6, min_length=6, label=_l('الكود اللي وصلك على الإيميل'))
    password = forms.CharField(min_length=6, max_length=128, widget=forms.PasswordInput,
                               label=_l('كلمة السر الجديدة'))


def seller_password_reset(request):
    """الخطوة ٢: الكود + كلمة سر جديدة ⇒ تتغيّر، والأجهزة التانية تخرج، ويدخل على طول."""
    form = SellerResetForm(request.POST or None, initial={'phone': request.GET.get('phone', '')[:30]})
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        phone = svc.normalize_phone(d['phone'])
        seller = MarketSeller.objects.filter(phone=phone, is_blocked=False).first() if phone else None
        entry = cache.get(_reset_key(seller.pk)) if seller else None
        import hmac
        if not entry or entry['tries'] >= RESET_MAX_TRIES or not hmac.compare_digest(
                entry['h'], _reset_hash(seller.pk, d['code'].strip())):
            if entry:
                entry['tries'] += 1
                if entry['tries'] >= RESET_MAX_TRIES:
                    cache.delete(_reset_key(seller.pk))
                else:
                    cache.set(_reset_key(seller.pk), entry, RESET_TTL)
            form.add_error('code', _('الكود غلط أو انتهت صلاحيته — اطلب كود جديد.'))
        else:
            cache.delete(_reset_key(seller.pk))
            seller.set_password(d['password'])
            seller.session_token = uuid.uuid4()
            seller.last_login_at = timezone.now()
            seller.save(update_fields=['password_hash', 'session_token', 'last_login_at'])
            cache.delete(f'seller_login_fail:{phone}')
            return svc.login_response(redirect('/marketplace/seller/?reset=1'), seller)
    return render(request, 'clients/marketplace/seller/password_reset.html', {'form': form})
