"""Customer recognition must not guess, and must not enrol faces on its own.

`customer_greet` is open to the device — no staff face needed — and what it
returns is that customer's own name, history and (for staff) their outstanding
balance. So a *wrong* match is a disclosure of one customer's data to another
person standing at the robot, and enrolling a face writes biometric data about
a member of the public.
"""

from unittest import mock

from django.test import SimpleTestCase

from robot import customers as customers_svc


class _QS(list):
    """Enough of a queryset for the slice the matcher takes."""

    def __getitem__(self, item):
        result = list.__getitem__(self, item)
        return _QS(result) if isinstance(item, slice) else result


class AmbiguousNameIsNotAMatchTests(SimpleTestCase):
    """A common first name must not resolve to an arbitrary record."""

    def _recognize(self, matches, name="أحمد"):
        customer_model = mock.Mock()
        customer_model.objects.filter.return_value = _QS(matches)
        modules = {
            "inventory.models": mock.Mock(Customer=customer_model, SaleInvoice=mock.Mock()),
            "robot.models": mock.Mock(),
        }
        with mock.patch.dict("sys.modules", modules), \
             mock.patch.object(customers_svc, "compare_embeddings", return_value=0.0):
            return customers_svc.recognize_customer(name=name)

    def test_two_people_share_the_name_so_nobody_is_greeted(self):
        ahmed_a, ahmed_b = mock.Mock(name="a"), mock.Mock(name="b")
        customer, method, score = self._recognize([ahmed_a, ahmed_b])
        self.assertIsNone(customer)
        self.assertEqual(method, "")
        self.assertEqual(score, 0.0)

    def test_a_name_matching_exactly_one_customer_is_accepted(self):
        only = mock.Mock()
        customer, method, _ = self._recognize([only])
        self.assertIs(customer, only)
        self.assertEqual(method, "name")

    def test_no_match_is_no_match(self):
        customer, method, _ = self._recognize([])
        self.assertIsNone(customer)
        self.assertEqual(method, "")

    def test_a_too_short_fragment_is_not_searched_on(self):
        # Two letters would match half the customer book.
        customer, method, _ = self._recognize([mock.Mock()], name="أح")
        self.assertIsNone(customer)
        self.assertEqual(method, "")


class FaceEnrolmentNeedsPermissionTests(SimpleTestCase):
    """Writing a customer's biometric data is opt-in, not a side effect."""

    def setUp(self):
        self.face = mock.Mock(visit_count=0, face_encoding=None)
        self.model = mock.Mock()
        self.model.objects.get_or_create.return_value = (self.face, True)

    def _remember(self, **kwargs):
        with mock.patch.dict(
            "sys.modules", {"robot.models": mock.Mock(RobotCustomerFace=self.model)}
        ):
            return customers_svc.remember_visit(mock.Mock(), **kwargs)

    def test_a_face_is_not_stored_without_permission(self):
        self._remember(embedding=[0.1] * 256)
        self.assertIsNone(self.face.face_encoding)

    def test_a_face_is_stored_when_permitted(self):
        embedding = [0.1] * 256
        self._remember(embedding=embedding, may_enroll_face=True)
        self.assertEqual(self.face.face_encoding, embedding)

    def test_the_visit_is_still_counted_either_way(self):
        self._remember(embedding=[0.1] * 256)
        self.assertEqual(self.face.visit_count, 1)
