"""Smart Parts Finder hits inventory.Product via OEM cross-reference."""
from django.db import connection

from smart_diagnostics.tests.base import DiagnosticsTenantTestCase
from smart_diagnostics.services.parts_finder import SmartPartsFinder


class PartsFinderTest(DiagnosticsTenantTestCase):

    def test_finds_part_by_oem_in_likely_parts(self):
        # Seed migration ran in public — but tenant schema doesn't have
        # diagnostics_catalog tables (it's SHARED), so DTCDefinition lookups go to public.
        # Need to switch to public to ensure the seed exists.
        connection.set_schema_to_public()
        from diagnostics_catalog.models import DTCDefinition
        # Ensure P0301 exists from the seed migration; if not, create it
        DTCDefinition.objects.update_or_create(
            code='P0301',
            defaults={
                'system': 'P', 'severity': 'high',
                'short_description': 'Misfire',
                'likely_oem_parts': ['IGN-COIL-X1'],
                'source': 'community',
            },
        )

        connection.set_tenant(self.tenant)
        from inventory.models import Branch, Product, Inventory
        branch, _ = Branch.objects.get_or_create(name='Main')
        product = Product.objects.create(
            name='Ignition Coil', part_number='IGN-COIL-X1',
            oem_cross_reference=['IGN-COIL-X1', 'ALT-001'],
        )
        Inventory.objects.create(branch=branch, product=product, quantity=5)

        matches = SmartPartsFinder.find_for_dtc('P0301')
        self.assertGreaterEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m.part_number, 'IGN-COIL-X1')
        self.assertEqual(m.in_stock_qty, 5)
