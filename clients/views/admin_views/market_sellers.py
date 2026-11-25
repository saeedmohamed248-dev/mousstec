"""
🏪 لوحة بائعين السوق لصاحب المنصة — /superadmin/market/sellers/

قبل كده البائع كان في أدمن Django بس: جدول خام من غير أرقام ولا تحكم. هنا (زي Seller Center في الأسواق
العالمية): مؤشرات، فلاتر بالقطاع والمحافظة والتخصص والحالة، نسبة اكتمال البروفايل، وتحكم كامل في كل بائع:
إظهار/إخفاء المتجر، إيقاف/تفعيل الحساب، تعديل البيانات، ملاحظات داخلية، كود تغيير كلمة السر على الإيميل،
وإخفاء/إظهار أي منتج — ومعاه عملاءه وعروضه وأسعاره.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from clients.models import CatalogItem, CatalogQuoteRequest, CatalogStore, MarketSeller, PriceQuote, TenderOffer
from clients.permissions import platform_owner_required
from clients.services import seller_profile as sp
from clients.services.price_board import gov_choices, gov_label

STATUS_FILTERS = (
    ('live', _l('ظاهر')),
    ('hidden', _l('متخفي')),
    ('blocked', _l('موقوف')),
    ('incomplete', _l('بروفايل ناقص')),
    ('no_products', _l('من غير منتجات')),
)
SORTS = (
    ('-created_at', _l('الأحدث')),
    ('-last_login_at', _l('آخر دخول')),
    ('-n_items', _l('الأكتر منتجات')),
    ('-n_quotes', _l('الأكتر طلبات')),
    ('store__name', _l('الاسم')),
)


def _owner(view):
    return login_required(platform_owner_required(view))


def _annotated(qs):
    month = timezone.now() - timedelta(days=30)
    return qs.select_related('store').annotate(
        n_items=Count('store__items', filter=Q(store__items__is_active=True), distinct=True),
        n_hidden=Count('store__items', filter=Q(store__items__admin_hidden=True), distinct=True),
        n_quotes=Count('store__items__quote_requests',
                       filter=Q(store__items__quote_requests__created_at__gte=month), distinct=True),
        n_offers=Count('tender_offers', distinct=True),
        n_won=Count('tender_offers', filter=Q(tender_offers__status='accepted'), distinct=True),
        last_price=Max('price_quotes__quoted_on'),
    )


def _filters(request):
    g = request.GET
    return {
        'q': (g.get('q') or '').strip()[:80], 'sector': g.get('sector', ''), 'gov': g.get('gov', ''),
        'spec': g.get('spec', ''), 'status': g.get('status', ''),
        'sort': g.get('sort') if g.get('sort') in dict(SORTS) else '-created_at',
    }


def _apply(qs, f):
    if f['q']:
        q = f['q']
        digits = ''.join(ch for ch in q if ch.isdigit())
        cond = (Q(store__name__icontains=q) | Q(owner_name__icontains=q) | Q(email__icontains=q)
                | Q(address__icontains=q))
        if len(digits) >= 4:
            cond |= Q(phone__contains=digits.lstrip('0')) | Q(whatsapp__contains=digits.lstrip('0'))
        qs = qs.filter(cond)
    if f['sector'] in ('printing', 'automotive'):
        qs = qs.filter(store__sector=f['sector'])
    if f['gov']:
        qs = qs.filter(governorate=f['gov'])
    if f['spec']:
        qs = qs.filter(Q(specialties__contains=[f['spec']]) | Q(capabilities__contains=[f['spec']]))
    st = f['status']
    if st == 'live':
        qs = qs.filter(store__is_active=True, is_blocked=False)
    elif st == 'hidden':
        qs = qs.filter(store__is_active=False)
    elif st == 'blocked':
        qs = qs.filter(is_blocked=True)
    elif st == 'incomplete':
        qs = qs.filter(Q(specialties=[]) | Q(governorate='') | Q(address='') | Q(store__logo='')
                       | Q(store__logo__isnull=True))
    elif st == 'no_products':
        qs = qs.filter(n_items=0)
    return qs.order_by(f['sort'], '-pk')


@_owner
def sellers_list(request):
    f = _filters(request)
    base = MarketSeller.objects.all()
    now = timezone.now()
    kpis = {
        'total': base.count(),
        'live': base.filter(store__is_active=True, is_blocked=False).count(),
        'new7': base.filter(created_at__gte=now - timedelta(days=7)).count(),
        'blocked': base.filter(is_blocked=True).count(),
        'items': CatalogItem.objects.filter(store__seller__isnull=False, is_active=True).count(),
        'quotes30': CatalogQuoteRequest.objects.filter(item__store__seller__isnull=False,
                                                       created_at__gte=now - timedelta(days=30)).count(),
        'won': TenderOffer.objects.filter(seller__isnull=False, status='accepted').count(),
        'priced_today': PriceQuote.objects.filter(seller__isnull=False, quoted_on=timezone.localdate())
        .values('seller').distinct().count(),
    }
    qs = _apply(_annotated(base), f)
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    for s in page.object_list:
        s.profile = sp.completeness(s)
        s.gov_label = gov_label(s.governorate) if s.governorate else ''
        s.spec_items = sp.specialty_items(s.store.sector, s.specialties)[:4]
        s.wa = sp.whatsapp_digits(s.whatsapp or s.phone)
        s.local_phone = s.phone.replace('+20', '0', 1)
    spec_opts = [('', _('كل التخصصات'))]
    for sec in ('printing', 'automotive'):
        if f['sector'] in ('', sec):
            spec_opts += sp.specialty_choices(sec) + sp.capability_choices(sec)
    from urllib.parse import urlencode
    keep = urlencode({k: v for k, v in f.items() if v and k != 'sort'})
    return render(request, 'clients/saas_admin/market_sellers.html', {
        'page': page, 'f': f, 'kpis': kpis, 'status_filters': STATUS_FILTERS, 'sorts': SORTS,
        'governorates': gov_choices(), 'spec_opts': spec_opts, 'keep': keep,
    })


@_owner
@require_POST
def sellers_bulk(request):
    ids = [i for i in request.POST.getlist('ids') if i.isdigit()]
    action = request.POST.get('action')
    qs = MarketSeller.objects.filter(pk__in=ids)
    n = qs.count()
    if action == 'show':
        CatalogStore.objects.filter(seller__in=qs).update(is_active=True)
    elif action == 'hide':
        CatalogStore.objects.filter(seller__in=qs).update(is_active=False)
    elif action == 'block':
        for s in qs:
            _set_blocked(s, True)
    elif action == 'unblock':
        qs.update(is_blocked=False)
    else:
        n = 0
    if n:
        messages.success(request, _('اتنفّذ على %(n)s بائع.') % {'n': n})
    back = request.POST.get('next') or '/superadmin/market/sellers/'
    return redirect(back if back.startswith('/superadmin/market/sellers/') else '/superadmin/market/sellers/')


def _set_blocked(seller, blocked):
    """إيقاف بيخرّجه من كل الأجهزة ويخفي متجره؛ التفعيل مابيظهرش المتجر لوحده (قرار منفصل)."""
    seller.is_blocked = blocked
    fields = ['is_blocked']
    if blocked:
        seller.session_token = uuid.uuid4()
        fields.append('session_token')
        CatalogStore.objects.filter(pk=seller.store_id).update(is_active=False)
    seller.save(update_fields=fields)


class AdminSellerForm(forms.ModelForm):
    store_name = forms.CharField(max_length=120, label=_l('اسم المتجر'))
    tagline = forms.CharField(max_length=200, required=False, label=_l('جملة تعريفية'))
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}), label=_l('عن المتجر'))
    is_featured = forms.BooleanField(required=False, label=_l('اختيار Mouss Tec (يطلع الأول)'))

    class Meta:
        model = MarketSeller
        fields = ('owner_name', 'email', 'governorate', 'address', 'whatsapp', 'website', 'years_in_business',
                  'working_hours', 'specialties', 'capabilities', 'admin_notes')
        widgets = {'admin_notes': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store = self.instance.store
        sector = store.sector
        self.fields['governorate'] = forms.ChoiceField(choices=[('', '—')] + gov_choices(), required=False,
                                                       label=_l('المحافظة'))
        self.fields['specialties'] = forms.MultipleChoiceField(
            choices=sp.specialty_choices(sector), required=False, widget=forms.CheckboxSelectMultiple,
            label=_l('التخصصات'))
        self.fields['capabilities'] = forms.MultipleChoiceField(
            choices=sp.capability_choices(sector), required=False, widget=forms.CheckboxSelectMultiple,
            label=_l('الإمكانيات'))
        self.initial.update({'store_name': store.name, 'tagline': store.tagline,
                             'description': store.description, 'is_featured': store.is_featured})

    def save(self, commit=True):
        seller = super().save(commit=commit)
        d = self.cleaned_data
        city = str(gov_label(d['governorate']))[:60] if d.get('governorate') else seller.store.city
        CatalogStore.objects.filter(pk=seller.store_id).update(
            name=' '.join(d['store_name'].split()), tagline=d['tagline'], description=d['description'],
            is_featured=d['is_featured'], city=city)
        return seller


@_owner
def seller_detail(request, pk):
    seller = get_object_or_404(MarketSeller.objects.select_related('store', 'upgraded_to'), pk=pk)
    store = seller.store
    form = AdminSellerForm(request.POST or None, instance=seller)
    if request.method == 'POST' and request.POST.get('action') == 'save':
        if form.is_valid():
            form.save()
            messages.success(request, _('اتحفظت بيانات البائع.'))
            return redirect(f'/superadmin/market/sellers/{pk}/?tab=profile')
    tab = request.GET.get('tab') or 'overview'
    items = list(store.items.order_by('-is_active', '-created_at'))
    from clients.views.catalog_views import SECTORS, item_category_label
    for it in items:
        it.category_label = item_category_label(SECTORS[store.sector], it)
    quotes = (CatalogQuoteRequest.objects.filter(item__store=store).select_related('item')
              .order_by('-created_at')[:50])
    offers = (TenderOffer.objects.filter(seller=seller).select_related('service_request')
              .order_by('-created_at')[:50])
    prices = (PriceQuote.objects.filter(seller=seller).select_related('spec')
              .order_by('-quoted_on', 'spec__sort')[:60])
    month = timezone.now() - timedelta(days=30)
    stats = {
        'items': sum(1 for i in items if i.is_active), 'hidden': sum(1 for i in items if i.admin_hidden),
        'quotes30': CatalogQuoteRequest.objects.filter(item__store=store, created_at__gte=month).count(),
        'quotes_done': CatalogQuoteRequest.objects.filter(item__store=store, status='done').count(),
        'offers': TenderOffer.objects.filter(seller=seller).count(),
        'won': TenderOffer.objects.filter(seller=seller, status='accepted').count(),
        'reviews': store.reviews.filter(is_visible=True).count(),
    }
    return render(request, 'clients/saas_admin/market_seller_detail.html', {
        'seller': seller, 'store': store, 'form': form, 'tab': tab, 'items': items, 'quotes': quotes,
        'offers': offers, 'prices': prices, 'stats': stats, 'profile': sp.completeness(seller),
        'gov_label': gov_label(seller.governorate) if seller.governorate else '',
        'spec_items': sp.specialty_items(store.sector, seller.specialties),
        'cap_items': sp.capability_items(store.sector, seller.capabilities),
        'wa': sp.whatsapp_digits(seller.whatsapp or seller.phone),
        'local_phone': seller.phone.replace('+20', '0', 1),
        'local_whatsapp': (seller.whatsapp or '').replace('+20', '0', 1),
    })


@_owner
@require_POST
def seller_action(request, pk):
    seller = get_object_or_404(MarketSeller.objects.select_related('store'), pk=pk)
    action = request.POST.get('action')
    msg = ''
    if action == 'show_store':
        CatalogStore.objects.filter(pk=seller.store_id).update(is_active=True)
        msg = _('المتجر ظاهر للعملاء.')
    elif action == 'hide_store':
        CatalogStore.objects.filter(pk=seller.store_id).update(is_active=False)
        msg = _('المتجر اتخفى عن العملاء.')
    elif action == 'block':
        _set_blocked(seller, True)
        msg = _('الحساب اتوقف واتخفى المتجر.')
    elif action == 'unblock':
        _set_blocked(seller, False)
        msg = _('الحساب اتفعّل — اظهر المتجر لو عايزه يرجع للعملاء.')
    elif action == 'logout_all':
        MarketSeller.objects.filter(pk=pk).update(session_token=uuid.uuid4())
        msg = _('البائع خرج من كل الأجهزة.')
    elif action == 'send_reset':
        if not seller.email:
            messages.error(request, _('البائع مالوش إيميل.'))
        else:
            from clients.views.market_seller_views import (
                RESET_TTL, _reset_hash, _reset_key, _send_reset_email,
            )
            import secrets

            from django.core.cache import cache
            code = f'{secrets.randbelow(1_000_000):06d}'
            cache.set(_reset_key(seller.pk), {'h': _reset_hash(seller.pk, code), 'tries': 0}, RESET_TTL)
            if _send_reset_email(seller, code):
                msg = _('اتبعت كود تغيير كلمة السر على إيميل البائع.')
            else:
                messages.error(request, _('الإيميل مااتبعتش — راجع إعدادات SMTP.'))
    elif action in ('item_hide', 'item_show'):
        item = get_object_or_404(CatalogItem, pk=request.POST.get('item'), store=seller.store)
        show = action == 'item_show'
        CatalogItem.objects.filter(pk=item.pk).update(is_active=show, admin_hidden=not show, pending_review=False)
        msg = _('المنتج ظاهر.') if show else _('المنتج اتخفى — والبائع مايقدرش يرجّعه.')
    if msg:
        messages.success(request, msg)
    tab = request.POST.get('tab') or 'overview'
    return redirect(f'/superadmin/market/sellers/{pk}/?tab={tab}')


__all__ = ['sellers_list', 'sellers_bulk', 'seller_detail', 'seller_action']

