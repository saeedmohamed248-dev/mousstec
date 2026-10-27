"""The bridge firmware and the server agree on every µ-law byte.

The fast voice sends µ-law both ways, so a single bit of difference between
the firmware's muEncode/muDecode and robot/audio.py would turn the robot's
voice (or the question it heard) into noise. The firmware's plain-C++ block
(between the `host-testable` markers in esp32_bridge.ino) is compiled here
with g++ and run: the codec is compared value by value with the server's,
and the reply buffering and header decoding are checked too. Skipped where
no C++ compiler is installed.
"""

import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from robot import audio

SKETCH = Path(__file__).resolve().parents[1] / "firmware" / "esp32_bridge" / "esp32_bridge.ino"

HARNESS = r"""
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdio>
%(block)s
int main(int argc, char** argv) {
  FILE* enc = fopen(argv[1], "wb");
  for (int s = -32768; s < 32768; s++) { uint8_t u = muEncode((int16_t) s); fwrite(&u, 1, 1, enc); }
  fclose(enc);
  FILE* dec = fopen(argv[2], "wb");
  for (int u = 0; u < 256; u++) { int16_t s = muDecode((uint8_t) u); fwrite(&s, 2, 1, dec); }
  fclose(dec);
  char out[64];
  urlDecode("%%D8%%A3%%D9%%8A%%D9%%88%%D9%%87+ok%%2", out, sizeof(out));
  printf("{\"url\": \"");
  for (const char* p = out; *p; p++) printf("\\u%%04x", (unsigned char) *p);
  printf("\", \"timing\": %%ld, \"pre\": [%%u, %%u, %%u, %%u, %%u, %%u]}\n",
         timingTotalMs("hear=900;think=150;voice=350"),
         prebufferSamples(96000, 0, 48000),        // speed not known yet: wait
         prebufferSamples(96000, 40000, 48000),    // fast line: 0.2 s
         prebufferSamples(40000, 9200, 48000),     // half speed: half of it + 0.2 s
         prebufferSamples(2000, 40000, 48000),     // a short reply: all of it
         prebufferSamples(-1, 9200, 48000),        // length unknown: 0.5 s
         prebufferSamples(960000, 1000, 48000));   // can't hold it all: the buffer
  return 0;
}
"""


def _block() -> str:
    src = SKETCH.read_text(encoding="utf-8")
    start = src.index("// ==== host-testable")
    end = src.index("// ==== end host-testable ====")
    return src[start:end]


@unittest.skipUnless(shutil.which("g++"), "no C++ compiler")
class FirmwareCodecTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        (tmp / "t.cpp").write_text(HARNESS % {"block": _block()}, encoding="utf-8")
        subprocess.run(["g++", "-std=c++17", "-O1", "-Wall", "-Werror", "-o", str(tmp / "t"),
                        str(tmp / "t.cpp")], check=True, capture_output=True)
        run = subprocess.run([str(tmp / "t"), str(tmp / "enc"), str(tmp / "dec")],
                             check=True, capture_output=True, text=True)
        cls.result = json.loads(run.stdout)
        cls.enc = (tmp / "enc").read_bytes()
        cls.dec = (tmp / "dec").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_encode_matches_the_server_for_every_sample(self):
        every = struct.pack("<65536h", *range(-32768, 32768))
        self.assertEqual(self.enc, audio.ulaw_encode(every))

    def test_decode_matches_the_server_for_every_byte(self):
        self.assertEqual(self.dec, audio.ulaw_decode(bytes(range(256))))

    def test_header_text_is_decoded(self):
        # ASCII-escaped bytes, rebuilt as UTF-8; a broken escape at the end stays as is.
        raw = bytes(ord(c) for c in self.result["url"]).decode("utf-8")
        self.assertEqual(raw, "أيوه ok%2")

    def test_server_timing_is_summed(self):
        self.assertEqual(self.result["timing"], 1400)

    def test_how_much_to_buffer_before_speaking(self):
        unknown_speed, fast, half, short, unknown_len, huge = self.result["pre"]
        room = 48_000 - 1024                           # the buffer, less a margin
        self.assertEqual(unknown_speed, room)          # wait (as much as it holds)
        self.assertEqual(fast, 3200)                   # 0.2 s
        self.assertEqual(half, 40_000 // 2 + 3200)     # remaining × (1 − 9200/18400) + 0.2 s
        self.assertEqual(short, 2000)
        self.assertEqual(unknown_len, 8000)
        self.assertEqual(huge, room)
