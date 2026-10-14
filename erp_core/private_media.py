"""
🔒 ملفات خاصة: مابتتعرضش لأي حد من `/media/` مباشرة.

🐛 [FIX]: كل الملفات المرفوعة كانت بتتخدم من Caddy لأي حد من غير تسجيل دخول،
وعلى أي دومين (فولدر واحد لكل الشركات)، وبأسماء الملفات الأصلية في فولدرات
بالسنة/الشهر — يعني كشف حساب بنك شركة أو صورة وش موظف أو ملف عملاء متستورد
كان ممكن يتنزّل بتخمين الاسم.

دلوقتي الفولدرات الحساسة دي بتعدّي على Django (Caddy بيحوّلها — deploy/Caddyfile):
  * لازم مستخدم داخل،
  * والملف لازم يكون مسجّل في بيانات **الشركة الحالية** (يعني شركة تانية مش هتلاقيه)،
  * ودور المستخدم مسموح له بنوع الملف ده.
"""
import mimetypes
import os
import re

from django.apps import apps
from django.conf import settings
from django.db import connection
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import redirect

ALL_STAFF = None
FINANCE = ('admin', 'manager', 'accountant')
STOCK_DOCS = ('admin', 'manager', 'accountant', 'stock', 'purchasing')
DATA_IMPORT = ('admin', 'manager', 'accountant', 'stock', 'purchasing')
HR_FILES = ('admin', 'manager', 'hr')

# بادئة المسار ← (الموديل، الحقل، الأدوار المسموحة)
PRIVATE_PREFIXES = {
    'bank_statements/': ('inventory.BankStatement', 'uploaded_file', FINANCE),
    'imports/': ('inventory.ImportSession', 'uploaded_file', DATA_IMPORT),
    'customs_docs/': ('inventory.ScrapDismantlingJob', 'customs_doc', STOCK_DOCS),
    'signatures/': ('inventory.CustomerFeedback', 'signature_image', ALL_STAFF),
    'hr/faces/': ('hr.Employee', 'face_photo', HR_FILES),
    # إيصالات دفع الاشتراك للمنصة (جدول مشترك): صاحب المنصة أو الشركة صاحبة الإيصال
    'manual_payments/': ('clients.ManualPaymentReceipt', 'receipt_image', FINANCE),
}
URL_PATTERN = r'^media/(?P<path>(?:%s).+)$' % '|'.join(re.escape(p) for p in PRIVATE_PREFIXES)


def _role(user):
    from mobile_api.permissions import role_of
    return role_of(user)


def _owner_row(model, field, path):
    return model.objects.filter(**{field: path}).first()


def private_media(request, path):
    prefix = next((p for p in PRIVATE_PREFIXES if path.startswith(p)), None)
    if prefix is None or '..' in path.split('/'):
        raise Http404
    if not request.user.is_authenticated:
        return redirect(f"/login/?next={request.path}")

    model_label, field, roles = PRIVATE_PREFIXES[prefix]
    model = apps.get_model(model_label)
    row = _owner_row(model, field, path)
    if row is None:
        raise Http404   # الملف مش بتاع الشركة دي (أو مش موجود)

    user = request.user
    if model_label == 'clients.ManualPaymentReceipt':
        is_platform_owner = user.is_superuser and connection.schema_name == 'public'
        own_tenant = (row.tenant_id and row.tenant_id == getattr(connection.tenant, 'pk', None)
                      and (user.is_superuser or _role(user) in roles))
        if not (is_platform_owner or own_tenant):
            raise Http404
    elif model_label == 'hr.Employee':
        if not (user.is_superuser or _role(user) in roles or row.user_id == user.pk
                or getattr(getattr(user, 'hr_employee', None), 'is_hr_manager', False)):
            return HttpResponseForbidden('صلاحيتك مش كفاية للملف ده.')
    elif roles is not None and not (user.is_superuser or _role(user) in roles):
        return HttpResponseForbidden('صلاحيتك مش كفاية للملف ده.')

    full = os.path.join(settings.MEDIA_ROOT, path)
    if not os.path.isfile(full):
        raise Http404
    ctype, _enc = mimetypes.guess_type(full)
    resp = FileResponse(open(full, 'rb'), content_type=ctype or 'application/octet-stream')
    resp['Cache-Control'] = 'private, no-store'
    resp['X-Content-Type-Options'] = 'nosniff'
    return resp
