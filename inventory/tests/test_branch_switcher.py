"""مبدّل الفروع — اختبارات انحدار لمشكلة "Bad Request (400)".

المشكلة اللي بنغطّيها هنا: تبديل الفرع كان بيتعمل بـ POST، فكان بيعدّي على
طبقات كتير قادرة ترفض الطلب **قبل** ما يوصل للـ view أصلاً:

  • فحص CSRF (توكن قديم/كوكي ضاعت بعد ما الصفحة قعدت مفتوحة).
  • تحليل جسم الطلب (TooManyFieldsSent / RequestDataTooBig /
    MultiPartParserError → كلها SuspiciousOperation → 400).
  • حارس الباقات (TenantQuotaMiddleware) اللي بيمنع أي POST في وضع
    "القراءة فقط" ويعمل redirect للـ Referer الخام — ولو الـ Referer جاي
    بسكيمة تطبيق (whatsapp:// … ) بيبقى DisallowedRedirect → 400 كمان.
  • بوابة الحضور (AttendanceGateMiddleware) اللي كانت بتبلع الـ POST
    وتحوّله GET، فالفرع مكانش بيتبدّل أصلاً لأدوار الفنيين.

الحل: التبديل بقى GET navigation عادي. الاختبارات دي بتثبّت السلوك الجديد
(GET شغال + الـ POST القديم لسه شغال) وبتثبّت إن الصلاحيات ما اتكسرتش.
"""
from django.test import RequestFactory
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware

from inventory.models import BranchAccess
from inventory.views_staff import switch_branch, _parse_branch_id, _safe_next

from .base import ERPTenantTestCase
from .factories import make_branch, make_employee


def _branches(*names):
    """يرجّع فروع جاهزة للاختبار من غير ما يصطدم بحد الباقة (فرعين).

    الـ tenant الجديد بيتولّد ومعاه فرع افتراضي، و TransactionTestCase
    بيفضّي الجداول بين الاختبارات — فعدد الفروع الموجود بيختلف من اختبار
    للتاني. بنعيد استخدام الموجود ومبنـنشئش غير الناقص.
    """
    from inventory.models import Branch
    out = list(Branch.objects.order_by('id'))
    for name in names[len(out):]:
        out.append(make_branch(name))
    return out[:len(names)]


def _wire(user, tenant, method='get', path='/system/switch-branch/', data=None):
    rf = RequestFactory()
    req = getattr(rf, method)(path, data or {})
    SessionMiddleware(lambda r: None).process_request(req)
    req.session.save()
    AuthenticationMiddleware(lambda r: None).process_request(req)
    req.user = user
    req.tenant = tenant
    return req


class BranchSwitchGetTests(ERPTenantTestCase):
    """التبديل شغال بـ GET (المسار الجديد) وبـ POST (توافق مع صفحات قديمة)."""

    def setUp(self):
        self.b1, self.b2 = _branches('فرع القاهرة', 'فرع طنطا')
        self.admin, self.admin_prof = make_employee('owner', role='admin', branch=self.b1)

    def _switch(self, user, method, branch, next_url='/system/dashboard/'):
        req = _wire(user, self.tenant, method=method,
                    data={'branch': branch, 'next': next_url})
        return req, switch_branch(req)

    def test_get_sets_active_branch(self):
        req, resp = self._switch(self.admin, 'get', self.b2.id)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], '/system/dashboard/')
        self.assertEqual(req.session['active_branch_id'], self.b2.id)

    def test_get_zero_clears_active_branch(self):
        req, _ = self._switch(self.admin, 'get', self.b2.id)
        self.assertIn('active_branch_id', req.session)
        req2, resp = self._switch(self.admin, 'get', 0)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('active_branch_id', req2.session)

    def test_legacy_post_still_works(self):
        """أي صفحة اتكاشت في متصفح المستخدم لسه بتبعت POST — لازم تفضل شغالة."""
        req, resp = self._switch(self.admin, 'post', self.b2.id)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(req.session['active_branch_id'], self.b2.id)

    def test_never_raises_on_garbage_input(self):
        """أي قيمة بايظة بترجّع redirect — مش استثناء يطلّع صفحة 400/500."""
        for junk in ('', 'abc', '../../etc', '1,608', '  ', '9' * 40, None):
            req = _wire(self.admin, self.tenant, data={'branch': junk} if junk is not None else {})
            resp = switch_branch(req)
            self.assertEqual(resp.status_code, 302, f'branch={junk!r}')

    def test_thousand_separator_value_is_accepted(self):
        """USE_THOUSAND_SEPARATOR ممكن يخلّي قالب قديم يبعت "1,608" بدل 1608."""
        formatted = f'{self.b2.id:,}'
        req, resp = self._switch(self.admin, 'get', formatted)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(req.session['active_branch_id'], self.b2.id)


class BranchSwitchSecurityTests(ERPTenantTestCase):
    """الصلاحيات والـ open-redirect لازم يفضلوا متحكّم فيهم بعد التحويل لـ GET."""

    def setUp(self):
        from inventory.models import Branch
        self.b1, self.b2 = _branches('فرع القاهرة', 'فرع طنطا')
        # الباقة الافتراضية بتسمح بفرعين بس، والـ quota signal بيرمي
        # ValidationError على الـ save. bulk_create مبيـ triggerش الـ signal،
        # وإحنا محتاجين الفرع التالت كـ "فرع مش مسموح للموظف" مش أكتر.
        self.b3 = Branch.objects.bulk_create([Branch(name='فرع أسوان')])[0]
        self.emp, self.prof = make_employee('cashier1', role='cashier', branch=self.b1)
        BranchAccess.objects.create(employee=self.prof, branch=self.b2, can_edit=False)

    def test_employee_can_switch_to_allowed_branch(self):
        req = _wire(self.emp, self.tenant, data={'branch': self.b2.id})
        switch_branch(req)
        self.assertEqual(req.session['active_branch_id'], self.b2.id)

    def test_employee_cannot_switch_to_foreign_branch(self):
        req = _wire(self.emp, self.tenant, data={'branch': self.b3.id})
        resp = switch_branch(req)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('active_branch_id', req.session)

    def test_external_next_is_rejected(self):
        for bad in ('https://evil.example/x', '//evil.example/x', 'javascript:alert(1)',
                    '/ok/\r\nX-Injected: 1'):
            req = _wire(self.emp, self.tenant,
                        data={'branch': self.b2.id, 'next': bad})
            resp = switch_branch(req)
            self.assertEqual(resp['Location'], '/system/dashboard/', f'next={bad!r}')

    def test_internal_next_is_honored(self):
        req = _wire(self.emp, self.tenant,
                    data={'branch': self.b2.id, 'next': '/system/products/?q=bmw'})
        resp = switch_branch(req)
        self.assertEqual(resp['Location'], '/system/products/?q=bmw')


class BranchSwitchHelperTests(ERPTenantTestCase):
    """الدوال المساعدة — الوحدات الصغيرة اللي الـ view بيتكل عليها."""

    def test_parse_branch_id(self):
        self.assertEqual(_parse_branch_id('1608'), 1608)
        self.assertEqual(_parse_branch_id('1,608'), 1608)
        self.assertEqual(_parse_branch_id(' 12 '), 12)
        self.assertEqual(_parse_branch_id('abc'), 0)
        self.assertEqual(_parse_branch_id(None), 0)
        self.assertEqual(_parse_branch_id(''), 0)

    def test_safe_next(self):
        self.assertEqual(_safe_next('/system/x/'), '/system/x/')
        self.assertEqual(_safe_next('//evil'), '/system/dashboard/')
        self.assertEqual(_safe_next('http://evil/x'), '/system/dashboard/')
        self.assertEqual(_safe_next('/a\nb'), '/system/dashboard/')
        self.assertEqual(_safe_next(None), '/system/dashboard/')
