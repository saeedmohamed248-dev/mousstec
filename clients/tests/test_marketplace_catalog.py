"""
🛍️ كتالوج السوق (متاجر Mouss Tec):
* المحتوى المبدئي: ٢٥+ متجر لكل قطاع، كل متجر ١٠–٢٠ منتج، والكلام بالعامية ومفيش أسعار.
* الصفحات بتفتح (قطاع، قسم، بحث، ماركة، متجر، منتج) والشفافية «بيتنفّذ عن طريق Mouss Tec» ظاهرة.
* «اطلب سعر» بيسجّل طلب لصاحب المنصة، وبيرفض بيانات ناقصة، وفيه حد للطلبات.
* صفحة الطلبات لصاحب المنصة بس، وتغيير الحالة شغّال، والعداد في «محتاج قرارك».
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Count
from django.test import Client as HttpClient, TestCase, override_settings

from clients.catalog_seed import seed_catalog
from clients.models import CatalogItem, CatalogQuoteRequest, CatalogStore
from clients.tests.test_printing_customer_flow import _TenantDomainMixin


@override_settings(ALLOWED_HOSTS=['*'])
class CatalogTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)
        self.item = CatalogItem.objects.filter(store__sector='automotive').first()

    def test_seed_content(self):
        for sector in ('printing', 'automotive'):
            self.assertGreaterEqual(CatalogStore.objects.filter(sector=sector).count(), 25)
        counts = CatalogStore.objects.annotate(n=Count('items')).values_list('n', flat=True)
        # 🆕 الدفعة التانية بتزوّد ٥–١٠ منتجات على متاجر المطابع القديمة
        self.assertTrue(all(10 <= n <= 30 for n in counts), sorted(counts))
        # الماركات الصيني والألماني موجودة
        makes = set(CatalogItem.objects.values_list('car_make', flat=True))
        for m in ('شيري', 'BMW', 'مرسيدس', 'MG', 'هيونداي'):
            self.assertIn(m, makes)
        titles = ' '.join(CatalogItem.objects.values_list('title', flat=True))
        for word in ('كوباية', 'أقلام', 'يونيفورم', 'بوجيهات', 'كاوتش', 'دفاتر'):
            self.assertIn(word, titles)
        # الإعادة مابتكرّرش
        before = CatalogItem.objects.count()
        seed_catalog(CatalogStore, CatalogItem)
        self.assertEqual(CatalogItem.objects.count(), before)

    def test_pages_render(self):
        c = HttpClient()
        store = CatalogStore.objects.filter(sector='printing').first()
        for url in ('/marketplace/catalog/printing/', '/marketplace/catalog/automotive/',
                    '/marketplace/catalog/printing/?cat=mugs', '/marketplace/catalog/automotive/?make=BMW&q=بوجيهات',
                    '/marketplace/catalog/printing/?page=3', f'/marketplace/catalog/store/{store.slug}/',
                    f'/marketplace/catalog/item/{self.item.item_code}/', '/marketplace/'):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
        html = c.get(f'/marketplace/catalog/item/{self.item.item_code}/').content.decode()
        self.assertIn('بيتنفّذ عن طريق Mouss Tec', html)
        self.assertIn('اطلب سعر', html)
        self.assertEqual(c.get('/marketplace/catalog/nothing/').status_code, 404)

    def test_anonymous_visitor_sees_both_markets(self):
        """🛍️ سوق الطباعة وسوق السيارات مفتوحين لأي زائر من غير تسجيل — وكروت الصفحة الرئيسية بتودّي عليهم."""
        c = HttpClient()
        for sector in ('printing', 'automotive'):
            r = c.get(f'/marketplace/catalog/{sector}/')
            self.assertEqual(r.status_code, 200, sector)
            html = r.content.decode()
            self.assertIn(f'/marketplace/{sector}/?tab=login', html)  # زرار «دخول» بدل «لوحتي»
            self.assertNotIn('href="/marketplace/dashboard/"', html)
        landing = c.get('/').content.decode()
        self.assertIn('href="/marketplace/catalog/automotive/" class="mt-card"', landing)
        self.assertIn('href="/marketplace/catalog/printing/" class="mt-card"', landing)

    def test_items_with_photos_come_first(self):
        """📸 المنتجات اللي ليها صورة بتظهر الأول في السوق وفي صفحة المتجر."""
        CatalogItem.objects.update(image='')
        store = CatalogStore.objects.filter(sector='printing').order_by('-sort').first()
        last = store.items.order_by('-sort', '-id').first()  # آخر منتج في الترتيب العادي
        CatalogItem.objects.filter(pk=last.pk).update(image='catalog/x.jpg')
        c = HttpClient()
        r = c.get('/marketplace/catalog/printing/')
        self.assertEqual(r.context['page'].object_list[0].pk, last.pk)
        r = c.get(f'/marketplace/catalog/store/{store.slug}/')
        self.assertEqual(r.context['page'].object_list[0].pk, last.pk)
        # من غير صور الترتيب الثابت زي ما هو، والصفحات مابتكررش
        CatalogItem.objects.update(image='')
        p1 = [i.pk for i in c.get('/marketplace/catalog/printing/').context['page'].object_list]
        p2 = [i.pk for i in c.get('/marketplace/catalog/printing/?page=2').context['page'].object_list]
        self.assertFalse(set(p1) & set(p2))

    def test_quote_request_created_and_validated(self):
        c = HttpClient()
        url = f'/marketplace/catalog/item/{self.item.item_code}/'
        r = c.post(url, {'name': 'م', 'phone': '12'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CatalogQuoteRequest.objects.exists())
        r = c.post(url, {'name': 'أحمد', 'phone': '01011112222', 'car_info': 'إلنترا ٢٠١٨', 'details': 'أصلي'})
        self.assertEqual(r.status_code, 302)
        q = CatalogQuoteRequest.objects.get()
        self.assertEqual((q.item, q.status, q.sector, q.car_info), (self.item, 'new', 'automotive', 'إلنترا ٢٠١٨'))
        self.assertEqual(q.store_name, self.item.store.name)

    def test_quote_rate_limit(self):
        c = HttpClient()
        url = f'/marketplace/catalog/item/{self.item.item_code}/'
        for _ in range(15):
            c.post(url, {'name': 'أحمد', 'phone': '01011112222'})
        r = c.post(url, {'name': 'أحمد', 'phone': '01011112222'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(CatalogQuoteRequest.objects.count(), 15)

    def test_admin_requests_page_owner_only_and_status_update(self):
        q = CatalogQuoteRequest.objects.create(item=self.item, item_title=self.item.title, sector='automotive',
                                               name='أحمد', phone='01011112222')
        self.assertNotEqual(HttpClient().get('/superadmin/catalog/requests/').status_code, 200)
        owner = User.objects.create(username='cat_owner@test.com', is_staff=True, is_superuser=True)
        c = HttpClient(); c.force_login(owner)
        r = c.get('/superadmin/catalog/requests/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('أحمد', html)
        self.assertIn('https://wa.me/201011112222', html)
        c.post(f'/superadmin/catalog/requests/{q.pk}/update/', {'status': 'contacted', 'admin_notes': 'قلتله ٣٥٠'})
        q.refresh_from_db()
        self.assertEqual((q.status, q.admin_notes), ('contacted', 'قلتله ٣٥٠'))
        dash = c.get('/superadmin/')
        if dash.status_code == 200:
            self.assertNotIn('طلبات أسعار جديدة من الكتالوج', dash.content.decode())  # مفيش «جديد» دلوقتي


@override_settings(ALLOWED_HOSTS=['*'], CATALOG_NOTIFY_SYNC=True, CATALOG_NOTIFY_EMAIL='owner@mousstec.test',
                   CATALOG_NOTIFY_WHATSAPP='01094850763',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CatalogNotifyAndEnglishTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)
        self.item = CatalogItem.objects.filter(store__sector='automotive').exclude(car_make='').first()

    def test_owner_gets_email_and_whatsapp(self):
        from unittest import mock
        from django.core import mail
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa, \
                self.captureOnCommitCallbacks(execute=True):
            HttpClient().post(f'/marketplace/catalog/item/{self.item.item_code}/',
                              {'name': 'أحمد', 'phone': '01011112222', 'car_info': 'إلنترا ٢٠١٨', 'quantity': '٢'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@mousstec.test'])
        self.assertIn('01011112222', mail.outbox[0].body)
        self.assertIn(self.item.title, mail.outbox[0].body)
        wa.assert_called_once()
        self.assertEqual(wa.call_args[0][0], '01094850763')
        q = CatalogQuoteRequest.objects.get()
        self.assertEqual((q.notified_email, q.notified_whatsapp), (True, True))

    def test_failed_whatsapp_is_recorded_and_shown(self):
        from unittest import mock
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=False), \
                self.captureOnCommitCallbacks(execute=True):
            HttpClient().post(f'/marketplace/catalog/item/{self.item.item_code}/', {'name': 'أحمد', 'phone': '01011112222'})
        q = CatalogQuoteRequest.objects.get()
        self.assertEqual((q.notified_email, q.notified_whatsapp), (True, False))
        owner = User.objects.create(username='cat_owner2@test.com', is_staff=True, is_superuser=True)
        c = HttpClient(); c.force_login(owner)
        self.assertIn('الإشعار ماوصلش', c.get('/superadmin/catalog/requests/').content.decode())

    def test_english_pages(self):
        c = HttpClient()
        c.cookies['django_language'] = 'en'
        html = c.get('/marketplace/catalog/automotive/?make=' + self.item.car_make).content.decode()
        self.assertIn('lang="en"', html)
        self.assertIn('dir="ltr"', html)
        self.assertIn('Spare parts', html)
        self.assertIn(self.item.title_en, html)
        self.assertIn(f'>{self.item.car_make_en}<', html)
        html = c.get(f'/marketplace/catalog/item/{self.item.item_code}/').content.decode()
        for s in (self.item.title_en, 'Request a quote', 'Fulfilled by Mouss Tec', self.item.store.name_en):
            self.assertIn(s, html)
        self.assertNotIn(self.item.title + '<', html)
        html = c.get('/marketplace/catalog/printing/?cat=mugs').content.decode()
        self.assertIn('Mugs', html)
        r = c.post(f'/marketplace/catalog/item/{self.item.item_code}/', {'name': 'A', 'phone': '1'})
        self.assertIn('Write your name', r.content.decode())


@override_settings(ALLOWED_HOSTS=['*'])
class DashboardMarketEntryTests(_TenantDomainMixin, TestCase):
    """🛍️ العميل أول ما يدخل لوحته يلاقي زرار السوق بارز (قطاعه + لينك للقطاع التاني)."""

    def setUp(self):
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)

    def _dash(self, sector):
        import uuid
        from clients.models import MarketplaceCustomer
        cust = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='عميل', phone=f'+2010{uuid.uuid4().int % 10**8:08d}',
            sector=sector, is_verified=True)
        c = HttpClient(); c.cookies['mp_session'] = str(cust.session_token)
        return c.get('/marketplace/dashboard/').content.decode()

    def test_market_button_first_and_points_to_customer_sector(self):
        for sector, other in (('printing', 'automotive'), ('automotive', 'printing')):
            html = self._dash(sector)
            n = CatalogItem.objects.filter(store__sector=sector, is_active=True).count()
            self.assertIn('class="market-card"', html)
            self.assertIn(f'href="/marketplace/catalog/{sector}/" class="market-card"', html)
            self.assertIn(f'href="/marketplace/catalog/{sector}/" class="market-btn"', html)
            self.assertIn(f'/marketplace/catalog/{other}/', html)
            self.assertIn(f'{n} منتج', html)
            # الكارت قبل «أهلاً» (أول حاجة في الصفحة)
            self.assertLess(html.find('class="market-card"'), html.find('<section class="hero">'))


@override_settings(ALLOWED_HOSTS=['*'])
class CatalogLanguageSwitchTests(_TenantDomainMixin, TestCase):
    """🌐 زرار ع / EN في كل صفحات الكتالوج — وبيرجّع لنفس الصفحة باللغة الجديدة."""

    def setUp(self):
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)

    def test_market_shows_company_offers_and_customer_requests(self):
        """🛍️ السوق وشّين: «معروضات الشركات» و«طلبات العملاء» — والطلبات بتتعرض من غير بيانات العميل."""
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone
        from clients.models import MarketplaceCustomer, ServiceRequest
        cust = MarketplaceCustomer.objects.create(
            phone='01055512345', full_name='منى السرّية', sector='printing', city='طنطا',
            is_verified=True, session_token='b6c1c0a8-3f0a-4f6e-9a55-0c1a2b3c4d5e')
        soon = timezone.now() + timedelta(days=3)

        def req(title, **kw):
            data = dict(customer=cust, sector='printing', title=title, description='تفاصيل الطلب بالكامل هنا',
                        customer_city='طنطا', status='open', is_approved=True, expires_at=soon,
                        max_budget=Decimal('98765'))
            data.update(kw)
            return ServiceRequest.objects.create(**data)
        req('محتاج ٥٠٠ علبة حلويات مطبوعة', urgency='urgent')
        req('طلب لسه مستني الموافقة', status='pending_approval', is_approved=False)
        req('طلب منتهي', expires_at=timezone.now() - timedelta(days=1))
        req('طلب قطع غيار مش طباعة', sector='automotive')

        c = HttpClient()
        home = c.get('/marketplace/catalog/printing/').content.decode()
        for needle in ('معروضات الشركات', 'طلبات العملاء', '?tab=requests', 'اكتب طلبك',
                       '/marketplace/printing/?tab=login&amp;next=%2Fmarketplace%2Fdashboard%2F%3Fnew%3D1'):
            self.assertIn(needle, home, needle)
        page = c.get('/marketplace/catalog/printing/?tab=requests')
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn('محتاج ٥٠٠ علبة حلويات مطبوعة', html)
        self.assertIn('طنطا', html)
        for hidden in ('طلب لسه مستني الموافقة', 'طلب منتهي', 'طلب قطع غيار مش طباعة',
                       'منى السرّية', '01055512345', '98765'):
            self.assertNotIn(hidden, html, hidden)
        self.assertIn('دخول الشركات', html)
        # المسجّل ⇒ زرار «اكتب طلبك» بيودّيه لفورم الطلب في لوحته على طول
        c.cookies['mp_session'] = str(cust.session_token)
        self.assertIn('href="/marketplace/dashboard/?new=1"', c.get('/marketplace/catalog/printing/?tab=requests').content.decode())
        # الإنجليزي مترجم (وبنرجّع اللغة عشان مانسيبهاش 'en' للاختبارات اللي بعدنا)
        from django.conf import settings
        from django.utils import translation
        en = HttpClient(); en.cookies['django_language'] = 'en'
        try:
            en_html = en.get('/marketplace/catalog/printing/?tab=requests').content.decode()
        finally:
            translation.activate(settings.LANGUAGE_CODE)
        for needle in ('Company listings', 'Customer requests', 'Post your request', 'Company sign-in'):
            self.assertIn(needle, en_html, needle)
        # سوق قطع الغيار بيعرض طلباته هو بس
        auto = c.get('/marketplace/catalog/automotive/?tab=requests').content.decode()
        self.assertIn('طلب قطع غيار مش طباعة', auto)
        self.assertNotIn('محتاج ٥٠٠ علبة حلويات مطبوعة', auto)

    def test_switcher_on_all_catalog_pages_and_switches(self):
        item = CatalogItem.objects.first()
        c = HttpClient()
        for url in ('/marketplace/catalog/printing/', f'/marketplace/catalog/store/{item.store.slug}/',
                    f'/marketplace/catalog/item/{item.item_code}/', '/marketplace/sell/'):
            html = c.get(url).content.decode()
            self.assertIn('name="language" value="en"', html, url)
            self.assertIn('name="language" value="ar"', html, url)
        r = c.post('/i18n/setlang/', {'language': 'en', 'next': '/marketplace/catalog/automotive/?make=BMW'})
        self.assertEqual(r['Location'], '/marketplace/catalog/automotive/?make=BMW')
        html = c.get('/marketplace/catalog/automotive/?make=BMW').content.decode()
        self.assertIn('lang="en"', html)
        self.assertIn('Spare parts market', html)


@override_settings(ALLOWED_HOSTS=['*'])
class CatalogAIImagesCommandTests(_TenantDomainMixin, TestCase):
    """🖼️ generate_catalog_images: صورة لكل نوع منتج، مابيلمسش الصور اللي اترفعت، وبيكمّل من مكانه."""

    def setUp(self):
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)

    def test_one_image_per_type_and_skips_uploaded(self):
        from io import StringIO
        from unittest import mock
        from django.core.management import call_command
        from clients.management.commands import generate_catalog_images as cmd
        mine = CatalogItem.objects.filter(store__sector='automotive').first()
        mine.image.name = 'catalog/mine.jpg'; mine.save()
        calls = []

        def fake(prompt):
            calls.append(prompt)
            self.assertNotIn('Tiggo', prompt)
            return b'\x89PNG fake'
        with mock.patch.object(cmd, '_generate', side_effect=fake), \
                mock.patch('django.core.files.storage.default_storage.save', side_effect=lambda n, c: n):
            call_command('generate_catalog_images', '--sector', 'automotive', '--sleep', '0', stdout=StringIO())
        keys = {cmd.image_key(i) for i in CatalogItem.objects.filter(store__sector='automotive')}
        self.assertEqual(len(calls), len(keys))           # صورة لكل نوع، مش لكل منتج
        self.assertFalse(CatalogItem.objects.filter(store__sector='automotive', image='').exists())
        mine.refresh_from_db()
        self.assertEqual(mine.image.name, 'catalog/mine.jpg')   # صورة صاحب المنصة زي ما هي
        with mock.patch.object(cmd, '_generate', side_effect=fake):
            call_command('generate_catalog_images', '--sector', 'automotive', '--sleep', '0', stdout=StringIO())
        self.assertEqual(len(calls), len(keys))           # التشغيل التاني مابيولّدش تاني


@override_settings(ALLOWED_HOSTS=['*'])
class CatalogImportImagesTests(_TenantDomainMixin, TestCase):
    """📥 import_catalog_images: ZIP بأسماء ملفات البرومبتات ⇒ كل صورة على كل منتجات نوعها مرة واحدة."""

    def setUp(self):
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)

    def test_zip_import_maps_by_code_and_respects_uploaded(self):
        import io
        import os
        import tempfile
        import zipfile
        from io import StringIO
        from unittest import mock
        from PIL import Image
        from django.core.management import call_command
        from clients.management.commands.generate_catalog_images import image_key, image_slots
        slots = {name[:4]: key for name, key, *_ in image_slots()}
        # نوع قطع غيار موجود في الداتا
        it = CatalogItem.objects.filter(store__sector='automotive').first()
        code = next(c for c, k in slots.items() if k == image_key(it))
        same = [i for i in CatalogItem.objects.select_related('store') if image_key(i) == image_key(it)]
        mine = same[-1]
        mine.image.name = 'catalog/mine.jpg'; mine.save()
        buf = io.BytesIO(); Image.new('RGB', (2400, 1800), 'red').save(buf, 'PNG')
        path = os.path.join(tempfile.mkdtemp(), 'batch.zip')
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr(f'{code}-whatever-name.png', buf.getvalue())
            z.writestr('random.png', buf.getvalue())
        saved = []
        with mock.patch('django.core.files.storage.default_storage.save',
                        side_effect=lambda n, c: saved.append((n, c.read())) or n):
            out = StringIO()
            call_command('import_catalog_images', path, stdout=out)
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0][0].endswith('.webp'))
        self.assertLessEqual(max(Image.open(io.BytesIO(saved[0][1])).size), 1600)   # اتصغّرت
        self.assertIn('random.png', out.getvalue())
        from clients.management.commands.import_catalog_images import assign_variants
        slot = assign_variants([i.store.slug for i in same], code, 1)
        for i in same[:-1]:                     # نسخة واحدة ⇒ متجر واحد بس ياخدها، والباقي من غير صورة متكررة
            i.refresh_from_db()
            self.assertEqual(i.image.name, saved[0][0] if slot[i.store.slug] == 1 else '')
        mine.refresh_from_db()
        self.assertEqual(mine.image.name, 'catalog/mine.jpg')

    def _variants_run(self, folder, code):
        """يشغّل الأمر على فولدر فيه نسخ الكود ⇒ اسم الملف المحفوظ لكل نسخة."""
        import os
        from io import StringIO
        from unittest import mock
        from PIL import Image
        from django.core.management import call_command
        for i, color in enumerate(('red', 'green', 'blue'), 1):
            Image.new('RGB', (40, 30), color).save(os.path.join(folder, f'{code}.png' if i == 1 else f'{code}-{i}.png'))
        saved = []
        with mock.patch('django.core.files.storage.default_storage.save',
                        side_effect=lambda n, c: saved.append(n) or n):
            call_command('import_catalog_images', folder, stdout=StringIO())
        return saved

    def test_variants_spread_across_stores_stable_and_respect_uploaded(self):
        """🛍️ ٣ نسخ لنفس المنتج ⇒ المتاجر بتتوزّع عليهم، والتوزيع ثابت، واللي اترفع بالإيد ماتلمسش."""
        import tempfile
        from clients.management.commands.generate_catalog_images import image_key, image_slots
        from clients.management.commands.import_catalog_images import assign_variants, auto_variant
        slots = {name[:4]: key for name, key, *_ in image_slots()}
        # كود موجود في متاجر كتير
        groups = {}
        for i in CatalogItem.objects.select_related('store'):
            groups.setdefault(image_key(i), []).append(i)
        key, items = max(groups.items(), key=lambda kv: len({i.store.slug for i in kv[1]}))
        code = next(c for c, k in slots.items() if k == key)
        self.assertGreaterEqual(len({i.store.slug for i in items}), 3)
        mine = items[-1]
        mine.image.name = 'catalog/mine.jpg'; mine.save()
        saved = self._variants_run(tempfile.mkdtemp(), code)
        rest = [CatalogItem.objects.get(pk=i.pk) for i in items[:-1]]
        used = {auto_variant(i.image.name, code) for i in rest} - {None}
        self.assertGreater(len(used), 1)                       # مش كله نفس الصورة
        # 🛍️ ولا صورة متكررة في شركتين: كل اسم ملف تبع متجر واحد بس
        owners = {}
        for i in rest:
            if i.image.name:
                owners.setdefault(i.image.name, set()).add(i.store.slug)
        self.assertTrue(all(len(v) == 1 for v in owners.values()), owners)
        slot = assign_variants([i.store.slug for i in items], code, 3)
        for i in rest:                                         # كل متجر واخد نسخته المحسوبة
            self.assertEqual(auto_variant(i.image.name, code), slot[i.store.slug])
        self.assertEqual(CatalogItem.objects.get(pk=mine.pk).image.name, 'catalog/mine.jpg')
        self.assertEqual(len(saved), len(used))                # كل نسخة اتحفظت مرة واحدة بس
        # تاني نشر بنفس الصور ⇒ ولا حاجة بتتغير ولا صورة جديدة بتتحفظ
        again = self._variants_run(tempfile.mkdtemp(), code)
        self.assertEqual(again, [])
        self.assertEqual([CatalogItem.objects.get(pk=i.pk).image.name for i in rest], [i.image.name for i in rest])

    def test_old_single_photo_moves_to_store_variant(self):
        """المتاجر اللي واخدة الصورة القديمة (نسخة ١) ⇒ بتتنقل لنسختها لما نسخ جديدة تتضاف."""
        import tempfile
        from clients.management.commands.generate_catalog_images import image_key, image_slots
        from clients.management.commands.import_catalog_images import assign_variants, auto_variant
        slots = {name[:4]: key for name, key, *_ in image_slots()}
        it = CatalogItem.objects.select_related('store').first()
        code = next(c for c, k in slots.items() if k == image_key(it))
        same = [i for i in CatalogItem.objects.select_related('store') if image_key(i) == image_key(it)]
        for i in same:
            i.image.name = f'catalog/photos/{code}-old12345.webp'; i.save()
        self._variants_run(tempfile.mkdtemp(), code)
        slot = assign_variants([i.store.slug for i in same], code, 3)
        for i in same:
            i.refresh_from_db()
            want = slot[i.store.slug]
            if want == 1:
                self.assertEqual(i.image.name, f'catalog/photos/{code}-old12345.webp')
            elif want is None:                  # مالوش نسخة خاصة ⇒ الصورة المتكررة اتشالت
                self.assertEqual(i.image.name, '')
            else:
                self.assertEqual(auto_variant(i.image.name, code), want)

    def test_enough_variants_means_no_photo_repeats_across_stores(self):
        """🛍️ كل نسخة لمتجر واحد بس — ولو النسخ أقل من المتاجر، الباقي من غير صورة (مش صورة متكررة)."""
        from collections import Counter
        from clients.management.commands.import_catalog_images import assign_variants
        slugs = [f'store-{i}' for i in range(19)]
        slot = assign_variants(slugs + slugs[:5], 'a025', 19)        # متجر متكرر في الليستة ⇒ مرة واحدة
        self.assertEqual(sorted(slot.values()), list(range(1, 20)))  # ولا نسخة متكررة
        self.assertEqual(slot, assign_variants(list(reversed(slugs)), 'a025', 19))   # ثابت مهما كان الترتيب
        few = Counter(assign_variants(slugs, 'a025', 4).values())
        self.assertEqual(few, Counter({None: 15, 1: 1, 2: 1, 3: 1, 4: 1}))   # كل نسخة لمتجر واحد بس
        self.assertEqual(Counter(assign_variants(slugs, 'a025', 1).values()), Counter({None: 18, 1: 1}))

    def test_photos_spread_over_most_companies(self):
        """🛍️ الصور بتتوزّع على أكبر عدد من الشركات (كل صورة لشركة واحدة من اللي بتبيع المنتج)."""
        from collections import Counter
        from clients.management.commands.import_catalog_images import plan_variants
        # ٣ منتجات كلها في نفس المتجرين + منتج تالت في متجر تالت بس
        stores_of = {'p001': ['a', 'b'], 'p002': ['a', 'b'], 'p003': ['a', 'b', 'c'], 'p004': ['c']}
        plan = plan_variants({'p001': 1, 'p002': 1, 'p003': 1, 'p004': 1}, stores_of)
        got = Counter(s for slot in plan.values() for s, v in slot.items() if v)
        self.assertEqual(set(got), {'a', 'b', 'c'})                  # كل شركة خدت صورة
        self.assertEqual(max(got.values()) - min(got.values()), 1)   # متوازن
        for code, slot in plan.items():                               # الصورة بس في متجر بيبيع المنتج
            self.assertTrue(set(s for s, v in slot.items() if v) <= set(stores_of[code]))
        self.assertEqual(plan, plan_variants({'p001': 1, 'p002': 1, 'p003': 1, 'p004': 1},
                                             {k: list(reversed(v)) for k, v in stores_of.items()}))   # ثابت

    def test_variant_name_parsing(self):
        from clients.management.commands.import_catalog_images import NAME_RE, auto_variant
        self.assertEqual(NAME_RE.match('p109.webp').groups(), ('p109', None))
        self.assertEqual(NAME_RE.match('p109-2.webp').groups(), ('p109', '2'))
        self.assertEqual(NAME_RE.match('p082-laser-die-cut.png').groups(), ('p082', None))
        self.assertIsNone(NAME_RE.match('p1090.webp'))
        self.assertEqual(auto_variant('catalog/photos/p109v3-ab12cd34.webp', 'p109'), 3)
        self.assertEqual(auto_variant('catalog/photos/p109-ab12cd34.webp', 'p109'), 1)
        self.assertIsNone(auto_variant('catalog/photos/p108-ab12cd34.webp', 'p109'))
        self.assertIsNone(auto_variant('catalog/mine.jpg', 'p109'))

    def test_prompts_file_covers_every_product(self):
        from clients.management.commands.generate_catalog_images import image_key, image_slots
        keys = {k for _, k, *_ in image_slots()}
        self.assertFalse({image_key(i) for i in CatalogItem.objects.select_related('store')} - keys)
        names = [n for n, *_ in image_slots()]
        self.assertEqual(len(names), len(set(n[:4] for n in names)))   # الأكواد مش متكررة


class BundledCatalogPhotosTests(TestCase):
    """🖼️ كل صورة في clients/catalog_photos اسمها كود معروف (p001…/a047) — وإلا مش هتترفع على حاجة."""

    def test_bundled_photos_have_known_codes(self):
        import os
        from django.conf import settings as s
        from clients.management.commands.generate_catalog_images import image_slots
        codes = {n[:4] for n, *_ in image_slots()}
        folder = os.path.join(s.BASE_DIR, 'clients', 'catalog_photos')
        files = [f for f in os.listdir(folder) if f.lower().endswith(('.webp', '.png', '.jpg', '.jpeg'))]
        self.assertTrue(files)
        for f in files:
            self.assertIn(f[:4].lower(), codes, f)
            self.assertLess(os.path.getsize(os.path.join(folder, f)), 400 * 1024, f)   # صور خفيفة
@override_settings(ALLOWED_HOSTS=['*'], CATALOG_NOTIFY_SYNC=True, CATALOG_NOTIFY_EMAIL='owner@mousstec.test',
                   CATALOG_NOTIFY_WHATSAPP='', SUPPORT_WHATSAPP='',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CatalogOptionsTests(_TenantDomainMixin, TestCase):
    """🎨 العميل بيحدد المقاس/اللون/الشكل/التشطيب ويرفع لوجو — وكل ده بيوصل لصاحب المنصة."""

    def setUp(self):
        cache.clear()
        if not CatalogStore.objects.exists():
            seed_catalog(CatalogStore, CatalogItem)

    def test_every_printing_item_has_options(self):
        from clients import catalog_options as co
        for it in CatalogItem.objects.filter(store__sector='printing'):
            opts = co.item_options(it)
            self.assertTrue(opts['colors'], it.title)
            self.assertTrue(opts['sizes'] or opts['shapes'] or opts['finishes'], it.title)

    def test_options_and_logo_validated_saved_and_notified(self):
        from django.core import mail
        from django.core.files.uploadedfile import SimpleUploadedFile
        from unittest import mock
        it = CatalogItem.objects.filter(store__sector='printing', category='apparel').first()
        url = f'/marketplace/catalog/item/{it.item_code}/'
        html = HttpClient().get(url).content.decode()
        for s in ('name="opt_size"', 'name="opt_color"', 'name="opt_shape"', 'name="attachment"',
                  'enctype="multipart/form-data"'):
            self.assertIn(s, html)
        logo = SimpleUploadedFile('logo.png', b'\x89PNG fake', content_type='image/png')
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True), \
                mock.patch('django.core.files.storage.FileSystemStorage.save', side_effect=lambda n, c, **k: n), \
                self.captureOnCommitCallbacks(execute=True):
            HttpClient().post(url, {'name': 'أحمد', 'phone': '01011112222', 'opt_size': 'XL', 'opt_color': 'navy',
                                    'opt_shape': 'أوفر سايز', 'opt_finish': 'مش موجود', 'attachment': logo})
        q = CatalogQuoteRequest.objects.get()
        self.assertEqual(q.options, {'size': 'XL', 'color_key': 'navy', 'color': 'كحلي', 'color_hex': '#1E3A8A',
                                     'shape': 'أوفر سايز'})
        self.assertTrue(q.attachment.name.endswith('logo.png'))
        body = mail.outbox[0].body
        self.assertIn('المقاس: XL', body)
        self.assertIn('اللون: كحلي', body)
        self.assertIn('لوجو/تصميم', body)

    def test_bad_attachment_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        it = CatalogItem.objects.filter(store__sector='printing').first()
        r = HttpClient().post(f'/marketplace/catalog/item/{it.item_code}/', {
            'name': 'أحمد', 'phone': '01011112222',
            'attachment': SimpleUploadedFile('virus.exe', b'MZ', content_type='application/octet-stream')})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(CatalogQuoteRequest.objects.exists())

    def test_parts_have_no_options_or_upload(self):
        it = CatalogItem.objects.filter(store__sector='automotive').first()
        html = HttpClient().get(f'/marketplace/catalog/item/{it.item_code}/').content.decode()
        self.assertNotIn('name="opt_color"', html)
        self.assertNotIn('name="attachment"', html)
