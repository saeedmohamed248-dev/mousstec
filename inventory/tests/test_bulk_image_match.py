"""
📸 Bulk-image code matching tests.

Covers `_match_product_by_code` — the logic that maps a code (from a file name
OR read out of the photo by OCR) to a product, tolerant of spaces/dashes and of
a bare part number printed inside a prefixed SKU.
"""
from .base import ERPTenantTestCase
from .factories import make_product
from inventory.views_lightning import _match_product_by_code


class BulkImageCodeMatchTests(ERPTenantTestCase):
    def test_exact_part_number(self):
        p = make_product(part_number='34116850568')
        self.assertEqual(_match_product_by_code('34116850568'), p)

    def test_barcode_match(self):
        p = make_product(part_number='X-1', barcode='6291041500213')
        self.assertEqual(_match_product_by_code('6291041500213'), p)

    def test_compact_form_ignores_spaces_and_dashes(self):
        """OCR often reads BMW numbers in groups: '3411 6850 568'."""
        p = make_product(part_number='34116850568')
        self.assertEqual(_match_product_by_code('3411 6850 568'), p)
        self.assertEqual(_match_product_by_code('3411-6850-568'), p)

    def test_sequence_suffix_stripped(self):
        p = make_product(part_number='BP99')
        self.assertEqual(_match_product_by_code('BP99_2'), p)

    def test_additional_part_numbers(self):
        p = make_product(part_number='MAIN-1', additional_part_numbers=['ALT-77', 'ALT-88'])
        self.assertEqual(_match_product_by_code('ALT-88'), p)

    def test_bare_number_inside_prefixed_sku(self):
        """Photo shows '34116850568'; stored SKU is 'BP-34116850568'."""
        p = make_product(part_number='BP-34116850568')
        self.assertEqual(_match_product_by_code('34116850568'), p)

    def test_ambiguous_substring_not_matched(self):
        """A short/duplicated fragment must not guess a single product."""
        make_product(part_number='BP-100200300')
        make_product(part_number='XX-100200300')
        # two SKUs contain the same digits → tolerant fallback must abstain
        self.assertIsNone(_match_product_by_code('100200300'))

    def test_unknown_code_returns_none(self):
        make_product(part_number='REAL-1')
        self.assertIsNone(_match_product_by_code('IMG_8083'))
