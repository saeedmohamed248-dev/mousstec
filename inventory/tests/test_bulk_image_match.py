"""
📸 Bulk-image code matching tests.

Covers `_match_product_by_code` — the logic that maps a code (from a file name
OR read out of the photo by OCR) to a product. Matching is EXACT-only (no
substring), tolerant of spaces/dashes and of a category prefix (AV) + index
suffix (-01) around the real part number — so a stray date/barcode on a label
never matches an unrelated product by coincidence.
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
        p = make_product(part_number='9151516')
        self.assertEqual(_match_product_by_code('9151516_2'), p)

    def test_additional_part_numbers(self):
        p = make_product(part_number='MAIN-100', additional_part_numbers=['ALT-9998877', 'ALT-1112223'])
        self.assertEqual(_match_product_by_code('ALT-9998877'), p)

    def test_clean_core_from_prefix_and_index(self):
        """Label reads 'AV 9187798-01'; product stored as the clean core '9187798'.

        The category letters (AV) and the '-01' index are dropped, leaving the
        real 7-digit part number. (A leading numeric group like '61.35-' has no
        letter boundary, so the OCR layer returns that clean core separately.)
        """
        p = make_product(part_number='9187798')
        self.assertEqual(_match_product_by_code('AV 9187798-01'), p)

    def test_no_substring_false_match(self):
        """A number that is only PART of a stored SKU must NOT match (precision)."""
        make_product(part_number='BP-100200300')
        self.assertIsNone(_match_product_by_code('100200300'))

    def test_short_noise_ignored(self):
        """Short fragments on a label (dates, indices) never match."""
        make_product(part_number='08')
        make_product(part_number='31')
        self.assertIsNone(_match_product_by_code('08'))
        self.assertIsNone(_match_product_by_code('31'))

    def test_unrelated_batch_number_no_match(self):
        """A batch/date code with no matching product returns None, not a guess."""
        make_product(part_number='9187798')
        self.assertIsNone(_match_product_by_code('213675 10'))

    def test_unknown_code_returns_none(self):
        make_product(part_number='REAL-100')
        self.assertIsNone(_match_product_by_code('IMG_8083'))
