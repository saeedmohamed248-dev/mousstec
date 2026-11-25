"""📈 بورصة أسعار المطابع — صفحات الإدخال (بائع سوق / شركة) + تأكيد بضغطة + المؤشر العام.

المنطق كله في clients/services/price_board.py.
"""
from __future__ import annotations

from django.db import connection
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from clients.models import Client
from clients.services import market_seller as seller_svc
from clients.services import price_board as pb
from clients.services import smart_import


def _suggest_key(provider):
    return f'pb_suggest:{provider.kind}:{provider.obj.pk}'


def _board_context(provider, request, *, errors=None, posted=None):
    """البنود + آخر سعر + (لو سعّر النهارده) متوسط السوق في محافظته — «ادّي تاخد».

    🤖 بعد رفع ملف أسعار (?suggested=1) الخانات بتتملى بالأسعار المقترحة من الملف ومتعلّمة.
    """
    from django.core.cache import cache
    specs = pb.active_specs()
    last = pb.last_quotes(provider)
    suggested = (cache.get(_suggest_key(provider)) or {}) if request.GET.get('suggested') == '1' else {}
    gov = (posted or {}).get('governorate') or pb.last_governorate(provider)
    done_today = pb.submitted_today(provider)
    market = pb.stats([s.pk for s in specs], gov) if done_today and gov else {}
    national = pb.stats([s.pk for s in specs]) if done_today else {}
    rows = []
    for s in specs:
        q = last.get(s.pk)
        raw = (posted or {}).get(f'p_{s.pk}')
        sug = suggested.get(s.pk)
        if raw is None and sug:
            raw = f'{sug[0].normalize():f}'
        rows.append({
            'spec': s, 'value': raw if raw is not None else (q.price.normalize() if q else ''),
            'suggested_from': sug[1] if sug else '',
            'last': q, 'market': market.get(s.pk), 'national': national.get(s.pk),
            'error': (errors or {}).get(s.pk),
        })
    return {
        'rows': rows, 'gov': gov, 'gov_label': pb.gov_label(gov) if gov else '',
        'governorates': pb.gov_choices(), 'done_today': done_today, 'has_last': bool(last),
        'products': [r for r in rows if r['spec'].kind == 'product'],
        'materials': [r for r in rows if r['spec'].kind == 'material'],
        'min_providers': pb.MIN_PROVIDERS, 'fresh_days': pb.FRESH_DAYS,
        'form_error': '', 'suggested_count': len(suggested), 'import_error': '',
        'accept': smart_import.ACCEPT,
    }


def _handle(request, provider, back):
    """POST: «احفظ الأسعار» أو «أسعاري زي امبارح» أو 🤖 «ارفع قايمة أسعارك». بيرجّع (redirect أو None, context)."""
    if request.method != 'POST':
        return None, _board_context(provider, request)
    if request.POST.get('action') == 'import':
        return _import_prices(request, provider, back)
    if request.POST.get('action') == 'confirm':
        n = pb.confirm_last(provider)
        return redirect(f'{back}?saved={n}'), None
    gov = request.POST.get('governorate', '')
    prices, errors = {}, {}
    for s in pb.active_specs():
        try:
            v = pb.parse_price(request.POST.get(f'p_{s.pk}'))
        except ValueError:
            errors[s.pk] = _('رقم مش مظبوط')
            continue
        if v is not None:
            prices[s.pk] = v
    ctx = None
    if gov not in pb.GOV_KEYS:
        ctx = _board_context(provider, request, errors=errors, posted=request.POST)
        ctx['form_error'] = _('اختار المحافظة.')
    elif errors:
        ctx = _board_context(provider, request, errors=errors, posted=request.POST)
        ctx['form_error'] = _('فيه أسعار مش مظبوطة — اكتب الرقم بس (زي 350).')
    elif not prices:
        ctx = _board_context(provider, request, posted=request.POST)
        ctx['form_error'] = _('اكتب سعر بند واحد على الأقل.')
    if ctx:
        return None, ctx
    n = pb.save_prices(provider, gov, prices)
    return redirect(f'{back}?saved={n}'), None


def _import_prices(request, provider, back):
    """🤖 ملف أسعار المطبعة (Excel/CSV/PDF/صورة) ⇒ أسعار مقترحة للبنود ⇒ تتملى في الفورم للمراجعة (مش بتتحفظ)."""
    from django.core.cache import cache

    from clients.services import smart_import as si
    upload = request.FILES.get('price_file')
    err = ''
    if not upload:
        err = _('اختار الملف.')
    else:
        try:
            rows, _how = si.read_any(upload, 'printing')
        except si.ImportFailed as e:
            err = str(e)
        else:
            sug = si.suggest_prices(rows, pb.active_specs())
            if not sug:
                err = _('قرينا الملف بس مالقيناش أسعار للبنود دي فيه — اكتبها بإيدك.')
            else:
                cache.set(_suggest_key(provider), sug, 3600)
                return redirect(f'{back}?suggested=1'), None
    ctx = _board_context(provider, request)
    ctx['import_error'] = err
    return None, ctx


# ─────────────── بائع السوق ───────────────

def seller_prices(request):
    seller = seller_svc.seller_from_request(request)
    if seller is None:
        return redirect('/marketplace/seller/login/?next=/marketplace/seller/prices/')
    if seller.store.sector != 'printing':
        return redirect('/marketplace/seller/')
    request.seller = seller
    resp, ctx = _handle(request, pb.Provider('seller', seller), '/marketplace/seller/prices/')
    if resp:
        return resp
    from .market_seller_views import _ctx
    return render(request, 'clients/marketplace/seller/prices.html',
                  _ctx(request, 'prices', saved=request.GET.get('saved'), **ctx))


# ─────────────── شركة على السيستم (مطبعة ERP) ───────────────

def merchant_prices(request):
    if not request.user.is_authenticated:
        return redirect('/secure-portal/')
    tenant = Client.objects.filter(schema_name=connection.schema_name).exclude(schema_name='public').first()
    if tenant is None or tenant.industry != 'printing':
        return redirect('/')
    resp, ctx = _handle(request, pb.Provider('merchant', tenant), '/marketplace/merchant/prices/')
    if resp:
        return resp
    return render(request, 'clients/marketplace/price_board/merchant.html',
                  {'tenant': tenant, 'saved': request.GET.get('saved'), **ctx})


# ─────────────── ⚡ لينك التذكير اليومي (من غير دخول) ───────────────

def prices_confirm(request, token):
    """لينك الواتساب: GET بيعرض الأسعار، والتأكيد POST (عشان معاينة اللينك في الواتساب ماتأكّدش لوحدها)."""
    provider = pb.Provider.from_token(token)
    if provider is None:
        return render(request, 'clients/marketplace/price_board/confirm.html', {'expired': True}, status=410)
    confirmed = False
    if request.method == 'POST':
        pb.confirm_last(provider)
        confirmed = True
    edit_url = '/marketplace/seller/prices/' if provider.kind == 'seller' else ''
    if provider.kind == 'merchant':
        dom = next((d.domain for d in provider.obj.domains.all() if d.is_primary), None)
        edit_url = f'https://{dom}/marketplace/merchant/prices/' if dom else ''
    ctx = _board_context(provider, request)
    return render(request, 'clients/marketplace/price_board/confirm.html', {
        'provider_name': provider.name, 'confirmed': confirmed, 'edit_url': edit_url, **ctx})


# ─────────────── 🌍 المؤشر العام ───────────────

def prices_index(request):
    gov = request.GET.get('gov', '')
    if gov not in pb.GOV_KEYS:
        gov = ''
    rows = pb.public_board('printing', gov or None)
    return render(request, 'clients/marketplace/price_board/index.html', {
        'products': [(s, st) for s, st in rows if s.kind == 'product'],
        'materials': [(s, st) for s, st in rows if s.kind == 'material'],
        'gov': gov, 'gov_label': pb.gov_label(gov) if gov else '',
        'governorates': pb.coverage('printing'), 'min_providers': pb.MIN_PROVIDERS,
        'fresh_days': pb.FRESH_DAYS, 'any_data': any(st for _s, st in rows),
    })

