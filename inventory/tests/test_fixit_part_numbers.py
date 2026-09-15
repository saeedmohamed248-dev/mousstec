"""اختبارات سريعة (بدون DB) لمنطق ربط FixIt: أرقام البارت + التصنيف.

بتستخدم SimpleTestCase عشان تشتغل بسرعة في CI من غير ما تلمس قاعدة البيانات.
"""
from django.test import SimpleTestCase

from inventory.models import Product
from inventory.services.fixit_sync import PART_CATEGORY_TO_FIXIT
from inventory.management.commands.classify_part_categories import infer_category


class AllPartNumbersTest(SimpleTestCase):
    def test_dedup_and_order(self):
        p = Product(part_number='ABC-1', additional_part_numbers=['XYZ-2', 'ABC-1', '', 'XYZ-2', 'QWE-3'])
        # الأساسي الأول، بدون تكرار، وبدون الفراغات، وبدون تكرار الأساسي
        self.assertEqual(p.all_part_numbers, ['ABC-1', 'XYZ-2', 'QWE-3'])

    def test_empty_additional(self):
        p = Product(part_number='ONLY-1', additional_part_numbers=[])
        self.assertEqual(p.all_part_numbers, ['ONLY-1'])

    def test_none_additional_safe(self):
        p = Product(part_number='ONLY-1', additional_part_numbers=None)
        self.assertEqual(p.all_part_numbers, ['ONLY-1'])


class CategoryMappingTest(SimpleTestCase):
    def test_mapped_values_are_known_fixit_categories(self):
        # كل قيمة مُرسَلة للموقع لازم تكون فئة معروفة عنده (نفس النصوص)
        known = {
            'كهرباء وإشعال', 'فلاتر وصيانة', 'عفشة وتعليق', 'فرامل',
            'تبريد', 'وقود', 'محرك', 'هيكل وإكسسوارات', 'أخرى',
        }
        for value in PART_CATEGORY_TO_FIXIT.values():
            self.assertIn(value, known)

    def test_choice_keys_are_valid_model_choices(self):
        valid = {c[0] for c in Product.PART_CATEGORY_CHOICES}
        for key in PART_CATEGORY_TO_FIXIT:
            self.assertIn(key, valid)


class InferCategoryTest(SimpleTestCase):
    def test_known_examples(self):
        cases = {
            'مارش BMW F30': 'electrical',
            'المارش': 'electrical',
            'تيل فرامل امامي': 'brakes',
            'فلتر زيت': 'filters',
            'مساعد امامي': 'suspension',
            'كلتش': 'engine',
            'رادياتير مياه': 'cooling',
        }
        for name, expected in cases.items():
            self.assertEqual(infer_category(name), expected, name)

    def test_unknown_returns_empty(self):
        self.assertEqual(infer_category('حاجة غريبة مش معروفة'), '')
