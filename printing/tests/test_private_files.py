"""ملفات تصميم العملاء الأصلية في المطبعة — مش لأي حد يعرف المسار."""
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import connection
from django.test import Client, override_settings

from inventory.tests.base import ERPTenantTestCase
from printing.models import PrintCustomer, PrintOrder, StaffPermission

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class PrintProjectFilesArePrivateTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.tenant.industry = 'printing'
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self._media = tempfile.TemporaryDirectory()
        self._override = override_settings(MEDIA_ROOT=self._media.name)
        self._override.enable()
        customer = PrintCustomer.objects.create(name='عميل التصميم', phone='01055556666')
        self.order = PrintOrder.objects.create(customer=customer)
        self.order.project_file.save('logo-final.ai', ContentFile(b'CLIENT-ARTWORK'), save=True)
        self.url = self.order.project_file.url

    def tearDown(self):
        self._override.disable()
        self._media.cleanup()
        super().tearDown()

    def _client(self, user):
        c = Client(HTTP_HOST=self.host)
        c.force_login(user)
        return c

    def test_project_files_need_login_and_the_files_permission(self):
        self.assertTrue(self.url.startswith('/media/print_projects/'), self.url)
        # من غير دخول ← صفحة الدخول
        r = Client(HTTP_HOST=self.host).get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'])
        # محاسب المطبعة (من غير «مشاهدة ملفات المشاريع») ← ممنوع
        accountant = User.objects.create_user('print_acct', password='x-Acct-123')
        StaffPermission.objects.create(user=accountant, can_view_treasury=True)
        self.assertEqual(self._client(accountant).get(self.url).status_code, 403)
        # المصمم (معاه الصلاحية) ← بينزل الملف
        designer = User.objects.create_user('print_designer', password='x-Design-123')
        StaffPermission.objects.create(user=designer, can_view_project_files=True)
        r = self._client(designer).get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), b'CLIENT-ARTWORK')
        # ملف .ai مش صورة ← بينزل كملف (مش بيتعرض في الصفحة)
        self.assertIn('attachment', r['Content-Disposition'])
