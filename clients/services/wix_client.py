"""🔌 Wix REST API client — thin wrapper over the Wix Stores + eCommerce APIs.

Auth: Wix API Key (account-level) + Site ID header. The site owner generates
an API key at https://manage.wix.com/account/api-keys and copies the Site ID
from the site dashboard. We send:
    Authorization: <api_key>
    wix-site-id:   <site_id>
    wix-account-id: <account_id>   (optional, some endpoints need it)

Every method returns a ``WixResult(ok, data, error)`` and never raises, so the
sync layer can record errors on the connection and keep going.

References:
    Stores Catalog V1:  https://dev.wix.com/api/rest/wix-stores/catalog
    eCommerce Orders:   https://dev.wix.com/api/rest/wix-ecommerce/order
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger('mouss_tec_core')

_BASE = 'https://www.wixapis.com'
_TIMEOUT = 25


@dataclass
class WixResult:
    ok: bool
    data: Optional[dict] = None
    error: str = ''


class WixClient:
    def __init__(self, api_key: str, site_id: str, account_id: str = ''):
        self.api_key = api_key
        self.site_id = site_id
        self.account_id = account_id

    def _headers(self) -> dict:
        h = {
            'Authorization': self.api_key,
            'wix-site-id': self.site_id,
            'Content-Type': 'application/json',
        }
        if self.account_id:
            h['wix-account-id'] = self.account_id
        return h

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> WixResult:
        import requests
        url = f'{_BASE}{path}'
        try:
            res = requests.request(method, url, headers=self._headers(),
                                   json=json_body, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning('[WIX] network error %s %s: %s', method, path, exc)
            return WixResult(False, error=f'network_error: {exc}')

        if res.status_code in (200, 201):
            try:
                return WixResult(True, data=res.json())
            except ValueError:
                return WixResult(True, data={})
        # Wix returns a structured error body
        detail = res.text[:400]
        logger.warning('[WIX] %s %s → %s: %s', method, path, res.status_code, detail)
        return WixResult(False, error=f'http_{res.status_code}: {detail}')

    # ── Connectivity ─────────────────────────────────────────────────
    def test_connection(self) -> WixResult:
        """يتأكد إن الـ credentials شغّالة عبر query خفيف على المنتجات."""
        return self._request('POST', '/stores/v1/products/query',
                             {'query': {'paging': {'limit': 1}}})

    # ── Products (Stores Catalog V1) ─────────────────────────────────
    def find_product_by_sku(self, sku: str) -> WixResult:
        return self._request('POST', '/stores/v1/products/query', {
            'query': {
                'filter': f'{{"sku": "{sku}"}}',
                'paging': {'limit': 1},
            },
        })

    def create_product(self, *, name: str, sku: str, price, description: str = '',
                       brand: str = '', visible: bool = True) -> WixResult:
        body = {'product': {
            'name': name[:80] or sku,
            'productType': 'physical',
            'priceData': {'price': float(Decimal(str(price)))},
            'sku': sku,
            'visible': visible,
            'description': description[:8000] if description else '',
        }}
        if brand:
            body['product']['brand'] = brand[:50]
        return self._request('POST', '/stores/v1/products', body)

    def update_product(self, wix_product_id: str, *, name: str = None, price=None,
                      description: str = None, visible: bool = None) -> WixResult:
        product: dict[str, Any] = {}
        if name is not None:
            product['name'] = name[:80]
        if price is not None:
            product['priceData'] = {'price': float(Decimal(str(price)))}
        if description is not None:
            product['description'] = description[:8000]
        if visible is not None:
            product['visible'] = visible
        if not product:
            return WixResult(True, data={})
        return self._request('PATCH', f'/stores/v1/products/{wix_product_id}',
                            {'product': product})

    def update_inventory(self, wix_product_id: str, quantity: int) -> WixResult:
        """يحدّث كمية المخزون (in_stock + quantity) للمنتج."""
        return self._request('PATCH', f'/stores/v1/inventoryItems/product/{wix_product_id}', {
            'inventoryItem': {
                'trackQuantity': True,
                'variants': [{'variantId': '00000000-0000-0000-0000-000000000000',
                              'quantity': max(int(quantity), 0),
                              'inStock': int(quantity) > 0}],
            },
        })

    # ── Orders (eCommerce V1) ────────────────────────────────────────
    def search_orders(self, *, limit: int = 50, cursor: str = '',
                     created_after: str = '') -> WixResult:
        """يسحب الطلبات الأحدث. created_after = ISO timestamp (اختياري)."""
        search: dict[str, Any] = {
            'cursorPaging': {'limit': limit},
            'sort': [{'fieldName': 'createdDate', 'order': 'DESC'}],
        }
        if cursor:
            search['cursorPaging']['cursor'] = cursor
        if created_after:
            search['filter'] = {'createdDate': {'$gte': created_after}}
        return self._request('POST', '/ecom/v1/orders/search', {'search': search})
