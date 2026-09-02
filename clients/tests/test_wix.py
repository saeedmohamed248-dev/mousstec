"""Wix integration — client request shaping + sync helpers (mocked network).

No live Wix calls. These pin the pieces most likely to regress silently:
the auth headers, result parsing, and the price/id extraction the sync
layer depends on.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from clients.services import wix_sync
from clients.services.wix_client import WixClient, WixResult


class WixClientHeaderTests(SimpleTestCase):
    def test_headers_include_key_and_site(self):
        c = WixClient('APIKEY', 'SITE123', 'ACC9')
        h = c._headers()
        self.assertEqual(h['Authorization'], 'APIKEY')
        self.assertEqual(h['wix-site-id'], 'SITE123')
        self.assertEqual(h['wix-account-id'], 'ACC9')

    def test_account_header_omitted_when_blank(self):
        c = WixClient('APIKEY', 'SITE123')
        self.assertNotIn('wix-account-id', c._headers())

    @mock.patch('requests.request')
    def test_ok_response_parsed(self, mock_req):
        m = mock.Mock(); m.status_code = 200; m.json.return_value = {'products': []}
        mock_req.return_value = m
        res = WixClient('k', 's').test_connection()
        self.assertTrue(res.ok)
        self.assertEqual(res.data, {'products': []})

    @mock.patch('requests.request')
    def test_error_response_captured(self, mock_req):
        m = mock.Mock(); m.status_code = 403; m.text = 'forbidden'
        mock_req.return_value = m
        res = WixClient('k', 's').test_connection()
        self.assertFalse(res.ok)
        self.assertIn('http_403', res.error)

    @mock.patch('requests.request', side_effect=__import__('requests').RequestException('boom'))
    def test_network_error_is_caught(self, _req):
        res = WixClient('k', 's').test_connection()
        self.assertFalse(res.ok)
        self.assertIn('network_error', res.error)


class WixSyncHelperTests(SimpleTestCase):
    def test_first_product_id_from_create(self):
        res = WixResult(True, data={'product': {'id': 'P1'}})
        self.assertEqual(wix_sync._first_product_id(res, key='product'), 'P1')

    def test_first_product_id_from_query(self):
        res = WixResult(True, data={'products': [{'id': 'Q9'}]})
        self.assertEqual(wix_sync._first_product_id(res), 'Q9')

    def test_first_product_id_empty(self):
        self.assertEqual(wix_sync._first_product_id(WixResult(False)), '')

    def test_line_price_parses_amount(self):
        self.assertEqual(wix_sync._line_price({'price': {'amount': '149.50'}}),
                         Decimal('149.50'))

    def test_line_price_falls_back_to_zero(self):
        self.assertEqual(wix_sync._line_price({'price': {'amount': 'NaN'}}),
                         Decimal('0'))
