"""A broken face_recognition install must not take the web server down.

When its model files are missing, `face_recognition` prints a hint and calls
quit() on import. That SystemExit killed the whole web process on every
/face/ request in production (502s until the container restarted). The robot
must treat the library as unavailable instead.
"""

import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from robot import faces


class FaceLibGuardTests(SimpleTestCase):

    def setUp(self):
        self._saved = (faces._FACE_LIB, faces._FACE_LIB_CHECKED)
        faces._FACE_LIB, faces._FACE_LIB_CHECKED = None, False
        self.tmp = tempfile.TemporaryDirectory()
        sys.path.insert(0, self.tmp.name)

    def tearDown(self):
        sys.path.remove(self.tmp.name)
        for mod in ("face_recognition", "face_recognition_models"):
            sys.modules.pop(mod, None)
        self.tmp.cleanup()
        faces._FACE_LIB, faces._FACE_LIB_CHECKED = self._saved

    def _package(self, name, body=""):
        d = Path(self.tmp.name) / name
        d.mkdir()
        (d / "__init__.py").write_text(body)

    def test_models_missing_means_unavailable_without_importing(self):
        self._package("face_recognition", "raise SystemExit('models missing')\n")
        self.assertFalse(faces.is_biometric())
        self.assertNotIn("face_recognition", sys.modules)

    def test_a_quit_during_import_is_contained(self):
        self._package("face_recognition_models")
        self._package("face_recognition", "raise SystemExit('models missing')\n")
        self.assertIsNone(faces._face_lib())
        self.assertFalse(faces.is_biometric())

    def test_extraction_falls_back_safely(self):
        self._package("face_recognition", "raise SystemExit('models missing')\n")
        self.assertIsNone(faces._dlib_embedding(b"not-an-image"))
