"""🖨️ صفحات سوق المطابع."""
import mimetypes
import os
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db.models import F, Q
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import auth
from .contact_guard import clean_text, find_contact
from .models import (ATTACH_EXT, IMAGE_EXT, PROOF_EXT, MarketAccount, MarketMessage, MarketOffer,
                     MarketOrder, MarketPayment, MarketProduct, MarketProof, check_upload, transfer_fee)
from .notify import notify

GOVERNORATES = (
    'القاهرة', 'الجيزة', 'الإسكندرية', 'القليوبية', 'الشرقية', 'الدقهلية', 'الغربية', 'المنوفية',
    'البحيرة', 'كفر الشيخ', 'دمياط', 'بورسعيد', 'الإسماعيلية', 'السويس', 'الفيوم', 'بني سويف',
    'المنيا', 'أسيوط', 'سوهاج', 'قنا', 'الأقصر', 'أسوان', 'البحر الأحمر', 'مطروح', 'شمال سيناء',
    'جنوب سيناء', 'الوادي الجديد',
)
PROOF_MAX_MB = 50
ATTACH_MAX_MB = 20
IMAGE_MAX_MB = 5


def _ctx(request, **extra):
    extra.setdefault('account', auth.current_account(request))
    return extra


def _guarded(request, account, text, max_len=None):
    """نص حر من المستخدم — لو فيه وسيلة تواصل بنعدّ المحاولة ونرمي ValidationError."""
    try:
        return clean_text(text, max_len)
    except ValidationError:
        if account is not None:
            MarketAccount.objects.filter(pk=account.pk).update(blocked_attempts=F('blocked_attempts') + 1)
        raise


def _err(request, exc):
    for m in getattr(exc, 'messages', [str(exc)]):
        messages.error(request, m)


def _back(request, order):
    return redirect('print_market:order_detail', code=order.code)


# ───────────────────────── عام ─────────────────────────

def home(request):
    qs = MarketProduct.objects.filter(is_active=True, shop__is_active=True).select_related('shop')
    q = (request.GET.get('q') or '').strip()[:80]
    cat = request.GET.get('cat') or ''
    city = request.GET.get('city') or ''
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(shop__name__icontains=q))
    if cat in dict(MarketProduct.CATEGORY_CHOICES):
        qs = qs.filter(category=cat)
    if city in GOVERNORATES:
        qs = qs.filter(shop__city=city)
    page = Paginator(qs, 24).get_page(request.GET.get('page'))
    return render(request, 'print_market/home.html', _ctx(
        request, page=page, q=q, cat=cat, city=city, categories=MarketProduct.CATEGORY_CHOICES,
        governorates=GOVERNORATES))


def home_for_account(request):
    acc = auth.current_account(request)
    if acc is None:
        return redirect('print_market:home')
    return redirect('print_market:shop_dashboard' if acc.is_shop else 'print_market:my_orders')


def shop_page(request, pk):
    shop = get_object_or_404(MarketAccount, pk=pk, role='shop', is_active=True)
    products = shop.products.filter(is_active=True)
    return render(request, 'print_market/shop.html', _ctx(request, shop=shop, products=products))


def product_detail(request, pk):
    product = get_object_or_404(MarketProduct.objects.select_related('shop'), pk=pk, is_active=True,
                                shop__is_active=True)
    return render(request, 'print_market/product.html', _ctx(request, product=product, shop=product.shop))


# ───────────────────────── حسابات ─────────────────────────

def signup(request):
    role = request.POST.get('role') or request.GET.get('role') or 'customer'
    if role not in ('shop', 'customer'):
        role = 'customer'
    data = {'role': role}
    if request.method == 'POST':
        data.update({k: (request.POST.get(k) or '').strip() for k in ('name', 'email', 'city')})
        email = data['email'].lower()
        password = request.POST.get('password') or ''
        errors = []
        if auth.rate_limited(request, 'signup', limit=8, window=3600):
            errors.append(_('محاولات كتير — جرّب كمان شوية.'))
        try:
            validate_email(email)
        except ValidationError:
            errors.append(_('اكتب إيميل صحيح.'))
        if not data['name']:
            errors.append(_('اكتب الاسم.'))
        elif find_contact(data['name']):
            errors.append(find_contact(data['name']))
        if role == 'shop' and data['city'] not in GOVERNORATES:
            errors.append(_('اختار محافظة المطبعة.'))
        if MarketAccount.objects.filter(email=email).exists():
            errors.append(_('الإيميل ده متسجّل قبل كده — ادخل بيه.'))
        try:
            validate_password(password)
        except ValidationError as e:
            errors.extend(e.messages)
        if not errors:
            acc = MarketAccount(role=role, email=email, name=data['name'][:120],
                                city=data['city'] if data['city'] in GOVERNORATES else '')
            acc.set_password(password)
            acc.save()
            messages.success(request, _('أهلاً بيك! حسابك اتعمل.'))
            dest = reverse('print_market:shop_dashboard') if acc.is_shop else auth.safe_next(
                request, reverse('print_market:my_orders'))
            return auth.login(redirect(dest), acc)
        for e in errors:
            messages.error(request, e)
    return render(request, 'print_market/signup.html', _ctx(
        request, data=data, governorates=GOVERNORATES, next=request.GET.get('next', '')))


def login_view(request):
    email = ''
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        password = request.POST.get('password') or ''
        if auth.rate_limited(request, 'login', limit=10, window=900):
            messages.error(request, _('محاولات كتير — جرّب كمان ربع ساعة.'))
        else:
            acc = MarketAccount.objects.filter(email=email).first()
            if acc and acc.check_password(password):
                if not acc.is_active:
                    messages.error(request, _('الحساب ده موقوف — كلّم دعم Mouss Tec.'))
                else:
                    default = reverse('print_market:shop_dashboard' if acc.is_shop else 'print_market:my_orders')
                    return auth.login(redirect(auth.safe_next(request, default)), acc)
            else:
                messages.error(request, _('الإيميل أو كلمة السر غلط.'))
    return render(request, 'print_market/login.html', _ctx(request, email=email, next=request.GET.get('next', '')))


@require_POST
def logout_view(request):
    acc = auth.current_account(request)
    if acc:
        acc.rotate_session()
    return auth.logout(redirect('print_market:home'))


# ───────────────────────── المطبعة ─────────────────────────

@auth.require_account('shop')
def shop_dashboard(request):
    shop = request.pm_account
    orders = shop.shop_orders.select_related('customer').prefetch_related('offers', 'proofs')
    active = [o for o in orders if o.status in MarketOrder.ACTIVE]
    done = [o for o in orders if o.status not in MarketOrder.ACTIVE][:30]
    return render(request, 'print_market/shop_dashboard.html', _ctx(
        request, shop=shop, active=active, done=done, products=shop.products.all(),
        categories=MarketProduct.CATEGORY_CHOICES, missing_payout=not shop.payout_account))


def _product_from_post(request, shop, product):
    title = _guarded(request, shop, request.POST.get('title'), 150)
    if not title:
        raise ValidationError(_('اكتب اسم المنتج.'))
    product.title = title
    product.description = _guarded(request, shop, request.POST.get('description'), 2000)
    product.unit = _guarded(request, shop, request.POST.get('unit'), 40)
    cat = request.POST.get('category')
    product.category = cat if cat in dict(MarketProduct.CATEGORY_CHOICES) else 'other'
    price = (request.POST.get('price_from') or '').strip()
    try:
        product.price_from = Decimal(price) if price else None
    except InvalidOperation:
        raise ValidationError(_('السعر لازم يكون رقم.')) from None
    if product.price_from is not None and product.price_from < 0:
        raise ValidationError(_('السعر لازم يكون رقم.'))
    product.is_active = request.POST.get('is_active', 'on') == 'on'
    img = request.FILES.get('image')
    if img:
        check_upload(img, IMAGE_EXT, IMAGE_MAX_MB)
        product.image = img
    product.save()
    return product


@auth.require_account('shop')
def product_form(request, pk=None):
    shop = request.pm_account
    product = get_object_or_404(MarketProduct, pk=pk, shop=shop) if pk else MarketProduct(shop=shop)
    if request.method == 'POST':
        try:
            _product_from_post(request, shop, product)
            messages.success(request, _('المنتج اتحفظ.'))
            return redirect('print_market:shop_dashboard')
        except ValidationError as e:
            _err(request, e)
    return render(request, 'print_market/product_form.html', _ctx(
        request, product=product, categories=MarketProduct.CATEGORY_CHOICES))


@require_POST
@auth.require_account('shop')
def product_delete(request, pk):
    product = get_object_or_404(MarketProduct, pk=pk, shop=request.pm_account)
    if product.orders.exists():
        product.is_active = False   # عليه طلبات — بنخفيه بس
        product.save(update_fields=['is_active'])
    else:
        product.delete()
    messages.success(request, _('المنتج اتشال من السوق.'))
    return redirect('print_market:shop_dashboard')


@auth.require_account()
def account_settings(request):
    acc = request.pm_account
    if request.method == 'POST':
        try:
            name = _guarded(request, acc, request.POST.get('name'), 120)
            if not name:
                raise ValidationError(_('اكتب الاسم.'))
            acc.name = name
            city = request.POST.get('city') or ''
            acc.city = city if city in GOVERNORATES else acc.city
            if acc.is_shop:
                acc.about = _guarded(request, acc, request.POST.get('about'), 1000)
                method = request.POST.get('payout_method') or ''
                acc.payout_method = method if method in dict(MarketAccount.PAYOUT_CHOICES) else ''
                acc.payout_account = (request.POST.get('payout_account') or '').strip()[:100]
                logo = request.FILES.get('logo')
                if logo:
                    check_upload(logo, IMAGE_EXT, IMAGE_MAX_MB)
                    acc.logo = logo
            new_pw = request.POST.get('new_password') or ''
            if new_pw:
                if not acc.check_password(request.POST.get('current_password') or ''):
                    raise ValidationError(_('كلمة السر الحالية غلط.'))
                validate_password(new_pw)
                acc.set_password(new_pw)
                acc.save()
                acc.rotate_session()
                messages.success(request, _('البيانات اتحفظت.'))
                return auth.login(redirect('print_market:settings'), acc)
            acc.save()
            messages.success(request, _('البيانات اتحفظت.'))
            return redirect('print_market:settings')
        except ValidationError as e:
            _err(request, e)
    return render(request, 'print_market/settings.html', _ctx(
        request, acc=acc, governorates=GOVERNORATES, payout_choices=MarketAccount.PAYOUT_CHOICES))


# ───────────────────────── العميل ─────────────────────────

@auth.require_account('customer')
def my_orders(request):
    orders = request.pm_account.orders.select_related('shop').prefetch_related('offers', 'proofs')
    return render(request, 'print_market/my_orders.html', _ctx(request, orders=orders))


@auth.require_account('customer')
def order_new(request, shop_id):
    shop = get_object_or_404(MarketAccount, pk=shop_id, role='shop', is_active=True)
    product = None
    pid = request.POST.get('product') or request.GET.get('product')
    if pid and str(pid).isdigit():
        product = shop.products.filter(pk=int(pid), is_active=True).first()
    data = {'title': product.title if product else '', 'details': '', 'quantity': '1'}
    if request.method == 'POST':
        cust = request.pm_account
        data.update({k: request.POST.get(k, '') for k in ('title', 'details', 'quantity')})
        try:
            title = _guarded(request, cust, data['title'], 150)
            details = _guarded(request, cust, data['details'], 3000)
            if not title or not details:
                raise ValidationError(_('اكتب عنوان الطلب وتفاصيله.'))
            try:
                qty = int(data['quantity'])
            except (TypeError, ValueError):
                qty = 0
            if not 1 <= qty <= 10_000_000:
                raise ValidationError(_('اكتب كمية صحيحة.'))
            att = request.FILES.get('attachment')
            check_upload(att, ATTACH_EXT, ATTACH_MAX_MB)
            order = MarketOrder.objects.create(customer=cust, shop=shop, product=product, title=title,
                                               details=details, quantity=qty, attachment=att or '')
            notify(request, shop, _('طلب جديد'), _('وصلك طلب جديد: %(t)s — قدّم عرضك.') % {'t': title}, order)
            messages.success(request, _('طلبك اتبعت للمطبعة — هيوصلك العرض هنا.'))
            return _back(request, order)
        except ValidationError as e:
            _err(request, e)
    return render(request, 'print_market/order_new.html', _ctx(
        request, shop=shop, product=product, data=data))


# ───────────────────────── الطلب (للطرفين) ─────────────────────────

def _order_for(request, code):
    acc = request.pm_account
    order = get_object_or_404(MarketOrder.objects.select_related('customer', 'shop', 'product'), code=code)
    if acc.pk not in (order.customer_id, order.shop_id):
        raise Http404
    return order


@auth.require_account()
def order_detail(request, code):
    order = _order_for(request, code)
    acc = request.pm_account
    offer = order.accepted_offer
    fees = {}
    if offer and order.status == 'awaiting_payment':
        for m, label in MarketPayment.METHOD_CHOICES:
            fee = transfer_fee(offer.price, m)
            fees[m] = {'label': label, 'fee': fee, 'total': offer.price + fee}
    return render(request, 'print_market/order_detail.html', _ctx(
        request, order=order, is_shop=acc.pk == order.shop_id, is_customer=acc.pk == order.customer_id,
        offers=order.offers.all(), proofs=order.proofs.all(), payments=order.payments.all(),
        chat=order.messages.select_related('sender'), offer=offer, pending_offer=order.pending_offer,
        fees=fees, instapay_handle=settings.INSTAPAY_HANDLE, instapay_link=settings.INSTAPAY_LINK,
        vodafone_number=settings.PRINT_MARKET_VODAFONE_CASH_NUMBER,
        proof_open=order.status in MarketProof.OPEN_ORDER_STATUSES))


def _action(view):
    """POST على طلب: بيجيب الطلب، ينفّذ، ويرجع لصفحته برسالة نجاح/خطأ."""
    def wrapper(request, code, *args, **kwargs):
        order = _order_for(request, code)
        try:
            msg = view(request, order, *args, **kwargs)
            if msg:
                messages.success(request, msg)
        except ValidationError as e:
            _err(request, e)
        return _back(request, order)
    wrapper.__name__ = view.__name__
    return require_POST(auth.require_account()(wrapper))


@_action
def offer_submit(request, order):
    shop = request.pm_account
    note = _guarded(request, shop, request.POST.get('note'), 500)
    try:
        price = Decimal((request.POST.get('price') or '').strip())
        days = int(request.POST.get('delivery_days') or 0)
    except (InvalidOperation, ValueError):
        raise ValidationError(_('اكتب السعر والمدة أرقام.')) from None
    MarketOffer.submit(order, shop, price, days, note)
    notify(request, order.customer, _('وصلك عرض سعر'), _('المطبعة بعتت عرض على طلبك «%(t)s».') % {'t': order.title},
           order)
    return _('العرض اتبعت للعميل.')


@_action
def offer_respond(request, order, offer_id):
    offer = get_object_or_404(MarketOffer, pk=offer_id, order=order)
    accept = request.POST.get('action') == 'accept'
    offer.respond(request.pm_account, accept)
    if accept:
        notify(request, order.shop, _('العميل قبل العرض'), _('العميل قبل عرضك على «%(t)s» — هيدفع لـ Mouss Tec.')
               % {'t': order.title}, order)
        return _('قبلت العرض — ادفع دلوقتي عشان المطبعة تبدأ.')
    notify(request, order.shop, _('العميل رفض العرض'), _('العميل رفض عرضك على «%(t)s» — ممكن تبعت عرض تاني.')
           % {'t': order.title}, order)
    return _('رفضت العرض — المطبعة تقدر تبعتلك عرض جديد.')


@_action
def payment_submit(request, order):
    method = request.POST.get('method') or ''
    reference = (request.POST.get('reference') or '').strip()
    receipt = request.FILES.get('receipt')
    if method not in dict(MarketPayment.METHOD_CHOICES):
        raise ValidationError(_('اختار طريقة الدفع.'))
    if not reference or not receipt:
        raise ValidationError(_('اكتب رقم العملية وارفع صورة الإيصال.'))
    check_upload(receipt, IMAGE_EXT, IMAGE_MAX_MB)
    MarketPayment.submit(order, request.pm_account, method, reference, receipt)
    return _('استلمنا الإيصال — هنأكّده في أقرب وقت.')


@_action
def proof_submit(request, order):
    shop = request.pm_account
    f = request.FILES.get('file')
    if not f:
        raise ValidationError(_('ارفع ملف البروفة (صورة أو PDF أو فيديو).'))
    check_upload(f, PROOF_EXT, PROOF_MAX_MB)
    note = _guarded(request, shop, request.POST.get('note'), 500)
    MarketProof.submit(order, shop, f, note)
    notify(request, order.customer, _('وصلتك بروفة'), _('المطبعة بعتت بروفة على «%(t)s» — وافق أو اطلب تعديل.')
           % {'t': order.title}, order)
    return _('البروفة اتبعتت للعميل.')


@_action
def proof_respond(request, order, proof_id):
    proof = get_object_or_404(MarketProof, pk=proof_id, order=order)
    approve = request.POST.get('action') == 'approve'
    comment = _guarded(request, request.pm_account, request.POST.get('comment'), 2000)
    proof.respond(request.pm_account, approve, comment)
    if approve:
        notify(request, order.shop, _('العميل وافق على البروفة'),
               _('العميل وافق على بروفة %(v)s في «%(t)s».') % {'v': proof.version, 't': order.title}, order)
        return _('وافقت على البروفة.')
    notify(request, order.shop, _('العميل طلب تعديل'),
           _('العميل طلب تعديل على بروفة %(v)s في «%(t)s».') % {'v': proof.version, 't': order.title}, order)
    return _('طلب التعديل اتبعت للمطبعة.')


@_action
def message_send(request, order):
    if order.status in ('cancelled', 'completed'):
        raise ValidationError(_('الطلب مقفول.'))
    body = _guarded(request, request.pm_account, request.POST.get('body'), 2000)
    if not body:
        return None
    MarketMessage.objects.create(order=order, sender=request.pm_account, body=body)
    other = order.shop if request.pm_account.pk == order.customer_id else order.customer
    notify(request, other, _('رسالة جديدة'), _('وصلتك رسالة على «%(t)s».') % {'t': order.title}, order)
    return None


@_action
def order_cancel(request, order):
    order.cancel(request.pm_account)
    other = order.shop if request.pm_account.pk == order.customer_id else order.customer
    notify(request, other, _('الطلب اتلغى'), _('الطلب «%(t)s» اتلغى.') % {'t': order.title}, order)
    return _('الطلب اتلغى.')


@_action
def order_delivered(request, order):
    order.mark_delivered(request.pm_account)
    notify(request, order.customer, _('طلبك اتسلّم'), _('المطبعة سجّلت تسليم «%(t)s» — أكّد إنك استلمت.')
           % {'t': order.title}, order)
    return _('اتسجّل التسليم — مستنيين تأكيد العميل.')


@_action
def order_received(request, order):
    order.confirm_received(request.pm_account)
    notify(request, order.shop, _('العميل أكّد الاستلام'),
           _('العميل أكّد استلام «%(t)s» — Mouss Tec هتحوّلك المبلغ.') % {'t': order.title}, order)
    return _('شكراً! الطلب اكتمل.')


# ───────────────────────── الملفات الخاصة ─────────────────────────

_FILE_KINDS = {
    'attachment': (MarketOrder, 'attachment', lambda o: o),
    'proof': (MarketProof, 'file', lambda o: o.order),
    'receipt': (MarketPayment, 'receipt', lambda o: o.order),
}
_RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)$')


def private_file(request, kind, pk):
    """ملفات الطلب (مرفق/بروفة/إيصال) — لطرفين الطلب بس (الإيصال للعميل بس) أو صاحب المنصة."""
    if kind not in _FILE_KINDS:
        raise Http404
    model, field, order_of = _FILE_KINDS[kind]
    obj = get_object_or_404(model, pk=pk)
    order = order_of(obj)
    acc = auth.current_account(request)
    user = getattr(request, 'user', None)
    is_owner = bool(user and user.is_authenticated and user.is_superuser)
    allowed = {order.customer_id} if kind == 'receipt' else {order.customer_id, order.shop_id}
    if not (is_owner or (acc and acc.pk in allowed)):
        raise Http404
    f = getattr(obj, field)
    if not f:
        raise Http404
    try:
        path = f.path
    except NotImplementedError:   # تخزين سحابي (S3) — من غير Range
        path = None
    ctype = mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
    inline = (ctype.startswith(('image/', 'video/')) and ctype != 'image/svg+xml') or ctype == 'application/pdf'
    if path is None:
        try:
            fh = f.open('rb')
        except (FileNotFoundError, OSError):
            raise Http404 from None
        return _harden(FileResponse(fh, content_type=ctype, as_attachment=not inline,
                                    filename=os.path.basename(f.name)), inline)
    if not os.path.isfile(path):
        raise Http404
    size = os.path.getsize(path)
    rng = _RANGE_RE.match(request.headers.get('Range', '')) if ctype.startswith('video/') else None
    if rng and (rng.group(1) or rng.group(2)):
        # 🎬 Range عشان الفيديو يشتغل على الموبايل (iPhone بيطلبه إجباري)
        if rng.group(1):
            start = int(rng.group(1))
            end = min(int(rng.group(2)) if rng.group(2) else size - 1, size - 1)
        else:
            start, end = max(size - int(rng.group(2)), 0), size - 1
        if start > end or start >= size:
            resp = HttpResponse(status=416)
            resp['Content-Range'] = f'bytes */{size}'
            return resp

        def _chunks():
            left = end - start + 1
            with open(path, 'rb') as fh:
                fh.seek(start)
                while left > 0:
                    data = fh.read(min(65536, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data
        resp = StreamingHttpResponse(_chunks(), status=206, content_type=ctype)
        resp['Content-Range'] = f'bytes {start}-{end}/{size}'
        resp['Content-Length'] = str(end - start + 1)
    else:
        resp = FileResponse(open(path, 'rb'), content_type=ctype, as_attachment=not inline,
                            filename=os.path.basename(path))
    resp['Accept-Ranges'] = 'bytes'
    return _harden(resp, inline)


def _harden(resp, inline):
    resp['Cache-Control'] = 'private, no-store'
    resp['X-Content-Type-Options'] = 'nosniff'
    if not inline:
        resp['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return resp



# ───────────────────────── نسيت كلمة السر ─────────────────────────

_RESET_SALT = 'print_market.reset'
_RESET_MAX_AGE = 60 * 60 * 2


def password_forgot(request):
    from django.core import signing
    from django.core.mail import send_mail
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        if not auth.rate_limited(request, 'forgot', limit=5, window=3600):
            acc = MarketAccount.objects.filter(email=email, is_active=True).first()
            if acc:
                # التوكن مربوط بالـ session_key: بيبطل بعد ما الكلمة تتغيّر
                token = signing.dumps({'id': acc.pk, 'k': str(acc.session_key)}, salt=_RESET_SALT)
                link = request.build_absolute_uri(reverse('print_market:password_reset', args=[token]))
                send_mail(_('سوق المطابع — تغيير كلمة السر'),
                          _('افتح اللينك ده عشان تغيّر كلمة السر (صالح ساعتين):') + f'\n{link}',
                          settings.DEFAULT_FROM_EMAIL, [acc.email], fail_silently=True)
        # نفس الرسالة دايماً — مانكشفش الإيميلات المتسجّلة
        messages.success(request, _('لو الإيميل متسجّل هيوصلك لينك تغيير كلمة السر.'))
        return redirect('print_market:login')
    return render(request, 'print_market/password_forgot.html', _ctx(request))


def password_reset(request, token):
    from django.core import signing
    try:
        data = signing.loads(token, salt=_RESET_SALT, max_age=_RESET_MAX_AGE)
        acc = MarketAccount.objects.get(pk=data['id'], session_key=data['k'], is_active=True)
    except (signing.BadSignature, MarketAccount.DoesNotExist, KeyError, TypeError, ValueError):
        messages.error(request, _('اللينك ده انتهى أو مش صحيح — اطلب لينك جديد.'))
        return redirect('print_market:password_forgot')
    if request.method == 'POST':
        pw = request.POST.get('password') or ''
        try:
            validate_password(pw)
            acc.set_password(pw)
            acc.save()
            acc.rotate_session()
            messages.success(request, _('كلمة السر اتغيّرت.'))
            dest = reverse('print_market:shop_dashboard' if acc.is_shop else 'print_market:my_orders')
            return auth.login(redirect(dest), acc)
        except ValidationError as e:
            _err(request, e)
    return render(request, 'print_market/password_reset.html', _ctx(request))
