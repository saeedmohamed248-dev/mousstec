"""
📝 المدونة:
* الـ Markdown آمن: أي HTML/JS في نص المقال بيظهر كنص، واللينكات javascript: مابتشتغلش.
* العناوين بتعمل فهرس، والجداول والقوايم والعريض بيتحوّلوا صح، واللينك الخارجي nofollow.
* أول ٦ مقالات بيتضافوا بالمهاجرة، وظاهرين في /blog/ والـ sitemap، وفيهم BlogPosting.
* مقال مش منشور (أو ميعاده لسه) = 404 للزوار.
"""
import json
import re
from datetime import timedelta

from django.core.cache import cache
from django.test import Client as HttpClient, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from blog.markdown import render
from blog.models import Article
from clients.tests.test_printing_customer_flow import _TenantDomainMixin


class MarkdownTests(SimpleTestCase):
    def test_escapes_html_and_blocks_js_links(self):
        html, _ = render('## عنوان <script>alert(1)</script>\n\nكلام <img src=x onerror=alert(1)> '
                         '[اضغط](javascript:alert(1)) و [آمن](/print/)')
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('href="javascript', html)
        self.assertIn('<a href="/print/">آمن</a>', html)

    def test_structure(self):
        html, toc = render('## أول\n\nفقرة **مهمة**.\n\n- بند ١\n- بند ٢\n\n### فرعي\n\n'
                           '| الكمية | الأنسب |\n|---|---|\n| ١٠٠ | ديجيتال |\n\n## تاني\n\n[خارجي](https://example.com)')
        self.assertEqual([t for _, t in toc], ['أول', 'تاني'])
        self.assertIn('<strong>مهمة</strong>', html)
        self.assertIn('<ul><li>بند ١</li><li>بند ٢</li></ul>', html)
        self.assertIn('<h3', html)
        self.assertIn('<th>الكمية</th>', html)
        self.assertIn('<td>ديجيتال</td>', html)
        self.assertNotIn('---', html)
        self.assertIn('rel="noopener nofollow"', html)


@override_settings(ALLOWED_HOSTS=['*'], BASE_DOMAIN='mousstec.com')
class BlogPagesTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.c = HttpClient()

    def test_first_articles_are_published(self):
        self.assertGreaterEqual(Article.objects.filter(is_published=True).count(), 6)
        html = self.c.get('/blog/').content.decode()
        self.assertIn('الطباعة الديجيتال ولا الأوفست؟', html)
        sm = self.c.get('/sitemap-blog.xml').content.decode()
        self.assertIn('https://mousstec.com/blog/digital-vs-offset-printing/', sm)

    def test_article_page_seo(self):
        r = self.c.get('/blog/digital-vs-offset-printing/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('<link rel="canonical" href="https://mousstec.com/blog/digital-vs-offset-printing/">', html)
        self.assertIn('<meta property="og:type" content="article">', html)
        self.assertEqual(html.count('property="og:type"'), 1)   # الـ middleware مايكررش
        ld = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
        post = next(d for d in ld if d['@type'] == 'BlogPosting')
        self.assertEqual(post['headline'], 'الطباعة الديجيتال ولا الأوفست؟ تختار إزاي وتوفّر فلوسك')
        self.assertIn('/print/flyers-brochures/', html)          # لينك الخدمة المرتبطة
        self.assertIn('/marketplace/dashboard/?new=1', html)     # زرار «اكتب طلبك»

    def test_unpublished_and_scheduled_are_404(self):
        Article.objects.create(slug='draft-x', title='مسودة', description='x', body='x', is_published=False)
        Article.objects.create(slug='later-x', title='بعدين', description='x', body='x',
                               published_at=timezone.now() + timedelta(days=2))
        self.assertEqual(self.c.get('/blog/draft-x/').status_code, 404)
        self.assertEqual(self.c.get('/blog/later-x/').status_code, 404)
        self.assertNotIn('later-x', self.c.get('/sitemap-blog.xml').content.decode())
