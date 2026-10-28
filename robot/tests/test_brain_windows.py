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
        # The old Python's own `py` doesn't know «install»: the manager is called by its name.
        self.assertIn('"pymanager exec -V:3.12-64"', text)
        self.assertIn("pymanager install 3.12-64", text)
        self.assertNotIn("py install", text)

    def test_the_robot_pc(self):
        # «جهّز البي سي اللي موصل في الروبوت»: one command — the robot starts with Windows, comes
        # back by itself if it ever stops, and the PC never sleeps.
        text = "\r\n".join(self.lines)
        self.assertIn('if /i "%~1"=="robotpc" goto robotpc', text)
        self.assertIn('echo call "%~f0" watch', text)                 # Startup runs the watcher
        watch = text[text.index(":watch_again"):]
        self.assertIn('call "%~f0"', watch.split("goto watch_again")[0])
        self.assertIn("timeout /t 10", watch)
        self.assertIn("powercfg /change standby-timeout-ac 0", text)


if __name__ == "__main__":
    unittest.main()
