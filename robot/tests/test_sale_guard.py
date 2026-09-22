"""The sale endpoint rejects bad quantities and prices off the wire.

`/sale/` invoices against the real ERP from values a floor-standing device
posts, so quantity and unit_price are untrusted input: a bad one is a wrong
invoice, not a 500. The price floor is the last of these — a device may
discount a used part down to its scrap price but no further.

The scrap-price floor only bites when a part *has* a scrap price. The field
defaults to 0.00 in `inventory.models.catalog`, so parts left at the default
have no floor at all; that is a per-shop business decision, and the test that
records it is deliberate, not an oversight.

No database: the product, device and customer lookups are stubbed, like the
other robot tests.
"""

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from robot import views


class _Product:
    def __init__(self, scrap_price="0.00", retail_price="500.00"):
        self.pk = 1
        self.id = 1
        self.name = "فلتر زيت"
        self.scrap_price = Decimal(scrap_price)
        self.retail_price = Decimal(retail_price)


def _post(body, *, product):
    """Call sale() with auth, product and customer resolution stubbed out."""
    request = APIRequestFactory().post("/api/robot/v1/sale/", body, format="json")
    device = mock.Mock(branch=mock.Mock())
    employee = mock.Mock()
    employee.name = "محمود"
    customer = mock.Mock()

    customers = mock.Mock()
    customers.get_or_create.return_value = (customer, False)
    fake_inventory = mock.Mock(Customer=mock.Mock(objects=customers))

    with mock.patch.object(views, "_device_or_401", return_value=(device, None)), \
            mock.patch.object(views, "_require_permission", return_value=(employee, None)), \
            mock.patch.object(views.services, "find_product", return_value=product), \
            mock.patch.dict("sys.modules", {"inventory.models": fake_inventory}), \
            mock.patch.object(views.services, "maybe_raise_procurement_signal"), \
            mock.patch.object(views.services, "create_robot_sale") as create:
        invoice = mock.Mock(pk=7, invoice_number="INV-7")
        invoice.id = 7
        invoice.total_amount = Decimal("500")
        create.return_value = invoice
        response = views.sale(request)
    return response, create


class ThePriceFloorTests(SimpleTestCase):
    """A device may discount to scrap, never below it."""

    def test_a_price_below_scrap_is_refused(self):
        response, create = _post(
            {"part_number": "OF-1", "unit_price": "1"},
            product=_Product(scrap_price="120.00"),
        )
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_the_reason_names_the_floor(self):
        response, _ = _post(
            {"part_number": "OF-1", "unit_price": "1"},
            product=_Product(scrap_price="120.00"),
        )
        self.assertIn("120", str(response.data))

    def test_a_price_at_the_floor_is_accepted(self):
        response, create = _post(
            {"part_number": "OF-1", "unit_price": "120.00"},
            product=_Product(scrap_price="120.00"),
        )
        self.assertEqual(response.status_code, 201)
        create.assert_called_once()

    def test_a_price_above_the_floor_is_accepted(self):
        response, create = _post(
            {"part_number": "OF-1", "unit_price": "480.00"},
            product=_Product(scrap_price="120.00"),
        )
        self.assertEqual(response.status_code, 201)
        create.assert_called_once()

    def test_no_price_falls_back_to_retail_and_skips_the_floor(self):
        response, create = _post(
            {"part_number": "OF-1"}, product=_Product(scrap_price="120.00"),
        )
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(create.call_args.kwargs["unit_price"])

    def test_a_part_with_no_scrap_price_has_no_floor(self):
        """Recorded on purpose: scrap_price defaults to 0, so most parts are
        unprotected by this check and a device can still invoice at 0.01."""
        response, create = _post(
            {"part_number": "OF-1", "unit_price": "0.01"},
            product=_Product(scrap_price="0.00"),
        )
        self.assertEqual(response.status_code, 201)
        create.assert_called_once()


class QuantityAndPriceAreValidatedTests(SimpleTestCase):

    def test_a_non_numeric_quantity_is_a_400_not_a_500(self):
        response, create = _post(
            {"part_number": "OF-1", "quantity": "شمال"}, product=_Product(),
        )
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_a_negative_quantity_is_refused(self):
        response, create = _post(
            {"part_number": "OF-1", "quantity": -3}, product=_Product(),
        )
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_a_non_numeric_price_is_a_400_not_a_500(self):
        response, create = _post(
            {"part_number": "OF-1", "unit_price": "مجاناً"}, product=_Product(),
        )
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_a_zero_price_is_refused(self):
        response, create = _post(
            {"part_number": "OF-1", "unit_price": "0"}, product=_Product(),
        )
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_a_negative_price_is_refused(self):
        response, create = _post(
            {"part_number": "OF-1", "unit_price": "-50"}, product=_Product(),
        )
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()
