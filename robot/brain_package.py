"""robot/brain_package.py — «مخ موس» as a zip for a branch's computer.

A robot in every branch: the owner adds one on the dashboard (a branch, a
name), and downloads the brain from its page — this folder (robot/brain, no
Django in it) with a brain.ini that already has the server's address, the
robot's name and its own key. Unzipped in C:\\mouss-brain, `run_windows.bat`
does the rest.

The key is only in the zip right after it was made (a new robot, or its key
rotated): only its hash is stored (RobotDevice.issue_token). Any other time
the zip is the brain alone — an update, copied over the old folder, that
keeps the branch's own brain.ini.
"""

import io
import re
import zipfile
from pathlib import Path

BRAIN = Path(__file__).resolve().parent / "brain"
# Next to the brain folder too, as the guide says: double-click and go.
LAUNCHERS = ("run_windows.bat", "run_mac.sh", "install_ubuntu.sh")
_SKIP_DIRS = {"__pycache__"}
_SKIP_FILES = {"brain.ini"}                 # a developer's own, with their key


def _files():
    for path in sorted(BRAIN.rglob("*")):
        if not path.is_file() or path.suffix in (".pyc", ".pyo") or path.name in _SKIP_FILES:
            continue
        if _SKIP_DIRS & set(path.relative_to(BRAIN).parts):
            continue
        yield path


def brain_ini(api_base: str, token: str, name: str = "موس") -> str:
    """brain.ini.example with this robot's address, key and name filled in."""
    text = (BRAIN / "brain.ini.example").read_text(encoding="utf-8")
    text = text.replace("; «مخ موس» — copy this file to brain.ini (next to the brain folder) and fill it in.",
                        "; «مخ موس» — this robot's own settings, made on its page on the dashboard.")
    text = re.sub(r"(?m)^api_base\s*=.*$", f"api_base = {api_base}", text, count=1)
    text = re.sub(r"(?m)^token\s*=.*$", f"token    = {token}", text, count=1)
    name = " ".join(str(name or "").split()) or "موس"
    text = re.sub(r"(?m)^name\s*=\s*\S+", f"name = {name}", text, count=1)
    # 🐛 [FIX]: «model = small» (Whisper) took 14 s a sentence on the shop's old computer, so the
    # server heard everything (Gemini, and its quota ran out). Vosk runs on any CPU in about a second,
    # and «run_windows.bat wake» is built on it; a fast computer can say small in brain.ini.
    text = re.sub(r"(?m)^model(\s*)=\s*small\b", r"model\1= vosk ", text, count=1)
    return text


def build(api_base: str, token: str = "", name: str = "موس") -> bytes:
    """The zip: brain/…, the launchers beside it, and — with a `token` — brain.ini.
    No folder around it: extracted into C:\\mouss-brain (a new one, or over the
    old one to update it), they land beside the computer's own brain.ini."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _files():
            rel = path.relative_to(BRAIN).as_posix()
            zf.write(path, f"brain/{rel}")
            if rel in LAUNCHERS:
                zf.write(path, rel)
        zf.write(BRAIN / "README.md", "README.md")
        if token:
            zf.writestr("brain.ini", brain_ini(api_base, token, name))
    return buf.getvalue()
