"""robot/brain/run_windows.bat — there's no Windows here to run it, so the
rules it lives by: CRLF line endings (cmd loses its labels otherwise), the
Arabic only above the chcp (old cmd versions misread UTF-8 lines after it),
every label it jumps to is there, and it only takes a 64-bit Python (the
speech model has no 32-bit build).
"""

import re
import unittest
from pathlib import Path

BAT = Path(__file__).resolve().parents[1] / "brain" / "run_windows.bat"


class RunWindowsTests(unittest.TestCase):

    def setUp(self):
        self.raw = BAT.read_bytes()
        self.lines = self.raw.decode("utf-8").split("\r\n")

    def test_crlf_only(self):
        self.assertNotIn(b"\n", self.raw.replace(b"\r\n", b""))

    def test_no_arabic_after_the_chcp(self):
        chcp = next(i for i, line in enumerate(self.lines) if line.startswith("chcp "))
        late = [line for line in self.lines[chcp:] if not line.isascii()]
        self.assertEqual(late, [])

    def test_every_label_it_goes_to_is_there(self):
        text = "\r\n".join(self.lines)
        labels = {line[1:].strip().lower() for line in self.lines if line.startswith(":")}
        wanted = {m.lower() for m in re.findall(r"(?:goto|call) :?([A-Za-z0-9_]+)", text, re.IGNORECASE)}
        self.assertEqual(wanted - labels, set())

    def test_only_a_64_bit_python_is_taken(self):
        text = "\r\n".join(self.lines)
        self.assertIn("struct.calcsize('P')", text)
        self.assertIn('call :pick %%~C', text)
        self.assertNotRegex(text, r'set "BASE=py ')                   # never by name alone
        self.assertIn("py install 3.12-64", text)


if __name__ == "__main__":
    unittest.main()
