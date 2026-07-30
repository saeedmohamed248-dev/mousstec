"""Attendance geofencing — haversine distance + flag decision.

Pure-logic tests: no DB, no tenant schema. We feed lightweight stand-in
objects into ``_geofence_check`` so the branch-distance decision is exercised
directly. Geofencing flags, it never blocks — every case still records the
check-in; the assertions are about the flag, not access.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from inventory.views_tech import _geofence_check, _haversine_m


class _FakeBranch:
    def __init__(self, lat, lng, radius=200):
        self.lat = Decimal(str(lat)) if lat is not None else None
        self.lng = Decimal(str(lng)) if lng is not None else None
        self.geofence_radius_m = radius

    @property
    def has_geofence(self):
        return self.lat is not None and self.lng is not None


class _FakeProfile:
    def __init__(self, branch):
        self.branch = branch


# A real Cairo workshop pin for the fixtures.
BR_LAT, BR_LNG = 30.044400, 31.235700


class HaversineTests(SimpleTestCase):
    def test_same_point_is_zero(self):
        self.assertAlmostEqual(_haversine_m(BR_LAT, BR_LNG, BR_LAT, BR_LNG), 0.0, places=3)

    def test_small_offset_is_tens_of_metres(self):
        # ~0.0005° latitude ≈ 55.6 m
        d = _haversine_m(BR_LAT, BR_LNG, BR_LAT + 0.0005, BR_LNG)
        self.assertTrue(50 < d < 65, f"expected ~55m, got {d:.1f}")

    def test_larger_offset_is_hundreds_of_metres(self):
        # ~0.005° latitude ≈ 556 m
        d = _haversine_m(BR_LAT, BR_LNG, BR_LAT + 0.005, BR_LNG)
        self.assertTrue(500 < d < 620, f"expected ~556m, got {d:.1f}")


class GeofenceDecisionTests(SimpleTestCase):
    def test_inside_radius_is_not_flagged(self):
        profile = _FakeProfile(_FakeBranch(BR_LAT, BR_LNG, radius=200))
        inside, reason = _geofence_check(profile, BR_LAT + 0.0005, BR_LNG, accuracy=20)
        self.assertTrue(inside)
        self.assertEqual(reason, '')

    def test_outside_radius_is_flagged(self):
        profile = _FakeProfile(_FakeBranch(BR_LAT, BR_LNG, radius=200))
        inside, reason = _geofence_check(profile, BR_LAT + 0.005, BR_LNG, accuracy=20)
        self.assertFalse(inside)
        self.assertEqual(reason, 'outside_radius')

    def test_low_gps_accuracy_is_flagged(self):
        profile = _FakeProfile(_FakeBranch(BR_LAT, BR_LNG, radius=200))
        inside, reason = _geofence_check(profile, BR_LAT, BR_LNG, accuracy=500)
        self.assertFalse(inside)
        self.assertEqual(reason, 'low_accuracy')

    def test_branch_without_pin_reports_no_geo(self):
        profile = _FakeProfile(_FakeBranch(None, None))
        inside, reason = _geofence_check(profile, BR_LAT, BR_LNG, accuracy=10)
        self.assertFalse(inside)
        self.assertEqual(reason, 'no_branch_geo')

    def test_no_branch_at_all_reports_no_geo(self):
        profile = _FakeProfile(None)
        inside, reason = _geofence_check(profile, BR_LAT, BR_LNG, accuracy=10)
        self.assertFalse(inside)
        self.assertEqual(reason, 'no_branch_geo')
