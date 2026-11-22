"""
سوق قطع الغيار — فصل «سوق الأفراد» عن «سوق التجار» + الصفحات.

* /marketplace/parts/individuals/ بيعرض قطع العملاء الأفراد بس (حاجات مش محتاجينها).
* /marketplace/parts/traders/ بيعرض قطع الشركات بس.
* /marketplace/parts/ (الكل) بيعرض الاتنين، وكل كارت عليه نوع البائع.
* 🐛 السوق كان بيقطع عند 60 قطعة من غير صفحات — دلوقتي فيه صفحات.
* 🐛 اللي داخل يبيع من غير حساب كان بيتوه بعد التسجيل (مفيش ?next).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.test import Client as HttpClient, TestCase

from clients.models import Client, PartListing
from marketplace_c2c.tests.test_parts_order_lifecycle import (
    _PublicDomainMixin, _customer, _listing, _make,
)


def _tenant():
    tag = uuid.uuid4().hex[:8]
    t = Client(schema_name=f'feed_{tag}', name=f'Shop {tag}', owner_name='O', phone='01000000000')
    t.auto_create_schema = False
    t.save(verbosity=0)
    return t


class PartsFeedSectionsTests(_PublicDomainMixin, TestCase):
    def setUp(self):
        self.http = HttpClient()
        self.make = _make()
        self.ind = _listing(_customer('Seller'), title='IND-' + uuid.uuid4().hex[:6], car_make=self.make)
        self.biz = _listing(None, seller_customer=None, seller_tenant=_tenant(),
                            title='BIZ-' + uuid.uuid4().hex[:6], car_make=self.make)

    def _html(self, url, **params):
        r = self.http.get(url, params)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_individuals_page_shows_only_customer_listings(self):
        html = self._html('/marketplace/parts/individuals/')
        self.assertIn(self.ind.title, html)
        self.assertNotIn(self.biz.title, html)
        self.assertIn('/marketplace/parts/sell/', html)  # زرار «اعرض حاجتك للبيع»

    def test_traders_page_shows_only_company_listings(self):
        html = self._html('/marketplace/parts/traders/')
        self.assertIn(self.biz.title, html)
        self.assertNotIn(self.ind.title, html)

    def test_all_page_shows_both_with_seller_badges(self):
        html = self._html('/marketplace/parts/')
        self.assertIn(self.ind.title, html)
        self.assertIn(self.biz.title, html)
        self.assertIn('badge-seller ind', html)
        self.assertIn('badge-seller biz', html)

    def test_private_seller_name_stays_hidden_on_cards(self):
        # الاسم الكامل للبائع الفرد مخفي لحد ما يبقى فيه طلب مدفوع (trust.py).
        html = self._html('/marketplace/parts/individuals/')
        self.assertNotIn(self.ind.seller_customer.full_name + '</', html)

    def test_make_counts_follow_the_open_section(self):
        _listing(_customer('S2'), car_make=self.make)
        r = self.http.get('/marketplace/parts/traders/')
        make = next(m for m in r.context['makes'] if m.pk == self.make.pk)
        self.assertEqual(make.live_count, 1)
        r = self.http.get('/marketplace/parts/individuals/')
        make = next(m for m in r.context['makes'] if m.pk == self.make.pk)
        self.assertEqual(make.live_count, 2)

    def test_feed_is_paginated_instead_of_cut_at_60(self):
        from clients.views.parts_feed_views import FEED_PAGE_SIZE
        seller = _customer('Bulk')
        PartListing.objects.bulk_create([
            PartListing(seller_customer=seller, title=f'Bulk {i}', description='Good part here',
                        car_make=self.make, price_egp=Decimal('100'), warranty_days=3,
                        status='active', moderation_status='approved')
            for i in range(FEED_PAGE_SIZE + 5)
        ])
        r1 = self.http.get('/marketplace/parts/individuals/')
        self.assertEqual(len(r1.context['listings']), FEED_PAGE_SIZE)
        self.assertTrue(r1.context['listings_page'].has_next())
        r2 = self.http.get('/marketplace/parts/individuals/', {'page': 2})
        self.assertEqual(len(r2.context['listings']), 6)  # 53 + القطعة الأصلية
        # رقم صفحة غلط مايطلعش 500.
        self.assertEqual(self.http.get('/marketplace/parts/', {'page': 'x'}).status_code, 200)

    def test_page_links_keep_filters(self):
        r = self.http.get('/marketplace/parts/individuals/', {'make': self.make.slug, 'sort': 'price_low'})
        self.assertIn('make=' + self.make.slug, r.context['filter_qs'])
        self.assertIn('sort=price_low', r.context['filter_qs'])

    def test_unknown_section_param_falls_back_to_all(self):
        r = self.http.get('/marketplace/parts/', {'seller': 'nope'})
        self.assertEqual(r.context['section'], 'all')

    def test_sell_page_for_anonymous_keeps_return_path(self):
        r = self.http.get('/marketplace/parts/sell/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('next=/marketplace/parts/sell/', r['Location'])

    def test_orders_pages_for_anonymous_keep_return_path(self):
        for path in ('/marketplace/parts/orders/', '/marketplace/parts/sales/'):
            r = self.http.get(path)
            self.assertEqual(r.status_code, 302)
            self.assertIn('next=' + path, r['Location'])
