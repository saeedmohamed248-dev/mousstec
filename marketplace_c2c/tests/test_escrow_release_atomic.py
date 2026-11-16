"""
مراجعة ٢٠٢٦-١١ — تحرير الـ Escrow للبائع بعد الضمان لازم يبقى عملية واحدة.

قبل التصليح: الطلب بيتقلب «released» وبعدين تحرير الـ hold في خطوة لوحدها؛ لو وقعت
الفلوس بتختفي من محفظة البائع ومفيش تحويل متسجّل والـ sweep مابيرجعلهاش تاني.
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from clients.models import EscrowHold, MarketplacePayout, PartOrder
from clients.views.parts_marketplace_views import auto_release_expired_warranties
from marketplace_c2c.tests.test_payouts_matching_ratings import _paid_delivered_order


def _expired(order):
    PartOrder.objects.filter(pk=order.pk).update(warranty_ends_at=timezone.now() - timezone.timedelta(hours=1))


class EscrowReleaseAtomicTests(TestCase):
    def test_failed_escrow_release_keeps_order_delivered_and_next_sweep_pays(self):
        order = _paid_delivered_order()
        _expired(order)
        with mock.patch('clients.services.escrow.release_to_seller', side_effect=RuntimeError('boom')):
            self.assertEqual(auto_release_expired_warranties(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertIsNone(order.released_at)
        self.assertEqual(EscrowHold.objects.get(order=order).status, 'held')

        # الـ sweep اللي بعده بيكمّل عادي والبائع ليه تحويل متسجّل
        self.assertEqual(auto_release_expired_warranties(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, 'released')
        self.assertEqual(EscrowHold.objects.get(order=order).status, 'released_to_seller')
        self.assertTrue(MarketplacePayout.objects.filter(order=order, kind='seller_payout').exists())

    def test_hold_already_refunded_is_never_released_to_seller(self):
        order = _paid_delivered_order()
        _expired(order)
        EscrowHold.objects.filter(order=order).update(status='refunded_to_buyer')
        self.assertEqual(auto_release_expired_warranties(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertFalse(MarketplacePayout.objects.filter(order=order, kind='seller_payout').exists())

    def test_hold_already_released_just_closes_the_order(self):
        order = _paid_delivered_order()
        _expired(order)
        EscrowHold.objects.filter(order=order).update(status='released_to_seller')
        self.assertEqual(auto_release_expired_warranties(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, 'released')
