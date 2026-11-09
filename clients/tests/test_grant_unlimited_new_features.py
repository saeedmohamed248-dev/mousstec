"""🔓 الشركة المفتوحة بالكامل (grant_unlimited) بتاخد أي ميزة تتضاف للكتالوج بعدين."""
from django.db import connection
from django.utils import timezone

from clients.models import Feature, TenantSubscription
from inventory.tests.base import ERPTenantTestCase


class GrantUnlimitedNewFeatureTests(ERPTenantTestCase):
    def _sub(self, locked):
        return TenantSubscription(locked_entitlements=locked, locked_at=timezone.now())

    def test_all_features_marker_opens_features_added_later(self):
        connection.set_schema_to_public()
        sub = self._sub({'old_feat': {'enabled': True, 'monthly_limit': 5},
                         TenantSubscription.ALL_FEATURES_KEY: True})
        Feature.objects.get_or_create(code='brand_new_feat', defaults=dict(name_ar='جديدة', name_en='new', is_active=True))
        ents = sub.effective_entitlements
        self.assertTrue(ents['brand_new_feat']['enabled'])
        self.assertEqual(ents['old_feat'], {'enabled': True, 'monthly_limit': 5})   # اللقطة ليها الأولوية
        self.assertNotIn('not_in_catalog', ents)
        self.assertNotIn(TenantSubscription.ALL_FEATURES_KEY, ents)

    def test_snapshot_without_marker_unchanged(self):
        connection.set_schema_to_public()
        Feature.objects.get_or_create(code='brand_new_feat2', defaults=dict(name_ar='ج', name_en='n', is_active=True))
        self.assertEqual(self._sub({'x': {'enabled': True}}).effective_entitlements, {'x': {'enabled': True}})
