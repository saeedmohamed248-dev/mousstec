"""Face matching may only authorize people when a real face model is installed.

A face match gates creating a sale, dispensing a part and clocking in, so what
it runs on matters. Without `face_recognition`, `robot.faces` falls back to a
16x16 grayscale thumbnail. That is not a face embedding: two different people
photographed by the same fixed shop camera score far above the 0.85 threshold
against each other, so the fallback would authorize the first enrolled employee
for anybody. The backend fails closed instead, and these tests hold it there.
"""

import io
import random
from unittest import mock

from django.test import SimpleTestCase

from robot import faces, security


def _face(seed: int, bg: int = 160) -> bytes:
    """A crude but distinct synthetic 'face' photo."""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    img = Image.new("RGB", (200, 200), (bg, bg, bg))
    draw = ImageDraw.Draw(img)
    cx, cy = rnd.randint(70, 130), rnd.randint(70, 130)
    radius = rnd.randint(40, 70)
    skin = rnd.randint(80, 220)
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=(skin, skin, skin))
    for eye in (-radius // 2, radius // 2):
        draw.ellipse([cx + eye - 6, cy - 12, cx + eye + 6, cy], fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class FallbackCannotTellPeopleApartTests(SimpleTestCase):
    """The measurement behind the fail-closed rule, so it can't quietly change."""

    def test_different_faces_all_clear_the_match_threshold(self):
        embeddings = [faces._fallback_embedding(_face(i)) for i in range(6)]
        self.assertTrue(all(e for e in embeddings), "Pillow fallback produced nothing")

        scores = [
            security.compare_embeddings(embeddings[i], embeddings[j])
            for i in range(len(embeddings))
            for j in range(i + 1, len(embeddings))
        ]
        # Every pair is a *different* face, so a real biometric would score
        # these low. The thumbnail scores them all as the same person.
        self.assertTrue(
            all(s >= 0.85 for s in scores),
            f"expected the fallback to false-accept every pair, got {scores}",
        )

    def test_fallback_is_not_reported_as_biometric(self):
        with mock.patch.object(faces, "FACE_PROVIDER", "fallback"):
            self.assertFalse(faces.is_biometric())


class MatchingFailsClosedTests(SimpleTestCase):
    """Nothing is authorized while the fallback is what's running."""

    def test_matching_is_unavailable_without_a_real_model(self):
        with mock.patch.object(faces, "is_biometric", return_value=False), \
             mock.patch.object(security, "_ALLOW_INSECURE_MATCH", False):
            self.assertFalse(security.matching_available())

    def test_identify_employee_refuses_rather_than_guessing(self):
        with mock.patch.object(faces, "is_biometric", return_value=False), \
             mock.patch.object(security, "_ALLOW_INSECURE_MATCH", False):
            employee, score = security.identify_employee([0.1] * 256)
        self.assertIsNone(employee)
        self.assertEqual(score, 0.0)

    def test_a_real_model_makes_matching_available(self):
        with mock.patch.object(faces, "is_biometric", return_value=True):
            self.assertTrue(security.matching_available())

    def test_the_insecure_override_is_opt_in_and_explicit(self):
        with mock.patch.object(faces, "is_biometric", return_value=False), \
             mock.patch.object(security, "_ALLOW_INSECURE_MATCH", True):
            self.assertTrue(security.matching_available())
