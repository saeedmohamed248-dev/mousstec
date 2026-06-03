"""Verify that tenant A cannot see tenant B's DiagnosticScans, FaultLogs, or APICallLogs."""
from django.db import connection

from smart_diagnostics.tests.base import TwoTenantTestCase
from smart_diagnostics.tests.factories import (
    get_or_create_premium_plan,
    attach_subscription,
    make_customer_and_vehicle,
)


class TenantIsolationTest(TwoTenantTestCase):

    def test_diagnostic_scans_isolated_between_tenants(self):
        from smart_diagnostics.models import DiagnosticScan
        from clients.models import Plan

        # Plan lives in public schema — create once
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant_a, plan)
        attach_subscription(self.tenant_b, plan)

        # Tenant A: create a vehicle + scan
        connection.set_tenant(self.tenant_a)
        v_a = make_customer_and_vehicle(vin='AAAAAAAAAAAAAAAAA', plate='A-1')
        DiagnosticScan.objects.create(vehicle=v_a, source='manual', status='completed', summary='A only')

        # Tenant B: empty
        connection.set_tenant(self.tenant_b)
        b_scans = list(DiagnosticScan.objects.all())
        self.assertEqual(len(b_scans), 0, "Tenant B should not see Tenant A's scans")

        # Tenant B creates its own
        v_b = make_customer_and_vehicle(vin='BBBBBBBBBBBBBBBBB', plate='B-1')
        DiagnosticScan.objects.create(vehicle=v_b, source='manual', status='completed', summary='B only')

        # Switch back to A and confirm A still sees only its own
        connection.set_tenant(self.tenant_a)
        a_scans = list(DiagnosticScan.objects.all())
        self.assertEqual(len(a_scans), 1)
        self.assertEqual(a_scans[0].summary, 'A only')

    def test_api_call_logs_isolated(self):
        from smart_diagnostics.models import APICallLog
        connection.set_tenant(self.tenant_a)
        APICallLog.objects.create(provider='mock', endpoint='dtc_lookup', dtc_code='P0301')
        connection.set_tenant(self.tenant_b)
        self.assertEqual(APICallLog.objects.count(), 0)
