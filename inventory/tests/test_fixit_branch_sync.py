"""🏬 «تحديث الموقع الآن» من فرع معيّن بيرفع قطع الفرع ده بس (مش كل منتجات الشركة)."""
from unittest import mock

from django.test import override_settings

from inventory.services import fixit_sync
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product


@override_settings(FIXIT_SYNC_URL='https://example.test/api/sync', FIXIT_SYNC_SECRET='s', FIXIT_TENANT_SCHEMA='')
class FixitBranchSyncTests(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        with mock.patch.object(fixit_sync, '_post_async'):   # signals الحفظ مايبعتوش حاجة
            from inventory.models import Branch
            self.eg = Branch.objects.order_by('pk').first() or make_branch('الفرع الرئيسي')
            self.uae = make_branch('الامارات')
            self.p_eg, self.p_uae, self.p_both, self.p_zero = (
                make_product(name='مصر'), make_product(name='إمارات'), make_product(name='الاتنين'),
                make_product(name='صفر في الإمارات'))
            make_inventory(self.p_eg, self.eg, 3)
            make_inventory(self.p_uae, self.uae, 2)
            make_inventory(self.p_both, self.eg, 1)
            make_inventory(self.p_both, self.uae, 1)
            make_inventory(self.p_zero, self.eg, 4)
            make_inventory(self.p_zero, self.uae, 0)

    def _push(self, **kw):
        sent = []
        with mock.patch.object(fixit_sync, '_post', side_effect=lambda payload: sent.append(payload) or {'ok': True}):
            res = fixit_sync.push_all_products(**kw)
        skus = {it['sku'] for p in sent if p['action'] == 'upsert' for it in p['items']}
        return res, skus, sent

    def test_branch_pushes_only_its_stocked_parts_and_never_prunes(self):
        res, skus, sent = self._push(branch=self.uae, prune=True)
        self.assertEqual(skus, {self.p_uae.part_number, self.p_both.part_number})
        self.assertEqual(res['items'], 2)
        self.assertFalse(any(p['action'] == 'prune' for p in sent))   # مايمسحش قطع مصر من الموقع

    def test_all_branches_pushes_everything(self):
        _res, skus, _sent = self._push()
        self.assertTrue({self.p_eg.part_number, self.p_uae.part_number, self.p_both.part_number,
                         self.p_zero.part_number} <= skus)
