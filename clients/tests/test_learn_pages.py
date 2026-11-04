"""🎓 صفحات «تعليم السيستم» (السيارات والمطابع) + زراير الوصول ليها.

* /automotive/learn/ عامة: كل الدروس بفيديوهاتها وأغلفتها، ومن غير «افتح الشاشة» (الزائر مش داخل).
* من جوه الشركة: كل درس معاه رابط الشاشة اللي بيشرحها، ولوحة التحكم والشريط فيهم الزرار.
* صفحة القطاع فيها الزرار، والهيدر/الهيرو بيظهروا (🐛 الثيم الفاتح كان مخبّي .hero-glow كله).
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client as HttpClient, SimpleTestCase, TestCase, override_settings

from clients.tutorials import auto_lessons, print_lessons
from inventory.tests.base import ERPTenantTestCase
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin


class LessonFilesTests(SimpleTestCase):

    def test_every_lesson_has_its_video_and_poster_in_static(self):
        static_dir = Path(settings.BASE_DIR) / 'static'
        for lessons in (auto_lessons(), print_lessons()):
            self.assertEqual(len(lessons), 12)
            self.assertEqual(len({lesson['slug'] for lesson in lessons}), 12)
            for lesson in lessons:
                for key in ('video', 'poster'):
                    path = static_dir / lesson[key]
                    self.assertTrue(path.is_file(), f'{lesson["slug"]}: {path} مش موجود')
                    self.assertGreater(path.stat().st_size, 1000)

    def test_core_topics_are_covered(self):
        slugs = [lesson['slug'] for lesson in auto_lessons()]
        for topic in ('inventory', 'sales', 'workshop', 'diagnostics', 'whatsapp', 'marketing', 'reports'):
            self.assertIn(topic, slugs)
        slugs = [lesson['slug'] for lesson in print_lessons()]
        for topic in ('quotes', 'orders', 'production', 'design', 'materials', 'treasury', 'whatsapp', 'marketing'):
            self.assertIn(topic, slugs)


@override_settings(ALLOWED_HOSTS=['*'])
class PublicLearnPageTests(_PublicDomainMixin, TestCase):

    def test_learn_page_lists_all_lessons_without_in_app_links(self):
        r = HttpClient().get('/automotive/learn/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context['in_app'])
        self.assertContains(r, 'تعليم السيستم')
        self.assertContains(r, 'class="lesson ', count=12)
        self.assertContains(r, 'videos/auto/06-whatsapp')
        self.assertContains(r, 'videos/auto/05-diagnostics')
        self.assertNotContains(r, 'id="now-open"')
        self.assertContains(r, '?industry=automotive')

    def test_learn_page_in_english(self):
        r = HttpClient().get('/automotive/learn/', HTTP_ACCEPT_LANGUAGE='en', HTTP_COOKIE='django_language=en')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'dir="ltr"')
        self.assertContains(r, 'System Tutorials')
        self.assertContains(r, 'WhatsApp &amp; auto-reply')

    def test_printing_learn_page_lists_its_own_lessons(self):
        r = HttpClient().get('/printing/learn/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['sector']['key'], 'printing')
        self.assertContains(r, 'class="lesson ', count=12)
        self.assertContains(r, 'videos/print/02-quotes')
        self.assertContains(r, 'videos/print/10-whatsapp')
        self.assertNotContains(r, 'videos/auto/')
        self.assertContains(r, '?industry=printing')
        self.assertContains(r, 'للمطابع واستوديوهات التصميم')
        self.assertContains(r, 'href="/printing/"')          # رجوع لصفحة القطاع

    def test_printing_landing_links_to_tutorials(self):
        r = HttpClient().get('/printing/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'href="/printing/learn/"', count=2)

    def test_automotive_landing_links_to_tutorials_and_shows_hero(self):
        r = HttpClient().get('/automotive/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'href="/automotive/learn/"', count=2)
        self.assertContains(r, 'اتعلّم السيستم بالفيديو')
        # 🐛 [FIX]: الثيم الفاتح كان بيدّي .hero-glow (غلاف الهيدر والهيرو) display:none
        css = r.content.decode()
        self.assertNotRegex(css, r'\.hero-glow\s*\{[^}]*display\s*:\s*none')


@override_settings(ALLOWED_HOSTS=['*'])
class InAppLearnTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.owner = User.objects.create_user('learner', password='x', is_staff=True, is_superuser=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)

    def test_logged_in_learn_page_links_each_lesson_to_its_screen(self):
        r = self.c.get('/automotive/learn/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['in_app'])
        self.assertContains(r, 'id="now-open"')
        self.assertContains(r, 'data-app="/system/job-card/"')
        self.assertContains(r, 'data-app="/diagnostic/shop/"')
        self.assertContains(r, 'رجوع للسيستم')

    def test_dashboard_and_navbar_show_tutorials_button(self):
        r = self.c.get('/system/dashboard/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'تعليم السيستم بالفيديو')
        # بانر لوحة التحكم + أيقونة الشريط + قائمة الموبايل
        self.assertContains(r, 'href="/automotive/learn/"', count=3)


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingDashboardLearnButtonTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.tenant.industry = 'printing'
        self.tenant.save()
        self.host = self.domain.domain
        self.owner = User.objects.create_user('print_learner', password='x', is_staff=True, is_superuser=True)
        self.c = HttpClient()
        self.c.force_login(self.owner)

    def test_printing_dashboard_has_tutorials_button(self):
        r = self.c.get('/secure-portal/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'href="/printing/learn/"')

    def test_logged_in_printing_learn_page_links_to_admin_screens(self):
        r = self.c.get('/printing/learn/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['in_app'])
        self.assertContains(r, 'data-app="/secure-portal/printing/printorder/add/"')
        self.assertContains(r, 'href="/secure-portal/"')   # «رجوع للسيستم» = لوحة المطبعة
